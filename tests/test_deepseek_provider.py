import json

import httpx
import pytest
from conftest import ROOT

from review_agent import app
from review_agent.budget import cost
from review_agent.config import TaskConfig, policy_versions
from review_agent.contracts import AgentError, digest, json_text
from review_agent.gateway import Gateway
from review_agent.pricing import current_deepseek_pricing
from review_agent.providers import DeepSeekProvider
from review_agent.report import render
from review_agent.review import decode_reply, make_request
from review_agent.safety import Safety
from review_agent.storage import Storage

API_KEY = "ds-test-credential-value-123456789"
VALID_BODY = '{"action":"abstain","findings":[],"reason":"No supported defect."}'


def deepseek_config(**updates):
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
        "max_output_tokens": 512,
        **policy_versions(),
    }
    values.update(updates)
    return TaskConfig(**values)


def response(*, body=VALID_BODY, finish="stop", usage=True):
    data = {
        "id": "deepseek-request-1",
        "model": "deepseek-flash",
        "system_fingerprint": "fp-test",
        "choices": [{"finish_reason": finish, "message": {"content": body}}],
    }
    if usage:
        data["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 30,
            "prompt_cache_miss_tokens": 70,
        }
    return data


def test_prepare_quote_and_wire_body_share_one_request():
    captured = {}

    def handler(request):
        captured["body"] = request.content
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json=response())

    config = deepseek_config()
    provider = DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler))
    business = {
        "system": 'Return JSON. Example: {"action":"abstain","findings":[],"reason":"x"}',
        "unit_id": "unit-1",
        "unit_index": 0,
        "snapshot_id": "snapshot-1",
        "max_output_tokens": 512,
        "hunks": [],
        "output_schema": {"type": "object"},
        "finding_schema": {"type": "object"},
        "tools": [],
    }
    prepared = provider.prepare(business, config)
    quote = provider.quote(prepared, config)
    reply = provider.send(prepared)

    assert json.loads(captured["body"]) == prepared
    assert captured["body"] == json_text(prepared).encode()
    assert API_KEY not in captured["body"].decode()
    assert captured["authorization"] == f"Bearer {API_KEY}"
    assert prepared["model"] == "deepseek-flash"
    assert prepared["thinking"] == {"type": "disabled"}
    assert prepared["response_format"] == {"type": "json_object"}
    assert prepared["stream"] is False
    assert "JSON" in prepared["messages"][0]["content"]
    assert "Example" in prepared["messages"][0]["content"]
    assert quote.input_tokens == len(json_text(prepared["messages"]).encode()) + 256
    assert (
        quote.cost_nusd
        == (quote.input_tokens * 300_000_000 + quote.output_tokens * 1_200_000_000 + 999_999)
        // 1_000_000
    )
    assert reply.provider_request_id == "deepseek-request-1"
    assert reply.provider_finish_reason == "stop"


def test_gateway_persists_quotes_and_sends_the_same_prepared_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    state = tmp_path / "state"
    task_id = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=20_000,
        max_cost_nusd=10_000_000,
        max_output_tokens=512,
        schema_version=4,  # This test exercises the accepted pre-tool request contract.
    )
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json=response())

    store = Storage(app.task_path(state, task_id))
    try:
        config = store.config()
        provider = DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler))
        unit = store.units()[0]
        business = make_request(unit, store.snapshot(), provider.output_limit(config))
        result_ref = Gateway(store, provider, Safety((API_KEY,))).run(unit["unit_id"], business)
        snapshot = store.report_snapshot()
    finally:
        store.close()

    operation = snapshot["operations"][0]
    attempt = snapshot["attempts"][0]
    artifact = snapshot["artifacts"][operation["request_ref"]]
    assert artifact == json.loads(captured["body"])
    assert digest(artifact) == operation["request_digest"]
    assert json.loads(attempt["quote"]) == provider.quote(artifact, config).model_dump()
    assert API_KEY not in json.dumps(snapshot)
    assert "Return exactly one JSON object" in artifact["messages"][0]["content"]
    assert '"action":"abstain"' in artifact["messages"][0]["content"]
    assert snapshot["artifacts"][result_ref]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "source": "deepseek",
        "cache_hit_input_tokens": 30,
        "cache_miss_input_tokens": 70,
    }
    assert attempt["actual_cost_nusd"] == 45_180
    trace = app.trace(snapshot)
    assert trace["calls"][0]["request"]["data"] == artifact
    pricing = trace["pricing"]["pricing_snapshot"]
    assert pricing["version"] == "deepseek-flash-2026-09-11-observed"
    assert pricing["peak"]["cache_hit_nusd_per_mtok"] == 6_000_000
    assert trace["pricing"]["ledger_cost_basis"] == "peak_rate_local_estimate_upper_bound"
    assert trace["pricing"]["provider_bill_reconciled"] is False
    assert len(trace["pricing"]["pricing_reviews"]) == 1
    report = render(snapshot)
    assert "DeepSeek 真实模型小样例" in report
    assert "deepseek-request-1" in report and "finish：`stop`" in report
    assert "未与提供方账单核对" in report


