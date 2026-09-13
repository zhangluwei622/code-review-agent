"""Read-only projections of a single persisted task snapshot."""

import json

from review_agent.contracts import AgentError, digest, stable_id
from review_agent.safety import Safety

SECTIONS = ("all", "request", "budget-events", "pricing")


def _require(condition):
    if not condition:
        raise AgentError("AUDIT_INTEGRITY_ERROR")


def _artifact(snapshot, ref, kind):
    _require(ref in snapshot["artifacts"] and snapshot["artifact_kinds"].get(ref) == kind)
    return snapshot["artifacts"][ref]


def _request(snapshot, operation, config):
    request = _artifact(snapshot, operation["request_ref"], "request")
    _require(digest(request) == operation["request_digest"])
    prompt_digest = config["prompt_digest"]
    context = _context(snapshot, operation, config)
    if context and context["kind"] == "REPAIR":
        prompt_digest = config["repair_prompt_digest"]
        _require(config["max_repairs_per_unit"] == 1)
        source = next(
            op
            for op in snapshot["operations"]
            if op["operation_id"] == context["source_operation_id"]
        )
        _require(source["unit_id"] == operation["unit_id"])
        _require(source["result_ref"] == context["source_result_ref"])
        _require(
            operation["operation_id"] == stable_id("operation", source["operation_id"], "REPAIR", 1)
        )
        result = _artifact(snapshot, source["result_ref"], "result")
        _require(
            result["result_status"] == "FORMAT_INVALID" and result["completion_state"] == "COMPLETE"
        )
        payload = (
            request
            if config["execution_mode"] == "fixture"
            else json.loads(request["messages"][1]["content"])
        )
        _require(
            payload["repair"]
            == {
                "source_operation_id": source["operation_id"],
                "source_result_ref": source["result_ref"],
                "previous_response": result["safe_body"],
                "error_codes": ["INVALID_REVIEW_FORMAT"],
            }
        )
    if context:
        _require(context["prompt_digest"] == prompt_digest)
    if config.get("schema_version", 1) >= 5:
        from review_agent.tool_audit import inputs

        data = (
            request
            if config["execution_mode"] == "fixture"
            else json.loads(request["messages"][1]["content"])
        )
        inputs(snapshot, context, data)
    if config["execution_mode"] == "fixture":
        _require(request["snapshot_id"] == snapshot["task"]["snapshot_id"])
        _require(request["unit_id"] == operation["unit_id"])
        _require(digest(request["system"]) == prompt_digest)
    else:
        _require(request["model"] == config["model"])
        _require(request["thinking"] == {"type": "disabled"})
        _require(request["stream"] is False)
        _require(len(request["messages"]) == 2)
        _require(request["messages"][0]["role"] == "system")
        _require(digest(request["messages"][0]["content"]) == prompt_digest)
    return {
        "artifact_id": operation["request_ref"],
        "kind": "request",
        "request_digest": operation["request_digest"],
        "data": request,
    }


def _context(snapshot, operation, config):
    if config.get("schema_version", 1) < 4:
        return None
    matches = [
        c
        for c in snapshot.get("operation_contexts", [])
        if c["operation_id"] == operation["operation_id"]
    ]
    _require(len(matches) == 1)
    context = matches[0]
    _require(context["unit_id"] == operation["unit_id"] and context["kind"] in ("REVIEW", "REPAIR"))
    return context


def _check_attempt_chain(snapshot, operation):
    attempts = {
        a["attempt_id"]: a
        for a in snapshot["attempts"]
        if a["operation_id"] == operation["operation_id"]
    }
    roots = [a for a in attempts.values() if a["attempt_no"] == 1]
    _require(len(roots) == 1)
    current, visited = roots[0], set()
    while True:
        _require(current["attempt_id"] not in visited)
        visited.add(current["attempt_id"])
        decisions = [
            d
            for d in snapshot.get("retry_decisions", [])
            if d["source_attempt_id"] == current["attempt_id"]
        ]
        _require(len(decisions) <= 1)
        if not decisions or decisions[0]["status"] == "PENDING":
            break
        decision = decisions[0]
        _require(decision["status"] == "BOUND" and current["call_status"] == "UNKNOWN")
        _require(decision["bound_attempt_id"] in attempts and current["result_ref"] is None)
        target = attempts[decision["bound_attempt_id"]]
        _require(target["attempt_no"] == current["attempt_no"] + 1)
        current = target
    _require(len(visited) == len(attempts))
    _require(operation["result_ref"] == current["result_ref"])
    if operation["result_ref"]:
        _require(current["call_status"] == "COMPLETED")


