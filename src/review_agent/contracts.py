"""Small, JSON-only contracts. No provider or persistence dependencies."""

import hashlib
import json
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return prefix + "_" + digest(parts)[:24]


class AgentError(Exception):
    """Only host-authored error codes may cross graph/CLI boundaries."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BudgetQuote(StrictModel):
    input_tokens: int = Field(ge=0, le=10**9)
    output_tokens: int = Field(ge=0, le=10**9)
    tokens: int = Field(ge=0, le=2 * 10**9)
    cost_nusd: int = Field(ge=0, le=10**15)


class Usage(StrictModel):
    input_tokens: int = Field(ge=0, le=10**9)
    output_tokens: int = Field(ge=0, le=10**9)
    total_tokens: int = Field(ge=0, le=2 * 10**9)
    source: Literal["fixture", "deepseek"] = "fixture"
    cache_hit_input_tokens: int | None = Field(default=None, ge=0, le=10**9)
    cache_miss_input_tokens: int | None = Field(default=None, ge=0, le=10**9)

    @model_validator(mode="after")
    def validate_totals(self):
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("usage total mismatch")
        if self.source == "deepseek":
            if self.cache_hit_input_tokens is None or self.cache_miss_input_tokens is None:
                raise ValueError("deepseek cache usage missing")
            if self.cache_hit_input_tokens + self.cache_miss_input_tokens != self.input_tokens:
                raise ValueError("deepseek input usage mismatch")
        return self


class CandidateFinding(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    hunk_id: str
    side: Literal["old", "new"]
    line: int = Field(ge=1)
    trigger: str = Field(min_length=1, max_length=2000)
    actual_behavior: str = Field(min_length=1, max_length=2000)
    expected_behavior: str = Field(min_length=1, max_length=2000)
    introduced_by: str = Field(min_length=1, max_length=2000)
    expectation_evidence: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[str] = Field(min_length=1, max_length=20)
    impact: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    severity: Literal["high", "medium", "low"]
    confidence: Literal["high", "medium", "reference"]


class ReviewDecision(StrictModel):
    action: Literal["submit_review", "abstain"]
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    reason: str = Field(default="", max_length=2000)


class ToolDecision(StrictModel):
    action: Literal["request_tool"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any]


class SubmitDecision(StrictModel):
    action: Literal["submit_review"]
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    reason: str = Field(default="", max_length=2000)


class AbstainDecision(StrictModel):
    action: Literal["abstain"]
    reason: str = Field(min_length=1, max_length=2000)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=0)


class StoredResult(StrictModel):
    result_status: Literal["VALID", "FORMAT_INVALID", "UNUSABLE", "TRUNCATED", "SAFETY_REJECTED"]
    completion_state: Literal["COMPLETE", "OUTPUT_LIMIT", "LOCAL_SIZE_LIMIT", "UNCONFIRMED"]
    truncation_reason: str | None = None
    safe_body: str | None = None
    decision: dict[str, Any] | None = None
    error_code: str | None = None
    usage: Usage | None = None
    provider_request_id: str | None = Field(
        default=None, max_length=500, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    provider_model: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    provider_finish_reason: str | None = Field(default=None, max_length=100, pattern=r"^[a-z_]+$")
    system_fingerprint: str | None = Field(
        default=None, max_length=500, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class GraphState(TypedDict, total=False):
    task_id: str
    snapshot_id: str
    config_digest: str
    unit_index: int
    unit_id: str
    last_result_ref: str | None
    route: str
    pause_reason: str | None
    trace_id: str
    operation_id: str
    blocked_attempt_id: str | None
