import sqlite3
from datetime import datetime

import pytest
from conftest import ROOT

from review_agent import app
from review_agent.budget import cost
from review_agent.config import TaskConfig, policy_versions
from review_agent.contracts import AgentError, StoredResult, Usage, stable_id
from review_agent.ingest import prepare_diff
from review_agent.pricing import (
    cost_review,
    current_deepseek_pricing,
    historical_deepseek_pricing,
    tier_at,
    usage_cost,
)
from review_agent.providers import DeepSeekProvider
from review_agent.review import make_request
from review_agent.safety import Safety
from review_agent.storage import Storage

API_KEY = "ds-test-pricing-credential-123456789"


def current_config(**updates):
    pricing = current_deepseek_pricing()
    values = {
        "execution_mode": "deepseek",
        "model": "deepseek-flash",
        "api_style": "deepseek-chat-completions",
        "provider_endpoint": "https://api.deepseek.com/chat/completions",
        "credential_env": "DEEPSEEK_API_KEY",
        "pricing_source": pricing.source_url,
        "deepseek_pricing": pricing,
        "max_tokens": 20_000,
        "max_cost_nusd": 10_000_000,
        "max_output_tokens": 1024,
        **policy_versions(),
    }
    values.update(updates)
    return TaskConfig(**values)


def legacy_config(**updates):
    values = {
        "schema_version": 2,
        "execution_mode": "deepseek",
        "model": "deepseek-v4-flash",
        "api_style": "deepseek-chat-completions",
        "provider_endpoint": "https://api.deepseek.com/chat/completions",
        "credential_env": "DEEPSEEK_API_KEY",
        "price_cache_hit_nusd_per_mtok": 2_800_000,
        "price_cache_miss_nusd_per_mtok": 140_000_000,
        "price_output_nusd_per_mtok": 280_000_000,
        "pricing_source": "historical-frozen-test-rate",
        "max_tokens": 20_000,
        "max_cost_nusd": 10_000_000,
        "max_output_tokens": 1024,
        **policy_versions(),
    }
    values.update(updates)
    return TaskConfig(**values)


def create_legacy_task(tmp_path, config=None):
    config = config or legacy_config()
    state = tmp_path / "state"
    task_id = "task_" + "1" * 32
    directory = state / task_id
    directory.mkdir(parents=True)
    path = directory / "task.sqlite"
    path.touch(mode=0o600)
    snapshot, units = prepare_diff(ROOT / "examples/diffs/empty-list.diff", config, Safety())
    store = Storage(path)
    try:
        store.create_task(task_id, config, snapshot, units)
        store.conn.execute(
            "UPDATE tasks SET created_at='2026-09-09T12:00:00+00:00' WHERE task_id=?",
            (task_id,),
        )
    finally:
        store.close()
    return state, task_id


def test_historical_price_snapshot_records_source_version_schedule_and_rates():
    pricing = historical_deepseek_pricing()
    assert pricing.version == "deepseek-v4-flash-2026-08-16"
    assert pricing.model_version == "DeepSeek-V4-Flash-0731"
    assert pricing.source_url == "https://api-docs.deepseek.com/quick_start/pricing/"
    assert pricing.source_checked_on == "2026-09-09"
    assert pricing.effective_at == "2026-08-16T16:00:00Z"
    assert pricing.peak_weekdays_utc == [0, 1, 2, 3, 4]
    assert [(window.start_utc, window.end_utc) for window in pricing.peak_windows_utc] == [
        ("01:00", "04:00"),
        ("06:00", "10:00"),
    ]
    assert pricing.off_peak.model_dump() == {
        "cache_hit_nusd_per_mtok": 7_000_000,
        "cache_miss_nusd_per_mtok": 220_000_000,
        "output_nusd_per_mtok": 660_000_000,
    }
    assert pricing.peak.model_dump() == {
        "cache_hit_nusd_per_mtok": 14_000_000,
        "cache_miss_nusd_per_mtok": 440_000_000,
        "output_nusd_per_mtok": 1_320_000_000,
    }


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-09-07T00:59:59+00:00", "off_peak"),
        ("2026-09-07T01:00:00+00:00", "peak"),
        ("2026-09-07T03:59:59+00:00", "peak"),
        ("2026-09-07T04:00:00+00:00", "off_peak"),
        ("2026-09-07T06:00:00+00:00", "peak"),
        ("2026-09-07T09:59:59+00:00", "peak"),
        ("2026-09-07T10:00:00+00:00", "off_peak"),
        ("2026-09-06T02:00:00+00:00", "off_peak"),
    ],
)
def test_peak_off_peak_boundaries_use_utc_weekdays(instant, expected):
    assert tier_at(datetime.fromisoformat(instant), current_deepseek_pricing()) == expected


