from decimal import ROUND_CEILING, Decimal, InvalidOperation
from importlib.resources import files
from typing import Literal

from pydantic import Field, model_validator

from review_agent.contracts import AgentError, StrictModel, digest


class DeepSeekTokenRates(StrictModel):
    cache_hit_nusd_per_mtok: int = Field(ge=0, le=10**12)
    cache_miss_nusd_per_mtok: int = Field(ge=0, le=10**12)
    output_nusd_per_mtok: int = Field(ge=0, le=10**12)


class DeepSeekPeakWindow(StrictModel):
    start_utc: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_utc: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class DeepSeekPricingSnapshot(StrictModel):
    version: str = Field(min_length=1, max_length=100)
    source_url: str = Field(pattern=r"^https://api-docs\.deepseek\.com/")
    source_checked_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    effective_at: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    timezone: Literal["UTC"] = "UTC"
    model_version: str = Field(min_length=1, max_length=100)
    peak_weekdays_utc: list[int] = Field(min_length=1, max_length=7)
    peak_windows_utc: list[DeepSeekPeakWindow] = Field(min_length=1, max_length=8)
    off_peak: DeepSeekTokenRates
    peak: DeepSeekTokenRates
    quote_tier: Literal["peak"] = "peak"
    quote_input_class: Literal["cache_miss"] = "cache_miss"
    billing_time_basis: Literal["local_dispatched_at_assumption"] = "local_dispatched_at_assumption"
    billing_time_basis_confirmed: Literal[False] = False
    provider_bill_reconciled: Literal[False] = False

    @model_validator(mode="after")
    def validate_schedule_and_rates(self):
        if len(set(self.peak_weekdays_utc)) != len(self.peak_weekdays_utc) or any(
            day < 0 or day > 6 for day in self.peak_weekdays_utc
        ):
            raise ValueError("invalid peak weekdays")
        if any(window.start_utc >= window.end_utc for window in self.peak_windows_utc):
            raise ValueError("invalid peak window")
        if any(
            peak < off_peak
            for peak, off_peak in (
                (self.peak.cache_hit_nusd_per_mtok, self.off_peak.cache_hit_nusd_per_mtok),
                (self.peak.cache_miss_nusd_per_mtok, self.off_peak.cache_miss_nusd_per_mtok),
                (self.peak.output_nusd_per_mtok, self.off_peak.output_nusd_per_mtok),
            )
        ):
            raise ValueError("peak rate must be the upper bound")
        return self


