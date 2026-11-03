#!/usr/bin/env python3
"""Measure real local Temporal recovery; raw histories/logs remain in .cache.

This starts and stops only its own server/worker processes. It performs no model,
cloud or external side-effect calls. `exercise` can also be used against an owned
kind installation with caller-supplied restart and snapshot callbacks.
"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import temporalio
from temporalio.client import Client
from temporalio.worker import Replayer

from agent_hardening_kit.controller import ApprovalController, send_decision
from agent_hardening_kit.durable import AgentWorkflow
from agent_hardening_kit.durable_activities import Activities


ROOT = Path(__file__).resolve().parents[1]
NODES = ("ahk.prepare", "ahk.reserve", "ahk.generate", "ahk.validate_settle",
         "ahk.authorize_decision", "ahk.apply_ticket")


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def pending(handle, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = await asyncio.wait_for(handle.query("status"), timeout=3)
            if state["stage"] == "pending_approval":
                return state
        except Exception:
            # A query may arrive before the first Workflow Task. Do not copy
            # exception content into evidence; it can contain user payloads.
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("workflow did not reach its approval boundary")


def history_counts(history):
    scheduled = {}
    counts = Counter()
    attempts = {}
    updates = 0
    for event in history.events:
        kind = event.WhichOneof("attributes")
        if kind == "activity_task_scheduled_event_attributes":
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            scheduled[event.event_id] = name
            counts[name] += 1
        elif kind == "activity_task_started_event_attributes":
            attributes = event.activity_task_started_event_attributes
            name = scheduled[attributes.scheduled_event_id]
            attempts[name] = max(attempts.get(name, 0), attributes.attempt)
        elif kind == "workflow_execution_update_accepted_event_attributes":
            updates += 1
    return {"scheduled": dict(sorted(counts.items())),
            "maximum_started_attempt": dict(sorted(attempts.items())),
            "accepted_updates": updates}


async def rejected_update(handle, payload):
    try:
        await asyncio.wait_for(handle.execute_update("decide", payload), timeout=15)
    except Exception as exc:
        return {"rejected": True, "error_type": type(exc).__name__}
    raise AssertionError("an unauthorized/conflicting update was accepted")


async def exercise(client, queue, restart, snapshot, outdir, *, run_id=None):
    """Exercise a real service with callbacks for owned worker restart/store reads.

    `restart()` must kill/restart only the test worker and await its readiness.
    `snapshot()` returns the Acme generation/ticket/reservation counters. `outdir`
    is for ignored raw histories; caller owns process cleanup and report metadata.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    attempts = []
    section = {"status": "running", "kill_point": "pending_approval", "attempts": attempts,
               "actual_model_spend_usd": 0, "recovery_attempted": 3,
               "recovery_completed": 0, "recovery_replay_count": 0}

    async def start(label, ttl=120):
        workflow_id = f"rt-{run_id}-{label}"
        handle = await client.start_workflow(AgentWorkflow.run, {
            "request": {"tenant_id": "acme", "request_id": workflow_id,
                        "question": "API authentication?"},
            "side_effect": True, "approval_ttl_seconds": ttl,
        }, id=workflow_id, task_queue=queue,
            execution_timeout=timedelta(minutes=3))
        row = {"workflow_id": workflow_id, "case": label, "status": "started"}
        attempts.append(row)
        return handle, row

    async def save_and_replay(handle, row):
        history = await handle.fetch_history()
        (outdir / f"{handle.id}.json").write_text(history.to_json() + "\n")
        row["history_sha256"] = hashlib.sha256(history.to_json().encode()).hexdigest()
        row["activities"] = history_counts(history)
        await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
        row["replayed"] = True
        return row["activities"]

    try:
        recovery = [await start(f"recover-{number}") for number in range(1, 4)]
        states = await asyncio.gather(*(pending(handle) for handle, _ in recovery))
        section["before_kill"] = await maybe_await(snapshot())
        assert section["before_kill"]["generation_count"] == 3
        assert section["before_kill"]["ticket_count"] == 0
        for (handle, row), state in zip(recovery, states):
            row["pre_kill_activity_counts"] = history_counts(await handle.fetch_history())["scheduled"]
            assert row["pre_kill_activity_counts"] == {name: 1 for name in NODES[:4]}
            row["pre_kill_stage"] = state["stage"]
        restart_started = time.monotonic()
        await maybe_await(restart())
        section["restart_seconds"] = time.monotonic() - restart_started
        after_states = await asyncio.gather(*(pending(handle) for handle, _ in recovery))
        assert [row["action_id"] for row in states] == [row["action_id"] for row in after_states]
        assert (await maybe_await(snapshot()))["generation_count"] == 3

        with tempfile.TemporaryDirectory(prefix="ahk-ctl-", dir="/tmp") as directory:
            control_socket = Path(directory) / "approval.sock"
            async with ApprovalController(client).listen(control_socket):
                for (handle, row), state in zip(recovery, after_states):
                    decision = await send_decision(control_socket, {
                        "workflow_id": handle.id, "tenant_id": "acme",
                        "action_id": state["action_id"], "decision": "approve",
                    })
                    assert decision == "approved"
                    row["approval_channel"] = "unix_socket_kernel_peer_uid"
                results = await asyncio.gather(*(asyncio.wait_for(handle.result(), 60) for handle, _ in recovery))
                retry_count = 0
                for (handle, row), result in zip(recovery, results):
                    assert result["answer"]["status"] == "answered"
                    assert result["approval_state"] == "approved"
                    assert result["effect"]["ticket_id"]
                    row.update(status="completed", approval_state=result["approval_state"],
                               effect_count=1, acknowledgement_retry_reused_effect=not result["effect"]["created"])
                    counts = await save_and_replay(handle, row)
                    assert counts["scheduled"] == {name: 1 for name in NODES}
                    retry = counts["maximum_started_attempt"].get("ahk.apply_ticket", 0)
                    if retry >= 2:
                        assert not result["effect"]["created"]
                        retry_count += 1
                    section["recovery_completed"] += 1
                    section["recovery_replay_count"] += 1
                assert retry_count >= 1, "lost acknowledgement did not exercise a real Activity retry"
                section["lost_acknowledgement_retries_observed"] = retry_count
                section["after_recovery"] = await maybe_await(snapshot())
                assert section["after_recovery"]["generation_count"] == 3
                assert section["after_recovery"]["ticket_count"] == 3

                reject_handle, reject_row = await start("reject", ttl=30)
                state = await pending(reject_handle)
                valid = {"tenant_id": "acme", "actor": "local-operator",
                         "action_id": state["action_id"], "decision": "approve"}
                reject_row["denied_decisions"] = {}
                for label, overrides in (("wrong_actor", {"actor": "untrusted"}),
                                         ("wrong_tenant", {"tenant_id": "beta"}),
                                         ("wrong_action", {"action_id": "0" * 64})):
                    reject_row["denied_decisions"][label] = await rejected_update(reject_handle, {**valid, **overrides})
                assert await send_decision(control_socket, {
                    "workflow_id": reject_handle.id, "tenant_id": "acme",
                    "action_id": state["action_id"], "decision": "reject",
                }) == "rejected"
                result = await asyncio.wait_for(reject_handle.result(), 30)
                assert result["approval_state"] == "rejected" and result["effect"] is None
                reject_row.update(status="completed", approval_state="rejected", effect_count=0)
                await save_and_replay(reject_handle, reject_row)

                duplicate_handle, duplicate_row = await start("duplicate", ttl=30)
                state = await pending(duplicate_handle)
                decision = {"tenant_id": "acme", "actor": "local-operator",
                            "action_id": state["action_id"], "decision": "approve"}
                update_id = "stable-approval-" + run_id
                first = await duplicate_handle.execute_update("decide", decision, id=update_id)
                second = await duplicate_handle.execute_update("decide", decision, id=update_id)
                assert first == second == "approved"
                duplicate_row["same_update_id_replayed_result"] = True
                duplicate_row["conflicting_decision"] = await rejected_update(
                    duplicate_handle, {**decision, "decision": "reject"})
                result = await asyncio.wait_for(duplicate_handle.result(), 30)
                assert result["approval_state"] == "approved" and result["effect"] is not None
                duplicate_row.update(status="completed", approval_state="approved", effect_count=1)
                counts = await save_and_replay(duplicate_handle, duplicate_row)
                assert counts["accepted_updates"] == 1

                expired_handle, expired_row = await start("expire", ttl=1)
                result = await asyncio.wait_for(expired_handle.result(), 30)
                assert result["approval_state"] == "expired" and result["effect"] is None
                expired_row.update(status="completed", approval_state="expired", effect_count=0)
                await save_and_replay(expired_handle, expired_row)
        section["final_snapshot"] = await maybe_await(snapshot())
        assert section["final_snapshot"]["generation_count"] == 6
        assert section["final_snapshot"]["ticket_count"] == 4
        section["total_replayed"] = sum(row.get("replayed", False) for row in attempts)
        section["status"] = "passed"
        return section
    except BaseException as exc:
        section["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        section["error_type"] = type(exc).__name__
        for row in attempts:
            if row["status"] == "started":
                row["status"] = "incomplete"
        # Preserve all attempts for the caller even on a failed exercise.
        (outdir / "exercise-failure.json").write_text(json.dumps(section, indent=2, sort_keys=True) + "\n")
        raise


def source_metadata():
    paths = sorted(path for path in (ROOT / "src" / "agent_hardening_kit").rglob("*")
                   if path.is_file() and path.suffix in {".py", ".json"})
    paths += [ROOT / name for name in ("pyproject.toml", "uv.lock", "requirements-dev.lock")]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    # Everything that can change what an evidence run builds, deploys or checks.
    tracked = ["src", "pyproject.toml", "uv.lock", "requirements-dev.lock", "Dockerfile", "deploy",
               "scripts", "observability", "Makefile", ".github/workflows"]
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", *tracked],
                                cwd=ROOT, check=True, capture_output=True, text=True).stdout)
    return {"git_revision": revision, "source_tree_sha256": digest,
            "source_files": hashes, "source_changes_uncommitted": dirty}


