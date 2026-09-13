import json
import os
from pathlib import Path

from review_agent.contracts import AgentError, digest
from review_agent.safety import Safety


def write_once(path, text):
    path = Path(path)
    Safety().require_safe(text)
    content = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise AgentError("EVALUATION_ARTIFACT_CONFLICT") from None
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def save_json(path, value):
    write_once(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def export(directory, value, name):
    path = Path(directory) / (name + "-" + digest(value) + ".json")
    save_json(path, value)
    return path