class TaskConfig(StrictModel):
    schema_version: int = 4
    execution_mode: Literal["fixture", "deepseek"] = "fixture"
    fixture_path: str | None = None
    fixture_digest: str | None = None
    model: str = "fixture"
    api_style: Literal["fixture", "deepseek-chat-completions"] = "fixture"
    provider_endpoint: str | None = None
    credential_env: str | None = None
    max_tokens: int = Field(ge=0, le=2 * 10**9)
    max_cost_nusd: int = Field(ge=0, le=10**15)
    max_output_tokens: int = Field(default=512, ge=1, le=100000)
    max_sends_per_unit: int = Field(default=6, ge=1, le=6)
    max_repairs_per_unit: int = Field(default=0, ge=0, le=1)
    repair_prompt_digest: str | None = None
    tool_registry: dict | None = None
    max_tools_per_unit: int = Field(default=4, ge=0, le=4)
    initial_context_lines: int = Field(default=1, ge=0, le=3)
    max_diff_bytes: int = 1024 * 1024
    max_files: int = 50
    max_unit_bytes: int = 16 * 1024
    max_reply_bytes: int = 64 * 1024
    price_input_nusd: int = Field(default=1000, ge=0, le=10**6)
    price_output_nusd: int = Field(default=2000, ge=0, le=10**6)
    price_cache_hit_nusd_per_mtok: int | None = Field(default=None, ge=0, le=10**12)
    price_cache_miss_nusd_per_mtok: int | None = Field(default=None, ge=0, le=10**12)
    price_output_nusd_per_mtok: int | None = Field(default=None, ge=0, le=10**12)
    pricing_source: str = "fixture"
    deepseek_pricing: DeepSeekPricingSnapshot | None = None
    policy_digest: str
    prompt_digest: str

    @model_validator(mode="after")
    def validate_provider(self):
        if self.schema_version >= 5 and (
            not isinstance(self.tool_registry, dict)
            or set(self.tool_registry) != {"protocol", "entries", "runtime_digest"}
            or self.tool_registry["protocol"] != 1
            or not isinstance(self.tool_registry["entries"], dict)
            or not self.tool_registry["entries"]
            or not isinstance(self.tool_registry["runtime_digest"], str)
        ):
            raise ValueError("tool registry configuration incomplete")
        if self.execution_mode == "fixture":
            if not self.fixture_path or not self.fixture_digest or self.api_style != "fixture":
                raise ValueError("fixture configuration incomplete")
        else:
            common_invalid = (
                self.fixture_path is not None
                or self.fixture_digest is not None
                or self.model not in ("deepseek-v4-flash", "deepseek-flash")
                or self.api_style != "deepseek-chat-completions"
                or self.provider_endpoint != "https://api.deepseek.com/chat/completions"
                or self.credential_env != "DEEPSEEK_API_KEY"
            )
            legacy_invalid = self.schema_version == 2 and (
                self.deepseek_pricing is not None
                or self.price_cache_hit_nusd_per_mtok is None
                or self.price_cache_miss_nusd_per_mtok is None
                or self.price_output_nusd_per_mtok is None
            )
            current_invalid = self.schema_version >= 3 and (
                self.deepseek_pricing is None
                or self.deepseek_pricing.model_version
                != {
                    "deepseek-v4-flash": "DeepSeek-V4-Flash-0731",
                    "deepseek-flash": "DeepSeek-V4.1-Flash",
                }.get(self.model)
                or self.pricing_source != self.deepseek_pricing.source_url
                or self.price_cache_hit_nusd_per_mtok is not None
                or self.price_cache_miss_nusd_per_mtok is not None
                or self.price_output_nusd_per_mtok is not None
            )
            if common_invalid or legacy_invalid or current_invalid or self.schema_version < 2:
                raise ValueError("deepseek configuration incomplete")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump())


class EvaluationTaskConfig(TaskConfig):
    """Batch binding is explicit; old task serialization remains unchanged."""

    schema_version: Literal[6] = 6
    execution_mode: Literal["deepseek"] = "deepseek"
    model: Literal["deepseek-flash"] = "deepseek-flash"
    evaluation_batch_dir: str
    evaluation_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_case_id: str


class DeliveryTaskConfig(TaskConfig):
    """S2 guidance with a new reply parser; no change to legacy serialization."""

    schema_version: Literal[7] = 7
    review_version: Literal["semantic-s2"] = "semantic-s2"
    reply_protocol: Literal["json-unique-keys-v1"] = "json-unique-keys-v1"


class SourceTaskConfig(DeliveryTaskConfig):
    """S2 review with an immutable source binding; previous configs remain unchanged."""

    schema_version: Literal[8] = 8
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def review_prompt_path(schema_version: int) -> str:
    if schema_version in (7, 8):
        return "prompts/review-tools-semantic-s2.md"
    return "prompts/review-tools.md" if schema_version >= 5 else "prompts/review.md"


def package_text(name: str) -> str:
    return files("review_agent").joinpath(name).read_text(encoding="utf-8")


def policy_versions() -> dict[str, str]:
    return {
        "policy_digest": digest(package_text("policies/secret-rules.toml")),
        "prompt_digest": digest(package_text("prompts/review.md")),
    }


def usd_to_nusd(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
            raise ValueError
        return int((amount * 10**9).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError):
        raise AgentError("INVALID_BUDGET") from None
