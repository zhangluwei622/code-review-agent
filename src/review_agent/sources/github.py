from review_agent.sources.common import RemoteFile, freeze
from review_agent.sources.contracts import SourceError
from review_agent.sources.url import integer, safe_path, sha


def identity(data, url):
    try:
        base, head = data["base"], data["head"]
        repo = base["repo"]
        if (
            integer(data["number"], minimum=1) != url.number
            or repo["full_name"].lower() != url.repository.lower()
        ):
            raise SourceError("SOURCE_IDENTITY_MISMATCH")
        # Deleted head repositories cannot establish the requested source identity.
        head_id = integer(head["repo"]["id"], minimum=1)
        return {
            "repository_id": integer(repo["id"], minimum=1),
            "base_sha": sha(base["sha"]),
            "head_sha": sha(head["sha"]),
            "head_repository_id": head_id,
            "changed_files": integer(data["changed_files"]),
            "additions": integer(data["additions"]),
            "deletions": integer(data["deletions"]),
        }
    except (KeyError, TypeError, AttributeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None


def remote_file(data):
    try:
        new = safe_path(data["filename"])
        kind = data["status"]
        old = safe_path(data["previous_filename"]) if kind == "renamed" else new
        added, removed = integer(data["additions"]), integer(data["deletions"])
        if integer(data["changes"]) != added + removed:
            raise SourceError("SOURCE_INVALID_METADATA")
        patch = data.get("patch")
        if patch is not None and not isinstance(patch, str):
            raise SourceError("SOURCE_INVALID_METADATA")
        return RemoteFile(old, new, kind, patch, added, removed)
    except (KeyError, TypeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None


def fetch(url, http, safety):
    root = f"/repos/{url.repository}"
    pull = f"{root}/pulls/{url.number}"
    original = identity(http.get(pull), url)
    if original["changed_files"] > http.limits.max_files:
        raise SourceError("SOURCE_FILE_LIMIT")
    base, head = original["base_sha"], original["head_sha"]
    discovery = http.get(f"{root}/compare/{base}...{head}?per_page=1&page=1")
    try:
        if sha(discovery["base_commit"]["sha"]) != base:
            raise SourceError("SOURCE_COMPARISON_MISMATCH")
        merge_base = sha(discovery["merge_base_commit"]["sha"])
    except (KeyError, TypeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None
    comparison = http.get(f"{root}/compare/{merge_base}...{head}?per_page=1&page=1")
    try:
        if (
            sha(comparison["base_commit"]["sha"]) != merge_base
            or sha(comparison["merge_base_commit"]["sha"]) != merge_base
        ):
            raise SourceError("SOURCE_COMPARISON_MISMATCH")
        rows = comparison["files"]
        if not isinstance(rows, list) or len(rows) != original["changed_files"]:
            raise SourceError("SOURCE_FILE_COUNT_MISMATCH")
        fixed = [remote_file(row) for row in rows]
    except (KeyError, TypeError):
        raise SourceError("SOURCE_INVALID_METADATA") from None
    live = []
    page = 1
    while True:
        rows = http.get(f"{pull}/files?per_page=30&page={page}")
        if not isinstance(rows, list) or len(rows) > 30:
            raise SourceError("SOURCE_INVALID_METADATA")
        live.extend(remote_file(row) for row in rows)
        if len(live) > http.limits.max_files:
            raise SourceError("SOURCE_FILE_LIMIT")
        if len(rows) < 30:
            break
        page += 1
    # Order may differ; every filename must occur once on each side.
    fixed_map, live_map = {f.new_path: f for f in fixed}, {f.new_path: f for f in live}
    if len(fixed_map) != len(fixed) or len(live_map) != len(live):
        raise SourceError("SOURCE_DUPLICATE_FILE")
    if fixed_map != live_map:
        raise SourceError("SOURCE_COMPARISON_MISMATCH")
    if (
        sum(f.additions for f in fixed) != original["additions"]
        or sum(f.deletions for f in fixed) != original["deletions"]
    ):
        raise SourceError("SOURCE_LINE_COUNT_MISMATCH")
    if identity(http.get(pull), url) != original:
        raise SourceError("SOURCE_VERSION_CHANGED")
    return freeze(
        url,
        {
            "repository_id": original["repository_id"],
            "base_sha": merge_base,
            "target_sha": base,
            "head_sha": head,
            "comparison": "MERGE_BASE_TO_HEAD",
        },
        fixed,
        http,
        safety,
    )
