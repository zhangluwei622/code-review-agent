import os
import selectors
import signal
import sqlite3
import subprocess
import sys

import pytest
from conftest import ROOT
from test_cli import cli
from test_repair import repair_spec
from test_retry import retry_spec

from review_agent import app

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGKILL suite requires POSIX")


def worker(harness, calls, *, point=None, ordinal=1, retry=None, tool_calls=None):
    command = [
        sys.executable,
        "-u",
        str(ROOT / "tests/kill_worker.py"),
        "--state",
        str(harness.state),
        "--task",
        harness.task_id,
        "--calls",
        str(calls),
    ]
    if point:
        command += ["--point", point, "--ordinal", str(ordinal)]
    if retry:
        command += ["--retry-unknown", retry]
    if tool_calls:
        command += ["--tool-calls", str(tool_calls)]
    # Explicit allowlist: no real credentials or remote tracing configuration inherited.
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        if point:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=20), "worker did not reach deterministic barrier"
            assert process.stdout.readline().strip() == "BARRIER", "worker exited before barrier"
            locked = cli("resume", "--task", harness.task_id, "--state-dir", harness.state)
            assert locked.returncode == 1 and "TASK_LOCKED" in locked.stderr
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            assert process.returncode == -signal.SIGKILL
        else:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            assert stdout.strip()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def sends(calls):
    return len(calls.read_text().splitlines()) if calls.exists() else 0


@pytest.mark.parametrize(
    "point,status,count",
    [
        ("after_request", "COMPLETED", 1),
        ("after_reserved", "COMPLETED", 1),
        ("before_dispatch_commit", "COMPLETED", 1),
        ("after_dispatched", "PAUSED_UNKNOWN", 0),
        ("after_reply", "PAUSED_UNKNOWN", 1),
        ("before_complete_commit", "PAUSED_UNKNOWN", 1),
        ("after_completed", "COMPLETED", 1),
        ("after_validation", "COMPLETED", 1),
    ],
)
def test_sigkill_review_boundaries(harness, tmp_path, point, status, count):
    harness.create()
    calls = tmp_path / "sends.jsonl"
    worker(harness, calls, point=point)
    for _ in range(2):
        worker(harness, calls)
    data = harness.read()
    assert data["task"]["status"] == status
    assert sends(calls) == count
    assert len(data["attempts"]) == 1
    assert data["units"][0]["sends"] == 1
    events = [e["event_type"] for e in data["budget_events"]]
    assert events == (["RESERVE", "SETTLE"] if status == "COMPLETED" else ["RESERVE"])
    assert data["totals"]["held_tokens"] == (0 if status == "COMPLETED" else 600)


@pytest.mark.parametrize(
    "point,status,count",
    [
        ("after_decision", "COMPLETED", 2),
        ("after_retry_attempt", "COMPLETED", 2),
        ("after_retry_reserve", "COMPLETED", 2),
        ("after_retry_binding", "COMPLETED", 2),
        ("after_retry_bound", "COMPLETED", 2),
        ("after_dispatched", "PAUSED_UNKNOWN", 1),
        ("after_reply", "PAUSED_UNKNOWN", 2),
        ("after_completed", "COMPLETED", 2),
    ],
)
def test_sigkill_retry_choice_is_consumed_once(
    harness, success_spec, tmp_path, point, status, count
):
    harness.create(spec=retry_spec(success_spec))
    calls = tmp_path / "sends.jsonl"
    worker(harness, calls)
    source = harness.read()["attempts"][0]["attempt_id"]
    worker(harness, calls, point=point, retry=source)
    decision_id = harness.read()["retry_decisions"][0]["decision_id"]
    worker(harness, calls)  # no repeated flag: persisted authorization is sufficient
    worker(harness, calls, retry=source)
    data = harness.read()
    assert data["task"]["status"] == status
    assert sends(calls) == count
    assert len(data["attempts"]) == 2
    assert len(data["retry_decisions"]) == 1
    assert data["retry_decisions"][0]["decision_id"] == decision_id
    assert data["retry_decisions"][0]["bound_attempt_id"] == data["attempts"][1]["attempt_id"]
    assert data["units"][0]["sends"] == 2
    assert len([e for e in data["budget_events"] if e["event_type"] == "RESERVE"]) == 2
    assert data["totals"]["held_tokens"] == (600 if status == "COMPLETED" else 1200)


@pytest.mark.parametrize(
    "point,ordinal,status,count",
    [
        ("after_validation", 1, "COMPLETED", 2),
        ("after_request", 2, "COMPLETED", 2),
        ("after_dispatched", 2, "PAUSED_UNKNOWN", 1),
        ("after_completed", 2, "COMPLETED", 2),
        ("before_validation_commit", 2, "COMPLETED", 2),
        ("after_validation", 2, "COMPLETED", 2),
    ],
)
def test_sigkill_repair_and_validation(
    harness, success_spec, tmp_path, point, ordinal, status, count
):
    harness.create(spec=repair_spec(success_spec), repairs=1)
    calls = tmp_path / "sends.jsonl"
    worker(harness, calls, point=point, ordinal=ordinal)
    for _ in range(2):
        worker(harness, calls)
    data = harness.read()
    assert data["task"]["status"] == status
    assert len(data["operations"]) == 2
    assert sends(calls) == count
    assert len(data["findings"]) == (1 if status == "COMPLETED" else 0)
    assert data["totals"]["settled_tokens"] == (600 if status == "COMPLETED" else 300)


@pytest.mark.parametrize("point", ["after_unknown", "after_pause"])
def test_sigkill_unknown_before_pause_checkpoint(harness, tmp_path, point):
    harness.create("timeout")
    calls = tmp_path / "sends.jsonl"
    worker(harness, calls, point=point)
    for _ in range(2):
        worker(harness, calls)
    assert harness.read()["task"]["status"] == "PAUSED_UNKNOWN"
    assert sends(calls) == 1
    assert harness.read()["totals"]["held_tokens"] == 600
