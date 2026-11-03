"""The actual SDK must export only finite metadata, never request content."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest

from agent_hardening_kit.models import Usage
from agent_hardening_kit.models import TenantPolicy
from agent_hardening_kit.backend import FixtureBackend
from agent_hardening_kit.durable_activities import Activities
from agent_hardening_kit.telemetry import Observer, local_endpoint


@pytest.mark.parametrize("value", (
    "https://example.com:4318", "http://example.com:4318", "http://127.0.0.1:4318/v1/traces",
    "http://user:password@localhost:4318", "http://localhost:4318?token=x", "http://localhost:4318#x",
    "http://localhost", "http://localhost:0", "http://localhost:99999", "http://localhost:invalid",
))
def test_export_requires_explicit_local_endpoint(value):
    with pytest.raises(ValueError):
        local_endpoint(value)


def test_no_endpoint_is_network_free(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default observer attempted network IO")

    monkeypatch.setattr("requests.Session.post", forbidden)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com:4318")
    observer = Observer()
    with observer.activity_span("prepare"):
        observer.record_outcome("answered", 0.01, Usage(input_tokens=1, output_tokens=2, cost_microusd=0))
    assert observer.flush()
    observer.shutdown()


def test_ambient_resources_and_exception_text_are_excluded(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "secret=TEST_ONLY_PRIVATE_ATTRIBUTE")
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    observer = Observer(span_exporter=spans, metric_reader=reader)
    try:
        with pytest.raises(ValueError):
            with observer.activity_span("generate"):
                raise ValueError("TEST_ONLY_PRIVATE_EXCEPTION sample@example.com")
        observer.record_outcome("answered", 0.25, Usage(input_tokens=10, output_tokens=3, cost_microusd=0))
        exported = spans.get_finished_spans()
        assert len(exported) == 1
        span = exported[0]
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        assert not span.events
        assert dict(span.resource.attributes) == {"service.name": "agent-hardening-kit", "service.version": "0.2.0"}
        assert dict(span.attributes) == {"ahk.stage": "generate", "gen_ai.operation.name": "invoke_agent",
                                         "gen_ai.provider.name": "fixture"}
        metrics = MessageToDict(encode_metrics(reader.get_metrics_data()))
        serialized = json.dumps(metrics)
        assert "TEST_ONLY_PRIVATE" not in serialized
        assert "sample@example.com" not in serialized
        assert "ahk.requests" in serialized
        assert "ahk.fixture.units" in serialized
        assert "synthetic_fixture" in serialized
    finally:
        observer.shutdown()


@pytest.mark.parametrize("status,duration", (("sample@example.com", 1), ("answered", -1),
                                            ("answered", float("nan")), ("answered", True)))
def test_unknown_labels_or_invalid_duration_never_export(status, duration):
    observer = Observer()
    with pytest.raises(ValueError):
        observer.record_outcome(status, duration)


def test_unknown_operation_never_reaches_span_name():
    observer = Observer()
    with pytest.raises(ValueError):
        with observer.activity_span("user-input-must-not-be-a-span-name"):
            pass


@contextmanager
def capture_otlp():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_protobuf_transport_has_no_ambient_auth_or_proxy(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=TEST_ONLY_AMBIENT_TOKEN")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=TEST_ONLY_TRACE_TOKEN")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private=TEST_ONLY_RESOURCE")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with capture_otlp() as (endpoint, received):
        observer = Observer(endpoint)
        with observer.activity_span("validate_settle"):
            pass
        observer.record_outcome("answered", 0.1, Usage(input_tokens=2, output_tokens=1, cost_microusd=0))
        observer.flush()
        observer.shutdown()
    assert {path for path, _, _ in received} == {"/v1/traces", "/v1/metrics"}
    for path, headers, body in received:
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/x-protobuf"
        message = ExportTraceServiceRequest() if path == "/v1/traces" else ExportMetricsServiceRequest()
        message.ParseFromString(body)
        assert "TEST_ONLY" not in json.dumps(MessageToDict(message))


def metric_points(reader, name):
    data = reader.get_metrics_data()
    return [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == name for point in metric.data.data_points]


def execute_activities(activities, request_id="r1", question="API authentication?", side_effect=False):
    plan = activities.prepare({"request": {"tenant_id": "acme", "request_id": request_id, "question": question},
                               "side_effect": side_effect})
    if "answer" in plan:
        return plan, plan["answer"]
    reserved = activities.reserve(plan)
    if "answer" in reserved:
        return plan, reserved["answer"]
    return plan, activities.validate_settle(activities.generate(reserved))


@pytest.mark.parametrize("fault", ("exception", "invalid_output"))
def test_backend_failure_is_failed_for_slo_even_when_safely_blocked(tmp_path, fault):
    class FaultBackend(FixtureBackend):
        def generate(self, request, documents):
            if fault == "exception":
                raise RuntimeError("TEST_ONLY_PRIVATE_PROVIDER_FAILURE")
            result = super().generate(request, documents)
            result["usage"] = {}
            return result

    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", backend=FaultBackend(), observer=observer)
        _, answer = execute_activities(activities)
        assert answer["status"] == "blocked"
        points = metric_points(reader, "ahk.requests")
        assert [(dict(point.attributes), point.value) for point in points] == [({"status": "failed"}, 1)]
    finally:
        observer.shutdown()


def test_policy_budget_denial_is_blocked_and_counted_once(tmp_path):
    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", policies={"acme": TenantPolicy(token_limit=0)}, observer=observer)
        _, answer = execute_activities(activities)
        assert answer["reason_code"] == "budget_exhausted"
        points = metric_points(reader, "ahk.requests")
        assert [(dict(point.attributes), point.value) for point in points] == [({"status": "blocked"}, 1)]
        assert activities.backend.calls == 0
    finally:
        observer.shutdown()


def test_early_no_evidence_refusal_is_included_in_request_denominator(tmp_path):
    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", observer=observer)
        _, answer = execute_activities(activities, question="Quantum zeppelin algebra?")
        assert answer["status"] == "refused"
        points = metric_points(reader, "ahk.requests")
        assert [(dict(point.attributes), point.value) for point in points] == [({"status": "refused"}, 1)]
        assert activities.backend.calls == 0
    finally:
        observer.shutdown()


def test_success_has_one_terminal_metric_and_finite_stage_durations(tmp_path):
    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", observer=observer)
        _, answer = execute_activities(activities)
        assert answer["status"] == "answered"
        requests = metric_points(reader, "ahk.requests")
        assert [(dict(point.attributes), point.value) for point in requests] == [({"status": "answered"}, 1)]
        stages = metric_points(reader, "ahk.stage.duration")
        assert {point.attributes["stage"] for point in stages} == {"prepare", "reserve", "generate", "validate_settle"}
        assert all(point.count == 1 and point.sum >= 0 for point in stages)
    finally:
        observer.shutdown()


def test_prepare_duration_starts_before_retrieval(tmp_path, monkeypatch):
    from agent_hardening_kit import durable_activities

    now = [100.0]
    original = durable_activities.search

    def measured_search(*args, **kwargs):
        now[0] += 1.0
        return original(*args, **kwargs)

    monkeypatch.setattr(durable_activities.time, "time", lambda: now[0])
    monkeypatch.setattr(durable_activities, "search", measured_search)
    activities = Activities(tmp_path / "db.sqlite")
    plan, answer = execute_activities(activities)
    assert answer["status"] == "answered"
    assert plan["started_at"] == 100.0
    assert now[0] > plan["started_at"]


def test_approval_delivery_counter_does_not_change_request_denominator(tmp_path):
    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", observer=observer)
        plan, _ = execute_activities(activities, side_effect=True)
        payload = {"plan": plan, "decision": {
            "tenant_id": "acme", "actor": "local-operator", "action_id": plan["action_id"], "decision": "approve"},
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        assert activities.authorize_decision(payload)["state"] == "approved"
        assert activities.authorize_decision(payload)["state"] == "approved"
        assert sum(point.value for point in metric_points(reader, "ahk.requests")) == 1
        approvals = metric_points(reader, "ahk.approvals")
        assert [(dict(point.attributes), point.value) for point in approvals] == [({"status": "approved"}, 2)]
    finally:
        observer.shutdown()


def test_invalid_input_is_counted_once_as_expected_denial(tmp_path):
    reader = InMemoryMetricReader()
    observer = Observer(metric_reader=reader)
    try:
        activities = Activities(tmp_path / "db.sqlite", observer=observer)
        with pytest.raises(Exception):
            activities.prepare({"request": {"tenant_id": "acme", "request_id": "r1", "question": ""}})
        points = metric_points(reader, "ahk.requests")
        assert [(dict(point.attributes), point.value) for point in points] == [({"status": "blocked"}, 1)]
    finally:
        observer.shutdown()


@pytest.mark.parametrize("status", ("approved", "rejected", "expired"))
def test_approval_status_cannot_enter_request_metric(status):
    with pytest.raises(ValueError):
        Observer().record_outcome(status, 1.0)
