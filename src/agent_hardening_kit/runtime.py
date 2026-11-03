"""Explicit local Temporal clients and worker, separate from the offline CLI."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from temporalio.client import Client
from temporalio.worker import Worker

from .controller import ApprovalController, send_decision
from .durable import AgentWorkflow
from .durable_activities import Activities
from .models import Request
from .telemetry import Observer


def local_address(value):
    parsed = urlsplit("//" + value)
    if (parsed.hostname not in {"127.0.0.1", "localhost", "temporal", "ahk-temporal", "ahk-agent-hardening-kit-temporal"}
            or not parsed.port or parsed.username or parsed.password or parsed.path):
        raise ValueError("use a local Temporal endpoint or its dev-cluster service")
    return value


async def run_worker(address, db, queue="ahk", otel_endpoint=None, health_file=None, fault_after_ticket_once=False):
    client = await Client.connect(local_address(address))
    observer = Observer(otel_endpoint)
    fault_sent = False

    def lost_acknowledgement():
        nonlocal fault_sent
        if fault_after_ticket_once and not fault_sent:
            fault_sent = True
            raise RuntimeError("synthetic lost acknowledgement after committed mock effect")

    activities = Activities(db, observer=observer, after_ticket_commit=lost_acknowledgement)
    ready = Path(health_file) if health_file else None
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            async with Worker(client, task_queue=queue, workflows=[AgentWorkflow],
                              activities=activities.registered(), activity_executor=executor):
                if ready:
                    ready.parent.mkdir(parents=True, exist_ok=True)
                    ready.write_text("ready\n")
                await asyncio.Event().wait()
    finally:
        if ready and ready.exists():
            ready.unlink()
        observer.shutdown()


async def run_command(args):
    if args.command == "worker":
        await run_worker(args.address, args.db, args.queue, args.otel_endpoint,
                         args.health_file, args.fault_after_ticket_once)
        return 0
    client = await Client.connect(local_address(args.address))
    if args.command == "start":
        request_id = args.request_id or uuid.uuid4().hex
        request = Request(tenant_id=args.tenant, request_id=request_id, question=args.question)
        workflow_id = "ahk-" + args.tenant + "-" + request_id
        handle = await client.start_workflow(AgentWorkflow.run, {
            "request": request.model_dump(), "side_effect": args.side_effect,
            "approval_ttl_seconds": args.approval_ttl_seconds,
        }, id=workflow_id, task_queue=args.queue)
        print(json.dumps({"workflow_id": handle.id, "actual_model_spend_usd": 0}))
        return 0
    if args.command == "approval-controller":
        async with ApprovalController(client).listen(args.socket):
            await asyncio.Event().wait()
        return 0
    handle = client.get_workflow_handle(args.workflow_id)
    if args.command == "status":
        print(json.dumps(await handle.query("status"), indent=2))
    elif args.command == "result":
        print(json.dumps(await asyncio.wait_for(handle.result(), args.timeout), indent=2))
    elif args.command == "decide":
        state = await handle.query("status")
        response = await send_decision(args.socket, {
            "workflow_id": args.workflow_id, "tenant_id": args.tenant,
            "action_id": state["action_id"], "decision": args.decision,
        })
        print(json.dumps({"approval": response}))
    return 0
