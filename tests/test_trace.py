import copy
import json
import sqlite3

import pytest
from test_cli import cli
from test_ledger_gateway import reserve

from review_agent import app
from review_agent.contracts import AgentError, digest


def test_full_finding_trace_includes_persisted_request_events_and_prices(harness):
    harness.create()
    data = harness.run()
    finding_id = data["findings"][0]["finding_id"]
    trace = app.finding_trace(data, finding_id)
    request = trace["request"]
    operation = trace["operation"]
    assert request["artifact_id"] == operation["request_ref"]
    assert request["kind"] == "request"
    assert request["data"] == data["artifacts"][operation["request_ref"]] == harness.calls[0]
    assert digest(request["data"]) == request["request_digest"] == operation["request_digest"]
    assert request["data"]["system"]
    assert request["data"]["hunks"]
    assert request["data"]["tools"] == []
    assert request["data"]["output_schema"] and request["data"]["finding_schema"]
    assert request["data"]["max_output_tokens"] == 200
    assert [e["event_type"] for e in trace["budget_events"]] == ["RESERVE", "SETTLE"]
    assert [(e["tokens"], e["cost_nusd"]) for e in trace["budget_events"]] == [
        (600, 800000),
        (300, 350000),
    ]
    pricing = trace["pricing"]
    assert pricing["price_input_nusd"] == 1000
    assert pricing["price_output_nusd"] == 2000
    assert pricing["price_unit"] == "nanoUSD/token"
    assert pricing["nano_usd_per_usd"] == 10**9
    assert pricing["limits"]["max_output_tokens"] == 512
    for key in ("prompt_digest", "policy_digest"):
        assert pricing[key] == data["config"][key]
    assert pricing["config_digest"] == data["task"]["config_digest"]


