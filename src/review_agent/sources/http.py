"""Bounded HTTPS GET with validated DNS addresses pinned to the actual socket."""

import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from review_agent.sources.contracts import SourceError, SourceLimits


class RequestControl:
    def __init__(self, deadline):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.socket = None
        self.lock = threading.Lock()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if self.cancelled.is_set() or left <= 0:
            raise SourceError("SOURCE_TIMEOUT")
        return left

    def attach(self, sock):
        with self.lock:
            if self.cancelled.is_set():
                sock.close()
                raise SourceError("SOURCE_TIMEOUT")
            self.socket = sock

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.socket.close()


def public_address(address):
    try:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            public_address(str(ip.ipv4_mapped))
        return str(ip)
    except ValueError:
        raise SourceError("SOURCE_DNS_BLOCKED") from None


class PinnedHTTPS:
    def __call__(self, host, path, headers, control, consume):
        # Runs in the request worker: DNS is also covered by the caller's deadline.
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        control.remaining()
        if not addresses:
            raise SourceError("SOURCE_DNS_BLOCKED")
        for family, kind, protocol, _, address in addresses:
            if (
                family not in (socket.AF_INET, socket.AF_INET6)
                or kind != socket.SOCK_STREAM
                or address[1] != 443
                or protocol not in (0, socket.IPPROTO_TCP)
            ):
                raise SourceError("SOURCE_DNS_BLOCKED")
            public_address(address[0])
        family, kind, protocol, _, address = addresses[0]
        sock = socket.socket(family, kind, protocol)
        connection = None
        control.attach(sock)
        try:
            sock.settimeout(control.remaining())
            # Numeric sockaddr only; no second DNS lookup or environment proxy.
            sock.connect(address)
            if public_address(sock.getpeername()[0]) != public_address(address[0]):
                raise SourceError("SOURCE_PEER_MISMATCH")
            sock.settimeout(control.remaining())
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            control.attach(sock)
            if public_address(sock.getpeername()[0]) != public_address(address[0]):
                raise SourceError("SOURCE_PEER_MISMATCH")
            connection = http.client.HTTPSConnection(host, timeout=control.remaining())
            connection.sock = sock
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            control.remaining()
            if response.status != 200:
                raise SourceError("SOURCE_HTTP_STATUS", {"http_status": response.status})
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise SourceError("SOURCE_ENCODING_UNSUPPORTED")
            media = response.getheader("Content-Type", "").split(";")[0].lower()
            if media != "application/json" and not media.endswith("+json"):
                raise SourceError("SOURCE_CONTENT_TYPE")
            while True:
                sock.settimeout(control.remaining())
                chunk = response.read1(65536)
                control.remaining()
                if not chunk:
                    if response.length not in (None, 0):
                        raise SourceError("SOURCE_TRUNCATED_RESPONSE")
                    break
                consume(chunk)
                # HTTPResponse can close its final socket reference on the last payload read.
                # Do not touch that socket again once the declared body is complete.
                if response.length == 0:
                    break
            metadata = {}
            for field, header in (
                (
                    "request_id",
                    "X-GitHub-Request-Id" if host == "api.github.com" else "X-Request-Id",
                ),
                ("etag", "ETag"),
            ):
                value = response.getheader(header)
                if value is not None:
                    if not re.fullmatch(r'[A-Za-z0-9:._"/\-]{1,160}', value):
                        raise SourceError("SOURCE_INVALID_METADATA")
                    metadata[field] = value
            return 200, metadata
        finally:
            if connection:
                connection.close()
            sock.close()


class SourceHTTP:
    def __init__(self, host, *, credential=None, limits=None, transport=None):
        if host not in ("api.github.com", "gitlab.com"):
            raise SourceError("SOURCE_HOST_BLOCKED")
        self.host = host
        self.limits = limits or SourceLimits()
        self.transport = transport or PinnedHTTPS()
        self.deadline = time.monotonic() + self.limits.total_seconds
        self._credential = credential
        self.events = []
        self.bytes = 0

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise SourceError("SOURCE_TIMEOUT", {"stage": "SOURCE_PREPARATION"})

    def get(self, path):
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or parsed.fragment
            or any(ord(c) <= 32 or ord(c) >= 127 for c in path)
            or "\\" in path
        ):
            raise SourceError("SOURCE_REQUEST_PATH")
        if len(self.events) >= self.limits.max_requests:
            raise SourceError("SOURCE_REQUEST_LIMIT")
        control = RequestControl(min(self.deadline, time.monotonic() + self.limits.request_seconds))
        control.remaining()
        event = {"request_no": len(self.events) + 1, "status": None, "bytes": 0, "error": None}
        self.events.append(event)
        chunks, channel = [], queue.Queue(maxsize=1)
        headers = {
            "Host": self.host,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "review-agent-source/1",
            "Connection": "close",
        }
        if self.host == "api.github.com":
            headers["X-GitHub-Api-Version"] = "2026-03-10"
            headers["Accept"] = "application/vnd.github+json"
        if self._credential:
            headers["Authorization" if self.host == "api.github.com" else "PRIVATE-TOKEN"] = (
                "Bearer " + self._credential if self.host == "api.github.com" else self._credential
            )

        def consume(chunk):
            control.remaining()
            event["bytes"] += len(chunk)
            self.bytes += len(chunk)
            if self.bytes > self.limits.max_response_bytes:
                raise SourceError("SOURCE_RESPONSE_LIMIT")
            chunks.append(chunk)

        def worker():
            try:
                response = self.transport(self.host, path, headers, control, consume)
                channel.put((response, None))
            except SourceError as error:
                channel.put((None, error))
            except Exception:
                channel.put((None, SourceError("SOURCE_NETWORK_ERROR")))

        started = time.monotonic()
        threading.Thread(target=worker, daemon=True).start()
        try:
            try:
                status, error = channel.get(timeout=control.remaining())
            except queue.Empty:
                raise SourceError("SOURCE_TIMEOUT") from None
            control.remaining()
            if error:
                raise error
            if isinstance(status, tuple):
                status, metadata = status
                event.update(metadata)
            event["status"] = status
            if status != 200:
                raise SourceError("SOURCE_HTTP_STATUS", {"http_status": status})
            try:
                value = json.loads(b"".join(chunks))
            except (ValueError, RecursionError):
                raise SourceError("SOURCE_INVALID_JSON") from None
            control.remaining()
            return value
        except SourceError as error:
            event["error"] = error.code
            event["status"] = error.diagnostics.get("http_status", event["status"])
            raise
        finally:
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            control.cancel()
