"""Local product adapter: real core execution, mocked network, no paid calls."""

import json
import threading
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_deepseek_provider import API_KEY, response
from test_ingest_safety import added_diff
from test_sources_common import URL, github_http

from review_agent import app
from review_agent.contracts import AgentError
from review_agent.providers import DeepSeekProvider
from review_agent.safety import Safety
from review_agent.workbench.server import Handler
from review_agent.workbench.service import (
    APIError,
    ResumeRequest,
    Submission,
    Workbench,
    demo_text,
)


@pytest.fixture
def desk(tmp_path):
    workbench = Workbench(tmp_path / "state")
    yield workbench
    workbench.close()


def submission(demo="tools", **changes):
    values = {
        "submission_id": str(uuid4()), "mode": "fixture", "input_type": "diff",
        "content": demo_text(demo, 1), "demo": demo,
    }
    return Submission.model_validate(values | changes)


def finish(desk):
    desk.worker.join(timeout=40)
    assert not desk.worker.is_alive()


@pytest.mark.parametrize("demo,sends,tools,status", [
    ("finding", 1, 0, "COMPLETED"), ("tools", 6, 4, "COMPLETED"),
    ("repair", 2, 0, "COMPLETED"), ("retry", 2, 1, "PAUSED_UNKNOWN"),
])
def test_submission_runs_core_and_exports_same_facts(desk, demo, sends, tools, status):
    job = desk.submit(submission(demo))
    finish(desk)
    detail = desk.detail(job["job_id"])
    assert detail["job"]["runner"] == "FINISHED", detail["job"]
    assert detail["summary"]["status"] == status
    assert detail["view"]["marked_sends"] == sends
    assert detail["view"]["tool_count"] == tools
    assert detail["summary"]["review_version"] == "semantic-s2"
    assert detail["summary"]["reply_protocol"] == "json-unique-keys-v1"
    if demo == "tools":
        assert any(r["kind"] == "REPAIR" for r in detail["view"]["relations"])
        assert {e["status"] for e in detail["events"] if e["kind"] == "REVIEW"} >= {
            "RESERVED", "DISPATCHED", "COMPLETED",
        }
        assert {e["status"] for e in detail["events"] if e["kind"] == "TOOL"} >= {
            "RUNNING", "SUCCEEDED",
        }
    original = desk.snapshot(job["job_id"])
    assert status.encode() in desk.download(job["job_id"], "report.md")[0]
    trace = json.loads(desk.download(job["job_id"], "trace.json")[0])
    observed = trace.pop("workbench")
    assert observed["events"] == detail["events"] and observed["task_status"] == status
    assert trace == app.trace(original)
    html = desk.download(job["job_id"], "report.html")[0]
    assert b"trace-data" in html and b"connect-src 'none'" in html
    for _ in range(2):
        desk.detail(job["job_id"])
        assert desk.snapshot(job["job_id"])["budget_events"] == original["budget_events"]


