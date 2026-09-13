import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_agent.budget import quote, quote_deepseek
from review_agent.config import TaskConfig
from review_agent.contracts import AgentError, BudgetQuote, StrictModel, digest, json_text
from review_agent.ingest import read_bounded


class RawProviderReply(StrictModel):
    body: str = ""
    usage: dict | None = None
    finish: Literal["COMPLETE", "OUTPUT_LIMIT", "UNCONFIRMED"] = "COMPLETE"
    response_complete: bool = True
    error: Literal["timeout", "not_sent", "transport"] | None = None
    provider_request_id: str | None = None
    provider_model: str | None = None
    provider_finish_reason: str | None = None
    system_fingerprint: str | None = None


class FixtureSpec(StrictModel):
    quote_input_tokens: int = Field(default=400, ge=0, le=10**9)
    quote_output_tokens: int = Field(default=200, ge=1, le=100000)
    responses: dict[str, RawProviderReply] = Field(default_factory=dict)
    default: RawProviderReply | None = None


class ProviderPort(Protocol):
    def output_limit(self, config: TaskConfig) -> int: ...
    def prepare(self, request: dict, config: TaskConfig) -> dict: ...
    def quote(self, request: dict, config: TaskConfig) -> BudgetQuote: ...
    def send(self, request: dict, *, context: dict | None = None) -> RawProviderReply: ...


class FixtureProvider:
    """Data-only fixture. Does not contact a model or import a target module."""

    def __init__(self, path: Path, observer: Callable[[dict], None] | None = None):
        raw = read_bounded(path, 512 * 1024)
        try:
            self.spec = FixtureSpec.model_validate_json(raw)
        except ValidationError:
            raise AgentError("INVALID_FIXTURE") from None
        self.fingerprint = digest(raw)
        self.observer = observer
        self.calls = 0

    def output_limit(self, config: TaskConfig) -> int:
        return min(config.max_output_tokens, self.spec.quote_output_tokens)

    def prepare(self, request: dict, config: TaskConfig) -> dict:
        return request

    def quote(self, request: dict, config: TaskConfig) -> BudgetQuote:
        return quote(self.spec.quote_input_tokens, request["max_output_tokens"], config)

    def send(self, request: dict, *, context: dict | None = None) -> RawProviderReply:
        self.calls += 1
        if self.observer:
            self.observer(request)
        kind = context["kind"] if context else "REVIEW"
        attempt_no = context["attempt_no"] if context else 1
        turn_no = context.get("turn_no", 0) if context else 0
        key = f"{request['unit_index']}:{turn_no}:{kind}:{attempt_no}"
        reply = self.spec.responses.get(key, self.spec.default)
        if reply is None or reply.error:
            raise AgentError("PROVIDER_UNKNOWN")
        return reply


class _DeepSeekMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    content: str | None = None


class _DeepSeekChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    finish_reason: Literal[
        "stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"
    ]
    message: _DeepSeekMessage


class _DeepSeekUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0)


class _DeepSeekResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]+$")
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]+$")
    system_fingerprint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]+$")
    choices: list[_DeepSeekChoice]
    usage: _DeepSeekUsage | None = None


class DeepSeekProvider:
    """DeepSeek V4 Flash transport with an auth-free persisted request body."""

    MAX_ENVELOPE_BYTES = 256 * 1024

    def __init__(
        self,
        config: TaskConfig,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        observer: Callable[[dict], None] | None = None,
    ):
        if config.execution_mode != "deepseek" or not api_key:
            raise AgentError("PROVIDER_CONFIGURATION_ERROR")
        self.config = config
        self.api_key = api_key
        self.transport = transport
        self.observer = observer
        self.calls = 0

    def output_limit(self, config: TaskConfig) -> int:
        return config.max_output_tokens

    def prepare(self, request: dict, config: TaskConfig) -> dict:
        model_input = {
            "unit_id": request["unit_id"],
            "unit_index": request["unit_index"],
            "snapshot_id": request["snapshot_id"],
            "hunks": request["hunks"],
            "available_tools": request["tools"],
            "output_schema": request["output_schema"],
            "finding_schema": request["finding_schema"],
        }
        if "repair" in request:
            model_input["repair"] = request["repair"]
        if "loop" in request:
            model_input["loop"] = request["loop"]
        return {
            "model": config.model,
            "messages": [
                {"role": "system", "content": request["system"]},
                {"role": "user", "content": json_text(model_input)},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": request["max_output_tokens"],
            "stream": False,
            "temperature": 0.0,
        }

    def quote(self, request: dict, config: TaskConfig) -> BudgetQuote:
        input_upper_bound = len(json_text(request["messages"]).encode("utf-8")) + 256
        return quote_deepseek(input_upper_bound, request["max_tokens"], config)

    def send(self, request: dict, *, context: dict | None = None) -> RawProviderReply:
        self.calls += 1
        body = json_text(request).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.observer:
            self.observer(request)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(30.0),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST", self.config.provider_endpoint, headers=headers, content=body
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.MAX_ENVELOPE_BYTES:
                            raise AgentError("PROVIDER_UNKNOWN")
        except Exception:
            raise AgentError("PROVIDER_UNKNOWN") from None
        return self._decode(bytes(raw))

    def _decode(self, raw: bytes) -> RawProviderReply:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # The HTTP response is complete, but these bytes cannot be safe text.
            return RawProviderReply(
                finish="UNCONFIRMED", provider_finish_reason="invalid_provider_encoding"
            )
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return RawProviderReply(
                body=text,
                finish="UNCONFIRMED",
                provider_finish_reason="invalid_provider_envelope",
            )
        # Usage remains a billing fact even if choices or metadata are malformed.
        try:
            usage = self._usage(_DeepSeekUsage.model_validate(data.get("usage")))
        except (ValidationError, AttributeError):
            usage = None
        try:
            response = _DeepSeekResponse.model_validate(data)
        except ValidationError:
            return RawProviderReply(
                # Decode JSON escapes before the common safety gate. Preserve the
                # envelope as text; it must never be accepted as a review decision.
                body=json_text(data),
                usage=usage,
                finish="UNCONFIRMED",
                provider_finish_reason="invalid_provider_envelope",
            )
        if len(response.choices) != 1:
            return RawProviderReply(
                body=json_text(data),
                usage=usage,
                finish="UNCONFIRMED",
                provider_request_id=response.id,
                provider_model=response.model,
                provider_finish_reason="invalid_choice_count",
                system_fingerprint=response.system_fingerprint,
            )
        choice = response.choices[0]
        finish = {"stop": "COMPLETE", "length": "OUTPUT_LIMIT"}.get(
            choice.finish_reason, "UNCONFIRMED"
        )
        return RawProviderReply(
            body=choice.message.content or "",
            usage=self._usage(response.usage),
            finish=finish,
            provider_request_id=response.id,
            provider_model=response.model,
            provider_finish_reason=choice.finish_reason,
            system_fingerprint=response.system_fingerprint,
        )

    @staticmethod
    def _usage(value: _DeepSeekUsage | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "input_tokens": value.prompt_tokens,
            "output_tokens": value.completion_tokens,
            "total_tokens": value.total_tokens,
            "source": "deepseek",
            "cache_hit_input_tokens": value.prompt_cache_hit_tokens,
            "cache_miss_input_tokens": value.prompt_cache_miss_tokens,
        }
