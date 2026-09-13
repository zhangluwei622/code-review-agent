import json
import socket
import threading
import time

import pytest

from review_agent.safety import Safety
from review_agent.sources import github
from review_agent.sources.contracts import SourceError, SourceLimits
from review_agent.sources.http import PinnedHTTPS, SourceHTTP
from review_agent.sources.url import parse_url

BASE, HEAD, MERGE = "a" * 40, "b" * 40, "c" * 40
URL = "https://github.com/example/project/pull/7"
PATCH = "@@ -1,2 +1 @@\n-guard = True\n value = 1\n"


def github_meta(files=1, added=0, removed=1):
    return {
        "number": 7,
        "base": {"sha": BASE, "repo": {"id": 1, "full_name": "example/project"}},
        "head": {"sha": HEAD, "repo": {"id": 2}},
        "changed_files": files,
        "additions": added,
        "deletions": removed,
    }


def github_file(path="sample.py", patch=PATCH):
    return {
        "filename": path,
        "status": "modified",
        "additions": 0,
        "deletions": 1,
        "changes": 1,
        **({"patch": patch} if patch is not None else {}),
    }


class JSONTransport:
    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, host, path, headers, control, consume):
        self.requests.append((host, path, dict(headers)))
        data = json.dumps(self.handler(path)).encode()
        for offset in range(0, len(data), 17):
            consume(data[offset : offset + 17])
        return 200


def github_http(rows=None, modify=None, limits=None):
    rows = [github_file()] if rows is None else rows
    meta = github_meta(
        len(rows), sum(r["additions"] for r in rows), sum(r["deletions"] for r in rows)
    )

    def handler(path):
        if "/compare/" in path:
            start = BASE if BASE + "..." in path else MERGE
            value = {
                "base_commit": {"sha": start},
                "merge_base_commit": {"sha": MERGE},
                "files": rows,
            }
        elif "/files?" in path:
            page = int(path.rsplit("=", 1)[1])
            value = rows[(page - 1) * 30 : page * 30]
        else:
            value = meta
        return modify(path, value) if modify else value

    transport = JSONTransport(handler)
    return SourceHTTP("api.github.com", transport=transport, limits=limits), transport


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/pull/1",
        "https://github.com:443/o/r/pull/1",
        "https://x@github.com/o/r/pull/1",
        "https://github.com/o/r/pull/1?x=y",
        "https://github.com/o/r/pull/1#files",
        "https://github.com/o/r/pull/1/",
        "https://github.com/o/%2e%2e/pull/1",
        "https://github.com.evil/o/r/pull/1",
        "https://127.0.0.1/o/r/pull/1",
        "https://gitlab.com/a/-/merge_requests/1",
        "https://gitlab.com/a/b/-/merge_requests/01",
        "https://github.com/a/b/pull/1\n",
    ],
)
def test_source_url_rejection(url):
    with pytest.raises(SourceError, match="INVALID_SOURCE_URL"):
        parse_url(url)


def test_nested_gitlab_url():
    url = parse_url("https://gitlab.com/team/subgroup/repo/-/merge_requests/12")
    assert url.repository == "team/subgroup/repo" and url.number == 12


def test_github_uses_fixed_merge_base_and_fork_identity():
    http, transport = github_http()
    source = github.fetch(parse_url(URL), http, Safety())
    assert source.manifest.base_sha == MERGE
    assert source.manifest.target_sha == BASE
    assert source.manifest.head_sha == HEAD
    assert source.manifest.coverage == "PYTHON_COMPLETE"
    assert len(transport.requests) == 5
    assert "/compare/" + MERGE + "..." + HEAD in transport.requests[2][1]
    assert all("Authorization" not in row[2] for row in transport.requests)


def test_github_missing_patch_is_not_an_exclusion():
    http, _ = github_http([github_file(patch=None)])
    with pytest.raises(SourceError, match="SOURCE_PATCH_MISSING"):
        github.fetch(parse_url(URL), http, Safety())
    http, _ = github_http([github_file(path="notes.md", patch=None)])
    source = github.fetch(parse_url(URL), http, Safety())
    assert source.manifest.coverage == "EXCLUDED_ONLY"
    assert source.manifest.files[0].reason == "NON_PYTHON"
    assert source.manifest.files[0].patch_state == "OMITTED_EXCLUDED"


