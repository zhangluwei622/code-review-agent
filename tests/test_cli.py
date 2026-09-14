import json
import os
import subprocess
import sys

from conftest import ROOT
from filelock import FileLock


def cli(*args):
    env = {**os.environ, "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}
    return subprocess.run(
        [sys.executable, "-m", "review_agent.cli", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_cli_review_resume_read_commands(tmp_path):
    state, output = tmp_path / "state", tmp_path / "report.md"
    run = cli(
        "review",
        "--diff",
        ROOT / "examples/diffs/empty-list.diff",
        "--fixture",
        ROOT / "examples/provider/success.json",
        "--max-tokens",
        "2000",
        "--max-cost-usd",
        "0.01",
        "--state-dir",
        state,
        "--output",
        output,
    )
    assert run.returncode == 0, run.stderr
    data = json.loads(run.stdout)
    assert output.exists() and data["status"] == "COMPLETED"
    assert "# 代码审阅报告" in output.read_text()
    assert "调用账本" not in output.read_text()
    audit = output.with_name("report.audit.md")
    assert data["audit_report_written"] and "调用账本" in audit.read_text()
    for command in ("resume", "status", "report", "trace"):
        args = [command, "--task", data["task_id"], "--state-dir", state]
        if command == "trace":
            args += ["--finding", data["findings"][0]]
        result = cli(*args)
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        if command != "trace":
            assert parsed["totals"]["settled_tokens"] == 300
        if command in ("resume", "report"):
            assert (state / data["task_id"] / "report.audit.md").is_file()


def test_task_lock_rejects_second_process(harness):
    harness.create()
    directory = harness.state / harness.task_id
    with FileLock(str(directory / "task.lock"), timeout=0):
        result = cli("resume", "--task", harness.task_id, "--state-dir", harness.state)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "TASK_LOCKED"
    assert not harness.read()["attempts"]


def test_invalid_provider_does_not_fall_back(tmp_path):
    result = cli("review", "--provider", "real-secret-input", "--state-dir", tmp_path)
    assert result.returncode == 1
    assert "real-secret-input" not in result.stderr
    assert "INVALID_ARGUMENTS" in result.stderr


def test_deepseek_missing_key_does_not_create_task_or_fall_back(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = cli(
        "review",
        "--provider",
        "deepseek",
        "--diff",
        ROOT / "examples/diffs/empty-list.diff",
        "--max-tokens",
        "20000",
        "--max-cost-usd",
        "0.01",
        "--state-dir",
        tmp_path / "state",
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "PROVIDER_CREDENTIAL_MISSING"
    assert not (tmp_path / "state").exists()


def test_invalid_task_id_is_not_echoed(tmp_path):
    secret = "sk-" + "SyntheticOnly" * 3
    result = cli("status", "--task", secret, "--state-dir", tmp_path)
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr
    assert json.loads(result.stderr)["task_id"] is None