def _call(snapshot, attempt, sections, config):
    operations = [
        op for op in snapshot["operations"] if op["operation_id"] == attempt["operation_id"]
    ]
    _require(len(operations) == 1)
    operation = operations[0]
    _check_attempt_chain(snapshot, operation)
    value = {"attempt": attempt, "operation": operation, "quote": json.loads(attempt["quote"])}
    context = _context(snapshot, operation, config)
    if context:
        value["operation_context"] = context
    if "request" in sections:
        value["request"] = _request(snapshot, operation, config)
        if config.get("schema_version", 1) >= 5:
            from review_agent.tool_audit import inputs

            request = value["request"]["data"]
            data = (
                request
                if config["execution_mode"] == "fixture"
                else json.loads(request["messages"][1]["content"])
            )
            value["tool_inputs"] = inputs(snapshot, context, data)
    if "all" in sections:
        value["result"] = (
            _artifact(snapshot, attempt["result_ref"], "result") if attempt["result_ref"] else None
        )
        value["retry_decisions"] = [
            d
            for d in snapshot.get("retry_decisions", [])
            if d["source_attempt_id"] == attempt["attempt_id"]
            or d["bound_attempt_id"] == attempt["attempt_id"]
        ]
        if context and context["kind"] == "REPAIR":
            source_attempts = [
                a for a in snapshot["attempts"] if a["result_ref"] == context["source_result_ref"]
            ]
            _require(len(source_attempts) == 1)
            source_operation = next(
                op
                for op in snapshot["operations"]
                if op["operation_id"] == source_attempts[0]["operation_id"]
            )
            _require(_context(snapshot, source_operation, config)["kind"] == "REVIEW")
            value["source_call"] = _call(snapshot, source_attempts[0], sections, config)
    return value


def build_trace(snapshot, *, finding_id=None, attempt_id=None, sections=None):
    """Default: full audit. No fixture, prompt file, SDK or graph access."""
    if finding_id and attempt_id:
        raise AgentError("INVALID_TRACE_SCOPE")
    selected = set(sections or ("all",))
    if not selected <= set(SECTIONS):
        raise AgentError("INVALID_TRACE_SECTION")
    if "all" in selected:
        selected = set(SECTIONS)
    try:
        value = _build_trace(snapshot, finding_id, attempt_id, selected)
        # Refuse unsafe data rather than rewrite the historical request on export.
        Safety().require_safe(value)
        return value
    except (KeyError, TypeError, ValueError, StopIteration):
        raise AgentError("AUDIT_INTEGRITY_ERROR") from None


