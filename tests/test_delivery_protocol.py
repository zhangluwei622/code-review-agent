"""Versioned duplicate-key rejection and the ordinary CLI's frozen S2 identity."""

import json
import os
import subprocess
import sys
from collections import Counter

import pytest
from conftest import ROOT
from test_tool_loop import ZERO, reply, spec, tool

from review_agent import app
from review_agent.config import DeliveryTaskConfig, package_text
from review_agent.contracts import AgentError, digest
from review_agent.providers import RawProviderReply
from review_agent.review import decode_reply
from review_agent.safety import Safety

S2_DIGEST = "2321125b8f81621f23c78322528f62d7c9f08144eaa8a14f81bfc54e3f9ab3fb"
DUPLICATE = '{"action":"submit_review","findings":[],"findings":[{"marker":1}]}'


def test_final_report_scope_does_not_repeat_pre_url_implementation_claim(harness):
    from review_agent.report import render

    harness.create(schema=7)
    report = render(harness.run())
    assert "未接入 GitHub/GitLab URL" not in report
    assert "GitLab 仅 mock 验收" in report
    assert "完整在线链路尚未验证" in report


@pytest.mark.parametrize(
    "body",
    [
        DUPLICATE,
        '{"action":"submit_review","action":"abstain","reason":"x"}',
        '{"action":"submit_review","findings":[],"findings":[]}',
        '{"action":"submit_review","findings":[],"find\\u0069ngs":[]}',
        '{"action":"submit_review","findings":[{"title":"a","title":"b"}]}',
        '{"action":"request_tool","name":"read_hunk",'
        '"arguments":{"hunk_id":"h0001","hunk_id":"h0002"}}',
        '{"action":"submit_review","findings":[{"x":{"k":1,"k":2}}]}',
    ],
)
def test_duplicate_objects_rejected_before_semantic_or_tool_validation(harness, body):
    harness.create(schema=7)
    with harness.store() as store:
        config = store.config()
    result = decode_reply(RawProviderReply(**reply(body)), config, Safety())
    assert result.result_status == "FORMAT_INVALID"
    assert result.error_code == "INVALID_REVIEW_FORMAT"
    assert result.safe_body == body and result.decision is None
    assert result.usage.total_tokens == 300
    assert result.completion_state == "COMPLETE"


@pytest.mark.parametrize("schema", [4, 5, 6])
def test_legacy_parser_and_serialization_remain_unchanged(harness, schema):
    harness.create(schema=4 if schema == 4 else 5)
    with harness.store() as store:
        config = store.config()
    # v6 shares v5 parsing; no evaluation batch is created or executed.
    config = config.model_copy(update={"schema_version": schema})
    assert "reply_protocol" not in config.model_dump()
    result = decode_reply(RawProviderReply(**reply(DUPLICATE)), config, Safety())
    assert result.result_status == "VALID"
    assert result.decision["findings"] == [{"marker": 1}]
    assert result.safe_body == DUPLICATE


def test_same_keys_in_separate_objects_and_inside_strings_are_not_duplicates(harness):
    harness.create(schema=7)
    with harness.store() as store:
        config = store.config()
    body = json.dumps(
        {
            "action": "submit_review",
            "findings": [{"k": 1}, {"k": 2}],
            "reason": 'Example text: {"k":1,"k":2}',
        }
    )
    assert decode_reply(RawProviderReply(**reply(body)), config, Safety()).result_status == "VALID"


@pytest.mark.parametrize(
    "finish,status", [("OUTPUT_LIMIT", "TRUNCATED"), ("UNCONFIRMED", "UNUSABLE")]
)
def test_duplicate_check_does_not_override_completion_classification(harness, finish, status):
    harness.create(schema=7)
    with harness.store() as store:
        config = store.config()
    result = decode_reply(RawProviderReply(**reply(DUPLICATE), finish=finish), config, Safety())
    assert result.result_status == status and result.decision is None
    assert result.safe_body == DUPLICATE and result.usage.total_tokens == 300


def test_deep_json_is_a_completed_format_failure_not_unknown(harness):
    harness.create(schema=7)
    with harness.store() as store:
        config = store.config()
    body = '{"action":"submit_review","findings":' + "[" * 1100 + "0" + "]" * 1100 + "}"
    result = decode_reply(RawProviderReply(**reply(body)), config, Safety())
    assert result.result_status == "FORMAT_INVALID" and result.safe_body == body


