import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError
from unidiff import PatchSet

from review_agent.config import policy_versions
from review_agent.contracts import AgentError, digest
from review_agent.ingest import prepare_text, read_bounded
from review_agent.safety import Safety
from review_agent.sources.contracts import FrozenSource, SourceError, SourceLimits
from review_agent.sources.http import SourceHTTP
from review_agent.sources.url import parse_url, safe_path, sha


def validate_source(source, safety=None, *, require_current_policy=True):
    safety = safety or Safety()
    try:
        safety.require_safe(source.model_dump())
        manifest = source.manifest
        url = parse_url(manifest.url)
        if (
            digest(manifest.model_dump()) != source.manifest_digest
            or digest(source.safe_diff) != manifest.safe_diff_digest
            or (
                require_current_policy
                and manifest.policy_digest != policy_versions()["policy_digest"]
            )
            or (url.repository, url.provider, url.number)
            != (manifest.repository_path, manifest.provider, manifest.change_number)
        ):
            raise ValueError
        for value in (manifest.base_sha, manifest.head_sha, manifest.target_sha):
            sha(value)
        if (
            url.provider == "github"
            and (manifest.version_id is not None or manifest.comparison != "MERGE_BASE_TO_HEAD")
        ) or (
            url.provider == "gitlab"
            and (not manifest.version_id or manifest.comparison != "DIFF_VERSION_BASE_TO_HEAD")
        ):
            raise ValueError
        limits = SourceLimits()
        if (
            len(source.safe_diff.encode()) > limits.max_diff_bytes
            or len(manifest.files) > limits.max_files
        ):
            raise ValueError
        seen = set()
        for item in manifest.files:
            safe_path(item.old_path)
            safe_path(item.new_path)
            if item.new_path in seen:
                raise ValueError
            seen.add(item.new_path)
            if item.disposition == "REVIEW":
                if item.reason is not None or item.patch_state != "PRESENT":
                    raise ValueError
            elif item.reason is None:
                raise ValueError
        reviewed = sum(f.disposition == "REVIEW" for f in manifest.files)
        coverage = (
            "EMPTY"
            if not manifest.files
            else "EXCLUDED_ONLY"
            if not reviewed
            else "PYTHON_COMPLETE"
            if reviewed == len(manifest.files)
            else "PARTIAL_SCOPE"
        )
        if coverage != manifest.coverage:
            raise ValueError
        snapshot, units = prepare_text(
            source.safe_diff, limits, safety, allow_empty=not manifest.files
        )
        if [f["path"] for f in snapshot["files"]] != [f.new_path for f in manifest.files]:
            raise ValueError
        review_paths = {u["path"] for u in units}
        if review_paths != {f.new_path for f in manifest.files if f.disposition == "REVIEW"}:
            raise ValueError
        for item, change in zip(manifest.files, PatchSet(source.safe_diff), strict=True):
            old = "/dev/null" if item.change == "added" else "a/" + item.old_path
            new = "/dev/null" if item.change == "removed" else "b/" + item.new_path
            if change.source_file != old or change.target_file != new:
                raise ValueError
            if item.disposition == "REVIEW":
                if (change.added, change.removed) != (item.additions, item.deletions):
                    raise ValueError
            elif item.reason == "BINARY":
                if not change.is_binary_file or item.patch_state != "BINARY":
                    raise ValueError
            elif item.new_path.endswith(".py") or item.patch_state != "OMITTED_EXCLUDED":
                raise ValueError
        return source
    except (ValueError, TypeError, AttributeError):
        raise SourceError("SOURCE_SNAPSHOT_INTEGRITY") from None


def prepare_source(source, config, safety, *, require_current_policy=True):
    validate_source(source, safety, require_current_policy=require_current_policy)
    snapshot, units = prepare_text(
        source.safe_diff, config, safety, allow_empty=not source.manifest.files
    )
    snapshot["redactions"] = source.manifest.redactions
    return snapshot, units


def fetch_source(url, *, authenticate=False, http=None):
    parsed = parse_url(url)
    credential = None
    if authenticate:
        name = "GITHUB_TOKEN" if parsed.provider == "github" else "GITLAB_TOKEN"
        credential = os.environ.get(name)
        if (
            not credential
            or len(credential) < 8
            or any(ord(c) <= 32 or ord(c) >= 127 for c in credential)
        ):
            raise SourceError("SOURCE_CREDENTIAL_MISSING")
    safety = Safety((credential,) if credential else ())
    safety.require_safe(parsed.canonical)
    client = http or SourceHTTP(parsed.api_host, credential=credential)
    if client.host != parsed.api_host:
        raise SourceError("SOURCE_HOST_BLOCKED")
    try:
        from review_agent.sources import github, gitlab

        adapter = github if parsed.provider == "github" else gitlab
        source = adapter.fetch(parsed, client, safety)
        client.check_deadline()
        validate_source(source, safety)
        client.check_deadline()
        return source
    except SourceError as error:
        diagnostics = {**error.diagnostics, "http_events": [dict(e) for e in client.events]}
        safety.require_safe(diagnostics)
        raise SourceError(error.code, diagnostics) from None
    except AgentError:
        raise
    except Exception:
        raise SourceError("SOURCE_INVALID_RESPONSE") from None


def load_source(path: Path):
    raw = read_bounded(path, 4 * 1024 * 1024)
    Safety().require_safe(raw)
    try:
        source = FrozenSource.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise SourceError("SOURCE_SNAPSHOT_INTEGRITY") from None
    return validate_source(source)


def save_source(source, path: Path, *, fault=None):
    validate_source(source)
    fault = fault or (lambda _: None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".source-", delete=False
        ) as stream:
            temp = Path(stream.name)
            json.dump(source.model_dump(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fault("before_source_publish")
        os.link(temp, path)  # Atomic, no replacement even if destination is a symlink.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fault("after_source_publish")
    except FileExistsError:
        raise SourceError("SOURCE_OUTPUT_EXISTS") from None
    except OSError:
        raise SourceError("SOURCE_OUTPUT_FAILED") from None
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def source_summary(source):
    return {
        "source_digest": source.manifest_digest,
        "source": source.manifest.model_dump(),
        "model_sends": 0,
        "held_tokens": 0,
        "held_cost_nusd": 0,
    }