def test_cache_categories_produce_range_and_peak_ledger_upper_bound():
    usage = Usage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        source="deepseek",
        cache_hit_input_tokens=30,
        cache_miss_input_tokens=70,
    )
    pricing = current_deepseek_pricing()
    assert usage_cost(usage, pricing.off_peak) == 22_590
    assert usage_cost(usage, pricing.peak) == 45_180
    assert cost(usage, current_config()) == 45_180
    review = cost_review(
        usage,
        pricing,
        review_kind="SETTLEMENT_ESTIMATE",
        recorded_at="2026-09-07T01:00:01+00:00",
        assumed_at="2026-09-07T00:59:59+00:00",
        completed_at="2026-09-07T01:00:01+00:00",
        assumed_at_kind="local_dispatched_at",
        original_cost_nusd=None,
        ledger_cost_nusd=45_180,
        uncertainties=["provider billing timestamp is unspecified"],
    )
    assert review["assumed_tier"] == "off_peak"
    assert review["completion_tier"] == "peak"
    assert review["crossed_tier_boundary"] is True
    assert (review["revised_estimate_min_nusd"], review["revised_estimate_max_nusd"]) == (
        22_590,
        45_180,
    )
    assert review["provider_bill_reconciled"] is False


def test_peak_quote_rejects_budget_that_legacy_quote_would_allow(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    state = tmp_path / "state"
    budget = 2_000_000
    task_id = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=20_000,
        max_cost_nusd=budget,
        max_output_tokens=1024,
    )
    store = Storage(app.task_path(state, task_id), readonly=True)
    try:
        config = store.config()
        provider = DeepSeekProvider(config, API_KEY)
        unit = store.units()[0]
        request = provider.prepare(
            make_request(unit, store.snapshot(), provider.output_limit(config)), config
        )
    finally:
        store.close()
    old = legacy_config(max_cost_nusd=budget)
    old_quote = DeepSeekProvider(old, API_KEY).quote(request, old)
    new_quote = provider.quote(request, config)
    assert old_quote.cost_nusd < budget < new_quote.cost_nusd
    calls = []
    snapshot = app.execute(task_id, state, observer=calls.append)
    assert snapshot["task"]["status"] == "PAUSED_BUDGET"
    assert snapshot["attempts"] == []
    assert calls == []


