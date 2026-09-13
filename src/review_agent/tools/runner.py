import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from review_agent.contracts import AgentError, json_text
from review_agent.safety import Safety
from review_agent.tools.schema import validate


def outcome(status, code=None, records=None):
    return {
        "status": status,
        "error_code": code,
        "records": records or [],
        "complete": status == "SUCCEEDED",
    }


class ToolRunner:
    def __init__(self, registry, safety=None):
        self.registry, self.safety = registry, safety or Safety()

    def run(self, name, arguments, snapshot, unit, frozen):
        self.registry.require_snapshot(frozen)
        if name not in self.registry.entries:
            return outcome("REJECTED", "TOOL_NOT_FOUND")
        spec = self.registry.entries[name]["spec"]
        try:
            self.safety.require_safe(arguments)
            validate(spec["input_schema"], arguments)
        except AgentError:
            return outcome("REJECTED", "TOOL_ARGUMENTS_REJECTED")
        # Do not even transmit unrelated hunk bodies to the worker.
        scoped = {
            "files": snapshot["files"] if "snapshot.files" in spec["permissions"] else [],
            "hunks": [
                h
                for h in snapshot["hunks"]
                if h["hunk_id"] in unit["hunk_ids"] and "unit.hunks" in spec["permissions"]
            ],
        }
        self.safety.require_safe(scoped)
        payload = json_text(
            {
                "name": name,
                "arguments": arguments,
                "snapshot": scoped,
                "unit": {"hunk_ids": unit["hunk_ids"]},
                "registry": frozen,
            }
        ).encode()
        if len(payload) > 4 * 1024 * 1024:
            return outcome("FAILED", "TOOL_INPUT_LIMIT")
        return self._execute(payload, spec)

    def _execute(self, payload, spec):
        deadline = time.monotonic() + spec["timeout_ms"] / 1000
        records, buffer, used, offset, terminal = [], bytearray(), 0, 0, None
        with tempfile.TemporaryDirectory(prefix="review-tool-") as cwd:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).with_name("worker.py")),
                    str(self.registry.root),
                    str(os.getpid()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env={"PYTHONIOENCODING": "utf-8"},
            )
            try:
                with selectors.DefaultSelector() as selector:
                    for pipe, mode in (
                        (process.stdin, selectors.EVENT_WRITE),
                        (process.stdout, selectors.EVENT_READ),
                    ):
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, mode)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return outcome("TIMED_OUT", "TOOL_TIMEOUT", records)
                        for key, _ in selector.select(min(remaining, 0.05)):
                            if key.fileobj is process.stdin:
                                try:
                                    offset += os.write(
                                        process.stdin.fileno(), payload[offset : offset + 4096]
                                    )
                                except BrokenPipeError:
                                    offset = len(payload)
                                if offset == len(payload):
                                    selector.unregister(process.stdin)
                                    process.stdin.close()
                                continue
                            chunk = os.read(
                                process.stdout.fileno(),
                                min(4096, spec["max_output_bytes"] - used + 1),
                            )
                            if not chunk:
                                if buffer or terminal is None:
                                    return outcome("FAILED", "TOOL_EXECUTION_FAILED", records)
                                return terminal
                            # Parse complete records within the byte budget even when this
                            # read also contains the extra byte used to detect overflow.
                            buffer.extend(chunk[: spec["max_output_bytes"] - used])
                            used += len(chunk)
                            while b"\n" in buffer:
                                line, _, rest = buffer.partition(b"\n")
                                buffer = bytearray(rest)
                                try:
                                    value = json.loads(line)
                                    if terminal is not None:
                                        raise ValueError
                                    if isinstance(value, dict) and set(value) == {"record"}:
                                        validate(spec["output_schema"], value["record"])
                                        self.safety.require_safe(value["record"])
                                        records.append(value["record"])
                                    elif value == {"done": True}:
                                        terminal = outcome("SUCCEEDED", records=records)
                                    elif isinstance(value, dict) and set(value) == {"error"}:
                                        code = value["error"]
                                        status = {
                                            "TOOL_PERMISSION_DENIED": "REJECTED",
                                            "TOOL_REFERENCE_DENIED": "REJECTED",
                                            "TOOL_OUTPUT_LIMIT": "TRUNCATED",
                                            "TOOL_SCHEMA_REJECTED": "FAILED",
                                            "TOOL_EXECUTION_FAILED": "FAILED",
                                        }.get(code)
                                        if status is None:
                                            raise ValueError
                                        terminal = outcome(status, code, records)
                                    else:
                                        raise ValueError
                                except AgentError as error:
                                    if error.code == "TOOL_SCHEMA_REJECTED":
                                        return outcome("FAILED", "TOOL_OUTPUT_SCHEMA_REJECTED")
                                    return outcome("BLOCKED_SECURITY", "UNSAFE_TOOL_OUTPUT")
                                except (ValueError, TypeError, UnicodeError):
                                    return outcome("FAILED", "TOOL_PROTOCOL_ERROR")
                            if used > spec["max_output_bytes"]:
                                return outcome("TRUNCATED", "TOOL_OUTPUT_LIMIT", records)
            finally:
                # The worker denies spawning descendants. Reap on every exit path.
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdin, process.stdout):
                    pipe.close()
