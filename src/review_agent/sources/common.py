import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from unidiff import PatchSet

from review_agent.config import policy_versions
from review_agent.contracts import digest
from review_agent.sources.contracts import FrozenSource, SourceError, SourceFile, SourceManifest
from review_agent.sources.url import safe_path


@dataclass(frozen=True)
class RemoteFile:
    old_path: str
    new_path: str
    change: str
    patch: str | None
    additions: int | None = None
    deletions: int | None = None


def assemble(files, safety, limits):
    if len(files) > limits.max_files:
        raise SourceError("SOURCE_FILE_LIMIT")
    seen, parts, entries = set(), [], []
    for item in files:
        old, new = safe_path(item.old_path), safe_path(item.new_path)
        if new in seen:
            raise SourceError("SOURCE_DUPLICATE_FILE")
        seen.add(new)
        if item.change not in ("added", "removed", "modified", "renamed"):
            raise SourceError("SOURCE_UNSUPPORTED_CHANGE")
        source = "/dev/null" if item.change == "added" else "a/" + old
        target = "/dev/null" if item.change == "removed" else "b/" + new
        # Explicit null side; do not invent file modes absent from the API.
        header = f"diff --git {source} {target}\n--- {source}\n+++ {target}\n"
        patch = item.patch
        # Match existing ingest scope: destination path; removals retain the old path.
        python = new.endswith(".py")
        binary = patch == f"Binary files {source} and {target} differ\n"
        if not python or binary:
            reason = "BINARY" if binary else "NON_PYTHON"
            fragment, count_add, count_del = "", item.additions or 0, item.deletions or 0
            entry = SourceFile(
                old_path=old,
                new_path=new,
                change=item.change,
                additions=count_add,
                deletions=count_del,
                disposition="EXCLUDED",
                reason=reason,
                patch_state="BINARY" if binary else "OMITTED_EXCLUDED",
            )
        else:
            if not isinstance(patch, str) or not patch.startswith("@@ "):
                raise SourceError(
                    "SOURCE_PATCH_MISSING", {"path": new, "reason": "PYTHON_PATCH_REQUIRED"}
                )
            fragment = patch if patch.endswith("\n") else patch + "\n"
            safe_fragment, _ = safety.sanitize(header + fragment, diff=True)
            try:
                parsed = PatchSet(safe_fragment)
                if len(parsed) != 1 or not len(parsed[0]):
                    raise ValueError
                # Unidiff must consume the whole fragment, not ignore a trailing partial hunk.
                normalized = re.sub(
                    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
                    lambda m: f"@@ -{m[1]},{m[2] or '1'} +{m[3]},{m[4] or '1'} @@",
                    safe_fragment,
                    flags=re.MULTILINE,
                )
                if str(parsed) != normalized:
                    raise ValueError
                count_add, count_del = parsed[0].added, parsed[0].removed
                if item.change == "added" and any(
                    h.source_length or h.source_start for h in parsed[0]
                ):
                    raise ValueError
                if item.change == "removed" and any(
                    h.target_length or h.target_start for h in parsed[0]
                ):
                    raise ValueError
                if (
                    item.additions is not None
                    and count_add != item.additions
                    or item.deletions is not None
                    and count_del != item.deletions
                ):
                    raise ValueError
            except Exception:
                raise SourceError("SOURCE_PATCH_INCOMPLETE", {"path": new}) from None
            if any(len(str(h).encode()) > limits.max_unit_bytes for h in parsed[0]):
                raise SourceError("SOURCE_HUNK_LIMIT", {"path": new})
            entry = SourceFile(
                old_path=old,
                new_path=new,
                change=item.change,
                additions=count_add,
                deletions=count_del,
                disposition="REVIEW",
                patch_state="PRESENT",
            )
        if binary:
            # Only an explicit, verified marker can produce a BINARY exclusion in ingest.
            parts.append(
                f"diff --git {source} {target}\nBinary files {source} and {target} differ\n"
            )
        else:
            parts.append(header + fragment)
        entries.append(entry)
        if sum(len(part.encode()) for part in parts) > limits.max_diff_bytes:
            raise SourceError("SOURCE_DIFF_LIMIT")
    safe_diff, redactions = safety.sanitize("".join(parts), diff=True)
    if len(safe_diff.encode()) > limits.max_diff_bytes:
        raise SourceError("SOURCE_DIFF_LIMIT")
    safety.require_safe(safe_diff)
    return safe_diff, entries, redactions


def freeze(url, identity, files, http, safety):
    http.check_deadline()
    safe_diff, entries, redactions = assemble(files, safety, http.limits)
    http.check_deadline()
    reviewed = sum(entry.disposition == "REVIEW" for entry in entries)
    coverage = (
        "EMPTY"
        if not entries
        else "EXCLUDED_ONLY"
        if not reviewed
        else "PYTHON_COMPLETE"
        if reviewed == len(entries)
        else "PARTIAL_SCOPE"
    )
    manifest = SourceManifest(
        provider=url.provider,
        url=url.canonical,
        repository_path=url.repository,
        change_number=url.number,
        fetched_at=datetime.now(UTC).isoformat(),
        safe_diff_digest=digest(safe_diff),
        redactions=redactions,
        files=entries,
        coverage=coverage,
        http_events=[dict(e) for e in http.events],
        limits=asdict(http.limits),
        policy_digest=policy_versions()["policy_digest"],
        **identity,
    )
    safety.require_safe(manifest.model_dump())
    return FrozenSource(
        manifest=manifest, manifest_digest=digest(manifest.model_dump()), safe_diff=safe_diff
    )