def test_duplicate_submission_and_concurrent_jobs_cannot_double_send(desk, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    execute = app.execute

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return execute(*args, **kwargs)

    monkeypatch.setattr(app, "execute", blocked)
    request = submission("finding")
    try:
        first = desk.submit(request)
        assert entered.wait(10)
        assert desk.submit(request)["job_id"] == first["job_id"]
        with pytest.raises(APIError) as error:
            desk.submit(submission("finding"))
        assert error.value.code == "WORKBENCH_BUSY"
        assert len(desk.list_jobs()["jobs"]) == 1
    finally:
        release.set()
    finish(desk)
    assert desk.detail(first["job_id"])["view"]["marked_sends"] == 1


def test_retry_action_is_idempotent_and_original_unknown_keeps_held(desk):
    job = desk.submit(submission("retry"))["job_id"]
    finish(desk)
    before = desk.detail(job)
    request = ResumeRequest(action_id=str(uuid4()))
    desk.resume(job, request)
    finish(desk)
    assert desk.detail(job)["summary"]["attempts"] == before["summary"]["attempts"]
    unknown = next(a for a in before["summary"]["attempts"] if a["call_status"] == "UNKNOWN")
    retry = ResumeRequest(action_id=str(uuid4()), retry_unknown=unknown["attempt_id"])
    desk.resume(job, retry)
    finish(desk)
    after = desk.detail(job)
    assert after["summary"]["status"] == "COMPLETED"
    assert after["view"]["marked_sends"] == 3
    assert after["summary"]["totals"]["held_tokens"] == 600
    assert after["summary"]["totals"]["held_cost_nusd"] == 800000
    desk.resume(job, retry)
    assert desk.active is None
    assert desk.detail(job)["summary"] == after["summary"]
    assert any(r["kind"] == "RETRY" for r in after["view"]["relations"])


@pytest.mark.parametrize("lose_binding", [False, True])
def test_restart_does_not_run_and_finds_already_committed_task(tmp_path, lose_binding):
    root = tmp_path / "restart"
    desk = Workbench(root)
    job = desk.submit(submission("finding"))["job_id"]
    finish(desk)
    before = desk.detail(job)["summary"]
    desk.update(job, "RUNNING")
    if lose_binding:
        desk.db.execute("UPDATE jobs SET task_id=NULL WHERE job_id=?", (job,))
    desk.close()
    reopened = Workbench(root)
    try:
        assert reopened.active is None
        detail = reopened.detail(job)
        assert detail["job"]["runner"] == "INTERRUPTED"
        assert detail["summary"] == before
        reopened.resume(job, ResumeRequest(action_id=str(uuid4())))
        finish(reopened)
        assert reopened.detail(job)["summary"] == before
    finally:
        reopened.close()


def test_low_budget_is_paused_without_model_sends(desk):
    job = desk.submit(submission("finding", max_tokens=1))["job_id"]
    finish(desk)
    data = desk.detail(job)
    assert data["summary"]["status"] == "PAUSED_BUDGET"
    assert data["view"]["marked_sends"] == 0


def test_live_explicit_enable_and_demo_identity_are_enforced(desk):
    with pytest.raises(APIError) as error:
        desk.submit(submission(mode="deepseek"))
    assert error.value.code == "LIVE_DISABLED"
    with pytest.raises(APIError) as error:
        desk.submit(submission(content="user changed diff"))
    assert error.value.code == "DEMO_INPUT_CHANGED"
    assert desk.list_jobs()["jobs"] == []


def test_live_adapter_keeps_raw_secret_in_memory_and_uses_mock_transport(desk, monkeypatch):
    desk.allow_live = True
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    marker = desk.root / "MUST_NOT_EXECUTE"
    secret = "SyntheticPasswordOnly"
    text = added_diff([f'password = "{secret}"', f'open({str(marker)!r}, "w").write("bad")'])
    calls = []

    def handler(request):
        calls.append(request.content)
        return httpx.Response(200, json=response(body='{"action":"submit_review","findings":[]}'))

    monkeypatch.setattr(app, "_provider", lambda config, **kw: (
        DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler)),
        Safety((API_KEY,)),
    ))
    job = desk.submit(submission(mode="deepseek", content=text))["job_id"]
    finish(desk)
    detail = desk.detail(job)
    assert detail["summary"]["status"] == "COMPLETED", detail["job"]
    assert detail["redactions"] == 1 and len(calls) == 1
    assert secret.encode() not in calls[0]
    assert not marker.exists()
    assert secret not in json.dumps(detail)
    for p in desk.root.rglob("*"):
        if p.is_file():
            assert secret.encode() not in p.read_bytes(), p.name
            assert API_KEY.encode() not in p.read_bytes(), p.name


