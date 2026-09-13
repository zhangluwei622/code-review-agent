from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from review_agent.contracts import json_text
from review_agent.tools import runner
from review_agent.tools.registry import ToolRegistry


def encoded_record(record):
    return (json_text({"record": record}) + "\n").encode()


@pytest.fixture
def scripted_output(monkeypatch):
    """Exercise the real reader with fixed pipe chunks, independent of OS timing."""

    def execute(data, limit, splits=()):
        process = Mock()
        process.poll.return_value = None
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(SimpleNamespace(fileobj=process.stdout), 1)]
        pending, offset = deque(splits), 0

        def read(fd, size):
            nonlocal offset
            assert fd == process.stdout.fileno()
            assert 0 < size <= min(4096, limit - offset + 1)
            end = min(len(data), offset + size, pending.popleft() if pending else len(data))
            chunk, offset = data[offset:end], end
            return chunk

        monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
        monkeypatch.setattr(runner.selectors, "DefaultSelector", Mock(return_value=selector))
        monkeypatch.setattr(
            runner, "os", SimpleNamespace(getpid=lambda: 1, set_blocking=Mock(), read=read)
        )
        registry = ToolRegistry()
        spec = {**registry.entries["list_changed_files"]["spec"], "max_output_bytes": limit}
        result = runner.ToolRunner(registry)._execute(b"{}", spec)
        assert offset == limit + 1  # Only the overflow sentinel may exceed the byte budget.
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        return result

    return execute


@pytest.mark.parametrize("chunking", ["coalesced", "partial_record", "complete_record", "utf8"])
@pytest.mark.parametrize("boundary", ["after_newline", "before_newline", "inside_utf8"])
def test_truncated_safe_prefix_is_independent_of_read_chunks(scripted_output, chunking, boundary):
    records = [
        {"file_id": "f1", "path": "synthetic.py"},
        {"file_id": "f2", "path": "上下文.py"},
        {"file_id": "f3", "path": "上下文.py"},
    ]
    first, second, third = [encoded_record(record) for record in records]
    prefix = first + second
    limit = {
        "after_newline": len(prefix),
        "before_newline": len(prefix + third) - 1,
        "inside_utf8": len(prefix) + third.index("上".encode()) + 1,
    }[boundary]
    splits = {
        "coalesced": (),
        "partial_record": (len(first) - 1,),
        "complete_record": (len(first),),
        "utf8": (len(first) + second.index("上".encode()) + 1,),
    }[chunking]
    result = scripted_output(prefix + third + b'{"error":"TOOL_OUTPUT_LIMIT"}\n', limit, splits)
    assert result == {
        "status": "TRUNCATED",
        "error_code": "TOOL_OUTPUT_LIMIT",
        "records": records[:2],
        "complete": False,
    }


@pytest.mark.parametrize(
    "invalid_line,status,code",
    [
        (encoded_record({"oops": 1}), "FAILED", "TOOL_OUTPUT_SCHEMA_REJECTED"),
        (
            encoded_record({"file_id": "f2", "path": "ghp_" + "SyntheticToolOnly" * 3}),
            "BLOCKED_SECURITY",
            "UNSAFE_TOOL_OUTPUT",
        ),
        (b"not-json\n", "FAILED", "TOOL_PROTOCOL_ERROR"),
    ],
)
def test_complete_records_in_overflow_chunk_still_require_validation(
    scripted_output, invalid_line, status, code
):
    prefix = encoded_record({"file_id": "f1", "path": "synthetic.py"}) + invalid_line
    result = scripted_output(prefix + b"x", len(prefix))
    assert result == {"status": status, "error_code": code, "records": [], "complete": False}