@pytest.mark.parametrize(
    "patch",
    [
        "@@ -1,3 +1 @@\n-a\n b\n",
        "@@ -1,2 +1 @@\n-a\n b\n@@ -5,2 +5,1 @@\n",
        PATCH + "ignored trailing text\n",
    ],
)
def test_github_incomplete_patch(patch):
    http, _ = github_http([github_file(patch=patch)])
    with pytest.raises(SourceError, match="SOURCE_PATCH_INCOMPLETE"):
        github.fetch(parse_url(URL), http, Safety())


def test_github_empty_and_pagination():
    http, _ = github_http([])
    assert github.fetch(parse_url(URL), http, Safety()).manifest.coverage == "EMPTY"
    http, transport = github_http([github_file(path=f"f{i}.py") for i in range(30)])
    assert len(github.fetch(parse_url(URL), http, Safety()).manifest.files) == 30
    assert any("/files?per_page=30&page=2" in path for _, path, _ in transport.requests)


@pytest.mark.parametrize("mode", ["version", "comparison", "count", "duplicate", "lines"])
def test_github_inconsistent_sources_fail(mode):
    reads = 0

    def modify(path, value):
        nonlocal reads
        value = json.loads(json.dumps(value))
        if "/compare/" not in path and "/files?" not in path:
            reads += 1
            if mode == "version" and reads == 2:
                value["head"]["sha"] = "d" * 40
            if mode == "lines":
                value["additions"] = 3
        if mode == "comparison" and "/files?" in path:
            value[0]["patch"] = PATCH.replace("1", "2")
        if mode == "count" and "/compare/" + MERGE in path:
            value["files"] = []
        if mode == "duplicate" and "/files?" in path:
            value = value + value
        return value

    http, _ = github_http(modify=modify)
    with pytest.raises(SourceError):
        github.fetch(parse_url(URL), http, Safety())


def test_http_byte_and_request_limits_apply_during_consumption():
    chunks = []

    def transport(host, path, headers, control, consume):
        for n in range(10):
            chunks.append(n)
            consume(b"x" * 8)
        return 200

    http = SourceHTTP(
        "api.github.com", transport=transport, limits=SourceLimits(max_response_bytes=10)
    )
    with pytest.raises(SourceError, match="SOURCE_RESPONSE_LIMIT"):
        http.get("/x")
    assert chunks == [0, 1]
    http = SourceHTTP(
        "api.github.com", transport=JSONTransport(lambda _: {}), limits=SourceLimits(max_requests=1)
    )
    http.get("/x")
    with pytest.raises(SourceError, match="SOURCE_REQUEST_LIMIT"):
        http.get("/y")


def test_http_hanging_headers_and_total_deadline_stop_without_retry():
    gate = threading.Event()
    calls = []

    def transport(host, path, headers, control, consume):
        calls.append(path)
        gate.wait(1)
        control.remaining()
        return 200

    http = SourceHTTP(
        "api.github.com",
        transport=transport,
        limits=SourceLimits(total_seconds=0.05, request_seconds=0.1),
    )
    start = time.monotonic()
    try:
        with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
            http.get("/x")
        assert time.monotonic() - start < 0.4
        with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
            http.get("/x")
        assert calls == ["/x"]
    finally:
        gate.set()


def test_slow_chunks_obey_total_deadline():
    def transport(host, path, headers, control, consume):
        for _ in range(30):
            time.sleep(0.01)
            consume(b" ")
        return 200

    http = SourceHTTP(
        "api.github.com",
        transport=transport,
        limits=SourceLimits(total_seconds=0.04, request_seconds=1),
    )
    with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
        http.get("/x")
    assert http.bytes < 30


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.2.1.1", "169.254.169.254", "::1", "fc00::1"])
def test_dns_rejects_nonpublic_before_socket(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )
    http = SourceHTTP("api.github.com")
    with pytest.raises(SourceError, match="SOURCE_DNS_BLOCKED"):
        http.get("/x")


def test_dns_is_bounded_and_never_connects_after_timeout(monkeypatch):
    gate = threading.Event()
    connected = []

    def resolve(*a, **k):
        gate.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", lambda *a: connected.append(a))
    http = SourceHTTP("api.github.com", limits=SourceLimits(request_seconds=0.03))
    try:
        with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
            http.get("/x")
    finally:
        gate.set()
    time.sleep(0.02)
    assert not connected