def stop_owned(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class OwnedRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.server = None
        self.worker = None
        self.logs = []
        self.queue = "runtime-" + uuid.uuid4().hex
        self.db = directory / "agent.sqlite"
        self.health = directory / "worker.ready"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.address = f"127.0.0.1:{self.port}"

    def spawn(self, arguments, name):
        output = (self.directory / name).open("wb")
        self.logs.append(output)
        return subprocess.Popen(arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True)

    async def start(self):
        binary = ROOT / ".bin" / "temporal"
        if not binary.is_file():
            raise FileNotFoundError("install pinned Temporal with make tools")
        self.server = self.spawn([
            str(binary), "--disable-config-file", "--disable-config-env", "server", "start-dev",
            "--ip", "127.0.0.1", "--port", str(self.port), "--headless",
            "--db-filename", str(self.directory / "temporal.sqlite"),
        ], "server.log")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError("owned Temporal server exited before readiness")
            try:
                client = await asyncio.wait_for(Client.connect(self.address), timeout=2)
                break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise TimeoutError("owned Temporal server readiness timed out")
        await self.start_worker()
        return client

    async def start_worker(self):
        self.health.unlink(missing_ok=True)
        self.worker = self.spawn([
            sys.executable, "-m", "agent_hardening_kit", "worker", "--address", self.address,
            "--queue", self.queue, "--db", str(self.db), "--health-file", str(self.health),
            "--fault-after-ticket-once",
        ], "worker-" + uuid.uuid4().hex[:8] + ".log")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.worker.poll() is not None:
                raise RuntimeError("owned worker exited before readiness")
            if self.health.is_file():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("owned worker readiness timed out")

    async def restart(self):
        if self.worker is None or self.worker.poll() is not None:
            raise RuntimeError("owned worker was not running at kill point")
        self.worker.kill()
        self.worker.wait(timeout=5)
        await self.start_worker()

    def snapshot(self):
        return Activities(self.db).snapshot("acme")

    def close(self):
        errors = []
        for process in (self.worker, self.server):
            try:
                stop_owned(process)
            except Exception as exc:
                errors.append(type(exc).__name__)
        for output in self.logs:
            output.close()
        if errors:
            raise RuntimeError("owned process cleanup failed")


async def run_owned(report, raw):
    runtime = OwnedRuntime(raw)
    try:
        client = await runtime.start()
        report["runtime"] = await exercise(client, runtime.queue, runtime.restart, runtime.snapshot,
                                            raw / "histories", run_id=report["run_id"])
        report["status"] = "passed"
    finally:
        failure = raw / "histories" / "exercise-failure.json"
        if failure.exists() and "runtime" not in report:
            report["runtime"] = json.loads(failure.read_text())
        try:
            runtime.close()
        finally:
            report["owned_processes_stopped"] = all(process is None or process.poll() is not None
                                                    for process in (runtime.worker, runtime.server))


def write_report(report, destination):
    destination.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (destination / "latest.json").write_text(encoded)
    runs = destination / "runs"
    runs.mkdir(exist_ok=True)
    (runs / (report["run_id"] + ".json")).write_text(encoded)
    runtime = report.get("runtime", {})
    lines = ["# Local Temporal recovery evidence", "", f"Status: **{report['status']}**.", "",
             "This uses real localhost Temporal and worker processes, not Kubernetes.",
             f"Source tree SHA256: `{report['source_tree_sha256']}`.",
             f"Model spend: ${report['actual_model_spend_usd']}.", "",
             f"Recovery: {runtime.get('recovery_completed', 0)}/{runtime.get('recovery_attempted', 0)} workflows completed "
             f"after SIGKILL at `{runtime.get('kill_point', 'unreached')}`.",
             f"Recovery histories replayed: {runtime.get('recovery_replay_count', 0)}.",
             f"Lost-acknowledgement Activity retries observed: {runtime.get('lost_acknowledgement_retries_observed', 0)}.",
             f"Owned processes stopped: {report.get('owned_processes_stopped', False)}.", "",
             "| Case | Status | Approval | Effects | Replay |", "|---|---|---|---|---|"]
    for row in runtime.get("attempts", []):
        lines.append(f"| {row['case']} | {row['status']} | {row.get('approval_state', 'n/a')} | "
                     f"{row.get('effect_count', 'n/a')} | {row.get('replayed', False)} |")
    lines += ["", "Limits: the fixture backend is deterministic and free; local SQLite and the",
              "unauthenticated development Temporal service are not a production multi-host service.",
              "The successful approval channel verifies the local kernel peer UID; direct Temporal access is trusted.",
              "A started generation with unknown outcome fails closed; external API exactly-once execution is not claimed."]
    (destination / "latest.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("evidence/runtime"))
    args = parser.parse_args()
    run_id = uuid.uuid4().hex[:12]
    raw = ROOT / ".cache" / "runtime-evidence" / run_id
    raw.mkdir(parents=True)
    report = {"schema_version": 1, "run_id": run_id, "status": "running",
              "scope": "real local Temporal process recovery and authorization controls",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
              "sdk_version": temporalio.__version__, "actual_model_spend_usd": 0,
              **source_metadata()}
    started = time.monotonic()
    try:
        report["server_version"] = subprocess.run([str(ROOT / ".bin" / "temporal"), "--version"],
                                                   check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        asyncio.run(run_owned(report, raw))
        report["source_tree_sha256_after"] = source_metadata()["source_tree_sha256"]
        if report["source_tree_sha256_after"] != report["source_tree_sha256"]:
            raise RuntimeError("application source changed during the evidence run")
    except BaseException as exc:
        report["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        report["error_type"] = type(exc).__name__
    finally:
        report["duration_seconds"] = time.monotonic() - started
        write_report(report, args.out)
    print(f"Temporal recovery evidence: {report['status']}; {args.out / 'latest.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
