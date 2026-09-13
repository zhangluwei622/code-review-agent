import json
import socket
from contextlib import contextmanager
from pathlib import Path

import pytest

from review_agent import app
from review_agent.contracts import AgentError
from review_agent.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("NETWORK_FORBIDDEN_IN_FIXTURE_TEST")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


class Harness:
    def __init__(self, directory):
        self.directory = directory
        self.state = directory / "state"
        self.calls = []
        self.task_id = None

    def create(
        self,
        scenario="success",
        diff="empty-list",
        *,
        spec=None,
        text=None,
        tokens=5000,
        amount=10_000_000,
        repairs=0,
        schema=4,
        tools=4,
    ):
        self.fixture = self.directory / "fixture.json"
        spec = spec or json.loads((ROOT / f"examples/provider/{scenario}.json").read_text())
        self.fixture.write_text(json.dumps(spec))
        self.diff = self.directory / "input.diff"
        self.diff.write_text(
            text if text is not None else (ROOT / f"examples/diffs/{diff}.diff").read_text()
        )
        self.task_id = app.create_task(
            self.diff,
            self.fixture,
            self.state,
            max_tokens=tokens,
            max_cost_nusd=amount,
            max_repairs_per_unit=repairs,
            schema_version=schema,
            max_tools_per_unit=tools,
        )
        return self.task_id

    def run(self, fault=None, **kwargs):
        return app.execute(
            self.task_id, self.state, observer=self.calls.append, fault=fault, **kwargs
        )

    def read(self):
        return app.read_task(self.task_id, self.state)

    @contextmanager
    def store(self):
        store = Storage(app.task_path(self.state, self.task_id))
        try:
            yield store
        finally:
            store.close()

    def crash(self, point):
        def fault(event):
            if event == point:
                raise AgentError("TEST_CRASH")

        with pytest.raises(AgentError, match="TEST_CRASH"):
            self.run(fault)


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def success_spec():
    return json.loads((ROOT / "examples/provider/success.json").read_text())
