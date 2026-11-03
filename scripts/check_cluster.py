#!/usr/bin/env python3
"""Build, deploy, kill a worker pod, verify recovery/metrics, and remove an owned kind cluster."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid

import requests
from temporalio.client import Client

from check_runtime import ROOT, exercise, source_metadata, stop_owned
from agent_hardening_kit.durable import AgentWorkflow


HELM = str(ROOT / ".bin/helm") if (ROOT / ".bin/helm").is_file() else "helm"


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


class Cluster:
    def __init__(self, run_id, directory):
        self.name = "ahk-chaos-" + run_id
        self.directory = directory
        self.kubeconfig = directory / "kubeconfig"
        self.namespace = "ahk"
        self.context = "kind-" + self.name
        self.image = "ahk-worker:" + run_id
        self.forwarders = []
        self.owned = False
        self.checks = {}
        self.stage = "init"
        self.image_built = False
        self.log = (directory / "commands.log").open("w")

    def run(self, args, timeout=240, capture=False, check=True):
        self.log.write("COMMAND " + " ".join(str(arg) for arg in args) + "\n")
        self.log.flush()
        result = subprocess.run([str(arg) for arg in args], cwd=ROOT, timeout=timeout,
                                stdout=subprocess.PIPE if capture else self.log,
                                stderr=self.log, text=True)
        if check and result.returncode:
            raise RuntimeError("local cluster command failed; inspect ignored command log")
        return result.stdout if capture else result.returncode

    def kubectl(self, *args, **kwargs):
        return self.run([ROOT / ".bin/kubectl", "--kubeconfig", self.kubeconfig,
                         "--context", self.context, "--namespace", self.namespace, *args], **kwargs)

    def helm(self, *args, **kwargs):
        return self.run([HELM, *args, "--kubeconfig", self.kubeconfig,
                         "--kube-context", self.context, "--namespace", self.namespace], **kwargs)

    def pod(self):
        data = json.loads(self.kubectl("get", "pods", "-l", "app.kubernetes.io/component=worker", "-o", "json", capture=True))
        return data["items"][0] if data["items"] else None

    async def ready_worker(self, previous_uid=None):
        end = time.monotonic() + 180
        while time.monotonic() < end:
            pod = await asyncio.to_thread(self.pod)
            if (pod and pod["metadata"]["uid"] != previous_uid and
                    not pod["metadata"].get("deletionTimestamp") and
                    len(pod.get("status", {}).get("containerStatuses", [])) == 2 and
                    all(row["ready"] for row in pod["status"]["containerStatuses"])):
                return pod
            await asyncio.sleep(1)
        raise TimeoutError("replacement worker pod did not become Ready")

    async def restart(self):
        before = await asyncio.to_thread(self.pod)
        self.old_pod = {key: before["metadata"][key] for key in ("name", "uid")}
        await asyncio.to_thread(self.kubectl, "delete", "pod", before["metadata"]["name"],
                                "--grace-period=0", "--force", "--wait=true", timeout=90)
        after = await self.ready_worker(before["metadata"]["uid"])
        self.new_pod = {key: after["metadata"][key] for key in ("name", "uid")}

    async def snapshot(self):
        code = ("import json; from agent_hardening_kit.durable_activities import Activities; "
                "print(json.dumps(Activities('/data/agent.sqlite', policies={}).snapshot('acme')))")
        output = await asyncio.to_thread(self.kubectl, "exec", "deployment/ahk-worker", "-c", "worker",
                                         "--", "python", "-c", code, capture=True)
        return json.loads(output)

    async def forward(self, service, remote_port):
        port = free_port()
        process = subprocess.Popen([str(ROOT / ".bin/kubectl"), "--kubeconfig", str(self.kubeconfig),
                                    "--context", self.context, "--namespace", self.namespace,
                                    "port-forward", "service/" + service, f"{port}:{remote_port}",
                                    "--address", "127.0.0.1"], cwd=ROOT, stdout=self.log, stderr=self.log)
        self.forwarders.append(process)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if process.poll() is not None:
                raise RuntimeError("owned port-forward exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return port
            except OSError:
                await asyncio.sleep(0.2)
        raise TimeoutError("owned port-forward did not start")

    def setup(self):
        existing = self.run([ROOT / ".bin/kind", "get", "clusters"], capture=True).splitlines()
        if self.name in existing:
            raise FileExistsError("refusing to reuse an existing cluster")
        self.stage = "image_build"
        # Mark first so a partial or interrupted build is still removed.
        self.image_built = True
        self.run(["docker", "build", "-t", self.image, "."], timeout=600)
        self.stage = "cluster_create"
        # Mark ownership before creation so a partial creation is still cleaned up.
        self.owned = True
        self.run([ROOT / ".bin/kind", "create", "cluster", "--name", self.name,
                  "--kubeconfig", self.kubeconfig, "--config", "deploy/kind.yaml", "--wait", "120s"], timeout=300)
        self.run([ROOT / ".bin/kind", "load", "docker-image", self.image, "--name", self.name], timeout=180)
        values = ["--set", "fullnameOverride=ahk", "--set-string", "image.repository=ahk-worker",
                  "--set-string", "image.tag=" + self.image.split(":")[1], "--set", "worker.faultAfterTicketOnce=true"]
        self.stage = "manifest_validation"
        rendered = self.directory / "rendered.yaml"
        rendered.write_text(self.run([HELM, "template", "ahk", "deploy/chart", "--namespace", self.namespace,
                                      *values], capture=True))
        self.run([sys.executable, ROOT / "scripts/check_manifests.py", rendered, "--local-image", self.image])
        self.run([ROOT / ".bin/kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context,
                  "create", "namespace", self.namespace])
        # Strict schema validation by the owned cluster's API server; nothing is persisted.
        self.kubectl("apply", "--dry-run=server", "--validate=strict", "-f", rendered)
        self.checks["server_side_schema_dry_run"] = True
        self.checks["manifest_policy"] = True
        self.stage = "helm_install"
        self.helm("install", "ahk", "deploy/chart", *values, "--wait", "--timeout", "10m", timeout=630)

    def inspect_deployment(self):
        pods = json.loads(self.kubectl("get", "pods", "-o", "json", capture=True))["items"]
        unbounded = [f"{pod['metadata']['name']}/{row['name']}" for pod in pods for row in pod["spec"]["containers"]
                     if not all(key in row.get("resources", {}).get(section, {})
                                for section in ("requests", "limits") for key in ("cpu", "memory"))]
        assert not unbounded, "containers without requests/limits: " + ", ".join(unbounded)
        worker = self.pod()
        limits = {row["name"]: row["resources"] for row in worker["spec"]["containers"]}
        claims = json.loads(self.kubectl("get", "pvc", "-o", "json", capture=True))["items"]
        phases = {claim["metadata"]["name"]: claim["status"].get("phase") for claim in claims}
        assert phases and all(phase == "Bound" for phase in phases.values()), "unbound PVC"
        data_claim = next(volume["persistentVolumeClaim"]["claimName"] for volume in worker["spec"]["volumes"]
                          if volume["name"] == "data")
        mounted = self.kubectl("exec", "deployment/ahk-worker", "-c", "worker", "--", "python", "-c",
                               "import os; print(os.path.ismount('/data') and os.access('/data', os.W_OK))",
                               capture=True).strip()
        assert mounted == "True", "worker /data is not a writable mount"
        services = json.loads(self.kubectl("get", "services", "-o", "json", capture=True))["items"]
        types = {svc["metadata"]["name"]: svc["spec"].get("type") for svc in services}
        assert all(kind == "ClusterIP" for kind in types.values()), "non-cluster-local Service"
        ingresses = json.loads(self.kubectl("get", "ingresses", "-o", "json", capture=True))["items"]
        assert not ingresses, "unexpected Ingress"
        self.checks.update(all_containers_bounded=True, pods_checked=len(pods), worker_resources=limits,
                           pvc_phases=phases, worker_data_claim=data_claim, worker_data_mounted_writable=True,
                           service_types=types, ingress_count=0)

    def diagnose(self):
        """Safe pod/container state summary for a failed run (no logs or payloads)."""
        pods = json.loads(self.kubectl("get", "pods", "-o", "json", capture=True, timeout=30))["items"]
        states = []
        for pod in pods:
            for row in pod.get("status", {}).get("containerStatuses", []):
                state = next(iter(row.get("state", {})), "unknown")
                states.append({"pod_component": pod["metadata"]["labels"].get("app.kubernetes.io/component"),
                               "container": row["name"], "ready": row.get("ready", False), "state": state,
                               "reason": row.get("state", {}).get(state, {}).get("reason"),
                               "restarts": row.get("restartCount", 0)})
        return states

    def cleanup(self):
        self.stage_at_cleanup = self.stage
        for process in self.forwarders:
            try:
                stop_owned(process)
            except Exception:
                pass  # The cluster deletion below also ends every port-forward.
        if self.owned:
            # Uninstall is best effort; deleting the owned cluster must always be attempted.
            try:
                self.helm("uninstall", "ahk", "--wait", "--timeout", "45s", check=False, timeout=60)
            except subprocess.TimeoutExpired:
                self.log.write("helm uninstall timed out; deleting the owned cluster anyway\n")
            self.run([ROOT / ".bin/kind", "delete", "cluster", "--name", self.name,
                      "--kubeconfig", self.kubeconfig], timeout=180)
            remaining = self.run([ROOT / ".bin/kind", "get", "clusters"], capture=True).splitlines()
            if self.name in remaining:
                raise RuntimeError("owned cluster teardown did not complete")
            self.checks_after = {"owned_cluster_listed_after_teardown": False,
                                 "kind_get_clusters_after": [name for name in remaining if name.startswith("ahk-")]}
        if self.image_built:
            try:
                self.run(["docker", "image", "rm", self.image], check=False, timeout=60)
            except subprocess.TimeoutExpired:
                pass
        self.log.close()


async def verify(cluster, report):
    await asyncio.to_thread(cluster.setup)
    await cluster.ready_worker()
    cluster.stage = "deployment_inspection"
    await asyncio.to_thread(cluster.inspect_deployment)
    report["deployment_checks"] = cluster.checks
    report["tool_versions"] = {
        "helm": cluster.run([HELM, "version", "--short"], capture=True).strip(),
        "kind": cluster.run([ROOT / ".bin/kind", "version"], capture=True).strip(),
        "kubectl_client": json.loads(cluster.run([ROOT / ".bin/kubectl", "version", "--client", "-o", "json"],
                                                 capture=True))["clientVersion"]["gitVersion"],
        "docker_server": cluster.run(["docker", "version", "--format", "{{.Server.Version}}"], capture=True).strip(),
    }
    docker_cpus, docker_memory = cluster.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
                                             capture=True).split()
    report["docker_resources"] = {"ncpu": int(docker_cpus), "mem_total_bytes": int(docker_memory)}
    report["kubernetes_version"] = json.loads(cluster.kubectl("version", "-o", "json", capture=True))["serverVersion"]["gitVersion"]
    report["image_id"] = cluster.run(["docker", "image", "inspect", cluster.image, "--format", "{{.Id}}"], capture=True).strip()
    report["runtime_image_pins"] = json.loads(cluster.kubectl("get", "pods", "-o", "json", capture=True))["items"]
    # Retain image digests and safe resource identities; never whole pod payloads.
    report["runtime_image_pins"] = [
        {"component": pod["metadata"]["labels"].get("app.kubernetes.io/component"),
         "containers": [{"name": row["name"], "image": row["image"], "image_id": row.get("imageID")}
                        for row in pod.get("status", {}).get("containerStatuses", [])]}
        for pod in report["runtime_image_pins"]]
    cluster.stage = "pod_kill_recovery"
    temporal_port = await cluster.forward("ahk-temporal", 7233)
    client = await Client.connect(f"127.0.0.1:{temporal_port}")
    report["runtime"] = await exercise(client, "ahk", cluster.restart, cluster.snapshot,
                                       cluster.directory / "histories", run_id=report["run_id"])
    report["killed_pod"] = cluster.old_pod
    report["replacement_pod"] = cluster.new_pod
    cluster.stage = "telemetry"
    canary = "PRIVATE_PROMPT_CANARY_84271"
    handle = await client.start_workflow(AgentWorkflow.run, {
        "request": {"tenant_id": "beta", "request_id": "telemetry-" + report["run_id"],
                    "question": "API authentication? " + canary + " person84271@example.com"},
    }, id="telemetry-" + report["run_id"], task_queue="ahk")
    await asyncio.wait_for(handle.result(), 30)
    trace_code = "from pathlib import Path; print(Path('/telemetry/traces.jsonl').read_text())"
    traces = cluster.kubectl("exec", "deployment/ahk-worker", "-c", "worker", "--", "python", "-c", trace_code, capture=True)
    assert "ahk.apply_ticket" in traces and "ahk.prepare" in traces
    assert canary not in traces and "person84271@example.com" not in traces
    # /telemetry is a pod-scoped emptyDir: every span here came from the replacement pod.
    assert (await asyncio.to_thread(cluster.pod))["metadata"]["uid"] == cluster.new_pod["uid"]
    report["telemetry_pod"] = cluster.new_pod["name"]
    report["restarted_worker_collector_receipt"] = True
    report["trace_canaries_absent"] = True
    report["trace_artifact_sha256"] = hashlib.sha256(traces.encode()).hexdigest()
    cluster.stage = "metrics_dashboard"
    prometheus_port = await cluster.forward("ahk-prometheus", 9090)
    grafana_port = await cluster.forward("ahk-grafana", 3000)
    session = requests.Session()
    session.trust_env = False
    try:
        url = f"http://127.0.0.1:{prometheus_port}/api/v1/query?" + urlencode({"query": "sum(ahk_requests_total)"})
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            response = session.get(url, timeout=5)
            response.raise_for_status()
            result = response.json()["data"]["result"]
            if result and float(result[0]["value"][1]) >= 3:
                report["prometheus_observed_requests"] = float(result[0]["value"][1])
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("live Prometheus did not ingest worker metrics")
        response = session.get(f"http://127.0.0.1:{prometheus_port}/federate",
                               params={"match[]": '{__name__=~"ahk_.*"}'}, timeout=10)
        response.raise_for_status()
        assert "ahk_" in response.text
        assert canary not in response.text and "person84271@example.com" not in response.text
        report["metrics_canaries_absent"] = True
        report["metrics_series_sha256"] = hashlib.sha256(response.content).hexdigest()
        dashboard = json.loads((ROOT / "observability/grafana/agent-hardening-kit.json").read_text())
        response = session.get(f"http://127.0.0.1:{grafana_port}/api/dashboards/uid/{dashboard['uid']}", timeout=10)
        response.raise_for_status()
        assert response.json()["dashboard"]["uid"] == dashboard["uid"]
        report["grafana_dashboard_query_passed"] = True
        response = session.get(f"http://127.0.0.1:{grafana_port}/api/datasources/uid/ahk-prometheus/health", timeout=10)
        response.raise_for_status()
        assert response.json()["status"] == "OK"
        report["grafana_prometheus_datasource_healthy"] = True
    finally:
        session.close()


def write_report(report, destination):
    destination.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (destination / "runs").mkdir(exist_ok=True)
    (destination / "runs" / (report["run_id"] + ".json")).write_text(text)
    (destination / "latest.json").write_text(text)
    runtime = report.get("runtime", {})
    checks = report.get("deployment_checks", {})
    versions = report.get("tool_versions", {})
    lines = ["# Local kind pod-kill recovery evidence", "",
             f"Status: **{report['status']}**" + (f" (failed stage: `{report.get('failed_stage')}`, "
                                                   f"{report.get('error_type')})" if report["status"] != "passed" else "") + ".",
             "", f"Source revision `{report['git_revision']}`; source tree SHA256 `{report['source_tree_sha256']}`; "
             f"uncommitted source changes: {report['source_changes_uncommitted']}.",
             f"Owned cluster `{report['cluster_name']}` removed and absent from `kind get clusters`: "
             f"{report.get('owned_cluster_removed', False)}.",
             f"Kubernetes {report.get('kubernetes_version', 'n/a')}; {versions.get('kind', 'kind n/a')}; "
             f"helm {versions.get('helm', 'n/a')}; Docker {versions.get('docker_server', 'n/a')}.",
             f"Environment: {report['environment']}; Docker resources: "
             + (f"{report['docker_resources']['ncpu']} CPUs, "
                f"{report['docker_resources']['mem_total_bytes'] / 2**30:.2f} GiB memory."
                if report.get("docker_resources") else "not recorded."),
             f"Total duration {report.get('duration_seconds', 0):.0f} s, including image build, cluster create and teardown.",
             f"Actual model spend: ${report['actual_model_spend_usd']}.", "",
             "## Recovery", "",
             f"Kill point: `{runtime.get('kill_point', 'unreached')}` with Activities `prepare`, `reserve`, `generate` and "
             "`validate_settle` completed. Only the worker pod was force-deleted.",
             f"Worker pod deleted to replacement Ready: {runtime.get('restart_seconds', 0):.1f} s.",
             f"Runs surviving the killed worker pod: **{runtime.get('recovery_completed', 0)}/"
             f"{runtime.get('recovery_attempted', 0)}**; histories replayed after restart: "
             f"{runtime.get('total_replayed', 0)}/{len(runtime.get('attempts', []))}.",
             f"Synthetic ticket effects: before kill {runtime.get('before_kill', {}).get('ticket_count', 'n/a')}, "
             f"after recovery {runtime.get('after_recovery', {}).get('ticket_count', 'n/a')}, "
             f"final {runtime.get('final_snapshot', {}).get('ticket_count', 'n/a')} "
             f"(one per approved action); lost-acknowledgement Activity retries reusing the effect: "
             f"{runtime.get('lost_acknowledgement_retries_observed', 0)}.", "",
             "| Case | Status | Approval | Effects | Replayed |", "|---|---|---|---|---|"]
    for row in runtime.get("attempts", []):
        lines.append(f"| {row['case']} | {row['status']} | {row.get('approval_state', 'n/a')} | "
                     f"{row.get('effect_count', 'n/a')} | {row.get('replayed', False)} |")
    failed = [row["case"] for row in runtime.get("attempts", []) if row["status"] != "completed"]
    lines += ["", f"Failed, timed-out or incomplete runs: {', '.join(failed) if failed else 'none'}.", "",
              "## Deployment and telemetry checks", "",
              f"- Rendered-manifest policy check: {checks.get('manifest_policy', False)}; strict server-side "
              f"schema dry run: {checks.get('server_side_schema_dry_run', False)}.",
              f"- All {checks.get('pods_checked', 0)} pods have CPU/memory requests and limits: "
              f"{checks.get('all_containers_bounded', False)}.",
              f"- PVCs bound: {checks.get('pvc_phases', {})}; worker `/data` mounted writable: "
              f"{checks.get('worker_data_mounted_writable', False)}.",
              f"- Service types: {sorted(set(checks.get('service_types', {}).values()))}; Ingress objects: "
              f"{checks.get('ingress_count', 'n/a')}.",
              f"- Spans received by the restarted worker's Collector: {report.get('restarted_worker_collector_receipt', False)}; "
              f"canaries absent from traces: {report.get('trace_canaries_absent', False)} and metrics: "
              f"{report.get('metrics_canaries_absent', False)}.",
              f"- Prometheus observed requests: {report.get('prometheus_observed_requests', 'n/a')}; Grafana dashboard "
              f"loaded: {report.get('grafana_dashboard_query_passed', False)}.", "",
              "## Limits", "",
              f"Observed recovery of one single-node kind cluster on {report['environment']}, not a universal SLO or a",
              "production availability guarantee. One worker replica with SQLite on a ReadWriteOnce PVC; the Temporal",
              "service is the unauthenticated development server. The backend is the deterministic fixture backend.",
              "Raw histories, logs and rendered manifests stay in the ignored `.cache/cluster-evidence/` directory."]
    (destination / "latest.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("evidence/cluster"))
    args = parser.parse_args()
    run_id = uuid.uuid4().hex[:10]
    directory = ROOT / ".cache/cluster-evidence" / run_id
    directory.mkdir(parents=True)
    report = {"schema_version": 1, "scope": "owned local kind pod recovery and live observability",
              "run_id": run_id, "cluster_name": "ahk-chaos-" + run_id, "status": "running",
              "environment": (f"GitHub Actions runner ({platform.system()} {platform.machine()})"
                              if os.environ.get("GITHUB_ACTIONS") == "true"
                              else f"a local machine ({platform.system()} {platform.machine()})"), "generated_at": datetime.now(timezone.utc).isoformat(),
              "actual_model_spend_usd": 0, **source_metadata()}
    assets = [ROOT / "Dockerfile", *(ROOT / "deploy").rglob("*")]
    report["deployment_files"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in assets if path.is_file()}
    cluster = Cluster(run_id, directory)
    started = time.monotonic()
    previous_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        asyncio.run(verify(cluster, report))
        assert report["source_tree_sha256"] == source_metadata()["source_tree_sha256"], "source changed during proof"
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        report["error_type"] = type(exc).__name__
        report["failed_stage"] = cluster.stage
        if isinstance(exc, (AssertionError, TimeoutError, RuntimeError, FileExistsError)):
            # Messages come from this driver only; they carry no request content.
            report["error_message"] = str(exc)[:200]
        if cluster.owned:
            try:
                report["container_states_at_failure"] = cluster.diagnose()
            except Exception as diagnostic:
                report["container_states_at_failure"] = "unavailable: " + type(diagnostic).__name__
        failure = directory / "histories/exercise-failure.json"
        if failure.exists():
            report["runtime"] = json.loads(failure.read_text())
    finally:
        try:
            cluster.cleanup()
            report["owned_cluster_removed"] = True
            report["teardown"] = getattr(cluster, "checks_after", {})
        except BaseException as exc:
            report["owned_cluster_removed"] = False
            report["cleanup_error_type"] = type(exc).__name__
            report["status"] = "failed"
        signal.signal(signal.SIGTERM, previous_handler)
        report["duration_seconds"] = time.monotonic() - started
        write_report(report, args.out)
    print(f"Local cluster proof: {report['status']}; teardown={report['owned_cluster_removed']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