def test_ui_errors_do_not_include_untrusted_exception(desk, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("do-not-persist-this-private-input")

    monkeypatch.setattr(app, "create_task", fail)
    job = desk.submit(submission())["job_id"]
    finish(desk)
    value = desk.detail(job)
    assert value["job"]["error_code"] == "WORKBENCH_RUN_FAILED"
    assert "do-not-persist" not in json.dumps(value)


def test_url_adapter_uses_mock_source_and_resume_keeps_frozen_snapshot(desk, monkeypatch):
    from review_agent.sources import service

    desk.allow_live = True
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    http, _ = github_http()
    fetch = service.fetch_source
    source_calls = []

    def get_source(url, **kwargs):
        source_calls.append(url)
        return fetch(url, http=http)

    monkeypatch.setattr(service, "fetch_source", get_source)
    sends = []

    def handler(request):
        sends.append(request.content)
        return httpx.Response(200, json=response(body='{"action":"submit_review","findings":[]}'))

    monkeypatch.setattr(app, "_provider", lambda config, **kw: (
        DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler)),
        Safety((API_KEY,)),
    ))
    job = desk.submit(submission(mode="deepseek", input_type="url", content=URL))["job_id"]
    finish(desk)
    before = desk.detail(job)
    assert before["summary"]["status"] == "COMPLETED", before["job"]
    assert before["summary"]["schema_version"] == 8
    assert before["summary"]["source"]["url"] == URL
    assert before["safe_diff"] == desk.snapshot(job)["snapshot"]["safe_diff"]
    desk.resume(job, ResumeRequest(action_id=str(uuid4())))
    finish(desk)
    assert desk.detail(job)["summary"] == before["summary"]
    assert len(source_calls) == len(sends) == 1


def test_observer_failure_cannot_change_model_outcome(desk, monkeypatch):
    capture = desk.capture
    counter = 0

    def fail_after_initial(job):
        nonlocal counter
        counter += 1
        if counter == 4:
            raise AgentError("OBSERVATION_FAILED")
        capture(job)

    monkeypatch.setattr(desk, "capture", fail_after_initial)
    job = desk.submit(submission("finding"))["job_id"]
    finish(desk)
    assert desk.detail(job)["summary"]["status"] == "COMPLETED"
    assert desk.detail(job)["view"]["marked_sends"] == 1


def request(desk, path, *, body=None, headers=None, method="GET"):
    """Exercise the full HTTP parser/router without binding a socket in unit tests."""
    output = BytesIO()
    header_values = {"Host": "127.0.0.1:8765", "X-Workbench-Token": "test-session-value"}
    if method == "POST":
        header_values |= {"Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"}
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    if method == "POST":
        header_values["Content-Length"] = str(len(raw))
    header_values.update(headers or {})
    head = f"{method} {path} HTTP/1.0\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in header_values.items() if v is not None
    )

    class InMemoryHandler(Handler):
        def setup(self):
            self.rfile = BytesIO(head.encode() + b"\r\n" + raw)
            self.wfile = output

        def finish(self):
            pass

    server = SimpleNamespace(
        origin="http://127.0.0.1:8765", token="test-session-value", workbench=desk,
        cookie_name="review_workbench_8765",
    )
    InMemoryHandler(None, ("127.0.0.1", 12345), server)
    result = output.getvalue()
    head, payload = result.split(b"\r\n\r\n", 1)
    return int(head.split()[1]), head, payload


