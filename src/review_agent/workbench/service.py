"""Job orchestration and observation, separate from the core task ledger.

Raw inputs stay in memory until app.create_task runs the existing safety boundary.
Observations are committed facts with observation times, not invented graph events.
"""

import html
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from filelock import FileLock, Timeout
from pydantic import Field, SecretStr, model_validator

from review_agent import app
from review_agent.config import usd_to_nusd
from review_agent.contracts import AgentError, StrictModel, digest, json_text
from review_agent.report import render_audit, render_review, review_scope_note
from review_agent.safety import Safety
from review_agent.viewer.export import render_html
from review_agent.viewer.projection import project

DEMOS = {
    "finding": ("发现问题", "empty-list.diff", "success.json"),
    "tools": ("工具与格式修复", "phase-4-context.diff", "phase-4-tools.json"),
    "repair": ("格式修复", "empty-list.diff", "phase-3-repair.json"),
    "retry": ("未知结果与人工重试", "phase-4-context.diff", "phase-4-tool-retry.json"),
}
COMMITTED_HOOKS = {
    "after_request", "after_reserved", "after_dispatched", "after_completed",
    "after_validation", "after_pause", "after_tool_registered", "after_tool_running",
    "after_tool_completed",
}
IDENTIFIER = r"^[a-zA-Z0-9_-]{16,64}$"


def now():
    return datetime.now(UTC).isoformat()


def demo_text(name, index):
    return files("review_agent.workbench").joinpath("demo", DEMOS[name][index]).read_text()


class APIError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class Submission(StrictModel):
    submission_id: str = Field(pattern=IDENTIFIER)
    mode: str = Field(pattern=r"^(fixture|deepseek)$")
    input_type: str = Field(pattern=r"^(diff|url)$")
    content: str = Field(min_length=1, max_length=1048576)
    demo: str = Field(default="tools", pattern=r"^(finding|tools|repair|retry)$")
    max_tokens: int = Field(default=100000, ge=1, le=1000000)
    max_cost_usd: str = Field(default="0.05", pattern=r"^\d{1,3}(\.\d{1,9})?$")
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < usd_to_nusd(self.max_cost_usd) <= 1000000000:
            raise ValueError("INVALID_BUDGET")
        if len(self.content.encode("utf-8")) > 1048576:
            raise ValueError("INPUT_TOO_LARGE")
        return self


class ResumeRequest(StrictModel):
    action_id: str = Field(pattern=IDENTIFIER)
    retry_unknown: str | None = Field(default=None, pattern=r"^attempt_[0-9a-f]{24}$")


class CredentialSettings(StrictModel):
    action: str = Field(pattern=r"^(save|clear)$")
    api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)

    @model_validator(mode="after")
    def validate_key(self):
        value = self.api_key.get_secret_value() if self.api_key is not None else None
        if self.action == "save":
            if value is None or not re.fullmatch(r"[!-~]{8,512}", value):
                raise ValueError("INVALID_CREDENTIAL")
        elif value is not None:
            raise ValueError("INVALID_CREDENTIAL")
        return self


