"""Local LangGraph adapter with explicit optional controls and trusted accounting."""

import hashlib
import json
import sqlite3
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from .budget import AdmissionDenied
from .models import Answer, Document, ModelResponse, Quote, Request, Usage
from .retrieval import INJECTION, evidence_covers_question, redact, route, search


class AgentState(TypedDict, total=False):
    request: Request
    sources: list[str]
    documents: list[Document]
    raw: object
    answer: Answer
    reservation: object
    quote: Quote


def _blocked(reason, *, usage=None):
    return Answer(status="blocked", reason_code=reason, usage=usage or Usage(),
                  usage_known=usage is not None)


def _fingerprint(request, corpus, backend, mode):
    identity = getattr(backend, "cache_key", getattr(backend, "name", type(backend).__qualname__))
    if not isinstance(identity, str):
        raise ValueError("backend cache identity must be a string")
    document_rows = sorted((d.model_dump() for d in corpus),
                           key=lambda d: (d["tenant_id"], d["id"], d["source"], d["text"]))
    payload = {"request": request.model_dump(), "mode": mode,
               "backend": type(backend).__module__ + "." + type(backend).__qualname__ + ":" + identity,
               "corpus": document_rows}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _supported(text, citations, documents):
    visible = {d.id: d for d in documents}
    if not citations or len(citations) != len(set(citations)) or any(cid not in visible for cid in citations):
        return False
    evidence = [" ".join(visible[cid].text.split()) for cid in citations]
    # Extractive support only. This is not a semantic judge for paraphrased LLM prose.
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return bool(lines) and all(any(line in source for source in evidence) for line in lines)


def run_request(request, *, mode: Literal["baseline", "hardened"] = "hardened", store, backend, corpus):
    """Run an offline graph. Caller-supplied tenant identity is trusted, not authenticated.

    Baseline disables added controls and is intentionally unmetered. Both modes
    retain strict transport contracts and use the same backend/retriever algorithm.
    Custom adapters are trusted code; their quote and usage must be independently
    metered, never accepted from a future model's generated accounting claims.
    """
    if mode not in {"baseline", "hardened"}:
        raise ValueError("unsupported execution mode")
    try:
        request = request if isinstance(request, Request) else Request.model_validate(request)
    except (ValidationError, TypeError):
        return _blocked("invalid_input")
    documents = [value if isinstance(value, Document) else Document.model_validate(value) for value in corpus]
    keys = [(doc.tenant_id, doc.id) for doc in documents]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate tenant document identity")
    hardened = mode == "hardened"
    if hardened:
        try:
            store.snapshot(request.tenant_id)
        except AdmissionDenied as exc:
            return _blocked(exc.reason)
        except sqlite3.Error:
            return _blocked("admission_unavailable")
    if hardened and INJECTION.search(request.question):
        return _blocked("input_injection")
    effective = request.model_copy(update={"question": redact(request.question)}) if hardened else request

    def classify(state):
        return {"sources": route(state["request"].question)}

    def retrieve(state):
        found = []
        for source in state["sources"]:
            found.extend(search(documents, question=state["request"].question, source=source,
                                tenant_id=request.tenant_id, hardened=hardened))
        return {"documents": found}

    def synthesize(state):
        if not state["documents"]:
            return {"answer": Answer(status="refused", reason_code="insufficient_evidence")}
        try:
            value = backend.quote(state["request"], state["documents"])
            quote = value if isinstance(value, Quote) else Quote.model_validate(value)
        except Exception:
            return {"answer": _blocked("backend_quote_failed")}
        reservation = None
        if hardened:
            try:
                reservation = store.reserve(request.tenant_id, request.request_id, "synthesis",
                                            _fingerprint(request, documents, backend, mode), quote)
            except AdmissionDenied as exc:
                return {"answer": _blocked(exc.reason)}
            except sqlite3.Error:
                return {"answer": _blocked("admission_unavailable")}
            if not reservation.created:
                if reservation.response is not None:
                    return {"answer": Answer.model_validate(reservation.response)}
                return {"answer": _blocked("request_in_progress" if reservation.status == "reserved" else "previous_outcome_unknown")}
        try:
            raw = backend.generate(state["request"], state["documents"])
            return {"raw": raw, "quote": quote, "reservation": reservation}
        except Exception:
            answer = _blocked("backend_failure")
            if reservation:
                try:
                    store.settle(reservation, response=answer, failed=True)
                except sqlite3.Error:
                    answer = _blocked("settlement_unavailable")
            return {"answer": answer}

    def validate(state):
        if state.get("answer") is not None:
            return {}
        raw, quote, reservation = state["raw"], state["quote"], state.get("reservation")
        usage = None
        if isinstance(raw, dict):
            try:
                usage = Usage.from_meter(raw.get("usage"))
            except (ValueError, TypeError):
                pass
        if hardened and isinstance(raw, dict) and "tool_calls" in raw:
            answer = _blocked("unsupported_tool_call", usage=usage)
        else:
            try:
                response = raw if isinstance(raw, ModelResponse) else ModelResponse.model_validate(raw)
                usage = response.usage
                text = redact(response.answer) if hardened else response.answer
                if hardened and (usage.total_tokens > quote.total_tokens or usage.cost_microusd > quote.cost_microusd):
                    answer = _blocked("usage_overrun", usage=usage)
                elif not text.strip():
                    answer = Answer(status="refused", reason_code="insufficient_evidence", usage=usage)
                elif hardened and (INJECTION.search(text) or not _supported(text, response.citations, state["documents"])):
                    answer = _blocked("unsupported_evidence", usage=usage)
                elif hardened and not evidence_covers_question(state["request"].question, state["documents"]):
                    answer = Answer(status="refused", reason_code="insufficient_evidence", usage=usage)
                else:
                    answer = Answer(status="answered", answer=text, citations=response.citations,
                                    reason_code="ok", usage=usage)
            except (ValidationError, TypeError):
                answer = _blocked("invalid_output", usage=usage)
        if reservation:
            try:
                store.settle(reservation, usage, response=answer, failed=usage is None)
            except sqlite3.Error:
                answer = _blocked("settlement_unavailable", usage=usage)
        return {"answer": answer}

    graph = StateGraph(AgentState)
    graph.add_node("route", classify)
    graph.add_node("retrieve", retrieve)
    graph.add_node("synthesize", synthesize)
    graph.add_node("validate", validate)
    graph.add_edge(START, "route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "synthesize")
    graph.add_edge("synthesize", "validate")
    graph.add_edge("validate", END)
    # An inherited personal LangSmith configuration must not turn a fixture run
    # into network tracing. Explicitly disable it for the entire graph invocation.
    with tracing_context(enabled=False):
        result = graph.compile().invoke({"request": effective})
    return result["answer"]