def test_pinned_socket_tls_host_and_no_dns_relookup(monkeypatch):
    import review_agent.sources.http as mod

    seen = []

    class Sock:
        def settimeout(self, v):
            pass

        def connect(self, addr):
            seen.append(("connect", addr))

        def getpeername(self):
            return ("8.8.8.8", 443)

        def close(self):
            pass

        def shutdown(self, *a):
            pass

    class Context:
        def wrap_socket(self, sock, server_hostname):
            seen.append(("tls", server_hostname))
            return sock

    class Response:
        status = 200
        length = 0

        def getheader(self, name, default=None):
            return "application/json" if name == "Content-Type" else default

        def read1(self, n):
            return b""

    class Connection:
        def __init__(self, host, **k):
            seen.append(("host", host))

        def request(self, method, path, headers):
            seen.append(("request", headers["Host"]))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    def resolve(host, *a, **k):
        seen.append(("dns", host))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(mod.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", Connection)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    from review_agent.sources.http import RequestControl

    PinnedHTTPS()(
        "api.github.com",
        "/x",
        {"Host": "api.github.com"},
        RequestControl(time.monotonic() + 1),
        lambda _: None,
    )
    assert seen == [
        ("dns", "api.github.com"),
        ("connect", ("8.8.8.8", 443)),
        ("tls", "api.github.com"),
        ("host", "api.github.com"),
        ("request", "api.github.com"),
    ]


@pytest.mark.parametrize(
    "change,old,new,patch,added,removed",
    [
        ("added", "new.py", "new.py", "@@ -0,0 +1 @@\n+x=1\n", 1, 0),
        ("removed", "old.py", "old.py", "@@ -1 +0,0 @@\n-x=1\n", 0, 1),
        ("renamed", "old.py", "new.py", PATCH, 0, 1),
    ],
)
def test_added_removed_and_renamed_paths_preserve_evidence_sides(
    change, old, new, patch, added, removed
):
    from review_agent.sources.service import fetch_source

    row = {
        "filename": new,
        "status": change,
        "patch": patch,
        "additions": added,
        "deletions": removed,
        "changes": added + removed,
    }
    if change == "renamed":
        row["previous_filename"] = old
    http, _ = github_http([row])
    result = fetch_source(URL, http=http)
    assert result.manifest.files[0].old_path == old and result.manifest.files[0].new_path == new
    assert result.manifest.coverage == "PYTHON_COMPLETE"
    if change == "removed":
        assert "--- a/old.py\n+++ /dev/null" in result.safe_diff
    if change == "added":
        assert "--- /dev/null\n+++ b/new.py" in result.safe_diff


@pytest.mark.parametrize(
    "change,source,target",
    [
        ("modified", "a/b.py", "b/b.py"),
        ("added", "/dev/null", "b/b.py"),
        ("removed", "a/b.py", "/dev/null"),
    ],
)
def test_explicit_binary_marker_is_a_verified_exclusion(change, source, target):
    from review_agent.sources.service import fetch_source

    row = {
        "filename": "b.py",
        "status": change,
        "patch": f"Binary files {source} and {target} differ\n",
        "additions": 0,
        "deletions": 0,
        "changes": 0,
    }
    http, _ = github_http([row])
    result = fetch_source(URL, http=http)
    assert result.manifest.coverage == "EXCLUDED_ONLY"
    assert result.manifest.files[0].reason == "BINARY"


def test_github_empty_python_rename_and_mode_changes_are_not_proven_noop():
    from review_agent.sources.service import fetch_source

    for status in ("renamed", "modified"):
        row = {
            "filename": "b.py",
            "previous_filename": "a.py",
            "status": status,
            "additions": 0,
            "deletions": 0,
            "changes": 0,
        }
        http, _ = github_http([row])
        with pytest.raises(SourceError, match="SOURCE_PATCH_MISSING"):
            fetch_source(URL, http=http)


@pytest.mark.parametrize(
    "path", ["../x.py", "/x.py", "dir/../x.py", "two words.py", "x\ny.py", "dir\\x.py"]
)
def test_untrusted_source_paths_never_reach_diff_headers(path):
    from review_agent.sources.service import fetch_source

    http, _ = github_http([github_file(path=path)])
    with pytest.raises(SourceError, match="UNSAFE_SOURCE_PATH"):
        fetch_source(URL, http=http)


def test_source_hunk_and_diff_limits():
    from review_agent.sources.service import fetch_source

    for limits, error in [
        (SourceLimits(max_unit_bytes=10), "SOURCE_HUNK_LIMIT"),
        (SourceLimits(max_diff_bytes=10), "SOURCE_DIFF_LIMIT"),
    ]:
        http, _ = github_http(limits=limits)
        with pytest.raises(SourceError, match=error):
            fetch_source(URL, http=http)
