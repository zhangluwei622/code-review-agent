from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from review_agent.config import TaskConfig, policy_versions
from review_agent.contracts import AgentError, StrictModel, digest
from review_agent.ingest import prepare_diff, read_bounded
from review_agent.providers import FixtureProvider
from review_agent.review import evidence_index
from review_agent.safety import Safety

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Group(StrictModel):
    group_id: Identifier
    split: Literal["development", "holdout"]
    rationale: str = Field(min_length=1, max_length=3000)
    known_before_split: bool = False

    @model_validator(mode="after")
    def no_known_holdout(self):
        if self.known_before_split and self.split != "development":
            raise ValueError("known scenario belongs in development")
        return self


class Issue(StrictModel):
    issue_id: Identifier
    trigger: str = Field(min_length=1, max_length=2000)
    actual: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=2000)
    causality: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=20)
    confidence: Literal["high", "medium", "reference"]


class Case(StrictModel):
    case_id: Identifier
    group_id: Identifier
    title: str = Field(min_length=1, max_length=300)
    diff: str
    input_digest: Fingerprint
    fixture: str
    fixture_digest: Fingerprint
    classification: Literal["defect", "non_defect", "insufficient_evidence"]
    issues: list[Issue] = Field(max_length=20)
    must_not_report: list[str] = Field(max_length=20)
    rationale: str = Field(min_length=1, max_length=4000)
    tool_requirement: Literal["required", "optional", "not_needed", "unavailable"]
    tool_evidence: list[str] = Field(max_length=20)
    tool_rationale: str = Field(min_length=1, max_length=2000)
    provenance: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def labels(self):
        if (self.classification == "defect") != bool(self.issues):
            raise ValueError("defect labels need issues")
        if len({i.issue_id for i in self.issues}) != len(self.issues):
            raise ValueError("duplicate issue")
        if self.tool_requirement == "required" and not self.tool_evidence:
            raise ValueError("required tools need evidence targets")
        return self


class Dataset(StrictModel):
    schema_version: Literal[1] = 1
    dataset_id: Identifier
    annotation_status: Literal["PENDING_USER_CONFIRMATION"]
    groups: list[Group] = Field(min_length=1, max_length=50)
    cases: list[Case] = Field(min_length=1, max_length=100)


class Observation(StrictModel):
    observation_id: str
    case_id: Identifier
    group_id: Identifier
    split: Literal["development", "holdout"]
    input_digest: Fingerprint
    execution_mode: Literal["fixture", "historical_live", "historical_experiment", "baseline_live"]
    started: bool
    task_id: str | None
    unit_states: list[str]
    reasons: list[str]
    findings: list[dict]
    tools: list[dict]
    retrieved_evidence: list[str]
    consumed_evidence: list[str]
    sends: int = Field(ge=0)
    totals: dict[str, Annotated[int, Field(ge=0)]]
    model_identity: dict
    pricing: dict
    source: dict

    @model_validator(mode="after")
    def consistent_facts(self):
        if (
            self.execution_mode != "fixture"
            and self.model_identity.get("requested_model") == "fixture"
        ):
            raise ValueError("fixture observation cannot be relabeled as live")
        if set(self.totals) != {
            "settled_tokens",
            "settled_cost_nusd",
            "held_tokens",
            "held_cost_nusd",
        }:
            raise ValueError("all ledger totals must be explicit")
        if not self.started and (
            self.sends or any(self.totals.values()) or self.findings or self.tools
        ):
            raise ValueError("unstarted case cannot have execution facts")
        for finding in self.findings:
            if (
                not isinstance(finding.get("finding_id"), str)
                or finding.get("confidence") not in ("high", "medium", "reference")
                or any(
                    not isinstance(finding.get(key), list)
                    or any(not isinstance(ref, str) for ref in finding[key])
                    for key in ("evidence", "expectation_evidence")
                )
            ):
                raise ValueError("invalid finding observation")
        return self


class Observations(StrictModel):
    schema_version: Literal[1] = 1
    observations: list[Observation]


def read_model(path, model):
    text = read_bounded(Path(path), 4 * 1024 * 1024)
    Safety().require_safe(text)
    try:
        return model.model_validate_json(text)
    except ValidationError:
        raise AgentError("INVALID_EVALUATION_DATA") from None


def local_file(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise AgentError("EVALUATION_PATH_REJECTED")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise AgentError("EVALUATION_PATH_REJECTED")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise AgentError("EVALUATION_PATH_REJECTED")
    return current


def load_dataset(path):
    path = Path(path)
    dataset = read_model(path, Dataset)
    groups = {g.group_id: g for g in dataset.groups}
    if len(groups) != len(dataset.groups) or len({c.case_id for c in dataset.cases}) != len(
        dataset.cases
    ):
        raise AgentError("DUPLICATE_EVALUATION_ID")
    config = TaskConfig(
        fixture_path="validation-only",
        fixture_digest=digest("validation-only"),
        max_tokens=0,
        max_cost_nusd=0,
        **policy_versions(),
    )
    bundles, seen = {}, {}
    for case in dataset.cases:
        if case.group_id not in groups:
            raise AgentError("UNKNOWN_SCENARIO_GROUP")
        diff_path = local_file(path.parent, case.diff)
        snapshot, units = prepare_diff(diff_path, config, Safety())
        if snapshot["snapshot_id"] != case.input_digest:
            raise AgentError("EVALUATION_INPUT_CHANGED")
        if len(units) != 1 or snapshot["excluded"]:
            raise AgentError("EVALUATION_REQUIRES_ONE_UNIT")
        if case.input_digest in seen and seen[case.input_digest] != case.group_id:
            raise AgentError("EVALUATION_SPLIT_LEAKAGE")
        seen[case.input_digest] = case.group_id
        index = evidence_index(snapshot)
        refs = case.tool_evidence + [ref for issue in case.issues for ref in issue.evidence]
        if any(ref not in index or index[ref]["redacted"] for ref in refs):
            raise AgentError("INVALID_EVALUATION_EVIDENCE")
        fixture_path = local_file(path.parent, case.fixture)
        fixture = FixtureProvider(fixture_path)
        fixture_text = read_bounded(fixture_path, 512 * 1024)
        Safety().require_safe(fixture_text)
        if fixture.fingerprint != case.fixture_digest:
            raise AgentError("EVALUATION_FIXTURE_CHANGED")
        bundles[case.case_id] = {
            "safe_diff": snapshot["safe_diff"],
            "fixture": fixture_text,
        }
    if set(groups) != {c.group_id for c in dataset.cases}:
        raise AgentError("EMPTY_SCENARIO_GROUP")
    return dataset, bundles, digest(dataset.model_dump())


def describe(path):
    dataset, _, fingerprint = load_dataset(path)
    groups = {g.group_id: g.split for g in dataset.groups}
    return {
        "dataset_digest": fingerprint,
        "annotation_status": dataset.annotation_status,
        "groups": len(groups),
        "cases": len(dataset.cases),
        "splits": {
            s: sum(groups[c.group_id] == s for c in dataset.cases)
            for s in ("development", "holdout")
        },
        "paid_calls_authorized": False,
    }
