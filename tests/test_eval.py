"""Golden coverage and CLI gates must detect regressions in the real graph."""

from copy import deepcopy
import json
import subprocess
import sys

import pytest

from agent_hardening_kit import agent, cli, evaluation


@pytest.fixture(scope="module")
def report():
    return evaluation.evaluate()


def test_every_golden_case_passes_with_controls(report):
    assert report["case_count"] == 39
    assert evaluation.passes_regression(report)
    rows = report["systems"]["hardened"]["cases"]
    assert {row["tenant_id"] for row in rows} == {"acme", "beta"}
    assert all(row["passed"] for row in rows)
    assert report["actual_model_spend_usd"] == 0


def test_repeated_report_is_byte_identical(report, tmp_path):
    second = evaluation.evaluate()
    evaluation.write_report(report, tmp_path / "first")
    evaluation.write_report(second, tmp_path / "second")
    for filename in ("latest.json", "latest.md"):
        assert (tmp_path / "first" / filename).read_bytes() == (tmp_path / "second" / filename).read_bytes()


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "unexpected", "group", "setup", "extra"))
def test_fixture_manifest_rejects_incomplete_or_unknown_cases(mutation):
    cases = evaluation.load_cases()
    if mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases.append(deepcopy(cases[0]))
    elif mutation == "unexpected":
        cases[0]["id"] = "unreviewed-case"
    elif mutation == "group":
        cases[0]["group"] = "admission"
    elif mutation == "setup":
        cases[0]["setup"] = "unimplemented-setup"
    else:
        cases[0]["unexpected_option"] = True
    with pytest.raises(ValueError):
        evaluation.validate_cases(cases)


def test_gate_rejects_deleted_case_even_if_summary_claims_success(report):
    incomplete = deepcopy(report)
    incomplete["systems"]["hardened"]["cases"].pop()
    assert not evaluation.passes_regression(incomplete)


def test_ablations_use_independent_allowances(report):
    by_mode = {mode: {row["id"]: row for row in values["cases"]}
               for mode, values in report["systems"].items()}
    for mode in ("baseline", "hardened"):
        assert by_mode[mode]["qa-acme-01"]["backend_calls"] == 1
    denied = by_mode["hardened"]["admission-token-budget"]
    assert denied["backend_calls"] == 0
    assert by_mode["baseline"]["admission-token-budget"]["backend_calls"] == 1


def test_evidence_score_is_a_gate(monkeypatch, tmp_path):
    original = evaluation.run_request

    def corrupt_answer(*args, **kwargs):
        return original(*args, **kwargs).model_copy(update={"answer": "unsupported made-up text"})

    monkeypatch.setattr(evaluation, "run_request", corrupt_answer)
    case = evaluation.load_cases()[0]
    case["required_terms"] = []
    row = evaluation.evaluate_case(case, "hardened", tmp_path)
    assert not row["passed"]
    assert "unsupported_answer_evidence" in row["failures"]


def test_citation_control_regression_fails_golden_gate(monkeypatch):
    monkeypatch.setattr(agent, "_supported", lambda *args: True)
    report = evaluation.evaluate()
    assert not evaluation.passes_regression(report)
    failed = {row["id"] for row in report["systems"]["hardened"]["cases"] if not row["passed"]}
    assert "fault-unknown-citation" in failed


def test_tenant_filter_regression_fails_golden_gate(monkeypatch):
    original = agent.search

    def unsafe_search(*args, **kwargs):
        kwargs["hardened"] = False
        return original(*args, **kwargs)

    monkeypatch.setattr(agent, "search", unsafe_search)
    report = evaluation.evaluate()
    assert not evaluation.passes_regression(report)
    assert report["systems"]["hardened"]["summary"]["tenant_leak_cases"] > 0


def test_cli_reports_nonzero_for_regression(monkeypatch, report, tmp_path):
    failed = deepcopy(report)
    failed["systems"]["hardened"]["cases"][0]["passed"] = False
    monkeypatch.setattr(cli, "evaluate", lambda: failed)
    assert cli.main(["eval", "--out", str(tmp_path)]) == 1


def test_cli_invalid_input_is_sanitized(capsys, tmp_path):
    assert cli.main(["demo", "--question", " ", "--db", str(tmp_path / "demo.sqlite")]) == 2
    captured = capsys.readouterr()
    assert "Invalid local request" in captured.err
    assert "Traceback" not in captured.err


def test_installed_demo_reads_resources_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_hardening_kit", "demo", "--db", str(tmp_path / "demo.sqlite")],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["result"]["status"] == "answered"
    assert data["result"]["citations"] == ["acme-auth"]
    assert data["actual_model_spend_usd"] == 0
