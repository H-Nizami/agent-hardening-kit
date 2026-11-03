"""Opt-in, content-free OTel telemetry for the local hardening lab.

Only fixed operation/status enums and numeric usage leave this module. It never
installs global providers, records exception messages, or resolves exporter and
resource configuration from the environment. No endpoint means no network.
"""

from contextlib import contextmanager
import math
import time
from urllib.parse import urlsplit

import requests
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode

from .models import Usage

STAGES = frozenset({
    "workflow", "prepare", "route", "retrieve", "quote", "admission", "reserve",
    "model", "generate", "validate", "validate_settle", "settle", "approval",
    "tool", "apply_ticket", "complete",
})
STATUSES = frozenset({"answered", "refused", "blocked", "failed"})
APPROVAL_STATUSES = frozenset({"approved", "rejected", "expired", "denied", "failed"})
OPERATIONAL_FAILURE_REASONS = frozenset({
    "backend_failure", "backend_quote_failed", "invalid_output", "unsupported_evidence",
    "usage_overrun", "previous_outcome_unknown", "admission_unavailable", "settlement_unavailable",
})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "collector", "otel-collector", "ahk-collector"})
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)


def outcome_status(answer):
    """Internal failures must remain observable even when transport safely blocks."""
    if answer.reason_code in OPERATIONAL_FAILURE_REASONS:
        return "failed"
    if answer.status not in STATUSES:
        raise ValueError("unsupported request outcome")
    return answer.status


def local_endpoint(value):
    """Accept HTTP loopback or the named in-cluster lab Collector only."""
    if not isinstance(value, str):
        raise ValueError("telemetry endpoint must be a local HTTP URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("telemetry endpoint must be a local HTTP base URL")
    try:
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError("telemetry endpoint needs an explicit port")
    except ValueError as exc:
        raise ValueError("telemetry endpoint needs a valid explicit port") from exc
    return value.rstrip("/")


class _LocalTransport:
    """No proxy, netrc, redirects, ambient auth headers, cookies or client certs."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.clear()

    def send(self, body):
        try:
            response = self.session.post(
                self.endpoint, data=body, headers={"Content-Type": "application/x-protobuf"},
                timeout=2, allow_redirects=False,
            )
            success = 200 <= response.status_code < 300
            response.close()
            self.session.cookies.clear()
            return success
        except requests.RequestException:
            return False

    def close(self):
        self.session.close()


class LocalSpanExporter(SpanExporter):
    """OTLP protobuf transport with no environment credential resolution."""

    def __init__(self, endpoint):
        self.transport = _LocalTransport(local_endpoint(endpoint) + "/v1/traces")

    def export(self, spans):
        success = self.transport.send(encode_spans(spans).SerializeToString())
        return SpanExportResult.SUCCESS if success else SpanExportResult.FAILURE

    def shutdown(self):
        self.transport.close()

    def force_flush(self, timeout_millis=30000):
        return True


class LocalMetricExporter(MetricExporter):
    def __init__(self, endpoint):
        super().__init__()
        self.transport = _LocalTransport(local_endpoint(endpoint) + "/v1/metrics")

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        success = self.transport.send(encode_metrics(metrics_data).SerializeToString())
        return MetricExportResult.SUCCESS if success else MetricExportResult.FAILURE

    def shutdown(self, timeout_millis=30000, **kwargs):
        self.transport.close()

    def force_flush(self, timeout_millis=10000):
        return True


class Observer:
    """Finite telemetry schema. Explicit exporters/readers support offline tests."""

    def __init__(self, endpoint=None, *, span_exporter=None, metric_reader=None):
        if endpoint is not None and (span_exporter is not None or metric_reader is not None):
            raise ValueError("use either an endpoint or explicit test exporters")
        self.tracer_provider = None
        self.meter_provider = None
        self._closed = False
        if endpoint is None and span_exporter is None and metric_reader is None:
            return
        if endpoint is not None:
            endpoint = local_endpoint(endpoint)
            span_exporter = LocalSpanExporter(endpoint)
            metric_reader = PeriodicExportingMetricReader(
                LocalMetricExporter(endpoint), export_interval_millis=5000, export_timeout_millis=5000,
            )
        # Resource.create() would merge ambient resource attributes/detectors.
        resource = Resource({"service.name": "agent-hardening-kit", "service.version": "0.2.0"})
        self.tracer_provider = TracerProvider(
            resource=resource, sampler=ALWAYS_ON, shutdown_on_exit=False,
            span_limits=SpanLimits(max_attributes=16, max_events=0, max_links=0),
        )
        if span_exporter is not None:
            self.tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        self.tracer = self.tracer_provider.get_tracer("agent_hardening_kit")
        self.meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader] if metric_reader is not None else [],
            shutdown_on_exit=False,
            views=[View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS))
                   for name in ("ahk.request.duration", "ahk.stage.duration")],
        )
        meter = self.meter_provider.get_meter("agent_hardening_kit")
        self.requests = meter.create_counter("ahk.requests", unit="{request}")
        self.duration = meter.create_histogram("ahk.request.duration", unit="s")
        self.stage_duration = meter.create_histogram("ahk.stage.duration", unit="s")
        self.approvals = meter.create_counter("ahk.approvals", unit="{decision}")
        self.units = meter.create_counter("ahk.fixture.units", unit="{unit}")
        self.cost = meter.create_counter("ahk.model.cost", unit="USD")

    @contextmanager
    def activity_span(self, stage):
        if stage not in STAGES:
            raise ValueError("unsupported telemetry operation")
        if self.tracer_provider is None or self._closed:
            yield
            return
        # OTel's default exception recording includes raw messages/stacktraces.
        started = time.perf_counter()
        with self.tracer.start_as_current_span(
            "ahk." + stage, attributes={"ahk.stage": stage,
                                       "gen_ai.operation.name": "invoke_agent",
                                       "gen_ai.provider.name": "fixture"},
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield
            except BaseException:
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                self.stage_duration.record(max(0.0, time.perf_counter() - started), {"stage": stage})

    def record_outcome(self, status, elapsed_seconds, usage=None):
        if status not in STATUSES:
            raise ValueError("unsupported telemetry status")
        if (type(elapsed_seconds) not in (int, float) or
                not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise ValueError("duration must be finite and nonnegative")
        if usage is not None:
            usage = Usage.model_validate(usage.model_dump() if isinstance(usage, Usage) else usage)
        if self.meter_provider is None or self._closed:
            return
        self.requests.add(1, {"status": status})
        self.duration.record(elapsed_seconds, {"status": status})
        if usage is not None:
            for kind, amount in (("input", usage.input_tokens), ("output", usage.output_tokens)):
                self.units.add(amount, {"direction": kind, "accounting_unit": "synthetic_fixture"})
            self.cost.add(usage.cost_microusd / 1_000_000, {"backend": "fixture"})

    def record_approval(self, status):
        if status not in APPROVAL_STATUSES:
            raise ValueError("unsupported approval outcome")
        if self.meter_provider is not None and not self._closed:
            self.approvals.add(1, {"status": status})

    def flush(self):
        if self._closed:
            return True
        spans_ok = self.tracer_provider is None or self.tracer_provider.force_flush(timeout_millis=5000)
        metrics_ok = self.meter_provider is None or self.meter_provider.force_flush(timeout_millis=5000)
        return bool(spans_ok and metrics_ok)

    def shutdown(self):
        if self._closed:
            return
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown(timeout_millis=5000)
        self._closed = True
