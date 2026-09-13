"""Trusted test worker. Fixture only, no inherited credentials, no target execution."""

import argparse
import json
import os
import socket
import threading
from pathlib import Path

from review_agent import app
from review_agent.contracts import digest


def deny_network(*args, **kwargs):
    raise AssertionError("NETWORK_FORBIDDEN_IN_KILL_WORKER")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--point")
    parser.add_argument("--ordinal", type=int, default=1)
    parser.add_argument("--retry-unknown")
    parser.add_argument("--tool-calls", type=Path)
    args = parser.parse_args()
    socket.socket.connect = deny_network
    socket.create_connection = deny_network
    assert app.read_task(args.task, args.state)["config"]["execution_mode"] == "fixture"
    matches = 0

    if args.tool_calls:
        from review_agent.tools.runner import ToolRunner

        original = ToolRunner._execute

        def tool_execute(self, payload, spec):
            with args.tool_calls.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"input_digest": digest(payload.hex())}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return original(self, payload, spec)

        ToolRunner._execute = tool_execute

    def observer(request):
        # Durable observer outside the business transaction: counts actual send entry.
        with args.calls.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"request_digest": digest(request)}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def fault(event):
        nonlocal matches
        if event == args.point:
            matches += 1
            if matches == args.ordinal:
                print("BARRIER", flush=True)
                threading.Event().wait()

    data = app.execute(
        args.task, args.state, observer=observer, fault=fault, retry_unknown=args.retry_unknown
    )
    print(json.dumps(app.summary(data)), flush=True)


if __name__ == "__main__":
    main()
