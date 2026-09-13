import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from review_agent.safety import Safety
from review_agent.sources.contracts import SourceError


@dataclass(frozen=True)
class SourceURL:
    provider: str
    repository: str
    number: int
    canonical: str

    @property
    def api_host(self):
        return "api.github.com" if self.provider == "github" else "gitlab.com"


def parse_url(value: str) -> SourceURL:
    try:
        if len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in ("github.com", "gitlab.com")
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in parsed.path
        ):
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if parsed.path != "/" + "/".join(parts):
            raise ValueError
        if parsed.netloc == "github.com":
            if len(parts) != 4 or parts[2] != "pull":
                raise ValueError
            repo, number, provider = parts[:2], parts[3], "github"
        else:
            if len(parts) < 5 or parts[-3:-1] != ["-", "merge_requests"]:
                raise ValueError
            repo, number, provider = parts[:-3], parts[-1], "gitlab"
        if not re.fullmatch(r"[1-9][0-9]{0,9}", number) or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) or p.endswith(".git") for p in repo
        ):
            raise ValueError
        Safety().require_safe(value)
        return SourceURL(provider, "/".join(repo), int(number), value)
    except Exception:
        raise SourceError("INVALID_SOURCE_URL") from None


def safe_path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(c.isspace() or ord(c) < 32 or c in "\\\"'" for c in value)
        or PurePosixPath(value).is_absolute()
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise SourceError("UNSAFE_SOURCE_PATH")
    Safety().require_safe(value)
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise SourceError("SOURCE_VERSION_NOT_READY")
    return value


def integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise SourceError("SOURCE_INVALID_METADATA")
    return value