def test_deepseek_provider_runs_through_langgraph_and_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    state = tmp_path / "state"
    task_id = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=20_000,
        max_cost_nusd=10_000_000,
        max_output_tokens=1024,
    )
    success_body = json.loads((ROOT / "examples/provider/success.json").read_text())["responses"][
        "0:0:REVIEW:1"
    ]["body"]

    def handler(request):
        return httpx.Response(200, json=response(body=success_body))

    store = Storage(app.task_path(state, task_id), readonly=True)
    try:
        config = store.config()
    finally:
        store.close()
    provider = DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        app,
        "_provider",
        lambda config, observer=None: (provider, Safety((API_KEY,))),
    )
    snapshot = app.execute(task_id, state)
    report = render(snapshot)
    assert snapshot["task"]["status"] == "COMPLETED"
    assert len(snapshot["findings"]) == 1
    assert snapshot["attempts"][0]["fee_status"] == "SETTLED"
    assert "DeepSeek 真实模型小样例" in report
    assert "deepseek-request-1" in report


@pytest.mark.parametrize(
    "body,finish,expected",
    [
        ("", "stop", "UNUSABLE"),
        ("{", "stop", "FORMAT_INVALID"),
        (VALID_BODY, "length", "TRUNCATED"),
        (VALID_BODY, "content_filter", "UNUSABLE"),
    ],
)
def test_empty_invalid_and_truncated_responses_keep_business_validation(body, finish, expected):
    config = deepseek_config()
    provider = DeepSeekProvider(config, API_KEY)
    reply = provider._decode(json.dumps(response(body=body, finish=finish)).encode())
    result = decode_reply(reply, config, Safety((API_KEY,)))
    assert result.result_status == expected
    assert result.provider_finish_reason == finish
    if finish == "length":
        assert result.completion_state == "OUTPUT_LIMIT"
        assert result.truncation_reason == "PROVIDER_OUTPUT_LIMIT"


def test_missing_cache_breakdown_keeps_usage_unsettled():
    data = response()
    del data["usage"]["prompt_cache_hit_tokens"]
    del data["usage"]["prompt_cache_miss_tokens"]
    config = deepseek_config()
    provider = DeepSeekProvider(config, API_KEY)
    result = decode_reply(provider._decode(json.dumps(data).encode()), config, Safety((API_KEY,)))
    assert result.usage is None


def test_unsafe_provider_metadata_is_not_persistable():
    data = response()
    data["id"] = API_KEY
    config = deepseek_config()
    provider = DeepSeekProvider(config, API_KEY)
    result = decode_reply(provider._decode(json.dumps(data).encode()), config, Safety((API_KEY,)))
    assert result.result_status == "SAFETY_REJECTED"
    assert result.error_code == "UNSAFE_REPLY_METADATA"
    assert result.provider_request_id is None


def test_actual_cost_uses_cache_hit_miss_and_output_prices():
    config = deepseek_config()
    provider = DeepSeekProvider(config, API_KEY)
    result = decode_reply(
        provider._decode(json.dumps(response()).encode()), config, Safety((API_KEY,))
    )
    assert cost(result.usage, config) == 45_180


def test_exact_api_key_is_removed_before_model_input(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    diff = tmp_path / "credential.diff"
    diff.write_text(
        "diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n"
        "@@ -1 +1 @@\n-value = 'old'\n+value = '" + API_KEY + "'\n"
    )
    state = tmp_path / "state"
    task_id = app.create_task(
        diff,
        None,
        state,
        provider_name="deepseek",
        max_tokens=20_000,
        max_cost_nusd=10_000_000,
    )
    store = Storage(app.task_path(state, task_id))
    try:
        config = store.config()
        provider = DeepSeekProvider(config, API_KEY)
        unit = store.units()[0]
        prepared = provider.prepare(
            make_request(unit, store.snapshot(), provider.output_limit(config)), config
        )
        assert API_KEY not in json_text(prepared)
        assert "[REDACTED_CREDENTIAL]" in json_text(prepared)
        assert API_KEY not in "\n".join(store.conn.iterdump())
    finally:
        store.close()


def test_missing_credential_creates_no_task_and_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    state = tmp_path / "state"
    with pytest.raises(AgentError, match="PROVIDER_CREDENTIAL_MISSING"):
        app.create_task(
            ROOT / "examples/diffs/empty-list.diff",
            None,
            state,
            provider_name="deepseek",
            max_tokens=20_000,
            max_cost_nusd=10_000_000,
        )
    assert not state.exists()


def test_http_failure_is_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": "synthetic"})

    provider = DeepSeekProvider(deepseek_config(), API_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(AgentError, match="PROVIDER_UNKNOWN"):
        provider.send({"messages": [], "max_tokens": 1})
    assert calls == 1
