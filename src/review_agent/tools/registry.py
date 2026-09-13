import json
import re
from pathlib import Path

from pydantic import Field, ValidationError

from review_agent.contracts import AgentError, StrictModel, digest
from review_agent.safety import Safety
from review_agent.tools.schema import check_schema

BUILTINS = Path(__file__).parent / "builtin"
PERMISSIONS = frozenset({"snapshot.files", "unit.hunks"})


class ToolSpec(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    handler_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict
    output_schema: dict
    permissions: list[str] = Field(max_length=2)
    timeout_ms: int = Field(ge=50, le=5000)
    max_output_bytes: int = Field(ge=128, le=65536)


class ToolRegistry:
    def __init__(self, trusted_root: Path = BUILTINS):
        # This path is host-owned: deliberately no CLI/model/target config entry.
        self.root = trusted_root.resolve()
        self.entries = {}
        try:
            for path in sorted(self.root.glob("*.json")):
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise AgentError("INVALID_TOOL_MANIFEST")
                value = json.loads(path.read_text(encoding="utf-8"))
                Safety().require_safe(value)
                spec = ToolSpec.model_validate(value)
                if (
                    spec.name in self.entries
                    or not set(spec.permissions) <= PERMISSIONS
                    or len(set(spec.permissions)) != len(spec.permissions)
                    or spec.input_schema.get("type") != "object"
                ):
                    raise AgentError("INVALID_TOOL_MANIFEST")
                check_schema(spec.input_schema)
                check_schema(spec.output_schema)
                implementation = self.root / (spec.handler_id + ".py")
                if implementation.is_symlink() or not implementation.is_file():
                    raise AgentError("TOOL_HANDLER_NOT_TRUSTED")
                self.entries[spec.name] = {
                    "spec": spec.model_dump(),
                    "implementation_digest": digest(implementation.read_bytes().hex()),
                }
        except (OSError, ValueError, ValidationError):
            raise AgentError("INVALID_TOOL_MANIFEST") from None
        if not self.entries:
            raise AgentError("EMPTY_TOOL_REGISTRY")

    def snapshot(self):
        # Include the runner contract so changed enforcement cannot silently resume.
        runtime = {
            p.name: digest(p.read_bytes().hex()) for p in sorted(Path(__file__).parent.glob("*.py"))
        }
        return {"protocol": 1, "entries": self.entries, "runtime_digest": digest(runtime)}

    def require_snapshot(self, frozen):
        if self.snapshot() != frozen:
            raise AgentError("TOOL_REGISTRY_MISMATCH")

    def handler_path(self, name):
        if name not in self.entries or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise AgentError("TOOL_NOT_FOUND")
        return self.root / (self.entries[name]["spec"]["handler_id"] + ".py")
