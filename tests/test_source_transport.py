import socket
import threading
import time

import pytest
from test_sources_common import JSONTransport

from review_agent.sources.contracts import SourceError, SourceLimits
from review_agent.sources.http import SourceHTTP


def pin_mock(
    monkeypatch,
    *,
    peer="8.8.8.8",
    status=200,
    encoding="identity",
    length=None,
    chunks=(),
    headers=None,
    connect_gate=None,
):
    import review_agent.sources.http as mod

    seen = []
    data = iter(chunks)

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, address):
            seen.append(("connect", address))
            if connect_gate:
                connect_gate.wait(1)

        def getpeername(self):
            return (peer, 443)

        def shutdown(self, *a):
            seen.append(("shutdown",))

        def close(self):
            seen.append(("close",))

    class Context:
        def wrap_socket(self, sock, server_hostname):
            seen.append(("tls", server_hostname))
            return sock

    class Response:
        def getheader(self, name, default=None):
            return {
                "Content-Encoding": encoding,
                "Content-Type": "application/json",
                **(headers or {}),
            }.get(name, default)

        def read1(self, size):
            seen.append(("read", size))
            return next(data, b"")

    Response.status = status
    Response.length = length

    class Connection:
        def __init__(self, host, **kwargs):
            self.sock = None

        def request(self, method, path, headers):
            seen.append(("request", headers))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(mod.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", Connection)
    return seen


def test_peer_mismatch_stops_before_tls_or_headers(monkeypatch):
    seen = pin_mock(monkeypatch, peer="1.1.1.1")
    with pytest.raises(SourceError, match="SOURCE_PEER_MISMATCH"):
        SourceHTTP("api.github.com").get("/x")
    assert not any(event[0] in ("tls", "request") for event in seen)


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500])
def test_http_failures_never_follow_redirect_or_retry(monkeypatch, status):
    seen = pin_mock(monkeypatch, status=status, headers={"Location": "https://127.0.0.1/"})
    client = SourceHTTP("api.github.com")
    with pytest.raises(SourceError, match="SOURCE_HTTP_STATUS"):
        client.get("/x")
    assert sum(event[0] == "connect" for event in seen) == 1
    assert not any(event[0] == "read" for event in seen)
    assert client.events[0]["status"] == status


def test_compressed_response_rejected_before_read(monkeypatch):
    seen = pin_mock(monkeypatch, encoding="gzip")
    with pytest.raises(SourceError, match="SOURCE_ENCODING_UNSUPPORTED"):
        SourceHTTP("api.github.com").get("/x")
    assert not any(event[0] == "read" for event in seen)


def test_pinned_stream_detects_truncated_content_length(monkeypatch):
    pin_mock(monkeypatch, length=2)
    with pytest.raises(SourceError, match="SOURCE_TRUNCATED_RESPONSE"):
        SourceHTTP("api.github.com").get("/x")


def test_response_limit_stops_pinned_stream_and_closes_socket(monkeypatch):
    seen = pin_mock(monkeypatch, chunks=[b" " * 8] * 100)
    with pytest.raises(SourceError, match="SOURCE_RESPONSE_LIMIT"):
        SourceHTTP("api.github.com", limits=SourceLimits(max_response_bytes=10)).get("/x")
    assert sum(event[0] == "read" for event in seen) == 2
    assert ("close",) in seen


def test_connection_deadline_closes_socket_and_prevents_late_request(monkeypatch):
    gate = threading.Event()
    seen = pin_mock(monkeypatch, connect_gate=gate)
    try:
        with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
            SourceHTTP("api.github.com", limits=SourceLimits(request_seconds=0.03)).get("/x")
        assert ("shutdown",) in seen
    finally:
        gate.set()
    time.sleep(0.02)
    assert not any(event[0] == "request" for event in seen)


def test_dns_answers_must_all_be_public_and_port_443(monkeypatch):
    for addresses in ([("8.8.8.8", 443), ("10.0.0.1", 443)], [("8.8.8.8", 80)]):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", p) for p in addresses
            ],
        )
        with pytest.raises(SourceError, match="SOURCE_DNS_BLOCKED"):
            SourceHTTP("api.github.com").get("/x")