@pytest.mark.parametrize("headers,status", [
    ({"Host": "evil.example:8765"}, 403), ({"Origin": "https://evil.example"}, 403),
    ({"Origin": None}, 403), ({"X-Workbench-Token": "wrong"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Type": "text/plain"}, 415),
    ({"Content-Length": "99999999"}, 413), ({"Transfer-Encoding": "chunked"}, 400),
])
def test_http_mutation_boundaries(desk, headers, status):
    actual, _, _ = request(desk, "/api/jobs", body=submission().model_dump(),
                           headers=headers, method="POST")
    assert actual == status
    assert desk.list_jobs()["jobs"] == []


@pytest.mark.parametrize("body", [
    b'{"submission_id":"first","submission_id":"second"}', b'{',
    b'{"content":"\\ud800"}', b'[]',
])
def test_bad_json_is_rejected_without_echo(desk, body):
    status, _, payload = request(desk, "/api/jobs", body=body, method="POST")
    assert status == 400 and json.loads(payload) == {"error": "INVALID_INPUT"}


def test_http_create_poll_download_and_static_csp(desk):
    code, headers, body = request(desk, "/")
    assert code == 200 and b"script-src 'self'" in headers and b"app.js" in body
    code, headers, body = request(desk, "/api/bootstrap", headers={"X-Workbench-Token": None})
    assert code == 200 and not json.loads(body)["live_enabled"]
    assert b"HttpOnly; SameSite=Strict" in headers
    assert request(desk, "/api/jobs", headers={"X-Workbench-Token": None})[0] == 403
    code, _, body = request(
        desk, "/api/jobs", body=submission("finding").model_dump(), method="POST"
    )
    assert code == 202
    job = json.loads(body)["job_id"]
    finish(desk)
    code, _, body = request(desk, f"/api/jobs/{job}")
    assert code == 200 and json.loads(body)["summary"]["status"] == "COMPLETED"
    code, headers, body = request(desk, f"/api/jobs/{job}/report.md")
    assert code == 200 and b"attachment" in headers and b"COMPLETED" in body
    assert request(desk, "/api/jobs/../../private")[0] == 404
    assert request(desk, "/../../pyproject.toml")[0] == 404


def test_download_cookie_cannot_authorize_mutations_or_cross_site_reads(desk):
    job = desk.submit(submission("finding"))["job_id"]
    finish(desk)
    cookie = {"X-Workbench-Token": None, "Cookie": "review_workbench_8765=test-session-value"}
    assert request(desk, f"/api/jobs/{job}/report.html", headers=cookie)[0] == 200
    assert request(desk, f"/api/jobs/{job}", headers=cookie)[0] == 403
    assert request(desk, f"/api/jobs/{job}/report.html", headers=cookie | {
        "Origin": "https://evil.example"
    })[0] == 403
    assert request(desk, "/api/jobs", body=submission().model_dump(), headers=cookie,
                   method="POST")[0] == 403


def test_api_settings_are_memory_only_no_validation_call_or_echo(desk, monkeypatch):
    import os

    from review_agent.workbench.service import CredentialSettings

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    value = CredentialSettings(action="save", api_key=API_KEY)
    assert API_KEY not in repr(value) and "api_key" not in value.model_dump()
    for _ in range(2):
        status, headers, body = request(desk, "/api/settings", method="POST", body={
            "action": "save", "api_key": API_KEY,
        })
        assert status == 200 and json.loads(body)["credential_source"] == "memory"
        assert API_KEY.encode() not in headers + body
    assert desk.active is None and desk.list_jobs()["jobs"] == []
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert request(desk, "/api/bootstrap")[0] == 200
    assert API_KEY.encode() not in request(desk, "/api/settings")[2]
    for p in desk.root.rglob("*"):
        if p.is_file():
            assert API_KEY.encode() not in p.read_bytes()
    assert request(desk, "/api/settings", method="POST", body={"action": "clear"})[0] == 200
    assert not desk.settings()["live_enabled"]
    with pytest.raises(APIError, match="LIVE_DISABLED"):
        desk.submit(submission(mode="deepseek"))


@pytest.mark.parametrize("body", [
    {"action": "save", "api_key": "short"},
    {"action": "save", "api_key": API_KEY + "\n"},
    {"action": "save", "api_key": "a" * 513},
    {"action": "save", "api_key": 123456789},
    {"action": "clear", "api_key": API_KEY},
    {"action": "save", "api_key": API_KEY, "endpoint": "https://evil.example"},
    b'{"action":"save","api_key":"first-value","api_key":"second-value"}',
])
def test_settings_reject_invalid_credentials_without_reflection(desk, body):
    status, _, result = request(desk, "/api/settings", method="POST", body=body)
    assert status == 400 and json.loads(result) == {"error": "INVALID_INPUT"}
    assert not desk.settings()["live_enabled"]


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Host": "evil.example:8765"},
    {"X-Workbench-Token": None, "Cookie": "review_workbench_8765=test-session-value"},
    {"Origin": None}, {"Sec-Fetch-Site": "cross-site"},
])
def test_settings_requires_same_origin_and_session_header(desk, headers):
    status, _, body = request(desk, "/api/settings", method="POST", headers=headers,
                              body={"action": "save", "api_key": API_KEY})
    assert status == 403 and API_KEY.encode() not in body
    assert not desk.settings()["live_enabled"]


def test_settings_size_limit_and_read_auth(desk):
    assert request(desk, "/api/settings", method="POST", body=b" " * 4097)[0] == 413
    assert request(desk, "/api/settings", headers={"X-Workbench-Token": None})[0] == 403


