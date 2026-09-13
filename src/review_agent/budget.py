from pydantic import ValidationError

from review_agent.config import TaskConfig
from review_agent.contracts import BudgetQuote, Usage
from review_agent.pricing import quote_cost, usage_cost


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def quote(input_tokens: int, output_tokens: int, config: TaskConfig) -> BudgetQuote:
    return BudgetQuote(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens=input_tokens + output_tokens,
        cost_nusd=input_tokens * config.price_input_nusd + output_tokens * config.price_output_nusd,
    )


def normalize_usage(raw: dict | None, expected_source: str) -> Usage | None:
    try:
        value = Usage.model_validate(raw)
        return value if value.source == expected_source else None
    except (ValidationError, TypeError):
        return None


def cost(usage: Usage, config: TaskConfig) -> int:
    if usage.source == "deepseek":
        if config.schema_version >= 3:
            # The ledger settles the local upper-bound estimate. Provider billing is not reconciled.
            return usage_cost(usage, config.deepseek_pricing.peak)
        return _ceil_div(
            usage.cache_hit_input_tokens * config.price_cache_hit_nusd_per_mtok
            + usage.cache_miss_input_tokens * config.price_cache_miss_nusd_per_mtok
            + usage.output_tokens * config.price_output_nusd_per_mtok,
            1_000_000,
        )
    return (
        usage.input_tokens * config.price_input_nusd
        + usage.output_tokens * config.price_output_nusd
    )


def quote_deepseek(input_tokens: int, output_tokens: int, config: TaskConfig) -> BudgetQuote:
    amount = (
        quote_cost(input_tokens, output_tokens, config.deepseek_pricing)
        if config.schema_version >= 3
        else _ceil_div(
            input_tokens * config.price_cache_miss_nusd_per_mtok
            + output_tokens * config.price_output_nusd_per_mtok,
            1_000_000,
        )
    )
    return BudgetQuote(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens=input_tokens + output_tokens,
        cost_nusd=amount,
    )
