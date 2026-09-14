"""Loopback-only HTTP adapter with memory-only credentials and no CORS."""

import json
import re
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from pydantic import ValidationError

from review_agent.contracts import AgentError
from review_agent.workbench.service import (
    DEMOS,
    APIError,
    CredentialSettings,
    ResumeRequest,
    Submission,
    Workbench,
    demo_text,
)

MAX_BODY = 7 * 1024 * 1024


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_KEY")
        value[key] = item
    return value


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, workbench, port=0):
        self.workbench = workbench
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = f"review_workbench_{self.server_port}"

    def handle_error(self, request, client_address):
        # Never log HTTP bodies or exception representations.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewWorkbench"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def reply(
        self, status, body, mime="application/json; charset=utf-8", *,
        filename=None, bootstrap=False
    ):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'none'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'"
        ))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if bootstrap:
            self.send_header("Set-Cookie", (
                f"{self.server.cookie_name}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            ))
        self.end_headers()
        self.wfile.write(body)

    def check(self, *, api=False, mutation=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            raise APIError("HOST_REJECTED", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise APIError("ORIGIN_REJECTED", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError("ORIGIN_REJECTED", 403)
        if mutation and origin != self.server.origin:
            raise APIError("ORIGIN_REQUIRED", 403)
        authorized = secrets.compare_digest(
            self.headers.get("X-Workbench-Token", ""), self.server.token
        )
        # Download navigation cannot set a custom header. Only these read-only
        # attachments accept the same-origin session cookie; mutations never do.
        if api and not mutation and re.fullmatch(
            r"/api/jobs/[a-zA-Z0-9_-]{16,64}/(report\.md|audit\.md|trace\.json|report\.html)",
            urlsplit(self.path).path,
        ):
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(self.server.cookie_name)
            authorized = authorized or (value is not None and secrets.compare_digest(
                value.value, self.server.token
            ))
        if api and not authorized:
            raise APIError("SESSION_REQUIRED", 403)

    def dispatch(self, mutation=False):
        path = urlsplit(self.path).path
        self.check(api=path.startswith("/api/") and path != "/api/bootstrap", mutation=mutation)
        workbench = self.server.workbench
        if mutation:
            if self.headers.get("Content-Type") != "application/json":
                raise APIError("JSON_REQUIRED", 415)
            if self.headers.get("Transfer-Encoding"):
                raise APIError("INVALID_LENGTH", 400)
            length = self.headers.get("Content-Length", "")
            limit = 4096 if path == "/api/settings" else MAX_BODY
            if not length.isdecimal() or not 0 < int(length) <= limit:
                raise APIError("INPUT_TOO_LARGE", 413)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise APIError("INVALID_BODY")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
            if path == "/api/settings":
                return self.reply(200, workbench.configure(CredentialSettings.model_validate(body)))
            if path == "/api/jobs":
                return self.reply(202, workbench.submit(Submission.model_validate(body)))
            match = re.fullmatch(r"/api/jobs/([a-zA-Z0-9_-]{16,64})/resume", path)
            if match:
                return self.reply(202, workbench.resume(
                    match[1], ResumeRequest.model_validate(body)
                ))
        elif path == "/api/bootstrap":
            return self.reply(200, {
                "token": self.server.token, **workbench.settings(),
                "review_version": "semantic-s2", "reply_protocol": "json-unique-keys-v1",
                "demos": [{"id": k, "title": v[0], "diff": demo_text(k, 1)}
                          for k, v in DEMOS.items()],
                "input_limit_bytes": 1048576,
            }, bootstrap=True)
        elif path in ("/", "/app.js", "/app.css"):
            name, mime = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
            }[path]
            asset = files("review_agent.workbench").joinpath(name).read_bytes()
            return self.reply(200, asset, mime)
        elif path == "/api/jobs":
            return self.reply(200, workbench.list_jobs())
        elif path == "/api/settings":
            return self.reply(200, workbench.settings())
        else:
            match = re.fullmatch(
                r"/api/jobs/([a-zA-Z0-9_-]{16,64})"
                r"(?:/(report\.md|audit\.md|trace\.json|report\.html))?", path
            )
            if match:
                if match[2]:
                    raw, mime = workbench.download(match[1], match[2])
                    return self.reply(200, raw, mime, filename=match[2])
                return self.reply(200, workbench.detail(match[1]))
        raise APIError("NOT_FOUND", 404)

    def guarded(self, mutation=False):
        try:
            self.dispatch(mutation)
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            self.reply(400, {"error": "INVALID_INPUT"})
        except APIError as error:
            self.reply(error.status, {"error": error.code})
        except AgentError as error:
            code = error.code if re.fullmatch(r"[A-Z_]{1,100}", error.code) else "READ_FAILED"
            self.reply(409, {"error": code})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.reply(500, {"error": "WORKBENCH_ERROR"})

    def do_GET(self):
        self.guarded()

    def do_POST(self):
        self.guarded(mutation=True)


def serve(state_dir, *, port=8765, allow_live=False):
    if not 0 <= port <= 65535:
        raise AgentError("INVALID_PORT")
    workbench = Workbench(state_dir, allow_live=allow_live)
    server = None
    try:
        server = Server(workbench, port)
        print(json.dumps({
            "mode": "local_workbench", "url": server.origin,
            "live_enabled": allow_live, "review_version": "semantic-s2",
        }), flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        workbench.close()
