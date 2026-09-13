import json
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from review_agent.budget import normalize_usage
from review_agent.config import TaskConfig, package_text
from review_agent.contracts import (
    AbstainDecision,
    AgentError,
    CandidateFinding,
    ReviewDecision,
    StoredResult,
    SubmitDecision,
    ToolDecision,
)
from review_agent.providers import RawProviderReply
from review_agent.safety import Safety

DECISION_V5 = TypeAdapter(
    Annotated[SubmitDecision | AbstainDecision | ToolDecision, Field(discriminator="action")]
)
SUMMARY_V5 = TypeAdapter(Annotated[SubmitDecision | AbstainDecision, Field(discriminator="action")])


def decision_schema(final_only=False):
    return (SUMMARY_V5 if final_only else DECISION_V5).json_schema()


def make_request(unit: dict, snapshot: dict, output_limit: int) -> dict:
    return {
        "system": package_text("prompts/review.md"),
        "unit_id": unit["unit_id"],
        "unit_index": unit["ordinal"],
        "snapshot_id": snapshot["snapshot_id"],
        "max_output_tokens": output_limit,
        "hunks": [h for h in snapshot["hunks"] if h["hunk_id"] in unit["hunk_ids"]],
        "output_schema": ReviewDecision.model_json_schema(),
        "finding_schema": CandidateFinding.model_json_schema(),
        "tools": [],
    }


def make_repair_request(
    unit, snapshot, output_limit, source_operation_id, source_result_ref, result
):
    if (
        result.result_status != "FORMAT_INVALID"
        or result.completion_state != "COMPLETE"
        or not result.safe_body
    ):
        raise AgentError("REPAIR_SOURCE_NOT_ELIGIBLE")
    request = make_request(unit, snapshot, output_limit)
    request["system"] = package_text("prompts/repair.md")
    request["repair"] = {
        "source_operation_id": source_operation_id,
        "source_result_ref": source_result_ref,
        "previous_response": result.safe_body,
        "error_codes": ["INVALID_REVIEW_FORMAT"],
    }
    return request


def _require_unique_json_keys(safe_body: str) -> None:
    def unique_object(pairs):
        keys = set()
        for key, _ in pairs:
            if key in keys:
                # Do not put untrusted keys or values in exception messages.
                raise ValueError("DUPLICATE_JSON_KEY")
            keys.add(key)
        return dict(pairs)

    json.loads(safe_body, object_pairs_hook=unique_object)


def decode_reply(reply: RawProviderReply, config: TaskConfig, safety: Safety) -> StoredResult:
    usage = normalize_usage(reply.usage, config.execution_mode)
    common = {
        "usage": usage,
        "completion_state": reply.finish,
        "provider_request_id": reply.provider_request_id,
        "provider_model": reply.provider_model,
        "provider_finish_reason": reply.provider_finish_reason,
        "system_fingerprint": reply.system_fingerprint,
    }
    try:
        safety.require_safe({key: value for key, value in common.items() if key != "usage"})
        # Apply the persisted metadata contract inside the rejection boundary,
        # so a completed response cannot be lost to a later ValidationError.
        StoredResult(result_status="UNUSABLE", **common)
    except Exception:
        return StoredResult(
            result_status="SAFETY_REJECTED",
            completion_state=reply.finish,
            error_code="UNSAFE_REPLY_METADATA",
            usage=usage,
        )
    try:
        body_bytes = reply.body.encode("utf-8")
    except UnicodeError:
        return StoredResult(result_status="SAFETY_REJECTED", error_code="UNSAFE_REPLY", **common)
    if len(body_bytes) > config.max_reply_bytes:
        return StoredResult(
            result_status="TRUNCATED",
            completion_state="LOCAL_SIZE_LIMIT",
            truncation_reason="LOCAL_BODY_LIMIT",
            error_code="OUTPUT_SIZE_LIMIT",
            usage=usage,
        )
    try:
        safe_body, changes = safety.sanitize(reply.body)
        if changes:
            raise ValueError
    except Exception:
        return StoredResult(result_status="SAFETY_REJECTED", error_code="UNSAFE_REPLY", **common)
    decision, error = None, None
    try:
        if config.schema_version in (7, 8):
            _require_unique_json_keys(safe_body)
        envelope = (
            DECISION_V5.validate_json(safe_body)
            if config.schema_version >= 5
            else ReviewDecision.model_validate_json(safe_body)
        )
        if envelope.action == "abstain" and (not envelope.reason or envelope.findings):
            raise ValueError
        decision = envelope.model_dump()
    except (ValidationError, ValueError, RecursionError):
        error = "INVALID_REVIEW_FORMAT"
    if reply.finish == "OUTPUT_LIMIT":
        return StoredResult(
            result_status="TRUNCATED",
            safe_body=safe_body,
            decision=decision,
            truncation_reason="PROVIDER_OUTPUT_LIMIT",
            error_code="OUTPUT_LIMIT",
            **common,
        )
    if reply.finish == "UNCONFIRMED":
        return StoredResult(
            result_status="UNUSABLE",
            safe_body=safe_body,
            error_code="COMPLETION_UNCONFIRMED",
            **common,
        )
    status = "VALID" if decision else ("FORMAT_INVALID" if safe_body.strip() else "UNUSABLE")
    return StoredResult(
        result_status=status, safe_body=safe_body, decision=decision, error_code=error, **common
    )


