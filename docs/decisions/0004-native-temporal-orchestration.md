# 0004. Native Temporal orchestration, not the preview LangGraph plugin

## Status

Accepted for T2 (2026-10-29).

## Context

ADR 0002 chose Temporal for durable execution. The T1 pipeline is a LangGraph
`StateGraph`. Temporal also publishes a
[LangGraph integration](https://docs.temporal.io/develop/python/integrations/langgraph)
that maps graph nodes to Activities and deterministic Workflow code. At the time
of this decision that integration is a preview API.

The cheapest option, wrapping the whole `run_request()` call in one retrying
Activity, would retry the model call, the budget reservation and any side effect
together. A retry after a lost acknowledgement could charge twice or open a second
ticket, and a pod kill would restart the whole run.

## Decision

`AgentWorkflow` (`src/agent_hardening_kit/durable.py`) is a native Temporal Python
Workflow. It calls separately recorded Activities: `ahk.prepare` (validate,
tenant-filter, redact), `ahk.reserve` (atomic SQLite reservation under a stable
call ID), `ahk.generate`, `ahk.validate_settle`, `ahk.authorize_decision` and
`ahk.apply_ticket`. These Activities reuse the T1 validation, retrieval and
accounting helpers. The approval wait is a Workflow Update with a validator and a
Temporal timer deadline.

This is **not** the Temporal LangGraph plugin and must not be described as that
integration. The offline `demo` and `eval` commands still run the LangGraph graph.

## Consequences

- Each step has its own timeout and bounded retry policy (three attempts, 30 s
  start-to-close, 90 s schedule-to-close). Policy and permission failures are not
  retried indefinitely.
- A worker killed while a run waits for approval resumes without repeating the
  completed steps. The local and kind evidence count each Activity once per run.
- The ticket Activity is idempotent on its action ID. A lost acknowledgement retry
  returns the existing ticket instead of creating a second one.
- Two orchestration surfaces exist (graph for offline eval, Workflow for durable
  runs). They share helpers, and the eval gate covers the shared code.
- Moving to the official plugin later is possible once it is stable. That change
  needs its own replay-compatibility test against retained histories.
