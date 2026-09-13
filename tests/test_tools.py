import copy
import json
import time

import pytest

from review_agent.contracts import AgentError
from review_agent.tools.registry import ToolRegistry
from review_agent.tools.runner import ToolRunner


def run(registry, name, arguments, snapshot=None, unit=None):
    return ToolRunner(registry).run(
        name,
        arguments,
        snapshot or {"files": [], "hunks": []},
        unit or {"hunk_ids": []},
        registry.snapshot(),
    )


def custom(tmp_path, code, *, permissions=None, timeout=2000, limit=4096):
    original = ToolRegistry().entries["list_changed_files"]["spec"]
    spec = {
        **copy.deepcopy(original),
        "name": "extra",
        "handler_id": "extra",
        "permissions": permissions or [],
        "timeout_ms": timeout,
        "max_output_bytes": limit,
    }
    (tmp_path / "extra.json").write_text(json.dumps(spec))
    (tmp_path / "extra.py").write_text(code)
    return ToolRegistry(tmp_path)


def test_add_handler_and_manifest_without_central_map_or_graph_edit(tmp_path):
    registry = custom(
        tmp_path,
        "def run(view, arguments, emit):\n    emit({'file_id':'f1','path':'synthetic.py'})\n",
    )
    result = run(registry, "extra", {})
    assert result == {
        "status": "SUCCEEDED",
        "error_code": None,
        "complete": True,
        "records": [{"file_id": "f1", "path": "synthetic.py"}],
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"hunk_id": 1},
        {"hunk_id": True},
        {"hunk_id": "h0001", "path": "/etc/passwd"},
        {},
        {"hunk_id": "x" * 65},
    ],
)
def test_strict_tool_arguments(arguments):
    assert run(ToolRegistry(), "read_hunk", arguments)["status"] == "REJECTED"


def test_scope_and_full_context(harness):
    harness.create(diff="two-files")
    data = harness.read()
    unit = data["units"][0]
    registry = ToolRegistry()
    result = run(registry, "read_hunk", {"hunk_id": unit["hunk_ids"][0]}, data["snapshot"], unit)
    assert result["status"] == "SUCCEEDED"
    assert len(result["records"]) == len(data["snapshot"]["hunks"][0]["lines"])
    denied = run(
        registry, "read_hunk", {"hunk_id": data["units"][1]["hunk_ids"][0]}, data["snapshot"], unit
    )
    assert denied["error_code"] == "TOOL_REFERENCE_DENIED"


def test_unknown_tool_and_empty_success():
    registry = ToolRegistry()
    assert run(registry, "absent", {})["error_code"] == "TOOL_NOT_FOUND"
    result = run(registry, "search_diff", {"query": "missing", "limit": 1})
    assert result["status"] == "SUCCEEDED" and result["records"] == []


@pytest.mark.parametrize(
    "code",
    [
        "def run(view, arguments, emit):\n    view.files()\n",
        "def run(view, arguments, emit):\n    open('/tmp/forbidden-tool-read')\n",
        "def run(view, arguments, emit):\n    __import__('os').system('false')\n",
    ],
)
def test_host_permissions_enforced(tmp_path, code):
    assert run(custom(tmp_path, code), "extra", {})["error_code"] == "TOOL_PERMISSION_DENIED"


def test_hung_handler_is_killed_during_execution(tmp_path):
    registry = custom(
        tmp_path, "def run(view, arguments, emit):\n    while True: pass\n", timeout=600
    )
    started = time.monotonic()
    result = run(registry, "extra", {})
    assert result["status"] == "TIMED_OUT" and time.monotonic() - started < 2


def test_output_flood_is_killed_before_handler_returns(tmp_path):
    registry = custom(
        tmp_path,
        "def run(view, arguments, emit):\n"
        "    import os\n"
        "    while True: os.write(1, b'x' * 4096)\n",
        limit=512,
    )
    started = time.monotonic()
    result = run(registry, "extra", {})
    assert result["status"] == "TRUNCATED" and time.monotonic() - started < 1.5
    assert result["records"] == []


def test_stream_limit_preserves_safe_complete_records(tmp_path):
    registry = custom(
        tmp_path,
        "def run(view, arguments, emit):\n"
        "    while True: emit({'file_id':'f1','path':'synthetic.py'})\n",
        limit=256,
    )
    result = run(registry, "extra", {})
    assert result["status"] == "TRUNCATED" and result["records"]
    assert not result["complete"]


def test_unsafe_and_invalid_output_are_not_persistable(tmp_path):
    secret = "ghp_" + "SyntheticToolOnly" * 3
    registry = custom(
        tmp_path,
        f"def run(view, arguments, emit):\n    emit({{'file_id':'f1','path':{secret!r}}})\n",
    )
    result = run(registry, "extra", {})
    assert result["status"] == "BLOCKED_SECURITY" and secret not in json.dumps(result)
    (tmp_path / "extra.py").write_text("def run(view, arguments, emit):\n    emit({'oops':1})\n")
    assert run(ToolRegistry(tmp_path), "extra", {})["status"] == "FAILED"


def test_version_change_and_unsupported_manifest_fail_closed(tmp_path):
    registry = custom(tmp_path, "def run(view, arguments, emit): pass\n")
    frozen = registry.snapshot()
    (tmp_path / "extra.py").write_text("def run(view, arguments, emit): return None\n")
    with pytest.raises(AgentError, match="TOOL_REGISTRY_MISMATCH"):
        ToolRegistry(tmp_path).require_snapshot(frozen)
    manifest = json.loads((tmp_path / "extra.json").read_text())
    manifest["input_schema"]["$ref"] = "https://invalid.example/schema"
    (tmp_path / "extra.json").write_text(json.dumps(manifest))
    with pytest.raises(AgentError, match="INVALID_TOOL_SCHEMA"):
        ToolRegistry(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("handler_id", "../../untrusted"),
        ("permissions", ["shell"]),
        ("timeout_ms", 0),
    ],
)
def test_invalid_registry_entries(tmp_path, field, value):
    custom(tmp_path, "def run(view, arguments, emit): pass\n")
    path = tmp_path / "extra.json"
    spec = json.loads(path.read_text())
    spec[field] = value
    path.write_text(json.dumps(spec))
    with pytest.raises(AgentError, match="INVALID_TOOL_MANIFEST"):
        ToolRegistry(tmp_path)