def evidence_index(snapshot: dict, hunk_ids: list[str] | None = None) -> dict:
    index = {}
    for hunk in snapshot["hunks"]:
        if hunk_ids is not None and hunk["hunk_id"] not in hunk_ids:
            continue
        for line in hunk["lines"]:
            for side in ("old", "new"):
                number = line[f"{side}_lineno"]
                if number is not None:
                    index[f"{hunk['hunk_id']}:{side}:{number}"] = {
                        **line,
                        "file_id": hunk["file_id"],
                    }
    return index


def validate_result(
    result: StoredResult, unit: dict, snapshot: dict, *, allowed_evidence=None
) -> dict:
    index, accepted, rejected = evidence_index(snapshot, unit["hunk_ids"]), [], []
    if result.result_status == "SAFETY_REJECTED":
        return {
            "status": "BLOCKED_SECURITY",
            "reason": "UNSAFE_REPLY",
            "findings": [],
            "rejected": [],
        }
    if result.decision:
        for ordinal, raw in enumerate(result.decision.get("findings", [])):
            try:
                candidate = CandidateFinding.model_validate(raw)
                anchor = f"{candidate.hunk_id}:{candidate.side}:{candidate.line}"
                references = candidate.evidence + candidate.expectation_evidence
                if anchor not in index or any(ref not in index for ref in references):
                    raise ValueError
                if allowed_evidence is not None and any(
                    ref not in allowed_evidence for ref in references + [anchor]
                ):
                    raise ValueError
                if index[anchor]["kind"] != ("+" if candidate.side == "new" else "-"):
                    raise ValueError
                if any(index[ref]["redacted"] for ref in references + [anchor]):
                    raise ValueError
            except (ValidationError, ValueError):
                rejected.append({"candidate_index": ordinal, "code": "INVALID_FINDING_EVIDENCE"})
                continue
            finding = candidate.model_dump()
            finding.update(
                {
                    "candidate_index": ordinal,
                    "confidence_reason": None,
                    "truncated_source": result.result_status == "TRUNCATED",
                }
            )
            if candidate.confidence == "high" and not candidate.expectation_evidence:
                finding["confidence"] = "reference"
                finding["confidence_reason"] = "MISSING_EXPECTATION_EVIDENCE"
            accepted.append(finding)
    if result.result_status == "TRUNCATED":
        status, reason = "PARTIAL_TRUNCATED", result.truncation_reason
    elif (
        result.result_status != "VALID"
        or rejected
        or result.decision.get("action") == "request_tool"
    ):
        status, reason = "PARTIAL_INVALID_RESULT", result.error_code or "INVALID_FINDING_EVIDENCE"
    elif result.decision["action"] == "abstain":
        status, reason = "ABSTAINED", result.decision["reason"]
    else:
        status, reason = "DONE", None
    return {"status": status, "reason": reason, "findings": accepted, "rejected": rejected}


def finding_data(row: dict) -> dict:
    return {
        **json.loads(row["data"]),
        "finding_id": row["finding_id"],
        "result_ref": row["result_ref"],
        "validation_ref": row["validation_ref"],
    }
