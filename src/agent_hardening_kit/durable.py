"""Native Temporal orchestration of the local hardening nodes.

All IO is in individual activities. The local development Temporal service trusts
its clients; principal strings are authorization claims from the trusted operator
controller, not proof of network authentication.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .durable_activities import Activities


@workflow.defn(name="ahk-agent-v1")
class AgentWorkflow:
    def __init__(self):
        self._stage = "preparing"
        self._approval_state = "not_required"
        self._plan = None
        self._decision = None
        self._deadline = None

    async def _execute(self, name, payload):
        return await workflow.execute_activity(
            name, payload,
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
        )

    @workflow.run
    async def run(self, payload: dict) -> dict:
        self._plan = await self._execute("ahk.prepare", payload)
        if "answer" in self._plan:
            self._stage = "completed"
            return {"answer": self._plan["answer"], "approval_state": "not_required", "effect": None}
        self._stage = "reserving"
        admitted = await self._execute("ahk.reserve", self._plan)
        if "answer" in admitted:
            answer = admitted["answer"]
        else:
            self._stage = "generating"
            generated = await self._execute("ahk.generate", admitted)
            self._stage = "validating"
            answer = await self._execute("ahk.validate_settle", generated)
        if not self._plan["side_effect"] or answer["status"] != "answered":
            self._stage = "completed"
            return {"answer": answer, "approval_state": "not_required", "effect": None}
        self._deadline = workflow.now() + timedelta(seconds=self._plan["approval_ttl_seconds"])
        self._approval_state = "pending"
        self._stage = "pending_approval"
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=self._plan["approval_ttl_seconds"]),
            )
        except TimeoutError:
            self._approval_state = "expired"
            self._stage = "completed"
            return {"answer": answer, "approval_state": "expired", "effect": None}
        self._stage = "authorizing"
        authorization = await self._execute("ahk.authorize_decision", {
            "plan": self._plan, "decision": self._decision,
            "deadline": self._deadline.isoformat(),
        })
        self._approval_state = authorization["state"]
        effect = None
        if self._approval_state == "approved":
            self._stage = "applying"
            effect = await self._execute("ahk.apply_ticket", {
                "plan": self._plan, "authorization": authorization,
            })
        self._stage = "completed"
        return {"answer": answer, "approval_state": self._approval_state, "effect": effect}

    def _check_decision(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"tenant_id", "actor", "action_id", "decision"} or
                any(not isinstance(value, str) for value in payload.values())):
            raise ValueError("invalid approval contract")
        if self._plan is None or self._deadline is None:
            raise ValueError("approval is not pending")
        if (payload["tenant_id"] != self._plan["request"]["tenant_id"] or
                payload["action_id"] != self._plan["action_id"] or
                payload["actor"] not in self._plan["approvers"] or
                payload["decision"] not in {"approve", "reject"}):
            raise ValueError("approval is not authorized for this action")
        if self._decision is not None:
            if payload != self._decision:
                raise ValueError("approval decision already recorded")
            return
        if self._stage != "pending_approval" or workflow.now() >= self._deadline:
            raise ValueError("approval expired or not pending")

    @workflow.update
    def decide(self, payload: dict) -> str:
        self._check_decision(payload)
        self._decision = dict(payload)
        return "approved" if payload["decision"] == "approve" else "rejected"

    @decide.validator
    def validate_decide(self, payload: dict) -> None:
        self._check_decision(payload)

    @workflow.query
    def status(self) -> dict:
        return {
            "stage": self._stage, "approval_state": self._approval_state,
            "tenant_id": self._plan["request"]["tenant_id"] if self._plan and "request" in self._plan else None,
            "action_id": self._plan.get("action_id") if self._plan else None,
            "approval_deadline": self._deadline.isoformat() if self._deadline else None,
        }