def test_later_round_duplicate_keeps_paid_source_and_one_bound_repair(harness):
    value = spec(tool(), reply(DUPLICATE))
    value["responses"]["0:1:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=7, repairs=1)
    data = harness.run()
    assert data["units"][0]["status"] == "DONE"
    assert len(data["attempts"]) == 3 and data["totals"]["settled_tokens"] == 900
    original, repaired = data["operations"][1:]
    raw = data["artifacts"][original["result_ref"]]
    assert raw["safe_body"] == DUPLICATE and raw["result_status"] == "FORMAT_INVALID"
    repair_request = data["artifacts"][repaired["request_ref"]]
    assert repair_request["repair"]["source_result_ref"] == original["result_ref"]
    assert repair_request["repair"]["previous_response"] == DUPLICATE
    assert repair_request["repair"]["error_codes"] == ["INVALID_REVIEW_FORMAT"]
    assert harness.calls[1]["loop"] == harness.calls[2]["loop"]
    assert digest(harness.calls[0]["system"]) == digest(harness.calls[1]["system"]) == S2_DIGEST
    assert harness.calls[2]["system"] == package_text("prompts/repair-tools.md")
    assert app.trace(data)["pricing"]["reply_protocol"] == "json-unique-keys-v1"
    assert all(a["fee_status"] == "SETTLED" for a in data["attempts"])
    for a in data["attempts"]:
        assert Counter(
            e["event_type"] for e in data["budget_events"] if e["attempt_id"] == a["attempt_id"]
        ) == {"RESERVE": 1, "SETTLE": 1}
    for _ in range(2):
        resumed = harness.run()
        assert resumed["attempts"] == data["attempts"] and resumed["totals"] == data["totals"]
    assert len(harness.calls) == 3


@pytest.mark.parametrize("repair_body,repairs,sends", [(DUPLICATE, 1, 2), (ZERO["body"], 0, 1)])
def test_failed_or_disabled_repair_is_terminal_and_still_paid(harness, repair_body, repairs, sends):
    value = spec(reply(DUPLICATE))
    value["responses"]["0:0:REPAIR:1"] = reply(repair_body)
    harness.create(spec=value, schema=7, repairs=repairs)
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    assert len(data["attempts"]) == sends and data["totals"]["settled_tokens"] == sends * 300
    assert not data["findings"]
    harness.run()
    assert len(harness.calls) == sends


def test_duplicate_tool_arguments_consume_send_but_no_tool_slot(harness):
    body = (
        '{"action":"request_tool","name":"read_hunk",'
        '"arguments":{"hunk_id":"h0001","hunk_id":"h0001"}}'
    )
    harness.create(spec=spec(reply(body)), schema=7, repairs=0)
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    assert not data["tool_calls"] and len(data["attempts"]) == 1
    assert data["totals"]["settled_tokens"] == 300


def test_unknown_repair_preserves_held_and_resume_does_not_resend(harness):
    value = spec(reply(DUPLICATE))
    value["responses"]["0:0:REPAIR:1"] = {"error": "timeout"}
    harness.create(spec=value, schema=7, repairs=1)
    data = harness.run()
    assert data["attempts"][0]["fee_status"] == "SETTLED"
    assert data["attempts"][1]["call_status"] == "UNKNOWN"
    assert data["attempts"][1]["fee_status"] == "HELD"
    assert harness.run()["totals"] == data["totals"] and len(harness.calls) == 2


def test_repair_quote_exhaustion_preserves_paid_duplicate_and_pause(harness):
    value = spec(reply(DUPLICATE))
    value["responses"]["0:0:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=7, repairs=1, tokens=800)
    data = harness.run()
    assert data["units"][0]["status"] == "PAUSED_BUDGET"
    assert data["totals"]["settled_tokens"] == 300 and len(harness.calls) == 1
    assert harness.run()["totals"] == data["totals"] and len(harness.calls) == 1


@pytest.mark.parametrize("point", ["after_completed", "after_validation"])
def test_duplicate_transaction_checkpoint_replay_is_idempotent(harness, point):
    value = spec(reply(DUPLICATE))
    value["responses"]["0:0:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=7, repairs=1)
    harness.crash(point)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED" and len(harness.calls) == 2
    assert data["totals"]["settled_tokens"] == 600


def test_legacy_resumed_sends_use_legacy_prompt_and_parser(harness):
    value = spec(tool(), reply('{"action":"submit_review","findings":[],"findings":[]}'))
    harness.create(spec=value, schema=5)
    harness.crash("after_completed")
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert all(c["system"] == package_text("prompts/review-tools.md") for c in harness.calls)
    assert "reply_protocol" not in data["config"]


def test_unknown_protocol_and_mutated_s2_prompt_block_before_send(harness, monkeypatch):
    harness.create(schema=7)
    config = harness.read()["config"]
    with pytest.raises(ValueError):
        DeliveryTaskConfig.model_validate({**config, "reply_protocol": "json-unique-keys-v999"})
    original = app.package_text
    monkeypatch.setattr(
        app,
        "package_text",
        lambda path: "changed" if path.endswith("review-tools-semantic-s2.md") else original(path),
    )
    with pytest.raises(AgentError, match="CONFIG_VERSION_MISMATCH"):
        harness.run()
    assert harness.calls == []


def test_ordinary_cli_default_is_s2_with_strict_protocol(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    state = tmp_path / "state"
    command = [
        sys.executable,
        "-m",
        "review_agent.cli",
        "review",
        "--diff",
        str(ROOT / "examples/diffs/empty-list.diff"),
        "--fixture",
        str(ROOT / "examples/provider/empty.json"),
        "--state-dir",
        str(state),
        "--max-tokens",
        "5000",
        "--max-cost-usd",
        "0.01",
    ]
    # Use a local data-only response; no target code or model is executed.
    fixture = tmp_path / "zero.json"
    fixture.write_text(json.dumps({"default": ZERO}))
    command[command.index("--fixture") + 1] = str(fixture)
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=True)
    output = json.loads(result.stdout)
    assert output["schema_version"] == 7 and output["review_version"] == "semantic-s2"
    assert (
        output["reply_protocol"] == "json-unique-keys-v1" and output["prompt_digest"] == S2_DIGEST
    )
    data = app.read_task(output["task_id"], state)
    assert data["config"]["max_output_tokens"] == 1024
    request = data["artifacts"][data["operations"][0]["request_ref"]]
    assert (
        request["system"]
        == (
            ROOT / "tests/fixtures/review-tools-semantic-s2-frozen.md"
        ).read_text()
    )


def test_actual_holdout_body_changes_only_under_new_parser(harness):
    body = (
        ROOT / "tests/fixtures/duplicate-findings-legacy.txt"
    ).read_text()
    harness.create(schema=7)
    with harness.store() as store:
        current = store.config()
    legacy = current.model_copy(update={"schema_version": 6})
    old = decode_reply(RawProviderReply(**reply(body)), legacy, Safety())
    new = decode_reply(RawProviderReply(**reply(body)), current, Safety())
    assert old.result_status == "VALID" and len(old.decision["findings"]) == 1
    assert new.result_status == "FORMAT_INVALID" and new.decision is None
    assert old.safe_body == new.safe_body == body
    assert old.usage == new.usage


def test_default_deepseek_request_archived_and_sent_with_s2(tmp_path, monkeypatch):
    import httpx
    from test_deepseek_provider import API_KEY, response

    from review_agent.providers import DeepSeekProvider

    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)  # Synthetic test credential.
    state = tmp_path / "state"
    task = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=100000,
        max_cost_nusd=50000000,
    )
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response(body=ZERO["body"]))

    monkeypatch.setattr(
        app,
        "_provider",
        lambda config, observer=None: (
            DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler)),
            Safety((API_KEY,)),
        ),
    )
    data = app.execute(task, state)
    assert data["config"]["schema_version"] == 7
    assert bodies[0] == data["artifacts"][data["operations"][0]["request_ref"]]
    assert digest(bodies[0]["messages"][0]["content"]) == S2_DIGEST
    assert bodies[0]["max_tokens"] == 1024 and bodies[0]["temperature"] == 0
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["model"] == "deepseek-flash" and not bodies[0]["stream"]
    assert data["attempts"][0]["fee_status"] == "SETTLED"
