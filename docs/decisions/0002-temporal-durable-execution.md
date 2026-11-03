# 0002. Temporal for durable agent execution

## Status

Accepted

## Context

An agent run is a long chain of LLM calls, tool calls and, for side-effecting tools, waits for human approval that can last minutes or days. In the demo baseline the whole run lives in one process: if the pod is killed, rescheduled or crashes, the run is lost, and retrying from scratch repeats LLM spend and can repeat side effects.

LangGraph's in-process checkpointers persist graph state, but resuming after a crash, retry policies, timeouts, and waiting on an external signal still have to be built and operated around them.

## Decision

Run each agent task as a Temporal workflow. LLM calls, tool calls and approval waits become activities or signals with explicit retry and timeout policies. The LangGraph graph runs inside the worker; Temporal owns durability, retries, and resumption on another worker after a crash.

## Consequences

- A killed worker pod resumes the run from the last completed activity instead of restarting it; this is what the "survive a killed pod" headline number will measure.
- Human approval is a durable wait (signal), not a held connection.
- Activities must be idempotent or deduplicated, because Temporal retries them.
- Workflow code must be deterministic; non-deterministic work (LLM calls, clock, randomness) goes in activities.
- Adds a Temporal server to operate (local dev server for kind; managed or self-hosted in production).
