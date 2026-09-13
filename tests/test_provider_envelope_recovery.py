"""A fully received bad provider envelope is a paid fact, not an UNKNOWN send."""

import json
from collections import Counter

import httpx
import pytest
from conftest import ROOT
from test_deepseek_provider import API_KEY, deepseek_config, response

from review_agent import app
from review_agent.contracts import json_text
from review_agent.providers import DeepSeekProvider
from review_agent.review import decode_reply
from review_agent.safety import Safety


def envelope_case(kind):
    value = response()
    if kind == "invalid_json":
        return b"{", "{", False
    if kind == "deep_json":
        raw = "[" * 1100 + "0" + "]" * 1100
        return raw.encode(), raw, False
    if kind == "invalid_utf8":
        return b"\xff", "", False
    if kind == "zero_choices":
        value["choices"] = []
    elif kind == "multiple_choices":
        value["choices"] *= 2
    elif kind == "bad_usage":
        value["usage"]["prompt_tokens"] = "100"
    elif kind == "unexpected_finish":
        value["choices"][0]["finish_reason"] = "future_reason"
    elif kind == "invalid_message":
        value["choices"][0]["message"] = {"content": 123}
    elif kind == "unsafe_escaped_content":
        value["choices"] = []
        value["untrusted"] = API_KEY
        # An escaped credential must be inspected as decoded text, not opaque JSON.
        raw = json.dumps(value).replace("ds-test", "\\u0064s-test").encode()
        return raw, None, True
    return json.dumps(value).encode(), json_text(value), kind != "bad_usage"


@pytest.mark.parametrize(
    "kind",
    [
        "invalid_json",
        "deep_json",
        "invalid_utf8",
        "zero_choices",
        "multiple_choices",
        "bad_usage",
        "unexpected_finish",
        "invalid_message",
        "unsafe_escaped_content",
    ],
)
def test_complete_bad_envelope_is_persisted_and_resume_does_not_send(tmp_path, monkeypatch, kind):
    raw, expected_body, billed = envelope_case(kind)
    state = tmp_path / "state"
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    task = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=100000,
        max_cost_nusd=50000000,
    )
    calls = []

    def handler(request):
        calls.append(request.content)
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(
        app,
        "_provider",
        lambda config, observer=None: (
            DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler)),
            Safety((API_KEY,)),
        ),
    )
    data = app.execute(task, state)
    (attempt,) = data["attempts"]
    result = data["artifacts"][attempt["result_ref"]]
    assert attempt["call_status"] == "COMPLETED"
    assert result["result_status"] == ("SAFETY_REJECTED" if expected_body is None else "UNUSABLE")
    assert result["safe_body"] == expected_body and result["decision"] is None
    assert result["completion_state"] == "UNCONFIRMED"
    assert attempt["fee_status"] == ("SETTLED" if billed else "HELD")
    assert data["totals"]["settled_tokens"] == (120 if billed else 0)
    assert data["totals"]["held_tokens"] == (0 if billed else attempt["quote_tokens"])
    assert data["totals"]["settled_cost_nusd"] == (45180 if billed else 0)
    assert Counter(e["event_type"] for e in data["budget_events"]) == (
        Counter(RESERVE=1, SETTLE=1) if billed else Counter(RESERVE=1)
    )
    for _ in range(2):
        after = app.execute(task, state)
        assert after["attempts"] == data["attempts"]
        assert after["budget_events"] == data["budget_events"]
        assert after["totals"] == data["totals"]
    assert len(calls) == len(data["operations"]) == 1  # No retry or REPAIR.
    trace = app.trace(after)
    assert trace["calls"][0]["result"] == result
    assert API_KEY not in json.dumps(data) + json.dumps(trace)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_request_id", "r" * 501),
        ("provider_model", "m" * 201),
        ("system_fingerprint", "f" * 501),
        ("provider_finish_reason", "INVALID_REASON"),
    ],
)
def test_invalid_metadata_cannot_escape_completed_reply_decoder(field, value):
    config = deepseek_config()
    reply = DeepSeekProvider(config, API_KEY)._decode(json.dumps(response()).encode())
    reply = reply.model_copy(update={field: value})
    result = decode_reply(reply, config, Safety((API_KEY,)))
    assert result.result_status == "SAFETY_REJECTED"
    assert result.error_code == "UNSAFE_REPLY_METADATA"
    assert result.usage.total_tokens == 120
    assert result.provider_request_id is None and result.provider_model is None


def test_non_utf8_model_string_is_rejected_without_losing_usage():
    config = deepseek_config()
    reply = DeepSeekProvider(config, API_KEY)._decode(json.dumps(response(body="\ud800")).encode())
    result = decode_reply(reply, config, Safety((API_KEY,)))
    assert result.result_status == "SAFETY_REJECTED" and result.safe_body is None
    assert result.usage.total_tokens == 120
