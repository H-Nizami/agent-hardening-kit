# Local telemetry and SLO checks

Instrumentation is opt-in. Construct `Observer("http://127.0.0.1:4318")` in the
worker's trusted configuration; request/workflow data cannot select an exporter.
`Observer()` is a no-op and makes no network calls. Providers are private to the
observer, not process-wide, and resources contain only fixed service name/version.

The HTTP/protobuf transport uses OpenTelemetry's SDK and protobuf encoders with a
private requests session. It disables ambient proxies/netrc, redirects and auth
headers. This is intentional: the stock HTTP exporter merges inherited OTLP
headers even when passed an empty header mapping. No credential handling is
needed for the local receiver. Only loopback or three explicitly named in-cluster
Collector services can be selected, with an explicit port and no URL credentials.

The [saved local roundtrip report](../evidence/telemetry/latest.md) records a
real Collector 0.164.0 run: 12 fixture activity chains, 49 received spans, all five
required metric series, and no sensitive canary data. Its latency values describe
only that platform/run and include synchronous local export overhead.

## Collector roundtrip

Use the pinned contrib Collector (`scripts/tools.sh otelcol-contrib promtool` installs the
version/checksum recorded in `docs/pins.md`):

```bash
mkdir -p .cache/telemetry
AHK_TRACE_FILE=.cache/telemetry/traces.jsonl .bin/otelcol-contrib --config observability/collector.yaml
```

In another shell, run the worker or an observer-enabled local integration. Traces
are written as newline-delimited OTLP JSON to the selected file. Numeric metrics
are exposed at `http://127.0.0.1:9464/metrics`. The OTLP receiver binds loopback;
9464 binds all interfaces so an internal cluster Service can scrape the sidecar.
Do not expose the lab Collector or metrics Service publicly. File paths are
operator configuration and require a writable directory, not request input.

The Collector applies a second attribute allowlist. It does not receive prompts,
answers, tenant/request identifiers, raw exception messages or payload logging
from this application. Tests cover actual protobuf HTTP export and hostile
ambient OTLP/resource/proxy configuration. A local HTTP test receiver is not
Collector evidence: root's integration must validate the actual Collector file
and scrape before claiming that roundtrip.

## Metric scope

| OTel instrument | Prometheus series | Meaning |
|---|---|---|
| `ahk.requests` | `ahk_requests_total` | Terminal outcomes by fixed status |
| `ahk.request.duration`, unit `s` | `ahk_request_duration_seconds_*` | Active execution duration; excludes human approval wait |
| `ahk.fixture.units` | `ahk_fixture_units_total` | Synthetic whitespace accounting units by input/output direction |
| `ahk.model.cost`, unit `USD` | `ahk_model_cost_USD_total` | Fixture model spend, currently zero |

The usage metric deliberately does not label fixture units as real model tokens.
Span attributes use fixed `gen_ai.operation.name=invoke_agent` and
`gen_ai.provider.name=fixture`. There is no measured live-model cost or quality.
Do not attach free-form inputs to `activity_span(stage)` or `record_outcome`.
Unknown operations/statuses and nonfinite/negative durations are rejected.

## SLO policy

The lab's illustrative objectives are 99% of requests without a runtime failure
and 99% of active request execution within 5 seconds. Policy denials and correct
refusals are expected outcomes, not system failures. Emit one request outcome per
completed request. Human approval wait is excluded from active execution timing.
A production service should choose its own objectives from real traffic.

`slo-rules.yaml` contains paired 1h/5m fast-burn and 6h/30m slow-burn alerts for
both availability and latency. Ratios must exceed 14.4 or 6 times the 1% error
budget in both windows. A minimum 100-request long window suppresses tiny-sample
pages; zero traffic does not page. No request data appears in alert labels.

```bash
.bin/promtool check rules observability/slo-rules.yaml
.bin/promtool test rules observability/slo-tests.yaml
```

Tests prove healthy/zero/tiny traffic stays quiet and sustained bad traffic fires
both alerts. `grafana/agent-hardening-kit.json` provides importable rate, latency,
availability, synthetic usage and cost panels. Its provisioned data source UID is
`ahk-prometheus`, matching the local Helm chart.
These dashboard/rule assets do not alone prove a live Prometheus/Grafana service.

Run the native Collector evidence check from the repository root:

```bash
.venv/bin/python scripts/check_telemetry.py --out evidence/telemetry
```

It refuses occupied lab ports, starts only its own Collector, drives 12 real
fixture activity chains across both tenants, and verifies received activity
spans, fresh Prometheus counters, schema allowlists and sensitive canary absence.
It stops its own process on success, failure or interruption and discards raw
trace/scrape data. The report records actual local duration distribution, source
file hashes, platform and Collector version. Failure/interruption reports are
explicitly incomplete. Existing operator-owned Collectors may be inspected with
`--existing --endpoint http://127.0.0.1:4318 --metrics-url http://127.0.0.1:9464/metrics
--trace-file /path/to/traces.jsonl`; that mode never stops the existing process.

Langfuse is not configured or claimed as a tested backend. Adding a live Langfuse
project requires operator-managed credentials and a separately verified sanitized
export path. Collector file evidence currently provides the trace artifact.
