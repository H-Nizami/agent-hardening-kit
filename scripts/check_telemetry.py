#!/usr/bin/env python3
"""Measure a real local fixture -> OTLP Collector -> file/Prometheus roundtrip.

The default mode owns a temporary Collector and refuses occupied lab ports. An
explicit existing mode inspects an operator-owned Collector without stopping it.
No model provider or credentials are used; raw traces stay in temporary storage.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

import requests

from agent_hardening_kit.durable_activities import Activities
from agent_hardening_kit.backend import FixtureBackend
from agent_hardening_kit.models import TenantPolicy
from agent_hardening_kit.telemetry import Observer, local_endpoint

REPO = Path(__file__).resolve().parents[1]
CANARIES = (
    "person@example.net", "202-555-0123", "TEST_ONLY_AMBIENT_AUTH",
    "TEST_ONLY_RESOURCE_PRIVATE", "TEST_ONLY_RAW_EXCEPTION", "TEST_ONLY_REQUEST_PRIVATE",
    "TEST_ONLY_PROVIDER_FAILURE",
)
EXPECTED_STAGES = {"ahk.prepare", "ahk.reserve", "ahk.generate", "ahk.validate_settle"}
RESOURCE_KEYS = {"service.name", "service.version"}
SPAN_KEYS = {"ahk.stage", "gen_ai.operation.name", "gen_ai.provider.name"}
SOURCE_FILES = (
    "scripts/check_telemetry.py", "src/agent_hardening_kit/telemetry.py",
    "src/agent_hardening_kit/durable_activities.py", "observability/collector.yaml",
    "src/agent_hardening_kit/backend.py", "src/agent_hardening_kit/budget.py",
    "src/agent_hardening_kit/models.py", "src/agent_hardening_kit/retrieval.py",
    "src/agent_hardening_kit/data/corpus.json",
)


def write_report(destination, report):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "latest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    latency = report.get("local_fixture_latency_seconds", {})
    lines = ["# Local Collector roundtrip", "", f"Status: {report['status']}.", "",
             "This measures deterministic local fixture execution, not live-model quality or provider latency.",
             f"Actual model spend: $0. Samples: {report.get('sample_count', 0)}.",
             f"Platform: {report['platform']}. Python: {report['python_version']}."]
    if report["status"] == "complete":
        lines += [f"Received spans: {report['received_spans']}; all required activity stages present.",
                  f"Sensitive canary absence and finite resource/span schema: {report['sanitization_passed']}.",
                  f"Prometheus usage/outcome/duration/cost series present: {report['metrics_passed']}.",
                  f"Local active fixture median: {latency['median']:.6f}s; p95: {latency['p95']:.6f}s."]
    else:
        lines += ["The roundtrip did not finish; this artifact is not completion evidence.",
                  f"Failure class: {report.get('failure_class', 'interrupted')}."]
    lines += ["", "Collector traces and raw scrape are temporary and are not included in this artifact.",
              "No Langfuse, remote trace store, production latency or live LLM quality is claimed."]
    (destination / "latest.md").write_text("\n".join(lines) + "\n")


def port_is_free(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def get_metrics(url):
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, timeout=2, allow_redirects=False)
        response.raise_for_status()
        return response.text


def wait_ready(process, metrics_url, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("owned Collector failed to start")
        try:
            get_metrics(metrics_url)
            return
        except requests.RequestException:
            time.sleep(0.1)
    raise TimeoutError("Collector did not become ready")


def metric_total(metrics, name):
    return sum(float(line.rsplit(" ", 1)[1]) for line in metrics.splitlines()
               if line.startswith(name + "{") or line.startswith(name + " "))


def inspect_received(trace_file, metrics, minimum_spans, initial_size=0, initial_outcomes=0, samples=0):
    raw = trace_file.read_bytes()[initial_size:].decode()
    combined = raw + metrics
    if any(canary in combined for canary in CANARIES):
        raise AssertionError("sensitive fixture data reached telemetry")
    spans = []
    for line in raw.splitlines():
        batch = json.loads(line)
        for resource in batch.get("resourceSpans", []):
            attrs = resource.get("resource", {}).get("attributes", [])
            if {item["key"] for item in attrs} - RESOURCE_KEYS:
                raise AssertionError("unexpected resource attribute")
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    if {item["key"] for item in span.get("attributes", [])} - SPAN_KEYS:
                        raise AssertionError("unexpected span attribute")
                    if span.get("events") or span.get("status", {}).get("message"):
                        raise AssertionError("exception content reached telemetry")
                    spans.append(span)
    if len(spans) < minimum_spans or not EXPECTED_STAGES.issubset({span["name"] for span in spans}):
        raise AssertionError("required activity spans missing")
    expected_metrics = {
        "ahk_requests_total", "ahk_request_duration_seconds_count",
        "ahk_request_duration_seconds_bucket", "ahk_fixture_units_total", "ahk_model_cost_USD_total",
        "ahk_stage_duration_seconds_count",
    }
    names = {line.split("{", 1)[0].split(" ", 1)[0] for line in metrics.splitlines() if line and not line.startswith("#")}
    if not expected_metrics.issubset(names):
        raise AssertionError("required exported metrics missing")
    if metric_total(metrics, "ahk_requests_total") < initial_outcomes + samples:
        raise AssertionError("new request outcomes did not reach metrics")
    return len(spans), sorted(expected_metrics)


def execute_pipeline(activities, request_id, question):
    plan = activities.prepare({"request": {
        "tenant_id": "acme", "request_id": request_id, "question": question,
    }})
    if "answer" in plan:
        return plan["answer"]
    admitted = activities.reserve(plan)
    if "answer" in admitted:
        return admitted["answer"]
    return activities.validate_settle(activities.generate(admitted))


def source_commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()


def run(args, report):
    process = None
    observer = None
    with tempfile.TemporaryDirectory(prefix="ahk-telemetry-") as temporary:
        directory = Path(temporary)
        trace_file = Path(args.trace_file).resolve() if args.existing else directory / "traces.jsonl"
        if args.existing and not trace_file.is_file():
            raise ValueError("existing Collector requires a readable trace file")
        endpoint = args.endpoint or "http://127.0.0.1:4318"
        metrics_url = args.metrics_url or "http://127.0.0.1:9464/metrics"
        local_endpoint(endpoint)
        if not metrics_url.endswith("/metrics"):
            raise ValueError("metrics endpoint must end in /metrics")
        local_endpoint(metrics_url.removesuffix("/metrics"))
        log_file = (directory / "collector.log").open("w")
        try:
            if not args.existing:
                if args.endpoint or args.metrics_url or args.trace_file:
                    raise ValueError("custom endpoint/trace paths require --existing")
                if not port_is_free(4318) or not port_is_free(9464):
                    raise RuntimeError("lab telemetry ports are occupied; no process was changed")
                environment = os.environ.copy()
                environment["AHK_TRACE_FILE"] = str(trace_file)
                process = subprocess.Popen(
                    [str(REPO / ".bin/otelcol-contrib"), "--config", str(REPO / "observability/collector.yaml")],
                    cwd=REPO, env=environment, stdout=log_file, stderr=subprocess.STDOUT,
                )
            wait_ready(process, metrics_url)
            initial_size = trace_file.stat().st_size if trace_file.exists() else 0
            initial_outcomes = metric_total(get_metrics(metrics_url), "ahk_requests_total")
            # Synthetic hostile inherited configuration: do not read real values.
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=" + CANARIES[2]
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "private=" + CANARIES[3]
            observer = Observer(endpoint)
            activities = Activities(directory / "admission.sqlite", observer=observer)
            latencies = []
            run_id = uuid.uuid4().hex[:10]
            for number in range(args.samples):
                started = time.perf_counter()
                plan = activities.prepare({"request": {
                    "tenant_id": "acme" if number % 2 == 0 else "beta",
                    "request_id": f"{CANARIES[5]}-{run_id}-{number}",
                    "question": f"What do refunds require? Contact {CANARIES[0]} or {CANARIES[1]}.",
                }})
                if "answer" in plan:
                    raise AssertionError("fixture unexpectedly bypassed backend activities")
                admitted = activities.reserve(plan)
                if "answer" in admitted:
                    raise AssertionError("fixture unexpectedly failed admission")
                generated = activities.generate(admitted)
                answer = activities.validate_settle(generated)
                if answer["status"] != "answered":
                    raise AssertionError("fixture did not produce a cited answer")
                latencies.append(time.perf_counter() - started)
            # Declared control cases exercise real terminal instrumentation paths.
            refused = execute_pipeline(activities, "control-refused-" + run_id, "Quantum zeppelin algebra?")
            if refused["status"] != "refused":
                raise AssertionError("unknown-topic control did not refuse")
            budget = Activities(directory / "budget.sqlite", observer=observer,
                                policies={"acme": TenantPolicy(token_limit=0)})
            denied = execute_pipeline(budget, "control-blocked-" + run_id, "API authentication?")
            if denied["reason_code"] != "budget_exhausted":
                raise AssertionError("budget control did not deny admission")

            class FaultBackend(FixtureBackend):
                def generate(self, request, documents):
                    raise RuntimeError(CANARIES[6])

            failing = Activities(directory / "failure.sqlite", observer=observer, backend=FaultBackend())
            failed = execute_pipeline(failing, "control-failed-" + run_id, "API authentication?")
            if failed["reason_code"] != "backend_failure":
                raise AssertionError("provider control did not fail safely")
            # Prove caught exception messages never become span events/status text.
            try:
                with observer.activity_span("model"):
                    raise ValueError(CANARIES[4])
            except ValueError:
                pass
            observer.flush()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process is not None and process.poll() is not None:
                    raise RuntimeError("owned Collector exited during export")
                metrics = get_metrics(metrics_url)
                try:
                    spans, metric_names = inspect_received(
                        trace_file, metrics, args.samples * 4 + 8,
                        initial_size, initial_outcomes, args.samples + 3,
                    )
                    for status in ("failed", "refused", "blocked"):
                        if not any(line.startswith("ahk_requests_total{") and f'status="{status}"' in line
                                   for line in metrics.splitlines()):
                            raise AssertionError("terminal control metric missing")
                    break
                except (FileNotFoundError, AssertionError, json.JSONDecodeError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("Collector did not receive required sanitized telemetry")
            ordered = sorted(latencies)
            report.update({
                "status": "complete", "sample_count": args.samples,
                "received_spans": spans, "required_activity_stages": sorted(EXPECTED_STAGES),
                "exported_metric_names": metric_names, "sanitization_passed": True,
                "sensitive_canary_checks": len(CANARIES), "metrics_passed": True,
                "terminal_control_cases": {"provider_fault": "failed", "budget_denial": "blocked",
                                           "unknown_topic": "refused"},
                "local_fixture_latency_seconds": {
                    "median": statistics.median(latencies),
                    "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
                    "min": min(latencies), "max": max(latencies),
                    "definition": "direct durable activity execution including synchronous local export; no approval wait",
                },
                "collector_owned_by_check": not args.existing,
            })
        finally:
            if observer is not None:
                observer.shutdown()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log_file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("evidence/telemetry"))
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--existing", action="store_true")
    parser.add_argument("--endpoint")
    parser.add_argument("--metrics-url")
    parser.add_argument("--trace-file")
    args = parser.parse_args()
    if not 2 <= args.samples <= 100:
        parser.error("samples must be between 2 and 100")
    if args.existing and not args.trace_file:
        parser.error("--existing requires --trace-file")
    report = {
        "schema_version": 1, "status": "running", "scope": "local fixture Collector roundtrip",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python_version": platform.python_version(),
        "actual_model_spend_usd": 0,
    }
    interrupted = False

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        report.update({
            "source_commit": source_commit(),
            "source_files_sha256": {
                filename: hashlib.sha256((REPO / filename).read_bytes()).hexdigest()
                for filename in SOURCE_FILES
            },
            "source_worktree_dirty": bool(subprocess.check_output(
                ["git", "status", "--porcelain", "--", *SOURCE_FILES], cwd=REPO, text=True,
            ).strip()),
            "collector_version": subprocess.check_output(
                [str(REPO / ".bin/otelcol-contrib"), "--version"], text=True,
            ).strip(),
            "collector_config_sha256": hashlib.sha256((REPO / "observability/collector.yaml").read_bytes()).hexdigest(),
        })
        run(args, report)
    except KeyboardInterrupt:
        interrupted = True
        report.update(status="interrupted", failure_class="interrupted")
    except Exception as exc:
        report.update(status="failed", failure_class=type(exc).__name__)
    write_report(args.out, report)
    print(f"Collector roundtrip: {report['status']}; model spend $0.")
    return 0 if report["status"] == "complete" else 130 if interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())
