import json

import pytest
from test_sources_common import BASE, HEAD, MERGE, PATCH, JSONTransport

from review_agent.safety import Safety
from review_agent.sources.contracts import SourceError
from review_agent.sources.http import SourceHTTP
from review_agent.sources.service import fetch_source

URL = "https://gitlab.com/team/subgroup/repo/-/merge_requests/12"


def gitlab_http(*, rows=None, modify=None):
    rows = (
        [
            {
                "old_path": "sample.py",
                "new_path": "sample.py",
                "diff": PATCH,
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "collapsed": False,
                "too_large": False,
            }
        ]
        if rows is None
        else rows
    )
    version = {
        "id": 9,
        "merge_request_id": 120,
        "base_commit_sha": MERGE,
        "head_commit_sha": HEAD,
        "start_commit_sha": BASE,
        "state": "collected",
        "real_size": str(len(rows)),
    }
    mr = {
        "id": 120,
        "iid": 12,
        "project_id": 3,
        "target_project_id": 3,
        "source_project_id": 4,
        "diff_refs": {"base_sha": MERGE, "head_sha": HEAD, "start_sha": BASE},
    }

    def handler(path):
        if "%2F" in path:
            value = {"id": 3, "path_with_namespace": "team/subgroup/repo"}
        elif "/versions?" in path:
            value = [version]
        elif path.endswith("/versions/9"):
            value = {**version, "diffs": rows}
        else:
            value = mr
        value = json.loads(json.dumps(value))
        return modify(path, value) if modify else value

    transport = JSONTransport(handler)
    return SourceHTTP("gitlab.com", transport=transport), transport


def test_gitlab_fixed_version_and_nested_namespace():
    http, transport = gitlab_http()
    source = fetch_source(URL, http=http)
    assert source.manifest.base_sha == MERGE and source.manifest.target_sha == BASE
    assert source.manifest.version_id == 9 and source.manifest.repository_id == 3
    assert source.manifest.comparison == "DIFF_VERSION_BASE_TO_HEAD"
    assert source.manifest.coverage == "PYTHON_COMPLETE"
    assert len(transport.requests) == 5
    assert transport.requests[0][1] == "/api/v4/projects/team%2Fsubgroup%2Frepo"


@pytest.mark.parametrize(
    "mode",
    [
        "not_ready",
        "not_found",
        "overflow",
        "without_files",
        "collapsed",
        "too_large",
        "real_size",
        "version_id",
        "refs",
        "mr_id",
        "race",
        "duplicate",
        "malformed_flags",
    ],
)
def test_gitlab_incomplete_or_changed_versions_stop(mode):
    reads = 0

    def modify(path, value):
        nonlocal reads
        if path.endswith("/merge_requests/12"):
            reads += 1
            if mode == "not_ready":
                value["diff_refs"] = None
            if mode == "race" and reads == 2:
                value["diff_refs"]["head_sha"] = "d" * 40
        if "/versions?" in path:
            if mode == "not_found":
                return []
            if mode == "overflow":
                value[0]["state"] = "overflow"
        if path.endswith("/versions/9"):
            if mode == "without_files":
                value["state"] = "without_files"
            if mode in ("collapsed", "too_large"):
                value["diffs"][0][mode] = True
            if mode == "real_size":
                value["real_size"] = "2"
            if mode == "version_id":
                value["id"] = 10
            if mode == "refs":
                value["start_commit_sha"] = "d" * 40
            if mode == "mr_id":
                value["merge_request_id"] = 121
            if mode == "duplicate":
                value["diffs"] *= 2
                value["real_size"] = "2"
            if mode == "malformed_flags":
                value["diffs"][0]["deleted_file"] = "false"
        return value

    http, _ = gitlab_http(modify=modify)
    with pytest.raises(SourceError):
        fetch_source(URL, http=http)


def test_gitlab_absent_patch_and_empty_rename_are_not_binary():
    for rename in (False, True):
        rows = [
            {
                "old_path": "old.py" if rename else "sample.py",
                "new_path": "sample.py",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": rename,
            }
        ]
        http, _ = gitlab_http(rows=rows)
        with pytest.raises(SourceError, match="SOURCE_PATCH_MISSING"):
            fetch_source(URL, http=http)


def test_gitlab_global_incomplete_cannot_be_excluded_by_extension():
    def modify(path, value):
        if path.endswith("/versions/9"):
            value["diffs"][0].update(old_path="doc.md", new_path="doc.md", too_large=True, diff="")
        return value

    http, _ = gitlab_http(modify=modify)
    with pytest.raises(SourceError, match="SOURCE_VERSION_INCOMPLETE"):
        fetch_source(URL, http=http)


def test_gitlab_empty_and_nonpython_scope():
    http, _ = gitlab_http(rows=[])
    assert fetch_source(URL, http=http).manifest.coverage == "EMPTY"
    rows = [
        {
            "old_path": "doc.md",
            "new_path": "doc.md",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
        }
    ]
    http, _ = gitlab_http(rows=rows)
    assert fetch_source(URL, http=http).manifest.coverage == "EXCLUDED_ONLY"


def test_source_secrets_redacted_and_irrelevant_metadata_not_saved():
    secret = "ghp_" + "x" * 36

    def modify(path, value):
        if path.endswith("/versions/9"):
            value["diffs"][0]["diff"] = PATCH.replace("guard = True", f'credential = "{secret}"')
            value["title"] = secret
        return value

    http, _ = gitlab_http(modify=modify)
    source = fetch_source(URL, http=http)
    assert secret not in source.model_dump_json()
    assert source.manifest.redactions > 0
    assert "title" not in source.manifest.model_dump()
    Safety().require_safe(source.model_dump())
