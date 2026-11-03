# Design notes

These notes summarize the scope, contracts and acceptance criteria for each stage.
Decisions with alternatives are recorded in [`decisions/`](decisions/); current
status and measured results are in the [README](../README.md).

## Stages

| Stage | Deliverable | Proof |
|---|---|---|
| T0 | Repository, CI and architecture diagram | CI green, diagram renders |
| T1 | Local graph, controls, tenant admission, fixture eval, CLI | Offline tests and golden report; no credentials or Docker |
| T2 | Temporal durability and approval | Real Temporal service; kill/restart worker between activities, replay, duplicate-side-effect checks |
| T3 | OTel, local traces/metrics, SLOs | Collector receives spans, sanitized attributes, dashboards and alert-rule checks |
| T4 | kind/Helm deployment and chaos | Real cluster, approval flow, pod-kill recovery report, teardown |
| T5 | Delivery and optional live-model comparison | Evidence-backed README and cover, runnable demo; paid calls only after explicit approval |

A stage is complete only when its proof has run. Unmeasured headline numbers are marked
`Not yet measured`.

## Contracts

All models are strict Pydantic models (`extra="forbid"`).

- `Request`: `tenant_id`, `request_id`, `question`. IDs are bounded strings over a
  small safe alphabet; the question is nonempty and bounded. Tenant identity is
  supplied by the trusted local caller. A future network API must bind it to an
  authenticated identity instead of trusting JSON.
- `Document`: stable `id`, `tenant_id`, `source` (product/support), bounded text.
  No user-supplied path or URL is opened.
- `Answer`: `status` (`answered`, `refused`, `blocked`), text, citation IDs, a
  stable `reason_code` and usage. Raw exceptions, full prompts and private document
  text are never returned as diagnostics.
- `Usage`: nonnegative integer fixture token units and integer `cost_microusd`.
  Offline cost is zero.
- Tool requests use an exact allow-list with strict argument schemas. Unknown tools,
  shell/network commands, path arguments, cross-tenant searches and extra arguments
  are denied before invocation.

The entry point is `run_request(request, *, mode, store, backend, corpus)`. `mode`
is explicitly `baseline` or `hardened`; controls never depend on question text.
The backend and clock are injectable so tests can use spies and exact boundaries.

## Graph and controls

The pipeline is a compiled LangGraph `StateGraph` with separate routing, source
retrieval, synthesis and validation nodes. Local specialists are retrieval
adapters, not independent live models. The deterministic backend derives answers
from retrieved content and never reads expected answers or case labels;
adversarial fake backends in tests cover malformed output, unsupported citations,
hallucinated claims and invalid tool calls.

Hardened mode validates inputs and tool arguments, limits retrieval to the tenant,
redacts the supported synthetic email/phone forms before the backend call, and
checks output structure and citations. An answer needs extractive evidence from
retrieved tenant-visible documents; otherwise it is refused. This is a text
provenance check, not a semantic grounding judge or a complete PII detector.
Pattern-based injection checks are one fixture defense, not general injection
resistance.

## Tenant admission

SQLite is the authoritative local store for budgets and rate windows. `reserve`
and `settle` run in `BEGIN IMMEDIATE` transactions, so two connections cannot
overspend one tenant. Token and micro-USD caps are tracked independently. The
prompt plus maximum output allowance is reserved before the backend is called;
a denied request makes zero backend calls, and one tenant's exhausted budget does
not block another.

A reservation is identified by `(tenant_id, request_id, call_id)` and bound to an
input digest; reusing the identity for different input is an error. Failed calls
and missing usage keep the full reservation. A reported overrun is recorded and
blocks further admission. This gives same-host durability, not a distributed
quota service.

## Offline evaluation

Fixed cases across two synthetic tenants cover answerable and unknown questions,
confidential documents, PII forms, injected instructions, invalid output and
citations, unsupported tool arguments and budget/rate exhaustion. Baseline and
hardened modes run with the same corpus, backend and initial allowance; each case
is isolated. The report gives per-case results, retrieval recall@k, citation
support, tenant leakage, refusal correctness and denied-backend-call counts. These
are deterministic fixture scores, not measured LLM quality.

## Durable execution and approval

A Temporal workflow records `retrieve`, `reserve`, `generate` and `settle` as
separate Activities ([ADR 0004](decisions/0004-native-temporal-orchestration.md)).
File, database, model and metric operations stay in Activities; workflow code uses
deterministic logic and Temporal timers. Retry policies are bounded, permanent
policy failures do not retry, and a possibly billed model call is never repeated
automatically after an ambiguous crash.

The only side effect is a tenant-scoped synthetic ticket in SQLite, applied by the
separate `apply_ticket` Activity. Its idempotency key binds tenant, request,
workflow and action digest. Approval is a durable pending/approved/rejected/expired
state with a server-chosen deadline and a single final decision. The local approval
controller reads the caller's UID from the Unix socket (`SO_PEERCRED` on Linux,
`getpeereid` on macOS) and authorizes it against tenant policy; actor fields in
JSON are ignored. Wrong principal, tenant or action, duplicates and late decisions
cannot create a ticket.

Checks run a real local Temporal dev server, kill the worker at a recorded
boundary, finish the run on a new worker, assert that completed Activities did not
run again, and replay the fetched history. Local process recovery is not
Kubernetes pod survival.

## Telemetry and SLOs

Tracing and metrics are opt-in; importing a module never changes global SDK or
exporter state. Exported attributes are limited to fixed names, a bounded
synthetic tenant identifier, outcome enums, fixture units, durations and fixture
cost. Question, answer and document text, full tool arguments, raw exception
messages and credentials are never exported, and request/workflow IDs are not
metric labels.

The Collector config defines an OTLP receiver, a memory limiter, an attribute
allow-list and trace/metric pipelines. Prometheus rules cover availability and
latency SLOs with multi-window burn-rate alerts and a minimum-traffic guard,
tested with promtool. The integration check sends real workload telemetry through the
Collector and asserts that injected canary text does not appear. A remote trace
store (Langfuse, Tempo) is a target, not built.

## Deployment and chaos

`make chaos` creates one uniquely named kind cluster, never uses the current
kubectl context, and deletes only what it created. The image and chart run
non-root with dropped capabilities, resource limits, no auto-mounted service
account token and explicit SQLite storage on one worker replica
([ADR 0005](decisions/0005-single-worker-sqlite-pvc.md)).

The run installs the chart, starts several workflows, deletes the worker pod
while they wait for approval, approves them after the replacement is Ready, and
verifies one effect per approved action, replay after restart, telemetry from the
restarted worker and the absence of canaries. It reports attempted and completed
runs, then deletes the cluster and confirms it is gone. This is observed local
recovery, not a universal SLO.
