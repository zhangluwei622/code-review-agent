"""Isolated host-tool worker. Arguments are data, never executable target content."""

import json
import os
import sys
import threading
import time
from pathlib import Path

# -I ignores PYTHONPATH and the working directory. Only this installed package is added.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from review_agent.contracts import AgentError, digest, json_text  # noqa: E402
from review_agent.tools.registry import ToolRegistry  # noqa: E402
from review_agent.tools.schema import validate  # noqa: E402
from review_agent.tools.view import SnapshotView  # noqa: E402


def main():
    owner = int(sys.argv[2])
    if os.getppid() != owner:
        os._exit(1)

    def watch_parent():
        while True:
            if os.getppid() != owner:
                os._exit(1)
            time.sleep(0.05)

    threading.Thread(target=watch_parent, daemon=True).start()
    payload = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024 + 1))
    registry = ToolRegistry(Path(sys.argv[1]))
    registry.require_snapshot(payload["registry"])
    entry = registry.entries[payload["name"]]
    spec = entry["spec"]
    validate(spec["input_schema"], payload["arguments"])
    view = SnapshotView(payload["snapshot"], payload["unit"], spec["permissions"])

    def guard(event, args):
        # Defense in depth for trusted implementations, not an arbitrary-code sandbox.
        if event == "open" or event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")):
            raise AgentError("TOOL_PERMISSION_DENIED")
        if event in ("os.system", "os.fork", "os.forkpty", "ctypes.dlopen"):
            raise AgentError("TOOL_PERMISSION_DENIED")

    # Read vetted implementation bytes before closing filesystem capabilities.
    source = registry.handler_path(payload["name"]).read_bytes()
    if digest(source.hex()) != entry["implementation_digest"]:
        raise AgentError("TOOL_REGISTRY_MISMATCH")
    sys.addaudithook(guard)
    namespace = {"__name__": "trusted_snapshot_tool"}
    exec(compile(source, "<trusted-tool>", "exec"), namespace)
    used = 0

    def write(value):
        nonlocal used
        encoded = (json_text(value) + "\n").encode()
        used += len(encoded)
        if used > spec["max_output_bytes"]:
            raise AgentError("TOOL_OUTPUT_LIMIT")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()

    def emit(record):
        validate(spec["output_schema"], record)
        write({"record": record})

    try:
        namespace["run"](view, payload["arguments"], emit)
        write({"done": True})
    except AgentError as error:
        # A fixed protocol footer; the parent also enforces the total byte limit.
        codes = {
            "TOOL_PERMISSION_DENIED",
            "TOOL_REFERENCE_DENIED",
            "TOOL_OUTPUT_LIMIT",
            "TOOL_SCHEMA_REJECTED",
        }
        code = error.code if error.code in codes else "TOOL_EXECUTION_FAILED"
        sys.stdout.buffer.write((json_text({"error": code}) + "\n").encode())
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # No exception text, source, or stderr may leak across the worker boundary.
        os._exit(1)
