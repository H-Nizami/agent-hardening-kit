"""Deterministic extractive fixture backend; no LLM, tokenizer or API is invoked."""

import re

from .models import Quote, Usage
from .retrieval import terms


class FixtureBackend:
    name = "deterministic-extractive-fixture-v1"
    cache_key = name

    def __init__(self):
        self.calls = 0
        self.last_documents = []
        self.last_request = None

    def quote(self, request, documents):
        # Whitespace units are an explicit fixture accounting convention.
        return Quote(input_tokens=len(request.question.split()) + sum(len(d.text.split()) for d in documents),
                     max_output_tokens=512, cost_microusd=0)

    def generate(self, request, documents):
        self.calls += 1
        self.last_documents = list(documents)
        self.last_request = request
        query = terms(request.question)
        passages, citations = [], []
        for document in documents:
            sentences = [value.strip() for value in re.split(r"(?<=[.!?])\s+|\n+", document.text) if value.strip()]
            ranked = sorted(enumerate(sentences), key=lambda item: (-len(query & terms(item[1])), item[0]))
            if ranked:
                passage = " ".join(ranked[0][1].split()[:120])
                if passage not in passages:
                    passages.append(passage)
                    citations.append(document.id)
        text = "\n".join(passages)
        usage = Usage(input_tokens=self.quote(request, documents).input_tokens,
                      output_tokens=len(text.split()), cost_microusd=0)
        return {"answer": text, "citations": citations, "usage": usage.model_dump()}
