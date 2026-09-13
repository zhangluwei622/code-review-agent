from pathlib import Path, PurePosixPath

from unidiff import PatchSet

from review_agent.config import TaskConfig
from review_agent.contracts import AgentError, digest
from review_agent.safety import Safety


def read_bounded(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise AgentError("INPUT_TOO_LARGE")
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise AgentError("INPUT_READ_FAILED") from None


def prepare_diff(path: Path, config: TaskConfig, safety: Safety) -> tuple[dict, list[dict]]:
    return prepare_text(read_bounded(path, config.max_diff_bytes), config, safety)


def prepare_text(
    text: str, config: TaskConfig, safety: Safety, *, allow_empty=False
) -> tuple[dict, list[dict]]:
    # No raw diff reaches SQLite, a graph input, a callback or a parser exception.
    if len(text.encode("utf-8")) > config.max_diff_bytes:
        raise AgentError("INPUT_TOO_LARGE")
    safe_diff, redactions = safety.sanitize(text, diff=True)
    try:
        patch = PatchSet(safe_diff)
        if not patch and not (allow_empty and safe_diff == ""):
            raise ValueError
    except Exception:
        raise AgentError("INVALID_DIFF") from None
    if len(patch) > config.max_files:
        raise AgentError("TOO_MANY_FILES")
    snapshot = {
        "snapshot_id": digest(safe_diff),
        "safe_diff": safe_diff,
        "redactions": redactions,
        "files": [],
        "hunks": [],
        "excluded": [],
    }
    units = []
    for file_index, change in enumerate(patch):
        file_id = f"f{file_index + 1:04d}"
        name = change.path
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise AgentError("UNSAFE_DIFF_PATH")
        file_info = {"file_id": file_id, "path": name}
        snapshot["files"].append(file_info)
        if change.is_binary_file or not name.endswith(".py") or not len(change):
            reason = "BINARY" if change.is_binary_file else "NO_PYTHON_TEXT_CHANGE"
            snapshot["excluded"].append({**file_info, "reason": reason})
            continue
        group, group_size = [], 0

        def flush():
            if group:
                ordinal = len(units)
                units.append(
                    {
                        "unit_id": f"u{ordinal + 1:04d}",
                        "ordinal": ordinal,
                        "file_id": file_id,
                        "path": name,
                        "hunk_ids": list(group),
                        "context_flags": ["DIFF_ONLY", "NO_REPOSITORY_EXECUTION"],
                    }
                )

        for hunk in change:
            hunk_id = f"h{len(snapshot['hunks']) + 1:04d}"
            size = len(str(hunk).encode())
            if size > config.max_unit_bytes:
                raise AgentError("HUNK_TOO_LARGE")
            if group_size + size > config.max_unit_bytes:
                flush()
                group, group_size = [], 0
            lines = [
                {
                    "kind": line.line_type,
                    "old_lineno": line.source_line_no,
                    "new_lineno": line.target_line_no,
                    "text": line.value,
                    "redacted": "[REDACTED" in line.value,
                }
                for line in hunk
                if line.line_type in (" ", "+", "-")
            ]
            snapshot["hunks"].append(
                {
                    "hunk_id": hunk_id,
                    "file_id": file_id,
                    "header": hunk.section_header,
                    "lines": lines,
                }
            )
            group.append(hunk_id)
            group_size += size
        flush()
    return snapshot, units
