"""Local rules, no network, raw-value logs, repository configuration or callbacks."""

import re
import tomllib

from review_agent.config import package_text
from review_agent.contracts import AgentError, json_text


class Safety:
    def __init__(self, exact_secrets: tuple[str, ...] = ()):
        rules = tomllib.loads(package_text("policies/secret-rules.toml"))
        self.token = re.compile(rules["token_pattern"])
        self.assignment = re.compile(rules["assignment_pattern"])
        self.begin = re.compile(rules["private_key_begin"])
        self.end = re.compile(rules["private_key_end"])
        self.exact_secrets = tuple(secret for secret in exact_secrets if len(secret) >= 8)

    def sanitize(self, text: str, *, diff: bool = False) -> tuple[str, int]:
        try:
            return self._scan(text, diff=diff)
        except AgentError:
            raise
        except Exception:
            raise AgentError("SAFETY_SCAN_FAILED") from None

    def _scan(self, text: str, *, diff: bool) -> tuple[str, int]:
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise AgentError("UNSAFE_CONTROL_CHARACTER")
        result, redactions, in_key = [], 0, False
        for line in text.splitlines(keepends=True):
            original = line
            if self.begin.search(line):
                in_key = True
            if in_key:
                end_key = bool(self.end.search(line))
                prefix = line[:1] if diff and line[:1] in ("+", "-", " ") else ""
                ending = "\n" if line.endswith("\n") else ""
                line = prefix + "[REDACTED_PRIVATE_KEY]" + ending
                redactions += 1
                in_key = not end_key
            else:
                for secret in self.exact_secrets:
                    count = line.count(secret)
                    if count:
                        line = line.replace(secret, "[REDACTED_CREDENTIAL]")
                        redactions += count
                line, count = self.token.subn("[REDACTED_TOKEN]", line)
                redactions += count

                def replace(match):
                    if match[2].startswith("[REDACTED"):
                        return match[0]
                    return match[1] + "[REDACTED_SECRET]" + match[3]

                replaced = self.assignment.sub(replace, line)
                redactions += int(replaced != line)
                line = replaced
            if diff and original.startswith(("diff --git ", "--- ", "+++ ", "@@")):
                if line != original:
                    raise AgentError("SECRET_IN_DIFF_METADATA")
            result.append(line)
        if in_key:
            raise AgentError("UNCLOSED_PRIVATE_KEY")
        return "".join(result), redactions

    def require_safe(self, value) -> None:
        _, changes = self.sanitize(json_text(value))
        if changes:
            raise AgentError("UNSAFE_REQUEST")