def _build_trace(snapshot, finding_id, attempt_id, sections):
    task = snapshot["task"]
    # Read the saved configuration verbatim: current defaults are not audit evidence.
    config = json.loads(task["config"])
    _require(digest(config) == task["config_digest"])
    finding = None
    if finding_id:
        matches = [f for f in snapshot["findings"] if f["finding_id"] == finding_id]
        if not matches:
            raise AgentError("FINDING_NOT_FOUND")
        _require(len(matches) == 1)
        finding = matches[0]
        attempts = [a for a in snapshot["attempts"] if a["result_ref"] == finding["result_ref"]]
        _require(len(attempts) == 1)
    elif attempt_id:
        attempts = [a for a in snapshot["attempts"] if a["attempt_id"] == attempt_id]
        if not attempts:
            raise AgentError("ATTEMPT_NOT_FOUND")
        _require(len(attempts) == 1)
    else:
        attempts = snapshot["attempts"]
    selected_attempt_ids = {attempt["attempt_id"] for attempt in attempts}
    pricing_reviews = [
        review
        for review in snapshot.get("pricing_reviews", [])
        if review["attempt_id"] in selected_attempt_ids
    ]
    for review in pricing_reviews:
        _require(review["pricing_version"] == review["data"]["pricing"]["version"])
        _require(review["review_kind"] == review["data"]["review_kind"])
    attempt_timings = [
        timing
        for timing in snapshot.get("attempt_timings", [])
        if timing["attempt_id"] in selected_attempt_ids
    ]
    value = {
        "task_id": task["task_id"],
        "trace_id": task["trace_id"],
        "execution_mode": config["execution_mode"],
        "config_digest": task["config_digest"],
        "scope": {"finding_id": finding_id, "attempt_id": attempt_id},
    }
    if config.get("schema_version") == 8:
        _require(digest(snapshot["source"]) == config["source_digest"])
        _require(snapshot["source"]["safe_diff_digest"] == task["snapshot_id"])
        value["source"] = snapshot["source"]
    # Pricing-only queries do not need to materialize request/result content.
    if "request" in sections or "budget-events" in sections:
        calls = [_call(snapshot, a, sections, config) for a in attempts]
        if finding_id or attempt_id:
            value.update(calls[0])
        else:
            value["calls"] = calls
            if "request" in sections:
                attempted = {attempt["operation_id"] for attempt in attempts}
                value["unattempted_operations"] = [
                    {"operation": operation, "request": _request(snapshot, operation, config)}
                    for operation in snapshot["operations"]
                    if operation["operation_id"] not in attempted
                ]
    if "budget-events" in sections:
        attempt_ids = {a["attempt_id"] for a in attempts}
        value["budget_events"] = [
            event for event in snapshot["budget_events"] if event["attempt_id"] in attempt_ids
        ]
        # These totals are explicitly task-wide, even when the event scope is narrower.
        value["task_totals"] = snapshot["totals"]
        value["send_block"] = {
            "reason": task["send_block_reason"],
            "attempt_id": task["send_block_attempt_id"],
        }
    if "pricing" in sections:
        pricing = {
            "source": "tasks.config",
            "currency": "USD",
            "nano_usd_per_usd": 10**9,
            "pricing_source": config.get("pricing_source", "fixture"),
            "prompt_digest": config["prompt_digest"],
            "policy_digest": config["policy_digest"],
            "config_digest": task["config_digest"],
            "limits": {
                key: config[key]
                for key in (
                    "max_tokens",
                    "max_cost_nusd",
                    "max_output_tokens",
                    "max_sends_per_unit",
                )
            },
        }
        if config.get("schema_version", 1) >= 4:
            pricing["repair_prompt_digest"] = config["repair_prompt_digest"]
            pricing["limits"]["max_repairs_per_unit"] = config["max_repairs_per_unit"]
        if config.get("schema_version", 1) >= 5:
            pricing["limits"]["max_tools_per_unit"] = config["max_tools_per_unit"]
            pricing["tool_registry_digest"] = digest(config["tool_registry"])
        if config.get("schema_version") in (7, 8):
            pricing["review_version"] = config["review_version"]
            pricing["reply_protocol"] = config["reply_protocol"]
        if config["execution_mode"] == "deepseek":
            if config.get("schema_version", 1) >= 3:
                frozen = config["deepseek_pricing"]
                pricing.update(
                    {
                        "pricing_status": "CURRENT_FROZEN",
                        "price_unit": "nanoUSD/1M tokens",
                        "pricing_snapshot": frozen,
                        "quote_tier": "peak",
                        "quote_input_class": "cache_miss",
                        "ledger_cost_basis": "peak_rate_local_estimate_upper_bound",
                        "provider_bill_reconciled": False,
                    }
                )
            else:
                pricing.update(
                    {
                        "pricing_status": "HISTORICAL_FROZEN_LEGACY",
                        "price_unit": "nanoUSD/1M tokens",
                        "price_cache_hit_nusd_per_mtok": config["price_cache_hit_nusd_per_mtok"],
                        "price_cache_miss_nusd_per_mtok": config["price_cache_miss_nusd_per_mtok"],
                        "price_output_nusd_per_mtok": config["price_output_nusd_per_mtok"],
                        "quote_input_class": "cache_miss",
                        "ledger_cost_basis": "historical_local_rate_calculation",
                        "provider_bill_reconciled": False,
                    }
                )
            pricing["attempt_timings"] = attempt_timings
            pricing["pricing_reviews"] = pricing_reviews
        else:
            pricing.update(
                {
                    "price_unit": "nanoUSD/token",
                    "price_input_nusd": config["price_input_nusd"],
                    "price_output_nusd": config["price_output_nusd"],
                }
            )
        value["pricing"] = pricing
    if "all" in sections:
        if config.get("schema_version", 1) >= 5:
            from review_agent.tool_audit import tool_call

            value["tool_calls"] = [
                tool_call(snapshot, call)
                for call in snapshot["tool_calls"]
                if not (finding_id or attempt_id)
                or call["source_operation_id"] == attempts[0]["operation_id"]
                or call["tool_call_id"]
                in {item["call"]["tool_call_id"] for item in value.get("tool_inputs", [])}
            ]
        validation_refs = set()
        if finding:
            _require(value["operation"]["unit_id"] == finding["unit_id"])
            value["finding"] = finding
            value["validation"] = _artifact(snapshot, finding["validation_ref"], "validation")
            value["evidence"] = [e for e in snapshot["evidence"] if e["finding_id"] == finding_id]
            validation_refs.add(finding["validation_ref"])
        else:
            result_refs = {a["result_ref"] for a in attempts if a["result_ref"]}
            value["findings"] = [f for f in snapshot["findings"] if f["result_ref"] in result_refs]
            finding_ids = {f["finding_id"] for f in value["findings"]}
            value["evidence"] = [e for e in snapshot["evidence"] if e["finding_id"] in finding_ids]
            for ref in result_refs:
                validation_ref = stable_id("validation", ref)
                if validation_ref in snapshot["artifacts"]:
                    validation_refs.add(validation_ref)
            value["validations"] = [
                {"artifact_id": ref, "data": _artifact(snapshot, ref, "validation")}
                for ref in sorted(validation_refs)
            ]
        ids = {a["attempt_id"] for a in attempts}
        value["spans"] = [
            s
            for s in snapshot["spans"]
            if s["attempt_id"] in ids
            or s["span_id"] in validation_refs
            or s["span_id"] in {c["call"]["span_id"] for c in value.get("tool_calls", [])}
        ]
    return value
