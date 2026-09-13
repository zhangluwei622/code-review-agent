import json
import sqlite3
import sys

import pytest
from test_sources_common import URL, github_file, github_http
from test_tool_loop import ZERO, reply, spec

from review_agent import app, cli
from review_agent.contracts import AgentError
from review_agent.sources.contracts import SourceError
from review_agent.sources.service import fetch_source, load_source, save_source


def source_fixture(tmp_path, rows=None):
    http, transport = github_http(rows)
    source = fetch_source(URL, http=http)
    bundle = tmp_path / "source.json"
    save_source(source, bundle)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(spec(ZERO)))
    return source, bundle, fixture, transport


def create(tmp_path, bundle, fixture, **options):
    return app.create_task(
        None,
        fixture,
        tmp_path / "state",
        source_path=bundle,
        max_tokens=100000,
        max_cost_nusd=50_000_000,
        **options,
    )


def test_source_and_local_diff_requests_are_identical(tmp_path):
    source, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    data = app.execute(task, tmp_path / "state")
    local = tmp_path / "input.diff"
    local.write_text(source.safe_diff)
    local_task = app.create_task(
        local, fixture, tmp_path / "local", max_tokens=100000, max_cost_nusd=50_000_000
    )
    local_data = app.execute(local_task, tmp_path / "local")
    request = data["artifacts"][data["operations"][0]["request_ref"]]
    local_request = local_data["artifacts"][local_data["operations"][0]["request_ref"]]
    assert request == local_request
    assert data["config"]["schema_version"] == 8
    assert data["config"]["prompt_digest"] == local_data["config"]["prompt_digest"]
    assert "source" not in request and URL not in json.dumps(request)
    trace = app.trace(data)
    assert trace["source"]["base_sha"] == source.manifest.base_sha
    output = tmp_path / "report.md"
    app.export_report(data, output)
    assert "来源快照" in output.read_text()
    assert "未接入 GitHub/GitLab URL" not in output.read_text()
    assert "GitLab 仅 mock 验收" in output.read_text()


def test_resume_uses_committed_source_without_source_file_or_network(tmp_path, monkeypatch):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    bundle.unlink()
    import review_agent.sources.service as service

    monkeypatch.setattr(service, "fetch_source", lambda *a, **k: pytest.fail("source refetched"))
    before = app.execute(task, tmp_path / "state")
    after = app.execute(task, tmp_path / "state")
    assert before["attempts"] == after["attempts"] and before["totals"] == after["totals"]
    assert before["budget_events"] == after["budget_events"]


@pytest.mark.parametrize("point", ["before_source_task_commit", "after_source_task_commit"])
def test_source_and_task_share_commit_boundary(tmp_path, point):
    source, bundle, fixture, _ = source_fixture(tmp_path)

    def fault(event):
        if event == point:
            raise AgentError("INJECTED")

    with pytest.raises(AgentError, match="INJECTED"):
        create(tmp_path, bundle, fixture, fault=fault)
    database = next((tmp_path / "state").glob("task_*/task.sqlite"))
    with sqlite3.connect(database) as db:
        tasks = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        sources = db.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
        units = db.execute("SELECT COUNT(*) FROM review_units").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='checkpoints'").fetchone()[0]
            == 0
        )
    assert tasks == sources == (1 if point == "after_source_task_commit" else 0)
    assert units == (1 if tasks else 0)
    if tasks:
        bundle.unlink()
        data = app.execute(database.parent.name, tmp_path / "state")
        assert data["source"] == source.manifest.model_dump() and len(data["attempts"]) == 1


@pytest.mark.parametrize("point", ["before_source_publish", "after_source_publish"])
def test_source_publication_is_atomic_and_never_overwrites(tmp_path, point):
    http, _ = github_http()
    source = fetch_source(URL, http=http)
    path = tmp_path / "published.json"

    def fault(event):
        if event == point:
            raise AgentError("INJECTED")

    with pytest.raises(AgentError, match="INJECTED"):
        save_source(source, path, fault=fault)
    assert path.exists() == (point == "after_source_publish")
    if path.exists():
        assert load_source(path) == source
        before = path.read_bytes()
        with pytest.raises(SourceError, match="SOURCE_OUTPUT_EXISTS"):
            save_source(source, path)
        assert path.read_bytes() == before