@pytest.mark.parametrize("leaked_reply", [False, True])
def test_memory_key_used_by_core_input_safety_repair_and_transport(desk, monkeypatch, leaked_reply):
    import os

    from review_agent.workbench.service import CredentialSettings

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    desk.configure(CredentialSettings(action="save", api_key=API_KEY))
    captured = []

    def handler(req):
        assert req.headers["Authorization"] == f"Bearer {API_KEY}"
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert API_KEY.encode() not in req.content
        captured.append(req.content)
        if len(captured) == 1:
            # Invalid but received response must be persisted, then repaired normally.
            return httpx.Response(200, json=response(
                body="{malformed " + (API_KEY if leaked_reply else "payload")
            ))
        return httpx.Response(200, json=response(body='{"action":"submit_review","findings":[]}'))

    def provider(config, key, **kwargs):
        assert key == API_KEY
        return DeepSeekProvider(config, key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(app, "DeepSeekProvider", provider)
    job = desk.submit(submission(mode="deepseek", content=added_diff([
        f'# embedded credential {API_KEY}', "result = 1",
    ])))["job_id"]
    finish(desk)
    detail = desk.detail(job)
    assert detail["summary"]["status"] == ("BLOCKED_SECURITY" if leaked_reply else "COMPLETED")
    assert detail["redactions"] == 1 and len(captured) == (1 if leaked_reply else 2)
    assert any(n["kind"] == "REPAIR" for n in detail["view"]["nodes"]) is not leaked_reply
    assert detail["summary"]["totals"]["settled_cost_nusd"] > 0
    assert API_KEY not in json.dumps(detail)
    for kind in ("trace.json", "report.html", "report.md"):
        assert API_KEY.encode() not in desk.download(job, kind)[0]
    for p in desk.root.rglob("*"):
        if p.is_file():
            assert API_KEY.encode() not in p.read_bytes(), p.name


def test_settings_cannot_rotate_credential_during_a_task(desk, monkeypatch):
    from review_agent.workbench.service import CredentialSettings

    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise AgentError("TEST_STOP")

    monkeypatch.setattr(app, "create_task", blocked)
    desk.configure(CredentialSettings(action="save", api_key=API_KEY))
    try:
        desk.submit(submission(mode="deepseek"))
        assert entered.wait(10)
        for body in ({"action": "save", "api_key": "replacement-test-key"}, {"action": "clear"}):
            assert request(desk, "/api/settings", method="POST", body=body)[0] == 409
        assert desk.settings()["credential_source"] == "memory"
    finally:
        release.set()
    finish(desk)


def test_restart_requires_reentering_key_retry_preserves_held(tmp_path, monkeypatch):
    from review_agent.workbench.service import CredentialSettings

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    sent = []

    def handler(req):
        assert req.headers["Authorization"] == f"Bearer {API_KEY}"
        sent.append(req.content)
        if len(sent) == 1:
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json=response(body='{"action":"submit_review","findings":[]}'))

    monkeypatch.setattr(app, "DeepSeekProvider", lambda config, key, **kw: DeepSeekProvider(
        config, key, transport=httpx.MockTransport(handler), **kw,
    ))
    root = tmp_path / "restart-key"
    first = Workbench(root)
    first.configure(CredentialSettings(action="save", api_key=API_KEY))
    try:
        job = first.submit(submission(mode="deepseek"))["job_id"]
        finish(first)
        before = first.detail(job)
        assert before["summary"]["status"] == "PAUSED_UNKNOWN"
        held = before["summary"]["totals"]["held_cost_nusd"]
        unknown = before["summary"]["attempts"][0]["attempt_id"]
    finally:
        first.close()
    resumed = Workbench(root)
    try:
        assert not resumed.settings()["live_enabled"] and len(sent) == 1
        retry = ResumeRequest(action_id=str(uuid4()), retry_unknown=unknown)
        with pytest.raises(APIError, match="LIVE_DISABLED"):
            resumed.resume(job, retry)
        assert resumed.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        resumed.configure(CredentialSettings(action="save", api_key=API_KEY))
        resumed.resume(job, retry)
        finish(resumed)
        result = resumed.detail(job)
        assert result["summary"]["status"] == "COMPLETED" and len(sent) == 2
        assert result["summary"]["totals"]["held_cost_nusd"] == held > 0
        assert result["summary"]["totals"]["settled_cost_nusd"] > 0
        resumed.resume(job, retry)
        assert len(sent) == 2 and resumed.active is None
    finally:
        resumed.close()
