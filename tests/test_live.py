import io
import json
import urllib.error

import pytest

from agent_hardening_kit import live
from agent_hardening_kit.evaluation import load_corpus
from agent_hardening_kit.models import Request


def request():
    return Request(tenant_id="acme", request_id="r1", question="API authentication?")


def documents():
    return [doc for doc in load_corpus() if doc.tenant_id == "acme"][:2]


def echo_transport(key, payload):
    source = documents()[0]
    sentence = source.text.splitlines()[0]
    return live.Reply(json.dumps({"answer": sentence, "citations": [source.id], "usage": {"input_tokens": 0}}), 11, 7)


def test_usage_comes_from_provider_envelope_not_model_text():
    backend = live.GroqBackend("m", "k", transport=echo_transport)
    result = backend.generate(request(), documents())
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 7, "cost_microusd": 0}
    assert backend.tokens == 18 and backend.calls == 1


def test_non_object_reply_raises_so_the_graph_can_block_it():
    backend = live.GroqBackend("m", "k", transport=lambda key, payload: live.Reply("[1]", 1, 1))
    with pytest.raises(ValueError):
        backend.generate(request(), documents())


def test_quote_over_estimates_and_caps_output():
    backend = live.GroqBackend("m", "k", transport=echo_transport)
    quote = backend.quote(request(), documents())
    assert quote.input_tokens > sum(len(d.text) for d in documents()) // 4
    assert quote.max_output_tokens == live.MAX_OUTPUT_TOKENS


def test_auth_error_stops_at_once_and_hides_the_key(monkeypatch):
    calls = []

    def refuse(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"secret-body"))

    monkeypatch.setattr(live.urllib.request, "urlopen", refuse)
    with pytest.raises(live.ProviderError) as caught:
        live.http_transport("SECRET", {"model": "m"}, sleep=lambda _: None)
    assert len(calls) == 1
    assert "SECRET" not in str(caught.value) and "secret-body" not in str(caught.value)


def test_rate_limit_retries_then_gives_up(monkeypatch):
    calls = []

    def limited(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 429, "slow", {"retry-after": "1"}, io.BytesIO(b""))

    monkeypatch.setattr(live.urllib.request, "urlopen", limited)
    with pytest.raises(live.ProviderError):
        live.http_transport("k", {"model": "m"}, attempts=3, sleep=lambda _: None)
    assert len(calls) == 3


def test_missing_key_is_rejected(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ValueError):
        live.key_from_environment()


def test_live_run_writes_report_without_the_key(tmp_path):
    report = live.run_live("org/model", "SECRET", transport=echo_transport, delay_seconds=0)
    path = live.write_live_report(report, tmp_path)
    assert report["case_count"] == sum(g["total"] for g in report["systems"]["hardened"]["groups"].values())
    assert "org/model" in path.read_text()
    assert "SECRET" not in path.read_text() + path.with_suffix(".json").read_text()
