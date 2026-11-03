import multiprocessing
import hashlib

import pytest

from agent_hardening_kit.budget import AdmissionDenied, BudgetStore, SettlementConflict
from agent_hardening_kit.models import Answer, Quote, TenantPolicy, Usage

DIGEST = hashlib.sha256(b"input").hexdigest()


def configured(tmp_path, **kwargs):
    return BudgetStore(tmp_path / "budget.db", {"acme": TenantPolicy(**kwargs), "beta": TenantPolicy()})


def reserve(store, request="r1", tenant="acme", quote=None, digest=DIGEST):
    return store.reserve(tenant, request, "synthesis", digest, quote or Quote(input_tokens=10, max_output_tokens=50))


def test_pending_duplicates_have_one_owner(tmp_path):
    store = configured(tmp_path)
    first, second = reserve(store), reserve(store)
    assert first.created and not second.created
    assert store.snapshot("acme")["charged_tokens"] == 60
    assert store.snapshot("acme")["reservation_count"] == 1


def test_conflicting_input_identity_denied(tmp_path):
    store = configured(tmp_path)
    reserve(store)
    with pytest.raises(AdmissionDenied, match="request_conflict"):
        reserve(store, digest="b" * 64)


def test_same_settlement_idempotent_and_changed_settlement_denied(tmp_path):
    store = configured(tmp_path)
    reservation = reserve(store)
    usage = Usage(input_tokens=10, output_tokens=3)
    response = Answer(status="answered", answer="Known fact", citations=["doc"], reason_code="ok", usage=usage)
    store.settle(reservation, usage, response=response)
    store.settle(reservation, usage, response=response)
    assert store.snapshot("acme")["charged_tokens"] == 13
    with pytest.raises(SettlementConflict):
        store.settle(reservation, Usage(input_tokens=1))
    cached = reserve(store)
    assert not cached.created and cached.response["answer"] == "Known fact"


@pytest.mark.parametrize("usage,failed", [(None, False), (None, True), (Usage(input_tokens=2), True)])
def test_unknown_or_failed_outcome_retains_reservation(tmp_path, usage, failed):
    store = configured(tmp_path)
    store.settle(reserve(store), usage, failed=failed)
    assert store.snapshot("acme")["charged_tokens"] == 60


def test_cap_and_tenant_isolation(tmp_path):
    store = configured(tmp_path, token_limit=60)
    reserve(store)
    with pytest.raises(AdmissionDenied, match="budget_exhausted"):
        reserve(store, "r2")
    assert reserve(store, tenant="beta").created
    with pytest.raises(AdmissionDenied, match="unknown_tenant"):
        reserve(store, tenant="unknown")


def test_currency_cap_independent_from_tokens(tmp_path):
    store = configured(tmp_path, token_limit=1000, cost_limit_microusd=2)
    assert reserve(store, quote=Quote(input_tokens=1, max_output_tokens=1, cost_microusd=2)).created
    with pytest.raises(AdmissionDenied, match="budget_exhausted"):
        reserve(store, "r2", quote=Quote(input_tokens=0, max_output_tokens=0, cost_microusd=1))


def test_rate_window_reset_and_clock_rollback_no_sleep(tmp_path):
    now = [60.0]
    store = BudgetStore(tmp_path / "budget.db", {"acme": TenantPolicy(requests_per_window=1)}, clock=lambda: now[0])
    reserve(store)
    with pytest.raises(AdmissionDenied, match="rate_limited"):
        reserve(store, "r2")
    now[0] = 0
    with pytest.raises(AdmissionDenied, match="rate_limited"):
        reserve(store, "r2")
    now[0] = 120
    assert reserve(store, "r2").created


def test_actual_overrun_is_recorded_and_freezes_tenant(tmp_path):
    store = configured(tmp_path)
    store.settle(reserve(store), Usage(input_tokens=40, output_tokens=40))
    assert store.snapshot("acme")["charged_tokens"] == 80
    assert store.snapshot("acme")["frozen"]
    with pytest.raises(AdmissionDenied, match="tenant_frozen"):
        reserve(store, "r2")


def _parallel_admission(path, barrier, queue, request):
    store = BudgetStore(path)
    barrier.wait(timeout=10)
    try:
        reserve(store, request)
        queue.put("admitted")
    except AdmissionDenied as exc:
        queue.put(exc.reason)


def test_separate_processes_cannot_overspend(tmp_path):
    store = configured(tmp_path, token_limit=100)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_parallel_admission,
        args=(str(store.path), barrier, queue, f"r{i}")) for i in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in range(2)) == ["admitted", "budget_exhausted"]
    assert store.snapshot("acme")["charged_tokens"] == 60


def test_memory_database_rejected(tmp_path):
    with pytest.raises(ValueError, match="file-backed"):
        BudgetStore(":memory:")
