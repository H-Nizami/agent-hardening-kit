"""Small local source specialists. No path, network or side-effect tool exists."""

import re

from pydantic import ValidationError

from .models import Document, SearchCall

STOPWORDS = frozenset("a an the is are was were do does how what when where why can i we our your my to of for and or in on it this that please tell me about should with long many much get find use using provide provides support supports supported work works available process steps help service policy explain".split())
ALIASES = {"authentication": "auth", "authenticate": "auth", "retains": "retention",
           "retain": "retention", "retained": "retention", "pricing": "price", "cost": "price",
           "cancellation": "cancel", "cancelled": "cancel"}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\w)(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
INJECTION = re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|system\s+override|(?:send|reveal|print)\s+(?:all\s+)?(?:secrets|credentials)|disregard\s+(?:all\s+)?(?:instructions|rules)", re.I)


class ToolRejected(ValueError):
    pass


def terms(text):
    words = [ALIASES.get(word, word) for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOPWORDS]
    return {word[:-1] if len(word) > 4 and word.endswith("s") else word for word in words}


def evidence_covers_question(question, documents):
    material = terms(question)
    evidence = set().union(*(terms(document.text) for document in documents))
    return bool(material) and material <= evidence


def redact(text):
    return SSN.sub("[REDACTED_SSN]", PHONE.sub("[REDACTED_PHONE]", EMAIL.sub("[REDACTED_EMAIL]", text)))


def validate_tool_call(payload, tenant_id):
    try:
        call = SearchCall.model_validate(payload)
    except (ValidationError, TypeError) as exc:
        raise ToolRejected("unsupported_tool_call") from exc
    if call.arguments.tenant_id != tenant_id or not call.arguments.query.strip():
        raise ToolRejected("unsupported_tool_call")
    return call


def route(question):
    """Bounded deterministic router, shared by both compared modes."""
    words = terms(question)
    product = bool(words & {"plan", "plans", "pricing", "price", "product", "features", "limit", "limits", "storage", "quota", "quotas", "export", "retention", "integrations"})
    support = bool(words & {"support", "contact", "password", "reset", "refund", "refunds", "incident", "account", "response", "hours", "email", "phone", "billing", "cancel", "cancellation"})
    if product and not support:
        return ["product"]
    if support and not product:
        return ["support"]
    return ["product", "support"]


def search(corpus, *, question, source, tenant_id, hardened):
    if hardened:
        validate_tool_call({"name": "knowledge_search", "arguments": {
            "tenant_id": tenant_id, "query": question, "source": source}}, tenant_id)
    query_terms = terms(question)
    candidates = []
    for value in corpus:
        document = value if isinstance(value, Document) else Document.model_validate(value)
        if document.source != source or (hardened and document.tenant_id != tenant_id):
            continue
        if hardened and INJECTION.search(document.text):
            continue
        text = redact(document.text) if hardened else document.text
        score = len(query_terms & terms(text))
        if score:
            candidates.append((score, document.id, document.model_copy(update={"text": text})))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in candidates[:2]]
