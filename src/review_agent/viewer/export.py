"""Bounded safe input, inert JSON embedding and exclusive standalone output."""

import base64
import hashlib
import json
import math
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from review_agent.contracts import AgentError
from review_agent.safety import Safety
from review_agent.viewer.projection import project

MAX_TRACE_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 80
MAX_NODES = 250000


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AgentError("VIEW_DUPLICATE_KEY")
        value[key] = item
    return value


def _constant(_):
    raise AgentError("VIEW_INVALID_JSON")


def load_trace(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_TRACE_BYTES + 1)
        if len(raw) > MAX_TRACE_BYTES:
            raise AgentError("VIEW_INPUT_TOO_LARGE")
        trace = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise AgentError("VIEW_INVALID_JSON") from None
    except OSError:
        raise AgentError("VIEW_INPUT_READ_FAILED") from None
    if not isinstance(trace, dict):
        raise AgentError("VIEW_INVALID_TRACE")
    safety = Safety()
    pending, count = [(trace, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise AgentError("VIEW_TOO_COMPLEX")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise AgentError("VIEW_INVALID_JSON")
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
                _, redactions = safety.sanitize(item)
                if redactions:
                    raise ValueError
            except (AgentError, ValueError, UnicodeError):
                raise AgentError("VIEW_UNSAFE_TRACE") from None
    return trace, hashlib.sha256(raw).hexdigest()


def _asset(name):
    return files("review_agent.viewer").joinpath(name).read_text(encoding="utf-8")


def _hash(text):
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def render_html(trace, input_sha256):
    payload = {"input_sha256": input_sha256, "trace": trace, "view": project(trace)}
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for literal, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        embedded = embedded.replace(literal, escaped)
    css, script = _asset("viewer.css"), _asset("viewer.js")
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{_hash(script)}'; style-src 'sha256-{_hash(css)}'; "
        "connect-src 'none'; img-src 'none'; font-src 'none'; object-src 'none'; "
        "frame-src 'none'; base-uri 'none'; form-action 'none'"
    )
    # Replace a static template once; input text can never create template placeholders.
    template = _asset("viewer.html")
    return (
        template.replace("__CSP__", csp)
        .replace("__CSS__", css)
        .replace("__SCRIPT__", script)
        .replace("__PAYLOAD__", embedded)
    )


def export_view(trace_path, output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise AgentError("VIEW_OUTPUT_EXISTS")
    trace, input_sha256 = load_trace(trace_path)
    html = render_html(trace, input_sha256)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".review-view-", dir=output.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    except FileExistsError:
        raise AgentError("VIEW_OUTPUT_EXISTS") from None
    except OSError:
        raise AgentError("VIEW_OUTPUT_FAILED") from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return {
        "mode": "offline_trace_view",
        "viewer_version": 1,
        "task_id": trace["task_id"],
        "input_sha256": input_sha256,
        "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "output_written": True,
        "model_calls": 0,
        "database_access": False,
    }
