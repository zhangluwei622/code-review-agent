"""Versioned DeepSeek prices and conservative local cost estimates."""

from datetime import UTC, datetime

from review_agent.config import DeepSeekPeakWindow, DeepSeekPricingSnapshot, DeepSeekTokenRates
from review_agent.contracts import AgentError, Usage

CURRENT_DEEPSEEK_PRICING_VERSION = "deepseek-flash-2026-09-11-observed"


def current_deepseek_pricing() -> DeepSeekPricingSnapshot:
    historical = historical_deepseek_pricing()
    return historical.model_copy(
        update={
            "version": CURRENT_DEEPSEEK_PRICING_VERSION,
            "source_checked_on": "2026-09-11",
            "effective_at": None,
            "model_version": "DeepSeek-V4.1-Flash",
            "off_peak": DeepSeekTokenRates(
                cache_hit_nusd_per_mtok=3_000_000,
                cache_miss_nusd_per_mtok=150_000_000,
                output_nusd_per_mtok=600_000_000,
            ),
            "peak": DeepSeekTokenRates(
                cache_hit_nusd_per_mtok=6_000_000,
                cache_miss_nusd_per_mtok=300_000_000,
                output_nusd_per_mtok=1_200_000_000,
            ),
        }
    )


def historical_deepseek_pricing() -> DeepSeekPricingSnapshot:
    """Preserve the dated historical review basis; never reprice it with today's rates."""
    return DeepSeekPricingSnapshot(
        version="deepseek-v4-flash-2026-08-16",
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_on="2026-09-09",
        effective_at="2026-08-16T16:00:00Z",
        model_version="DeepSeek-V4-Flash-0731",
        peak_weekdays_utc=[0, 1, 2, 3, 4],
        peak_windows_utc=[
            DeepSeekPeakWindow(start_utc="01:00", end_utc="04:00"),
            DeepSeekPeakWindow(start_utc="06:00", end_utc="10:00"),
        ],
        off_peak=DeepSeekTokenRates(
            cache_hit_nusd_per_mtok=7_000_000,
            cache_miss_nusd_per_mtok=220_000_000,
            output_nusd_per_mtok=660_000_000,
        ),
        peak=DeepSeekTokenRates(
            cache_hit_nusd_per_mtok=14_000_000,
            cache_miss_nusd_per_mtok=440_000_000,
            output_nusd_per_mtok=1_320_000_000,
        ),
    )


def require_current_pricing(config) -> None:
    if config.execution_mode != "deepseek":
        return
    if config.schema_version < 3 or config.deepseek_pricing != current_deepseek_pricing():
        raise AgentError("STALE_PRICING_VERSION")


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def usage_cost(usage: Usage, rates: DeepSeekTokenRates) -> int:
    return _ceil_div(
        usage.cache_hit_input_tokens * rates.cache_hit_nusd_per_mtok
        + usage.cache_miss_input_tokens * rates.cache_miss_nusd_per_mtok
        + usage.output_tokens * rates.output_nusd_per_mtok,
        1_000_000,
    )


def quote_cost(input_tokens: int, output_tokens: int, pricing: DeepSeekPricingSnapshot) -> int:
    return _ceil_div(
        input_tokens * pricing.peak.cache_miss_nusd_per_mtok
        + output_tokens * pricing.peak.output_nusd_per_mtok,
        1_000_000,
    )


def tier_at(value: datetime, pricing: DeepSeekPricingSnapshot) -> str:
    if value.tzinfo is None:
        raise ValueError("pricing timestamp must be timezone-aware")
    utc = value.astimezone(UTC)
    if utc.weekday() not in pricing.peak_weekdays_utc:
        return "off_peak"
    minute = utc.hour * 60 + utc.minute
    for window in pricing.peak_windows_utc:
        start_hour, start_minute = map(int, window.start_utc.split(":"))
        end_hour, end_minute = map(int, window.end_utc.split(":"))
        if start_hour * 60 + start_minute <= minute < end_hour * 60 + end_minute:
            return "peak"
    return "off_peak"


def cost_review(
    usage: Usage,
    pricing: DeepSeekPricingSnapshot,
    *,
    review_kind: str,
    recorded_at: str,
    assumed_at: str,
    completed_at: str | None,
    assumed_at_kind: str,
    original_cost_nusd: int | None,
    ledger_cost_nusd: int | None,
    uncertainties: list[str],
) -> dict:
    assumed_time = datetime.fromisoformat(assumed_at.replace("Z", "+00:00"))
    assumed_tier = tier_at(assumed_time, pricing)
    completion_tier = (
        None
        if completed_at is None
        else tier_at(datetime.fromisoformat(completed_at.replace("Z", "+00:00")), pricing)
    )
    off_peak = usage_cost(usage, pricing.off_peak)
    peak = usage_cost(usage, pricing.peak)
    assumed = off_peak if assumed_tier == "off_peak" else peak
    return {
        "review_kind": review_kind,
        "recorded_at": recorded_at,
        "pricing": pricing.model_dump(),
        "usage": usage.model_dump(),
        "original_cost_nusd": original_cost_nusd,
        "revised_estimate_min_nusd": min(off_peak, peak),
        "revised_estimate_max_nusd": max(off_peak, peak),
        "difference_min_nusd": (
            None if original_cost_nusd is None else min(off_peak, peak) - original_cost_nusd
        ),
        "difference_max_nusd": (
            None if original_cost_nusd is None else max(off_peak, peak) - original_cost_nusd
        ),
        "assumed_at": assumed_at,
        "assumed_at_kind": assumed_at_kind,
        "assumed_tier": assumed_tier,
        "completed_at": completed_at,
        "completion_tier": completion_tier,
        "crossed_tier_boundary": (
            None if completion_tier is None else completion_tier != assumed_tier
        ),
        "assumed_cost_nusd": assumed,
        "ledger_cost_nusd": ledger_cost_nusd,
        "billing_time_basis_confirmed": False,
        "provider_bill_reconciled": False,
        "uncertainties": uncertainties,
    }
