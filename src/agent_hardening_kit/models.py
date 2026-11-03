"""Bounded transport and accounting contracts for the offline harness."""

from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Nonnegative = Annotated[int, Field(strict=True, ge=0, le=10**15)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Request(StrictModel):
    """Tenant identity comes from a trusted caller; this is not authentication."""

    tenant_id: Identifier
    request_id: Identifier
    question: str = Field(min_length=1, max_length=4096)

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value):
        if not value.strip() or any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t" for char in value):
            raise ValueError("question must contain printable text")
        return value.strip()


class Document(StrictModel):
    id: Identifier
    tenant_id: Identifier
    source: Literal["product", "support"]
    text: str = Field(min_length=1, max_length=8000)


class Usage(StrictModel):
    input_tokens: Nonnegative = 0
    output_tokens: Nonnegative = 0
    cost_microusd: Nonnegative = 0

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_meter(cls, value):
        """Require a complete report instead of interpreting missing counters as zero."""
        usage = value if isinstance(value, cls) else cls.model_validate(value)
        if not set(cls.model_fields).issubset(usage.model_fields_set):
            raise ValueError("backend usage report is incomplete")
        return usage


class Quote(StrictModel):
    input_tokens: Nonnegative
    max_output_tokens: Nonnegative
    cost_microusd: Nonnegative = 0

    @property
    def total_tokens(self):
        return self.input_tokens + self.max_output_tokens


class TenantPolicy(StrictModel):
    token_limit: Nonnegative = 100_000
    cost_limit_microusd: Nonnegative = 0
    requests_per_window: int = Field(default=1000, strict=True, ge=1, le=1_000_000)
    window_seconds: int = Field(default=60, strict=True, ge=1, le=86400)


class Answer(StrictModel):
    status: Literal["answered", "refused", "blocked"]
    answer: str = Field(default="", max_length=16000)
    citations: list[Identifier] = Field(default_factory=list, max_length=12)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    usage: Usage = Field(default_factory=Usage)
    usage_known: bool = True


class ModelResponse(StrictModel):
    answer: str = Field(max_length=16000)
    citations: list[Identifier] = Field(max_length=12)
    usage: Usage

    @field_validator("usage", mode="before")
    @classmethod
    def complete_meter_report(cls, value):
        return Usage.from_meter(value)


class SearchArguments(StrictModel):
    tenant_id: Identifier
    query: str = Field(min_length=1, max_length=4096)
    source: Literal["product", "support"]


class SearchCall(StrictModel):
    name: Literal["knowledge_search"]
    arguments: SearchArguments