def test_legacy_pricing_resume_is_blocked_before_provider_send(tmp_path, monkeypatch):
    state, task_id = create_legacy_task(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("OLD_PRICE_TASK_MUST_NOT_BUILD_OR_SEND_PROVIDER")

    monkeypatch.setattr(app, "_provider", forbidden)
    with pytest.raises(AgentError, match="STALE_PRICING_VERSION"):
        app.execute(task_id, state)
    snapshot = app.read_task(task_id, state)
    assert snapshot["attempts"] == []
    assert snapshot["operations"] == []


def test_legacy_unknown_keeps_reservation_when_pricing_changes(tmp_path, monkeypatch):
    state, task_id = create_legacy_task(tmp_path)
    path = app.task_path(state, task_id)
    store = Storage(path)
    try:
        config = store.config()
        provider = DeepSeekProvider(config, API_KEY)
        unit = store.units()[0]
        request = provider.prepare(
            make_request(unit, store.snapshot(), provider.output_limit(config)), config
        )
        operation_id = stable_id("operation", task_id, unit["unit_id"], 0, "REVIEW", 0)
        attempt = store.reserve_attempt(
            operation_id, unit["unit_id"], request, provider.quote(request, config)
        )
        assert store.mark_dispatched(attempt["attempt_id"])
        store.mark_unknown(attempt["attempt_id"])
        store.pause(unit["unit_id"], "PROVIDER_UNKNOWN")
        before = store.report_snapshot()
    finally:
        store.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("PAUSED_UNKNOWN_MUST_NOT_BUILD_OR_SEND_PROVIDER")

    monkeypatch.setattr(app, "_provider", forbidden)
    after = app.execute(task_id, state)
    assert after["totals"] == before["totals"]
    assert after["attempts"] == before["attempts"]
    assert [event["event_type"] for event in after["budget_events"]] == ["RESERVE"]
    assert after["attempts"][0]["call_status"] == "UNKNOWN"
    assert after["attempts"][0]["fee_status"] == "HELD"


def test_historical_review_is_append_only_and_traceable(tmp_path):
    state, task_id = create_legacy_task(tmp_path)
    path = app.task_path(state, task_id)
    usage = Usage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        source="deepseek",
        cache_hit_input_tokens=30,
        cache_miss_input_tokens=70,
    )
    store = Storage(path)
    try:
        config = store.config()
        provider = DeepSeekProvider(config, API_KEY)
        unit = store.units()[0]
        request = provider.prepare(
            make_request(unit, store.snapshot(), provider.output_limit(config)), config
        )
        operation_id = stable_id("operation", task_id, unit["unit_id"], 0, "REVIEW", 0)
        attempt = store.reserve_attempt(
            operation_id, unit["unit_id"], request, provider.quote(request, config)
        )
        store.mark_dispatched(attempt["attempt_id"])
        store.complete_attempt(
            attempt["attempt_id"],
            StoredResult(result_status="VALID", completion_state="COMPLETE", usage=usage),
        )
        store.conn.execute(
            "DELETE FROM attempt_timing WHERE attempt_id=?", (attempt["attempt_id"],)
        )
        original = {
            "task": store.task(),
            "attempts": store.rows("SELECT * FROM attempts"),
            "events": store.rows("SELECT * FROM budget_events"),
            "artifacts": store.rows("SELECT * FROM artifacts ORDER BY artifact_id"),
        }
    finally:
        store.close()

    first = app.review_historical_costs(task_id, state)
    second = app.review_historical_costs(task_id, state)
    assert first["pricing_reviews"] == second["pricing_reviews"]
    assert len(first["pricing_reviews"]) == 1
    review = first["pricing_reviews"][0]["data"]
    assert review["original_cost_nusd"] == 15_484
    assert review["revised_estimate_min_nusd"] == 28_810
    assert review["revised_estimate_max_nusd"] == 57_620
    assert review["difference_min_nusd"] == 13_326
    assert review["difference_max_nusd"] == 42_136
    assert review["assumed_at_kind"] == "task_created_at_proxy"
    assert review["provider_bill_reconciled"] is False

    store = Storage(path)
    try:
        assert store.task() == original["task"]
        assert store.rows("SELECT * FROM attempts") == original["attempts"]
        assert store.rows("SELECT * FROM budget_events") == original["events"]
        assert store.rows("SELECT * FROM artifacts ORDER BY artifact_id") == original["artifacts"]
        with pytest.raises(sqlite3.IntegrityError, match="PRICING_REVIEW_IMMUTABLE"):
            store.conn.execute("UPDATE pricing_reviews SET recorded_at='changed'")
    finally:
        store.close()
    trace = app.trace(app.read_task(task_id, state), sections=["pricing"])
    assert trace["pricing"]["pricing_status"] == "HISTORICAL_FROZEN_LEGACY"
    assert trace["pricing"]["pricing_reviews"][0]["data"] == review
