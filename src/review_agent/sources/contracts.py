from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from review_agent.contracts import AgentError, StrictModel, digest


@dataclass(frozen=True)
class SourceLimits:
    max_requests: int = 20
    total_seconds: float = 60
    request_seconds: float = 10
    max_response_bytes: int = 4 * 1024 * 1024
    max_diff_bytes: int = 1024 * 1024
    max_files: int = 50
    max_unit_bytes: int = 16 * 1024


class SourceError(AgentError):
    def __init__(self, code: str, diagnostics: dict | None = None):
        super().__init__(code)
        self.diagnostics = diagnostics or {}


class SourceFile(StrictModel):
    old_path: str
    new_path: str
    change: Literal["added", "removed", "modified", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    disposition: Literal["REVIEW", "EXCLUDED"]
    reason: Literal["NON_PYTHON", "BINARY"] | None = None
    patch_state: Literal["PRESENT", "OMITTED_EXCLUDED", "BINARY"]


class HTTPEvent(StrictModel):
    request_no: int = Field(ge=1)
    status: int | None = Field(default=None, ge=100, le=599)
    bytes: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    error: str | None = Field(default=None, pattern=r"^[A-Z_]+$")
    request_id: str | None = Field(default=None, max_length=160)
    etag: str | None = Field(default=None, max_length=160)


class SourceManifest(StrictModel):
    protocol: Literal[1] = 1
    provider: Literal["github", "gitlab"]
    url: str
    repository_id: int = Field(gt=0)
    repository_path: str
    change_number: int = Field(gt=0)
    base_sha: str
    head_sha: str
    target_sha: str
    version_id: int | None = None
    comparison: Literal["MERGE_BASE_TO_HEAD", "DIFF_VERSION_BASE_TO_HEAD"]
    fetched_at: str
    safe_diff_digest: str
    redactions: int = Field(ge=0)
    files: list[SourceFile]
    coverage: Literal["EMPTY", "EXCLUDED_ONLY", "PARTIAL_SCOPE", "PYTHON_COMPLETE"]
    http_events: list[HTTPEvent]
    limits: dict
    policy_digest: str


class FrozenSource(StrictModel):
    protocol: Literal[1] = 1
    manifest: SourceManifest
    manifest_digest: str
    safe_diff: str

    @property
    def fingerprint(self):
        return digest(self.model_dump())
