from copy import deepcopy

from review_agent.contracts import AgentError


class SnapshotView:
    """Only sanitized data is injected; no path, Storage or credential capability."""

    def __init__(self, snapshot, unit, permissions):
        self._files = deepcopy(snapshot["files"])
        self._hunks = {
            h["hunk_id"]: deepcopy(h) for h in snapshot["hunks"] if h["hunk_id"] in unit["hunk_ids"]
        }
        self._permissions = frozenset(permissions)

    def _allow(self, permission):
        if permission not in self._permissions:
            raise AgentError("TOOL_PERMISSION_DENIED")

    def files(self):
        self._allow("snapshot.files")
        return deepcopy(self._files)

    def hunks(self):
        self._allow("unit.hunks")
        return deepcopy(list(self._hunks.values()))

    def hunk(self, hunk_id):
        self._allow("unit.hunks")
        if hunk_id not in self._hunks:
            raise AgentError("TOOL_REFERENCE_DENIED")
        return deepcopy(self._hunks[hunk_id])
