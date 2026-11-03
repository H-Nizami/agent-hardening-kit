"""IO boundaries and durable ledgers for the offline Temporal worker."""

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import time

from langsmith import tracing_context
from pydantic import Field, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .agent import _blocked, _fingerprint, _supported
from .backend import FixtureBackend
from .budget import AdmissionDenied, BudgetStore, Reservation
from .evaluation import load_corpus
from .models import Answer, Document, ModelResponse, Request, StrictModel, TenantPolicy, Usage
from .retrieval import INJECTION, evidence_covers_question, redact, route, search
from .telemetry import outcome_status


class WorkflowInput(StrictModel):
    request: Request
    side_effect: bool = False
    approval_ttl_seconds: int = Field(default=300, ge=1, le=3600)


def action_identity(request, workflow_id):
    identity = [request.tenant_id, request.request_id, workflow_id, "create_ticket"]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def workflow_binding():
    # The fallback is only for direct offline activity-contract tests. Real
    # workers always obtain this binding from SDK execution context, never JSON.
    return activity.info().workflow_id if activity.in_activity() else "local-unit"


class Activities:
    """Trusted worker configuration, never taken from Workflow input.

    Generation uses a durable started marker before invoking the local fixture.
    An interrupted call fails closed; it is not reissued as an unreserved call.
    External provider exactly-once execution is deliberately not claimed.
    """

    def __init__(self, db_path, policies=None, approvers=None, backend=None, observer=None,
                 after_ticket_commit=None):
        self.store = BudgetStore(db_path, policies if policies is not None else {
            "acme": TenantPolicy(), "beta": TenantPolicy(),
        })
        self.approvers = approvers if approvers is not None else {
            "acme": ["local-operator"], "beta": ["local-operator"],
        }
        if any(not isinstance(actors, (list, tuple)) or not actors or
               any(not isinstance(actor, str) or not actor or len(actor) > 64 for actor in actors)
               for actors in self.approvers.values()):
            raise ValueError("invalid trusted approver configuration")
        self.backend = backend if backend is not None else FixtureBackend()
        self.observer = observer
        if after_ticket_commit is not None and not callable(after_ticket_commit):
            raise ValueError("after-ticket hook must be a trusted callable")
        self.after_ticket_commit = after_ticket_commit
        with self.store._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS durable_generations (
                    tenant TEXT NOT NULL, request TEXT NOT NULL, input_hash TEXT NOT NULL,
                    state TEXT NOT NULL, outcome TEXT,
                    PRIMARY KEY(tenant,request)
                );
                CREATE TABLE IF NOT EXISTS durable_approvals (
                    tenant TEXT NOT NULL, action_id TEXT NOT NULL, actor TEXT NOT NULL,
                    decision TEXT NOT NULL, deadline TEXT NOT NULL,
                    PRIMARY KEY(tenant,action_id)
                );
                CREATE TABLE IF NOT EXISTS durable_tickets (
                    tenant TEXT NOT NULL, action_id TEXT NOT NULL, request TEXT NOT NULL,
                    ticket_id TEXT NOT NULL, PRIMARY KEY(tenant,action_id)
                );
            """)

    def _span(self, stage):
        return self.observer.activity_span(stage) if self.observer else nullcontext()

    def _outcome(self, answer, started_at, usage=None):
        if self.observer:
            self.observer.record_outcome(
                outcome_status(answer), max(0.0, time.time() - started_at), usage=usage,
            )

    @contextmanager
    def _activity_errors(self, started_at):
        # Delivery metrics are at-least-once under Temporal retries, not a ledger.
        try:
            yield
        except Exception as exc:
            if self.observer:
                expected_denial = isinstance(exc, ApplicationError) and exc.type == "InvalidRequest"
                self.observer.record_outcome(
                    "blocked" if expected_denial else "failed", max(0.0, time.time() - started_at),
                )
            raise

    def registered(self):
        return [self.prepare, self.reserve, self.generate, self.validate_settle,
                self.authorize_decision, self.apply_ticket]

    @activity.defn(name="ahk.prepare")
    def prepare(self, payload: dict) -> dict:
        started_at = time.time()
        with self._span("prepare"), self._activity_errors(started_at):
            try:
                inputs = WorkflowInput.model_validate(payload)
            except (ValidationError, TypeError):
                raise ApplicationError("invalid workflow input", type="InvalidRequest", non_retryable=True) from None
            request = inputs.request
            try:
                self.store.snapshot(request.tenant_id)
            except AdmissionDenied as exc:
                answer = _blocked(exc.reason)
                self._outcome(answer, started_at)
                return {"answer": answer.model_dump()}
            if INJECTION.search(request.question):
                answer = _blocked("input_injection")
                self._outcome(answer, started_at)
                return {"answer": answer.model_dump()}
            request = request.model_copy(update={"question": redact(request.question)})
            corpus = load_corpus()
            documents = []
            for source in route(request.question):
                documents.extend(search(corpus, question=request.question, source=source,
                                        tenant_id=request.tenant_id, hardened=True))
            if not documents:
                answer = Answer(status="refused", reason_code="insufficient_evidence")
                self._outcome(answer, started_at)
                return {"answer": answer.model_dump()}
            workflow_id = workflow_binding()
            action_id = action_identity(request, workflow_id)
            return {"request": request.model_dump(), "documents": [doc.model_dump() for doc in documents],
                    "input_hash": _fingerprint(request, documents, self.backend, "hardened"),
                    "side_effect": inputs.side_effect,
                    "started_at": started_at,
                    "approval_ttl_seconds": inputs.approval_ttl_seconds,
                    "action_id": action_id,
                    "workflow_id": workflow_id,
                    "approvers": list(self.approvers.get(request.tenant_id, []))}

    @activity.defn(name="ahk.reserve")
    def reserve(self, plan: dict) -> dict:
        with self._span("reserve"), self._activity_errors(plan["started_at"]):
            request = Request.model_validate(plan["request"])
            documents = [Document.model_validate(row) for row in plan["documents"]]
            quote = self.backend.quote(request, documents)
            try:
                reservation = self.store.reserve(request.tenant_id, request.request_id,
                                                 "durable_synthesis", plan["input_hash"], quote)
            except AdmissionDenied as exc:
                answer = _blocked(exc.reason)
                self._outcome(answer, plan["started_at"])
                return {"answer": answer.model_dump()}
            if reservation.response is not None:
                self._outcome(Answer.model_validate(reservation.response), plan["started_at"])
                return {"answer": reservation.response}
            return {"plan": plan, "reservation": asdict(reservation), "quote": quote.model_dump()}

    @activity.defn(name="ahk.generate")
    def generate(self, admitted: dict) -> dict:
        with self._span("generate"), self._activity_errors(admitted["plan"]["started_at"]):
            plan = admitted["plan"]
            request = Request.model_validate(plan["request"])
            documents = [Document.model_validate(row) for row in plan["documents"]]
            with self.store._transaction() as connection:
                row = connection.execute("SELECT * FROM durable_generations WHERE tenant=? AND request=?",
                                         (request.tenant_id, request.request_id)).fetchone()
                if row:
                    if row["input_hash"] != plan["input_hash"]:
                        raise ApplicationError("generation identity conflict", type="Conflict", non_retryable=True)
                    if row["outcome"] is not None:
                        return {**admitted, **json.loads(row["outcome"])}
                    return {**admitted, "failure": "previous_outcome_unknown"}
                # Cross-check the real reservation before assigning model ownership.
                reservation = connection.execute(
                    "SELECT input_hash,status FROM reservations WHERE tenant=? AND request=? AND call='durable_synthesis'",
                    (request.tenant_id, request.request_id)).fetchone()
                if reservation is None or reservation["input_hash"] != plan["input_hash"] or reservation["status"] != "reserved":
                    raise ApplicationError("no active generation reservation", type="Conflict", non_retryable=True)
                connection.execute("INSERT INTO durable_generations VALUES(?,?,?,'started',NULL)",
                                   (request.tenant_id, request.request_id, plan["input_hash"]))
            try:
                # Do not let inherited LangSmith settings export fixture data.
                with tracing_context(enabled=False):
                    raw = self.backend.generate(request, documents)
                outcome = {"raw": raw}
                encoded = json.dumps(outcome)
            except Exception:
                outcome = {"failure": "backend_failure"}
                encoded = json.dumps(outcome)
            with self.store._transaction() as connection:
                connection.execute("UPDATE durable_generations SET state='complete',outcome=? WHERE tenant=? AND request=?",
                                   (encoded, request.tenant_id, request.request_id))
            return {**admitted, **outcome}

    @activity.defn(name="ahk.validate_settle")
    def validate_settle(self, generated: dict) -> dict:
        with self._span("validate_settle"), self._activity_errors(generated["plan"]["started_at"]):
            plan = generated["plan"]
            documents = [Document.model_validate(row) for row in plan["documents"]]
            request = Request.model_validate(plan["request"])
            raw = generated.get("raw")
            usage = None
            try:
                usage = Usage.from_meter(raw.get("usage")) if isinstance(raw, dict) else None
            except (ValueError, TypeError):
                pass
            if "failure" in generated:
                answer = _blocked(generated["failure"])
            elif isinstance(raw, dict) and "tool_calls" in raw:
                answer = _blocked("unsupported_tool_call", usage=usage)
            else:
                try:
                    response = ModelResponse.model_validate(raw)
                    usage = response.usage
                    quote = generated["quote"]
                    text = redact(response.answer)
                    if usage.total_tokens > quote["input_tokens"] + quote["max_output_tokens"] or usage.cost_microusd > quote["cost_microusd"]:
                        answer = _blocked("usage_overrun", usage=usage)
                    elif not text.strip():
                        answer = Answer(status="refused", reason_code="insufficient_evidence", usage=usage)
                    elif INJECTION.search(text) or not _supported(text, response.citations, documents):
                        answer = _blocked("unsupported_evidence", usage=usage)
                    elif not evidence_covers_question(request.question, documents):
                        answer = Answer(status="refused", reason_code="insufficient_evidence", usage=usage)
                    else:
                        answer = Answer(status="answered", answer=text, citations=response.citations,
                                        reason_code="ok", usage=usage)
                except (ValidationError, TypeError):
                    answer = _blocked("invalid_output", usage=usage)
            reservation = Reservation(**generated["reservation"])
            self.store.settle(reservation, usage, response=answer, failed=usage is None)
            self._outcome(answer, plan["started_at"], usage)
            return answer.model_dump()

    def _decision(self, payload):
        plan, decision = payload["plan"], payload["decision"]
        tenant = plan["request"]["tenant_id"]
        if (not isinstance(decision, dict) or set(decision) != {"tenant_id", "actor", "action_id", "decision"} or
                any(not isinstance(value, str) for value in decision.values()) or
                decision["tenant_id"] != tenant or decision["action_id"] != plan["action_id"] or
                decision["actor"] not in self.approvers.get(tenant, []) or
                decision["decision"] not in {"approve", "reject"}):
            raise ApplicationError("approval is not authorized", type="ApprovalDenied", non_retryable=True)
        deadline = datetime.fromisoformat(payload["deadline"])
        if deadline.tzinfo is None:
            raise ApplicationError("approval deadline must be timezone aware", type="ApprovalDenied", non_retryable=True)
        return plan, decision, deadline

    @staticmethod
    def _effect_request(plan):
        request = Request.model_validate(plan["request"])
        workflow_id = plan.get("workflow_id")
        if (not isinstance(workflow_id, str) or not workflow_id or len(workflow_id) > 1024 or
                plan.get("side_effect") is not True or
                plan["action_id"] != action_identity(request, workflow_id) or
                (activity.in_activity() and workflow_id != workflow_binding())):
            raise ApplicationError("action is not bound to this request", type="ApprovalDenied", non_retryable=True)
        return request

    @activity.defn(name="ahk.authorize_decision")
    def authorize_decision(self, payload: dict) -> dict:
        with self._span("approval"):
            try:
                result = self._authorize_decision(payload)
            except ApplicationError:
                if self.observer:
                    self.observer.record_approval("denied")
                raise
            except Exception:
                if self.observer:
                    self.observer.record_approval("failed")
                raise
            if self.observer:
                self.observer.record_approval(result["state"])
            return result

    def _authorize_decision(self, payload: dict) -> dict:
        # Recheck authorization in trusted worker code before persisting a grant.
        plan, decision, deadline = self._decision(payload)
        self._effect_request(plan)
        tenant = plan["request"]["tenant_id"]
        with self.store._transaction() as connection:
            existing = connection.execute("SELECT * FROM durable_approvals WHERE tenant=? AND action_id=?",
                                          (tenant, plan["action_id"])).fetchone()
            if existing:
                if existing["actor"] != decision["actor"] or existing["decision"] != decision["decision"] or existing["deadline"] != payload["deadline"]:
                    raise ApplicationError("approval already recorded", type="ApprovalDenied", non_retryable=True)
                return {"state": "approved" if existing["decision"] == "approve" else "rejected",
                        "action_id": plan["action_id"], "tenant_id": tenant}
            if datetime.now(timezone.utc) >= deadline:
                return {"state": "expired", "action_id": plan["action_id"], "tenant_id": tenant}
            connection.execute("INSERT INTO durable_approvals VALUES(?,?,?,?,?)",
                               (tenant, plan["action_id"], decision["actor"], decision["decision"], payload["deadline"]))
        return {"state": "approved" if decision["decision"] == "approve" else "rejected",
                "action_id": plan["action_id"], "tenant_id": tenant}

    @activity.defn(name="ahk.apply_ticket")
    def apply_ticket(self, payload: dict) -> dict:
        with self._span("apply_ticket"):
            plan, authorization = payload["plan"], payload["authorization"]
            request = self._effect_request(plan)
            tenant = request.tenant_id
            if authorization != {"state": "approved", "action_id": plan["action_id"], "tenant_id": tenant}:
                raise ApplicationError("ticket requires approval", type="ApprovalDenied", non_retryable=True)
            with self.store._transaction() as connection:
                existing = connection.execute("SELECT ticket_id FROM durable_tickets WHERE tenant=? AND action_id=?",
                                              (tenant, plan["action_id"])).fetchone()
                if existing:
                    return {"ticket_id": existing["ticket_id"], "action_id": plan["action_id"], "created": False}
                completion = connection.execute(
                    "SELECT input_hash,status,response FROM reservations WHERE tenant=? AND request=? AND call='durable_synthesis'",
                    (tenant, request.request_id)).fetchone()
                if (completion is None or completion["input_hash"] != plan["input_hash"] or
                        completion["status"] != "complete" or completion["response"] is None or
                        json.loads(completion["response"])["status"] != "answered"):
                    raise ApplicationError("ticket requires a validated answer", type="ApprovalDenied", non_retryable=True)
                approval = connection.execute("SELECT * FROM durable_approvals WHERE tenant=? AND action_id=?",
                                              (tenant, plan["action_id"])).fetchone()
                if approval is None or approval["decision"] != "approve" or approval["actor"] not in self.approvers.get(tenant, []):
                    raise ApplicationError("ticket requires recorded approval", type="ApprovalDenied", non_retryable=True)
                if datetime.now(timezone.utc) >= datetime.fromisoformat(approval["deadline"]):
                    raise ApplicationError("approval expired before execution", type="ApprovalDenied", non_retryable=True)
                ticket_id = "ticket-" + plan["action_id"][:20]
                connection.execute("INSERT INTO durable_tickets VALUES(?,?,?,?)",
                                   (tenant, plan["action_id"], plan["request"]["request_id"], ticket_id))
                receipt = {"ticket_id": ticket_id, "action_id": plan["action_id"], "created": True}
            # Trusted laboratory fault boundary: simulate a committed side effect
            # whose acknowledgement is lost before Temporal records completion.
            if self.after_ticket_commit is not None:
                self.after_ticket_commit()
            return receipt

    def snapshot(self, tenant_id):
        """Read-only local verification counts; never includes document or prompt text."""
        with self.store._transaction() as connection:
            generations = connection.execute("SELECT COUNT(*) FROM durable_generations WHERE tenant=?", (tenant_id,)).fetchone()[0]
            effects = connection.execute("SELECT COUNT(*) FROM durable_tickets WHERE tenant=?", (tenant_id,)).fetchone()[0]
        return {**self.store.snapshot(tenant_id), "generation_count": generations, "ticket_count": effects}