def test_source_manifest_and_diff_tampering_block_before_send(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    raw = json.loads(bundle.read_text())
    raw["safe_diff"] += "extra\n"
    bundle.write_text(json.dumps(raw))
    with pytest.raises(SourceError, match="SOURCE_SNAPSHOT_INTEGRITY"):
        create(tmp_path, bundle, fixture)
    assert not (tmp_path / "state").exists()


def test_persisted_source_is_immutable_and_required(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    with sqlite3.connect(app.task_path(tmp_path / "state", task)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE source_snapshots SET data='{}'")
        db.execute("DROP TRIGGER preserve_source_snapshot_update")
        db.execute("UPDATE source_snapshots SET data='{}'")
    with pytest.raises(AgentError, match="SOURCE_SNAPSHOT_INTEGRITY"):
        app.execute(task, tmp_path / "state")
    with sqlite3.connect(app.task_path(tmp_path / "state", task)) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "rows,coverage",
    [
        ([], "EMPTY"),
        ([github_file(path="doc.md", patch=None)], "EXCLUDED_ONLY"),
    ],
)
def test_empty_or_excluded_sources_send_nothing(tmp_path, rows, coverage):
    _, bundle, fixture, _ = source_fixture(tmp_path, rows)
    task = create(tmp_path, bundle, fixture)
    data = app.execute(task, tmp_path / "state")
    assert not data["attempts"] and data["source"]["coverage"] == coverage
    out = tmp_path / "report.md"
    app.export_report(data, out)
    assert "未进行模型审阅" in out.read_text()


def test_fetch_cli_only_prepares_and_does_not_construct_provider(tmp_path, monkeypatch, capsys):
    import review_agent.sources.service as service

    http, _ = github_http()
    real = service.fetch_source
    monkeypatch.setattr(service, "fetch_source", lambda url, **kw: real(url, http=http))
    monkeypatch.setattr(app, "create_task", lambda *a, **k: pytest.fail("task created"))
    monkeypatch.setattr(
        sys, "argv", ["review-agent", "fetch", "--url", URL, "--output", str(tmp_path / "s.json")]
    )
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_sends"] == 0 and output["held_tokens"] == 0
    assert load_source(tmp_path / "s.json").manifest_digest == output["source_digest"]


def test_source_bad_patch_stops_before_task_creation(tmp_path):
    _, _, fixture, _ = source_fixture(tmp_path)
    http, _ = github_http([github_file(patch=None)])
    with pytest.raises(SourceError, match="SOURCE_PATCH_MISSING"):
        app.create_task(
            None,
            fixture,
            tmp_path / "state",
            source_url=URL,
            source_http=http,
            max_tokens=100000,
            max_cost_nusd=50_000_000,
        )
    assert not (tmp_path / "state").exists()


def test_source_v8_retains_strict_reply_protocol_and_bound_repair(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    broken = ZERO["body"][:-1] + ',"findings":[]}'
    fixture.write_text(
        json.dumps({"responses": {"0:0:REVIEW:1": reply(broken), "0:0:REPAIR:1": ZERO}})
    )
    task = create(tmp_path, bundle, fixture)
    data = app.execute(task, tmp_path / "state")
    assert len(data["attempts"]) == 2
    assert data["attempts"][0]["result_status"] == "FORMAT_INVALID"
    assert data["attempts"][0]["fee_status"] == "SETTLED"
    assert data["operation_contexts"][1]["source_result_ref"] == data["attempts"][0]["result_ref"]
    assert data["config"]["reply_protocol"] == "json-unique-keys-v1"
    before = data["totals"]
    data = app.execute(task, tmp_path / "state")
    assert data["totals"] == before


def test_url_input_creates_same_bound_task_as_source_file(tmp_path):
    _, _, fixture, _ = source_fixture(tmp_path)
    http, transport = github_http()
    task = app.create_task(
        None,
        fixture,
        tmp_path / "state",
        source_url=URL,
        source_http=http,
        max_tokens=100000,
        max_cost_nusd=50_000_000,
    )
    data = app.execute(task, tmp_path / "state")
    assert len(transport.requests) == 5 and len(data["attempts"]) == 1
    assert data["source"]["url"] == URL


def test_source_units_and_snapshot_must_match_bound_safe_diff(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    path = app.task_path(tmp_path / "state", task)
    with sqlite3.connect(path) as db:
        row = json.loads(db.execute("SELECT data FROM snapshots").fetchone()[0])
        row["hunks"][0]["lines"][0]["text"] = "forged context\n"
        db.execute("UPDATE snapshots SET data=?", (json.dumps(row),))
    with pytest.raises(AgentError, match="SOURCE_SNAPSHOT_INTEGRITY"):
        app.execute(task, tmp_path / "state")


def test_source_unknown_retains_held_without_refetch_or_retry(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    fixture.write_text(json.dumps(spec({"error": "timeout"})))
    task = create(tmp_path, bundle, fixture)
    first = app.execute(task, tmp_path / "state")
    bundle.unlink()
    second = app.execute(task, tmp_path / "state")
    assert first["task"]["status"] == "PAUSED_UNKNOWN"
    assert first["totals"]["held_tokens"] > 0 and first["totals"] == second["totals"]
    assert first["attempts"] == second["attempts"] and len(second["attempts"]) == 1


def test_source_budget_pause_persists_without_new_reservations(tmp_path):
    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = app.create_task(
        None, fixture, tmp_path / "state", source_path=bundle, max_tokens=10, max_cost_nusd=10
    )
    first = app.execute(task, tmp_path / "state")
    second = app.execute(task, tmp_path / "state")
    assert first["task"]["status"] == "PAUSED_BUDGET"
    assert not first["attempts"] and first["totals"] == second["totals"]


@pytest.mark.parametrize("point", ["after_completed", "after_validation"])
def test_source_checkpoint_replay_after_real_sigkill(tmp_path, point):
    from types import SimpleNamespace

    from test_process_recovery import worker

    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    harness = SimpleNamespace(task_id=task, state=tmp_path / "state")
    calls = tmp_path / "calls.jsonl"
    worker(harness, calls, point=point)
    bundle.unlink()
    before = app.read_task(task, tmp_path / "state")
    worker(harness, calls)
    after = app.read_task(task, tmp_path / "state")
    assert len(calls.read_text().splitlines()) == 1
    assert before["totals"] == after["totals"]
    assert before["attempts"] == after["attempts"]
    assert after["task"]["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "mode,point,committed",
    [
        ("create", "before_source_task_commit", False),
        ("create", "after_source_task_commit", True),
        ("publish", "before_source_publish", False),
        ("publish", "after_source_publish", True),
    ],
)
def test_actual_sigkill_source_publication_and_task_creation(tmp_path, mode, point, committed):
    import os
    import signal
    import subprocess

    from conftest import ROOT

    source, bundle, fixture, _ = source_fixture(tmp_path)
    state = tmp_path / "crash-state"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/source_create_worker.py"),
            mode,
            point,
            str(bundle),
            str(fixture),
            str(state),
        ],
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    if mode == "publish":
        assert (state / "published.json").exists() == committed
        if committed:
            assert load_source(state / "published.json") == source
    else:
        database = next(state.glob("task_*/task.sqlite"))
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == int(committed)
            assert db.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0] == int(
                committed
            )
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        if committed:
            bundle.unlink()
            data = app.execute(database.parent.name, state)
            assert data["task"]["status"] == "COMPLETED" and len(data["attempts"]) == 1


def test_existing_fetch_output_stops_before_network(tmp_path, monkeypatch, capsys):
    import review_agent.sources.service as service

    path = tmp_path / "exists.json"
    path.write_text("preserve")
    monkeypatch.setattr(service, "fetch_source", lambda *a, **k: pytest.fail("unnecessary fetch"))
    monkeypatch.setattr(sys, "argv", ["review-agent", "fetch", "--url", URL, "--output", str(path)])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().err)["error"] == "SOURCE_OUTPUT_EXISTS"
    assert path.read_text() == "preserve"


def test_historical_source_read_does_not_require_current_policy_version(tmp_path, monkeypatch):
    import review_agent.sources.service as service

    _, bundle, fixture, _ = source_fixture(tmp_path)
    task = create(tmp_path, bundle, fixture)
    before = app.read_task(task, tmp_path / "state")
    monkeypatch.setattr(service, "policy_versions", lambda: {"policy_digest": "0" * 64})
    assert app.read_task(task, tmp_path / "state") == before
    with pytest.raises(SourceError, match="SOURCE_SNAPSHOT_INTEGRITY"):
        load_source(bundle)
