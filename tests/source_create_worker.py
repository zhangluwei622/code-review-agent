"""Trusted local crash worker for source publication and task creation; no network."""

import os
import signal
import socket
import sys
from pathlib import Path

from review_agent import app
from review_agent.sources.service import load_source, save_source


def deny(*args, **kwargs):
    raise AssertionError("NETWORK_FORBIDDEN")


def main():
    mode, point, bundle, fixture, state = sys.argv[1:]
    socket.socket.connect = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny

    def fault(event):
        if event == point:
            os.kill(os.getpid(), signal.SIGKILL)

    if mode == "create":
        app.create_task(
            None,
            Path(fixture),
            Path(state),
            source_path=Path(bundle),
            max_tokens=100000,
            max_cost_nusd=50_000_000,
            fault=fault,
        )
    else:
        save_source(load_source(Path(bundle)), Path(state) / "published.json", fault=fault)
    raise AssertionError("FAULT_NOT_REACHED")


if __name__ == "__main__":
    main()
