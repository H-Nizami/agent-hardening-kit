"""Offline fixtures, local runtime commands, and an explicit opt-in live evaluation."""
import asyncio
import argparse
import json
from pathlib import Path
import sys
import uuid

from pydantic import ValidationError

from .agent import run_request
from .backend import FixtureBackend
from .budget import BudgetStore
from .evaluation import evaluate, load_corpus, passes_regression, write_report
from .models import Request, TenantPolicy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run one local fixture query")
    demo.add_argument("--tenant", choices=("acme", "beta"), default="acme")
    demo.add_argument("--question", default="API authentication?")
    demo.add_argument("--request-id")
    demo.add_argument("--mode", choices=("baseline", "hardened"), default="hardened")
    demo.add_argument("--db", type=Path, default=Path(".cache/demo.sqlite"))
    evaluation = commands.add_parser("eval", help="Run fixed offline fixture/control cases")
    evaluation.add_argument("--out", type=Path, default=Path(".cache/eval"))
    live = commands.add_parser("eval-live", help="Run model-dependent cases against Groq (needs GROQ_API_KEY)")
    live.add_argument("--model", required=True)
    live.add_argument("--out", type=Path, default=Path("eval/results"))
    runtime_commands = {}
    for name in ("worker", "start", "status", "result", "approval-controller", "decide"):
        command = commands.add_parser(name, help="Local Temporal " + name)
        command.add_argument("--address", default="127.0.0.1:17333")
        runtime_commands[name] = command
    worker = runtime_commands["worker"]
    worker.add_argument("--db", type=Path, default=Path(".cache/runtime/agent.sqlite"))
    worker.add_argument("--queue", default="ahk")
    worker.add_argument("--otel-endpoint")
    worker.add_argument("--health-file", type=Path)
    worker.add_argument("--fault-after-ticket-once", action="store_true",
                        help="Lab test: lose one acknowledgement after a mock ticket commits")
    start = runtime_commands["start"]
    start.add_argument("--tenant", choices=("acme", "beta"), default="acme")
    start.add_argument("--question", default="API authentication?")
    start.add_argument("--request-id")
    start.add_argument("--queue", default="ahk")
    start.add_argument("--side-effect", action="store_true")
    start.add_argument("--approval-ttl-seconds", type=int, default=300)
    for name in ("status", "result", "decide"):
        runtime_commands[name].add_argument("--workflow-id", required=True)
    runtime_commands["result"].add_argument("--timeout", type=int, default=60)
    for name in ("approval-controller", "decide"):
        runtime_commands[name].add_argument("--socket", type=Path, default=Path(".cache/control/approve.sock"))
    runtime_commands["decide"].add_argument("--tenant", choices=("acme", "beta"), required=True)
    runtime_commands["decide"].add_argument("--decision", choices=("approve", "reject"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in runtime_commands:
            from .runtime import run_command
            return asyncio.run(run_command(args))
        if args.command == "demo":
            request = Request(tenant_id=args.tenant, request_id=args.request_id or uuid.uuid4().hex,
                              question=args.question)
            store = BudgetStore(args.db, {key: TenantPolicy() for key in ("acme", "beta")})
            backend = FixtureBackend()
            answer = run_request(request, mode=args.mode, store=store, backend=backend, corpus=load_corpus())
            print(json.dumps({"backend": backend.name, "mode": args.mode,
                              "actual_model_spend_usd": 0, "backend_calls": backend.calls,
                              "result": answer.model_dump()}, indent=2))
            return 0 if answer.status in {"answered", "refused"} else 2
        if args.command == "eval-live":
            from .live import key_from_environment, run_live, write_live_report
            report = run_live(args.model, key_from_environment())
            path = write_live_report(report, args.out)
            hardened = report["systems"]["hardened"]
            print(f"Live checks on {args.model}: {hardened['passed']}/{hardened['total']}. Report: {path}")
            return 0
        report = evaluate()
        write_report(report, args.out)
        summary = report["systems"]["hardened"]["summary"]
        print(f"Offline fixture checks: {summary['passed']}/{summary['total']}; actual model spend $0.")
        return 0 if passes_regression(report) else 1
    except (ValidationError, ValueError, OSError):
        print("Invalid local request, fixture data, or storage path.", file=sys.stderr)
        return 2
