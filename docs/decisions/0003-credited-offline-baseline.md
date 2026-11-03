# 0003. A credited local routing baseline and a free first increment

## Status

Accepted for T1 (2026-10-23); the baseline can be revisited before a later fork
or live-model comparison.

## Context

The kit needs an understandable agent to harden and a fair comparison. A live
deep-research benchmark adds search services and provider spend before admission
controls are in place. The older Open Deep Research repository is archived.

## Decision

Adapt the architecture of LangChain's maintained MIT tutorial,
[Build a multi-source knowledge base with routing](https://docs.langchain.com/oss/python/langchain/multi-agent/router-knowledge-base).
Pin its [source](https://github.com/langchain-ai/docs/blob/202c34b6d53c5f1a84913eacfc76409326846001/src/oss/langchain/multi-agent/router-knowledge-base.mdx)
at `202c34b6d53c5f1a84913eacfc76409326846001`. The reference routes queries to
specialized sources and synthesizes their results. Its MIT notice is preserved in
[`third_party/langchain-docs-LICENSE`](../../third_party/langchain-docs-LICENSE).

Our local adaptation uses product and support document specialists, synthetic
tenants, and a deterministic extractive backend. It executes a real LangGraph
StateGraph, but does not run the upstream application or live language models.
Both baseline and hardened modes share the graph, backend, documents, and tasks;
the comparison changes the added controls.

## Consequences

T1 measures local retrieval and control behavior. It provides no evidence of a
production LLM quality gain or an improvement to the upstream tutorial. Fault
injection tests are labelled separately from ordinary retrieval questions.
Synthetic fixture token units support budget tests; actual model spend is zero.
Temporal, telemetry, approvals, Kubernetes, and recovery measurements remain later
stages. The full-project headline stays unmeasured until those stages are built.