@pytest.mark.parametrize(
    "section,expected,absent",
    [
        ("request", "request", {"budget_events", "pricing", "result"}),
        ("budget-events", "budget_events", {"request", "pricing", "result"}),
        ("pricing", "pricing", {"request", "budget_events", "calls", "result"}),
    ],
)
def test_cli_sections_and_help(harness, section, expected, absent):
    harness.create()
    data = harness.run()
    result = cli(
        "trace",
        "--task",
        harness.task_id,
        "--state-dir",
        harness.state,
        "--finding",
        data["findings"][0]["finding_id"],
        "--section",
        section,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert expected in value
    assert not absent.intersection(value)
    help_result = cli("trace", "--help")
    assert help_result.returncode == 0
    for flag in ("--section", "--attempt", "--finding", "request", "budget-events", "pricing"):
        assert flag in help_result.stdout


def test_cli_default_and_repeat_sections(harness):
    harness.create()
    data = harness.run()
    args = [
        "trace",
        "--task",
        harness.task_id,
        "--state-dir",
        harness.state,
        "--finding",
        data["findings"][0]["finding_id"],
    ]
    result = cli(*args)
    assert result.returncode == 0, result.stderr
    assert {"request", "budget_events", "pricing", "result", "validation"} <= json.loads(
        result.stdout
    ).keys()
    result = cli(*args, "--section", "request", "--section", "pricing")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert "request" in value and "pricing" in value and "budget_events" not in value


@pytest.mark.parametrize(
    "scenario,state,events",
    [
        ("timeout", "UNKNOWN", ["RESERVE"]),
        ("no-findings", "COMPLETED", ["RESERVE", "SETTLE"]),
        ("truncated-valid", "COMPLETED", ["RESERVE", "SETTLE"]),
        ("usage-missing", "COMPLETED", ["RESERVE"]),
    ],
)
def test_audit_without_finding_by_attempt(harness, scenario, state, events):
    harness.create(scenario)
    data = harness.run()
    attempt_id = data["attempts"][0]["attempt_id"]
    result = cli(
        "trace", "--task", harness.task_id, "--state-dir", harness.state, "--attempt", attempt_id
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["attempt"]["call_status"] == state
    assert value["request"]["data"] == harness.calls[0]
    assert [event["event_type"] for event in value["budget_events"]] == events
    if state == "UNKNOWN":
        assert value["result"] is None
    assert len(harness.calls) == 1


def test_task_and_attempt_scope_do_not_mix_budget_events(harness):
    harness.create(diff="two-files")
    data = harness.run()
    full = app.trace(data)
    assert len(full["calls"]) == 2
    assert len(full["budget_events"]) == 4
    assert full["findings"] == data["findings"]
    assert full["evidence"] == data["evidence"]
    attempt_id = data["attempts"][1]["attempt_id"]
    scoped = app.trace(data, attempt_id=attempt_id, sections=["budget-events"])
    assert len(scoped["budget_events"]) == 2
    assert all(e["attempt_id"] == attempt_id for e in scoped["budget_events"])
    assert scoped["task_totals"]["settled_tokens"] == 600


def test_pricing_query_for_task_with_no_attempts(harness):
    harness.create(tokens=0)
    data = harness.run()
    assert not data["attempts"]
    result = cli(
        "trace", "--task", harness.task_id, "--state-dir", harness.state, "--section", "pricing"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pricing"]["limits"]["max_tokens"] == 0
    trace = app.trace(data)
    assert trace["calls"] == []
    assert len(trace["unattempted_operations"]) == 1
    assert trace["unattempted_operations"][0]["request"]["data"]["hunks"]


def test_reserved_and_released_events_remain_auditable(harness):
    harness.create()
    with harness.store() as store:
        row = reserve(store)
        store.cancel_reserved(row["attempt_id"])
    value = app.trace(harness.read(), attempt_id=row["attempt_id"])
    assert [e["event_type"] for e in value["budget_events"]] == ["RESERVE", "RELEASE"]
    assert value["attempt"]["fee_status"] == "RELEASED"
    assert value["request"]["data"]["hunks"]


def test_trace_reads_only_database_and_never_rebuilds_request(harness, monkeypatch):
    harness.create()
    data = harness.run()
    harness.diff.unlink()
    harness.fixture.unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("AUDIT_MUST_NOT_REBUILD_OR_SEND")

    monkeypatch.setattr("review_agent.providers.FixtureProvider.send", forbidden)
    monkeypatch.setattr("review_agent.review.make_request", forbidden)
    monkeypatch.setattr(app, "policy_versions", forbidden)
    before = harness.read()
    path = app.task_path(harness.state, harness.task_id)
    with sqlite3.connect(path) as db:
        before_dump = "\n".join(db.iterdump())
    for _ in range(2):
        assert app.trace(harness.read())["calls"][0]["request"]["data"] == harness.calls[0]
    result = cli("trace", "--task", harness.task_id, "--state-dir", harness.state)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pricing"]["prompt_digest"] == data["config"]["prompt_digest"]
    assert harness.read() == before
    with sqlite3.connect(path) as db:
        assert "\n".join(db.iterdump()) == before_dump
    assert len(harness.calls) == 1


def test_cli_request_contains_redactions_only(harness):
    from test_ingest_safety import added_diff

    secret = "ghp_" + "SyntheticAuditOnly" * 2
    harness.create("no-findings", text=added_diff([f'api_key = "{secret}"']))
    data = harness.run()
    result = cli(
        "trace",
        "--task",
        harness.task_id,
        "--state-dir",
        harness.state,
        "--attempt",
        data["attempts"][0]["attempt_id"],
        "--section",
        "request",
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert "[REDACTED_TOKEN]" in result.stdout


@pytest.mark.parametrize("broken", ["request_missing", "request_digest", "config_digest"])
def test_broken_audit_chain_fails_instead_of_reconstructing(harness, broken):
    harness.create()
    data = copy.deepcopy(harness.run())
    operation = data["operations"][0]
    if broken == "request_missing":
        del data["artifacts"][operation["request_ref"]]
    elif broken == "request_digest":
        data["artifacts"][operation["request_ref"]]["system"] = "changed prompt"
    else:
        data["task"]["config_digest"] = "invalid"
    with pytest.raises(AgentError, match="AUDIT_INTEGRITY_ERROR"):
        app.trace(data)
    assert len(harness.calls) == 1


def test_cli_rejects_invalid_or_ambiguous_selectors_without_echo(harness):
    harness.create()
    harness.run()
    args = ["trace", "--task", harness.task_id, "--state-dir", harness.state]
    secret = "sk-" + "SyntheticAuditOnly" * 2
    for extra, code in [
        (["--finding", secret], "FINDING_NOT_FOUND"),
        (["--attempt", secret], "ATTEMPT_NOT_FOUND"),
        (["--finding", secret, "--attempt", secret], "INVALID_ARGUMENTS"),
        (["--section", secret], "INVALID_ARGUMENTS"),
    ]:
        result = cli(*args, *extra)
        assert result.returncode == 1
        assert secret not in result.stdout + result.stderr
        assert code in result.stderr