class Workbench:
    def __init__(self, root: Path, *, allow_live=False):
        self.root = root.resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.process_lock = FileLock(str(self.root / "workbench.lock"), timeout=0, mode=0o600)
        try:
            self.process_lock.acquire()
        except Timeout:
            raise APIError("WORKBENCH_ALREADY_RUNNING", 409) from None
        self.allow_live = allow_live
        self._api_key = None
        self.lock = threading.RLock()
        self.active = None
        self.worker = None
        path = self.root / "workbench.sqlite"
        if path.is_symlink():
            self.process_lock.release()
            raise APIError("INVALID_STATE_DIRECTORY")
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, mode TEXT NOT NULL, input_type TEXT NOT NULL,
                demo TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                runner TEXT NOT NULL, task_id TEXT, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, retry_unknown TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                identity TEXT NOT NULL, observed_at TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE(job_id, identity)
            );
        """)
        # Restart only marks the UI runner interrupted. It never resumes the Agent.
        for row in self.db.execute("SELECT * FROM jobs WHERE runner IN ('PREPARING','RUNNING')"):
            job_id, task_id = row["job_id"], row["task_id"]
            if not task_id:
                candidates = list(self.state(job_id).glob("task_*/task.sqlite"))
                if len(candidates) == 1:
                    try:
                        app.read_task(candidates[0].parent.name, self.state(job_id))
                        task_id = candidates[0].parent.name
                    except Exception:
                        pass
            self.db.execute(
                "UPDATE jobs SET runner='INTERRUPTED',task_id=?,updated_at=? WHERE job_id=?",
                (task_id, now(), job_id),
            )
            self.event(job_id, "interrupted:" + now(), {
                "kind": "RUNNER", "status": "INTERRUPTED", "label": "服务重启，等待明确恢复",
            })

    def close(self):
        if self.worker:
            self.worker.join()
        self._api_key = None
        self.db.close()
        self.process_lock.release()

    def settings(self):
        with self.lock:
            return {
                "live_enabled": self._api_key is not None or self.allow_live,
                "credential_source": (
                    "memory" if self._api_key is not None
                    else "environment" if self.allow_live else "none"
                ),
                "provider": "DeepSeek", "model": "deepseek-flash",
                "endpoint": "https://api.deepseek.com/chat/completions",
            }

    def configure(self, request: CredentialSettings):
        with self.lock:
            # A running task must keep the credential used to sanitize its input.
            if self.active:
                raise APIError("WORKBENCH_BUSY", 409)
            self._api_key = request.api_key if request.action == "save" else None
            if request.action == "clear":
                self.allow_live = False
            return self.settings()

    def state(self, job_id):
        if not re.fullmatch(IDENTIFIER, job_id):
            raise APIError("JOB_NOT_FOUND", 404)
        path = self.root / "jobs" / job_id
        if path.is_symlink() or path.parent.is_symlink():
            raise APIError("INVALID_STATE_DIRECTORY")
        return path

    def job(self, job_id):
        self.state(job_id)
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise APIError("JOB_NOT_FOUND", 404)
        return dict(row)

    def event(self, job_id, identity, data):
        Safety().require_safe(data)
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO observations(job_id,identity,observed_at,data) "
                "VALUES (?,?,?,?)", (job_id, identity, now(), json_text(data)),
            )

    def update(self, job_id, runner, *, task_id=None, error=None):
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET runner=?,task_id=COALESCE(?,task_id),error_code=?,"
                "updated_at=? WHERE job_id=?", (runner, task_id, error, now(), job_id),
            )

    def submit(self, request: Submission):
        with self.lock:
            existing = self.db.execute(
                "SELECT job_id FROM jobs WHERE job_id=?", (request.submission_id,)
            ).fetchone()
            if existing:
                return self.job(request.submission_id)  # Network replay reuses the original job.
            if self.active:
                raise APIError("WORKBENCH_BUSY", 409)
            if request.mode == "deepseek" and not self.settings()["live_enabled"]:
                raise APIError("LIVE_DISABLED", 403)
            if request.mode == "fixture" and (
                request.input_type != "diff" or request.content != demo_text(request.demo, 1)
            ):
                raise APIError("DEMO_INPUT_CHANGED")
            job_id = request.submission_id
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,'PREPARING',NULL,NULL)",
                (job_id, request.mode, request.input_type, request.demo, now(), now()),
            )
            self.event(job_id, "accepted", {
                "kind": "RUNNER", "status": "PREPARING", "label": "已接收，检查输入与冻结配置",
            })
            self.launch(job_id, request=request)
            return self.job(job_id)

    def resume(self, job_id, request: ResumeRequest):
        with self.lock:
            job = self.job(job_id)
            previous = self.db.execute(
                "SELECT * FROM actions WHERE action_id=?", (request.action_id,)
            ).fetchone()
            if previous:
                if (previous["job_id"], previous["retry_unknown"]) != (
                    job_id, request.retry_unknown
                ):
                    raise APIError("ACTION_CONFLICT", 409)
                return job
            if self.active:
                raise APIError("WORKBENCH_BUSY", 409)
            if not job["task_id"]:
                raise APIError("INPUT_NOT_COMMITTED", 409)
            if job["mode"] == "deepseek" and not self.settings()["live_enabled"]:
                raise APIError("LIVE_DISABLED", 403)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO actions VALUES (?,?,?)", (
                    request.action_id, job_id, request.retry_unknown,
                ))
                self.update(job_id, "RUNNING")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            self.event(job_id, "action:" + request.action_id, {
                "kind": "RUNNER", "status": "RESUME_REQUESTED", "label": "用户请求恢复任务",
                "retry_unknown": request.retry_unknown,
            })
            self.launch(job_id, retry_unknown=request.retry_unknown)
            return self.job(job_id)

    def launch(self, job_id, *, request=None, retry_unknown=None):
        self.active = job_id
        self.worker = threading.Thread(
            target=self.run, args=(job_id, request, retry_unknown, self._api_key),
            name="review-workbench",
        )
        self.worker.start()

    def run(self, job_id, request, retry_unknown, credential=None):
        try:
            api_key = credential.get_secret_value() if credential is not None else None
            if request:
                fixture = (
                    Path(str(files("review_agent.workbench").joinpath(
                        "demo", DEMOS[request.demo][2]
                    ))) if request.mode == "fixture" else None
                )
                task_id = app.create_task(
                    None, fixture, self.state(job_id),
                    diff_text=request.content if request.input_type == "diff" else None,
                    source_url=request.content if request.input_type == "url" else None,
                    provider_name=request.mode, max_tokens=request.max_tokens,
                    max_cost_nusd=usd_to_nusd(request.max_cost_usd),
                    max_output_tokens=request.max_output_tokens,
                    api_key=api_key,
                )
                self.update(job_id, "RUNNING", task_id=task_id)
                request = None  # No raw input in job metadata or observations.
                self.capture(job_id)
            else:
                task_id = self.job(job_id)["task_id"]

            def observe(marker):
                if marker in COMMITTED_HOOKS:
                    try:
                        self.capture(job_id)
                    except Exception:
                        # An observer never decides execution or accounting outcomes.
                        try:
                            self.event(job_id, "observation-gap", {
                                "kind": "OBSERVATION", "status": "GAP",
                                "label": "部分运行观察不可用，以已提交的任务记录为准",
                            })
                        except Exception:
                            pass

            app.execute(
                task_id, self.state(job_id), retry_unknown=retry_unknown, fault=observe,
                api_key=api_key,
            )
            self.capture(job_id)
            self.update(job_id, "FINISHED")
        except Exception as error:
            code = (
                error.code if isinstance(error, (AgentError, APIError)) else "WORKBENCH_RUN_FAILED"
            )
            if not re.fullmatch(r"[A-Z_]{1,100}", code):
                code = "WORKBENCH_RUN_FAILED"
            self.update(job_id, "FAILED", error=code)
            self.event(job_id, "error:" + now(), {
                "kind": "RUNNER", "status": "FAILED", "label": "执行已停止", "error_code": code,
            })
        finally:
            with self.lock:
                self.active = None

    def capture(self, job_id):
        job = self.job(job_id)
        if not job["task_id"]:
            return
        snapshot = app.read_task(job["task_id"], self.state(job_id))
        records = [("TASK", snapshot["task"]["task_id"], snapshot["task"]["status"], None)]
        contexts = {c["operation_id"]: c for c in snapshot["operation_contexts"]}
        for a in snapshot["attempts"]:
            kind = contexts.get(a["operation_id"], {}).get("kind", "REVIEW")
            records.append((kind, a["attempt_id"], a["call_status"], a["fee_status"]))
        for tool in snapshot["tool_calls"]:
            records.append(("TOOL", tool["tool_call_id"], tool["status"], None))
        for unit in snapshot["units"]:
            records.append(("UNIT", unit["unit_id"], unit["status"], unit["validation_ref"]))
        for kind, identity, status, detail in records:
            self.event(job_id, digest([kind, identity, status, detail]), {
                "kind": kind, "id": identity, "status": status, "detail": detail,
            })

    def list_jobs(self):
        with self.lock:
            jobs = [dict(r) for r in self.db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"
            )]
        return {"jobs": jobs, "active_job": self.active}

    def snapshot(self, job_id):
        job = self.job(job_id)
        if not job["task_id"]:
            return None
        return app.read_task(job["task_id"], self.state(job_id))

    def detail(self, job_id):
        job = self.job(job_id)
        events = self.events(job_id)
        value = {"job": job, "events": events, "active": self.active == job_id}
        snapshot = self.snapshot(job_id)
        if snapshot:
            trace = app.trace(snapshot)
            value.update(
                summary=app.summary(snapshot), view=project(trace),
                review_scope_note=review_scope_note(snapshot),
                files=snapshot["snapshot"]["files"], excluded=snapshot["snapshot"]["excluded"],
                hunk_paths={
                    hunk_id: unit["path"]
                    for unit in snapshot["units"] for hunk_id in unit["hunk_ids"]
                },
                safe_diff=snapshot["snapshot"]["safe_diff"],
                redactions=snapshot["snapshot"]["redactions"],
                budget={k: snapshot["config"][k] for k in (
                    "max_tokens", "max_cost_nusd", "max_output_tokens", "max_tools_per_unit",
                    "max_repairs_per_unit",
                )},
            )
        Safety().require_safe(value)
        return value

    def events(self, job_id):
        with self.lock:
            return [
                {"seq": r["seq"], "observed_at": r["observed_at"], **json.loads(r["data"])}
                for r in self.db.execute(
                    "SELECT * FROM observations WHERE job_id=? ORDER BY seq", (job_id,)
                )
            ]

    def download(self, job_id, kind):
        snapshot = self.snapshot(job_id)
        if snapshot is None:
            raise APIError("INPUT_NOT_COMMITTED", 409)
        if kind == "report.md":
            return render_review(snapshot).encode(), "text/markdown; charset=utf-8"
        if kind == "audit.md":
            return render_audit(snapshot).encode(), "text/markdown; charset=utf-8"
        trace = app.trace(snapshot)
        trace["workbench"] = {
            "job_id": job_id, "task_status": snapshot["task"]["status"],
            "runner_status": self.job(job_id)["runner"], "events": self.events(job_id),
            "time_semantics": "工作台观察时间；不是模型内部步骤耗时或完整 checkpoint 事件",
        }
        Safety().require_safe(trace)
        raw = json.dumps(trace, ensure_ascii=False, indent=2).encode()
        if kind == "trace.json":
            return raw, "application/json; charset=utf-8"
        if kind == "report.html":
            import hashlib

            observation = html.escape(json.dumps(trace["workbench"], ensure_ascii=False, indent=2))
            panel = (
                '<details class="meta-panel"><summary>工作台运行状态与观察记录</summary>'
                '<p>记录已提交状态与用户恢复操作。观察时间不代表模型内部步骤耗时。</p>'
                f'<pre>{observation}</pre></details>'
            )
            rendered = render_html(trace, hashlib.sha256(raw).hexdigest())
            return rendered.replace("</main>", panel + "</main>").encode(), "text/html"
        raise APIError("NOT_FOUND", 404)
