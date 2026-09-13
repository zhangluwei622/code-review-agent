import re
from urllib.parse import quote

from review_agent.sources.common import RemoteFile, freeze
from review_agent.sources.contracts import SourceError
from review_agent.sources.url import integer, safe_path, sha


def identity(data, url, project_id):
    try:
        if (
            integer(data["iid"], minimum=1) != url.number
            or integer(data["project_id"], minimum=1) != project_id
            or integer(data["target_project_id"], minimum=1) != project_id
        ):
            raise SourceError("SOURCE_IDENTITY_MISMATCH")
        refs = data["diff_refs"]
        return {
            "merge_request_id": integer(data["id"], minimum=1),
            "source_project_id": integer(data["source_project_id"], minimum=1),
            "base_sha": sha(refs["base_sha"]),
            "head_sha": sha(refs["head_sha"]),
            "target_sha": sha(refs["start_sha"]),
        }
    except (TypeError, KeyError):
        raise SourceError("SOURCE_VERSION_NOT_READY") from None


def version_refs(data):
    return (
        sha(data["base_commit_sha"]),
        sha(data["head_commit_sha"]),
        sha(data["start_commit_sha"]),
    )


def count(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise SourceError("SOURCE_VERSION_INCOMPLETE")
    return int(value)


def remote_file(data):
    try:
        for flag in ("collapsed", "too_large"):
            # Collected/version/file counts and valid hunks are also required without flags.
            if data.get(flag, False) is not False:
                raise SourceError("SOURCE_VERSION_INCOMPLETE")
        flags = [data[key] for key in ("new_file", "deleted_file", "renamed_file")]
        if any(type(flag) is not bool for flag in flags) or sum(flags) > 1:
            raise SourceError("SOURCE_INVALID_METADATA")
        kind = next(
            (name for name, flag in zip(("added", "removed", "renamed"), flags) if flag), "modified"
        )
        old, new = safe_path(data["old_path"]), safe_path(data["new_path"])
        if (kind != "renamed" and old != new) or (kind == "renamed" and old == new):
            raise SourceError("SOURCE_INVALID_METADATA")
        patch = data.get("diff")
        if patch is not None and not isinstance(patch, str):
            raise SourceError("SOURCE_INVALID_METADATA")
        return RemoteFile(old, new, kind, patch)
    except (KeyError, TypeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None


def fetch(url, http, safety):
    try:
        project = http.get("/api/v4/projects/" + quote(url.repository, safe=""))
        project_id = integer(project["id"], minimum=1)
        if project["path_with_namespace"] != url.repository:
            raise SourceError("SOURCE_IDENTITY_MISMATCH")
        root = f"/api/v4/projects/{project_id}/merge_requests/{url.number}"
        original = identity(http.get(root), url, project_id)
        expected = tuple(original[k] for k in ("base_sha", "head_sha", "target_sha"))
        page, selected, seen = 1, None, set()
        while selected is None:
            versions = http.get(f"{root}/versions?per_page=30&page={page}")
            if not isinstance(versions, list) or len(versions) > 30:
                raise SourceError("SOURCE_INVALID_METADATA")
            for version in versions:
                version_id = integer(version["id"], minimum=1)
                if version_id in seen:
                    raise SourceError("SOURCE_DUPLICATE_VERSION")
                seen.add(version_id)
                if version_refs(version) == expected:
                    if (
                        integer(version["merge_request_id"], minimum=1)
                        != original["merge_request_id"]
                    ):
                        raise SourceError("SOURCE_IDENTITY_MISMATCH")
                    selected = version
                    break
            if len(versions) < 30:
                break
            page += 1
        if selected is None:
            raise SourceError("SOURCE_VERSION_NOT_READY")
        if selected["state"] != "collected":
            raise SourceError("SOURCE_VERSION_INCOMPLETE")
        expected_files = count(selected["real_size"])
        if expected_files > http.limits.max_files:
            raise SourceError("SOURCE_FILE_LIMIT")
        version_id = selected["id"]
        # Default diff fragments have @@ hunks; avoid unidiff=true's full headers.
        version = http.get(f"{root}/versions/{version_id}")
        if (
            integer(version["id"], minimum=1) != version_id
            or version_refs(version) != expected
            or integer(version["merge_request_id"], minimum=1) != original["merge_request_id"]
        ):
            raise SourceError("SOURCE_COMPARISON_MISMATCH")
        if version["state"] != "collected" or version.get("overflow", False) is not False:
            raise SourceError("SOURCE_VERSION_INCOMPLETE")
        rows = version["diffs"]
        if (
            not isinstance(rows, list)
            or count(version["real_size"]) != expected_files
            or len(rows) != expected_files
        ):
            raise SourceError("SOURCE_FILE_COUNT_MISMATCH")
        files = [remote_file(row) for row in rows]
        if identity(http.get(root), url, project_id) != original:
            raise SourceError("SOURCE_VERSION_CHANGED")
        return freeze(
            url,
            {
                "repository_id": project_id,
                "version_id": version_id,
                "base_sha": original["base_sha"],
                "head_sha": original["head_sha"],
                "target_sha": original["target_sha"],
                "comparison": "DIFF_VERSION_BASE_TO_HEAD",
            },
            files,
            http,
            safety,
        )
    except (KeyError, TypeError, AttributeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None
