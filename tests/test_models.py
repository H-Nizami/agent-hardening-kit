import pytest
from pydantic import ValidationError

from agent_hardening_kit.models import Document, Quote, Request, TenantPolicy, Usage


@pytest.mark.parametrize("field,value", [("tenant_id", "../acme"), ("tenant_id", ""),
    ("request_id", "x" * 65), ("question", "   "), ("question", "x" * 4097),
    ("question", 123), ("question", "bad\x00input"), ("question", "bad\ud800input")])
def test_request_contract(field, value):
    data = {"tenant_id": "acme", "request_id": "r1", "question": "What is the plan?"}
    data[field] = value
    with pytest.raises(ValidationError):
        Request.model_validate(data)


def test_extra_input_never_selects_cap_or_clock():
    with pytest.raises(ValidationError):
        Request(tenant_id="acme", request_id="r1", question="Plan?", token_limit=1_000_000)


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", float("nan"), float("inf"), 10**16])
def test_accounting_is_bounded_nonnegative_integer(value):
    for model, field in [(Usage, "input_tokens"), (Usage, "cost_microusd"), (TenantPolicy, "token_limit")]:
        with pytest.raises(ValidationError):
            model(**{field: value})
    with pytest.raises(ValidationError):
        Quote(input_tokens=1, max_output_tokens=value)


@pytest.mark.parametrize("field,value", [("requests_per_window", 0), ("requests_per_window", True), ("window_seconds", 0)])
def test_rate_policy(field, value):
    with pytest.raises(ValidationError):
        TenantPolicy(**{field: value})


def test_document_has_only_two_sources():
    with pytest.raises(ValidationError):
        Document(id="doc", tenant_id="acme", source="https://example.com", text="Data")