def test_response_request_identity_is_retained(monkeypatch):
    pin_mock(monkeypatch, chunks=[b"{}"], headers={"X-GitHub-Request-Id": "A1:B2", "ETag": '"abc"'})
    client = SourceHTTP("api.github.com")
    assert client.get("/x") == {}
    assert client.events[0]["request_id"] == "A1:B2"
    assert client.events[0]["etag"] == '"abc"'


def test_credential_is_only_in_host_specific_header_and_anonymous_ignores_environment(monkeypatch):
    from test_sources_common import URL, github_http

    from review_agent.sources import service

    secret = "synthetic-private-credential"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    http, transport = github_http()
    source = service.fetch_source(URL, http=http)
    assert not any("Authorization" in headers for _, _, headers in transport.requests)
    assert secret not in source.model_dump_json()
    for host in ("api.github.com", "gitlab.com"):
        transport = JSONTransport(lambda _: {})
        client = SourceHTTP(host, credential=secret, transport=transport)
        client.get("/x")
        headers = transport.requests[0][2]
        field = "Authorization" if host == "api.github.com" else "PRIVATE-TOKEN"
        assert headers[field] == ("Bearer " + secret if host == "api.github.com" else secret)
        assert secret not in str(client.events)


def test_raw_exception_never_escapes():
    def transport(*a):
        raise RuntimeError("synthetic-secret-body")

    client = SourceHTTP("api.github.com", transport=transport)
    with pytest.raises(SourceError) as failure:
        client.get("/x")
    assert str(failure.value) == "SOURCE_NETWORK_ERROR"
    assert "synthetic-secret-body" not in str(client.events)


def test_opt_in_source_credential_is_sanitized_before_publication(monkeypatch):
    from test_sources_common import URL, github_http

    from review_agent.sources import service

    secret = "synthetic-private-credential"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    def modify(path, value):
        if "/compare/" in path:
            value["files"][0]["patch"] = value["files"][0]["patch"].replace("True", secret)
        elif "/files?" in path:
            value[0]["patch"] = value[0]["patch"].replace("True", secret)
        return value

    _, transport = github_http(modify=modify)
    monkeypatch.setattr(
        service, "SourceHTTP", lambda host, **kw: SourceHTTP(host, transport=transport, **kw)
    )
    source = service.fetch_source(URL, authenticate=True)
    assert all(
        headers["Authorization"] == "Bearer " + secret for _, _, headers in transport.requests
    )
    assert secret not in source.model_dump_json()
    assert source.manifest.redactions > 0


def test_completed_http_does_not_publish_if_postprocessing_exceeds_deadline(monkeypatch):
    from test_sources_common import URL, github_http

    from review_agent.sources import service

    http, _ = github_http()
    original = service.validate_source

    def delayed(source, safety):
        result = original(source, safety)
        http.deadline = time.monotonic() - 1
        return result

    monkeypatch.setattr(service, "validate_source", delayed)
    with pytest.raises(SourceError, match="SOURCE_TIMEOUT"):
        service.fetch_source(URL, http=http)


def test_real_httpresponse_connection_close_after_last_payload(monkeypatch):
    import http.client
    import io

    import review_agent.sources.http as mod

    class Sock:
        connection_closed = False
        body_closed = False

        def settimeout(self, value):
            if self.connection_closed and self.body_closed:
                raise OSError("closed socket")

        def connect(self, address):
            pass

        def getpeername(self):
            return ("8.8.8.8", 443)

        def close(self):
            self.connection_closed = True

        def shutdown(self, *args):
            pass

        def makefile(self, *args):
            owner = self

            class Body(io.BytesIO):
                def close(self):
                    owner.body_closed = True
                    super().close()

            return Body(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n"
                b"Content-Type: application/json\r\n\r\n{}"
            )

    class Context:
        def wrap_socket(self, sock, server_hostname):
            return sock

    class Connection:
        def __init__(self, *a, **k):
            self.sock = None

        def request(self, *a, **k):
            pass

        def getresponse(self):
            response = http.client.HTTPResponse(self.sock)
            response.begin()
            assert response.will_close
            self.close()  # Same ownership transfer as the real HTTPConnection.
            return response

        def close(self):
            self.sock.close()

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(mod.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", Connection)
    client = SourceHTTP("api.github.com")
    assert client.get("/x") == {}
    assert client.events[0]["bytes"] == 2 and client.events[0]["status"] == 200
