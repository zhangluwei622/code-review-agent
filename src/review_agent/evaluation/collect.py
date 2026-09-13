from review_agent import app
from review_agent.contracts import AgentError, digest
from review_agent.review import finding_data
from review_agent.tool_loop import payload, supplied_evidence

from .data import Observation


def collect(data, case, split):
    # Validate the source ledger using the existing read-only audit chain.
    trace = app.trace(data)
    if data["snapshot"]["snapshot_id"] != case.input_digest:
        raise AgentError("EVALUATION_INPUT_CHANGED")
    consumed, retrieved, consumed_by, post_tool_attempts = set(), set(), {}, set()
    for call in trace["calls"]:
        request = payload(call["request"]["data"], data["config"]["execution_mode"])
        if "loop" not in request:
            continue
        history = request["loop"]["tool_history"]
        if history and call["attempt"]["call_status"] in ("DISPATCHED", "COMPLETED", "UNKNOWN"):
            post_tool_attempts.add(call["attempt"]["attempt_id"])
        if call["attempt"]["call_status"] != "COMPLETED":
            continue
        # Only tool records, not the initial preview, count as retrieved context.
        consumed.update(
            supplied_evidence(
                data["snapshot"],
                {
                    "hunks": [],
                    "loop": {"tool_history": history},
                },
            )
        )
        for item in history:
            consumed_by.setdefault(item["tool_call_id"], []).append(call["request"]["artifact_id"])
    tools, seen = [], set()
    for call in data["tool_calls"]:
        request = data["artifacts"][call["request_ref"]]
        result = data["artifacts"].get(call["result_ref"])
        signature = digest({"name": request["name"], "arguments": request["arguments"]})
        refs = (
            set()
            if result is None
            else supplied_evidence(
                data["snapshot"],
                {
                    "hunks": [],
                    "loop": {"tool_history": [{"result": result}]},
                },
            )
        )
        retrieved.update(refs)
        tools.append(
            {
                "tool_call_id": call["tool_call_id"],
                "name": request["name"],
                "arguments": request["arguments"],
                "status": call["status"],
                "error_code": None if result is None else result["error_code"],
                "records": 0 if result is None else len(result["records"]),
                "repeated_arguments": signature in seen,
                "retrieved_evidence": sorted(refs),
                "consumed_by_requests": sorted(set(consumed_by.get(call["tool_call_id"], []))),
                "source_request_ref": request["source_request_ref"],
                "request_ref": call["request_ref"],
                "result_ref": call["result_ref"],
            }
        )
        seen.add(signature)
    model_results = [c["result"] for c in trace["calls"] if c.get("result")]
    return Observation(
        observation_id=data["task"]["task_id"],
        case_id=case.case_id,
        group_id=case.group_id,
        split=split,
        input_digest=case.input_digest,
        execution_mode="fixture"
        if data["config"]["execution_mode"] == "fixture"
        else ("baseline_live" if data["config"]["schema_version"] == 6 else "historical_live"),
        started=True,
        task_id=data["task"]["task_id"],
        unit_states=[u["status"] for u in data["units"]],
        reasons=sorted({u["stop_reason"] for u in data["units"] if u["stop_reason"]}),
        findings=[finding_data(f) for f in data["findings"]],
        tools=tools,
        retrieved_evidence=sorted(retrieved),
        consumed_evidence=sorted(consumed),
        sends=sum(u["sends"] for u in data["units"]),
        totals=data["totals"],
        model_identity={
            "requested_model": data["config"]["model"],
            "reported_models": sorted(
                {r["provider_model"] for r in model_results if r.get("provider_model")}
            ),
            "actual_model_version": None,
            "actual_model_version_evidence": None,
            **(
                {
                    "documented_model_version": data["config"]["deepseek_pricing"]["model_version"],
                    "documented_model_version_source": data["config"]["pricing_source"],
                    "version_basis": "official_mapping_only",
                    **(
                        {
                            "actual_model_version": "DeepSeek-V4.1-Flash",
                            "actual_model_version_evidence": "provider_model_field",
                            "version_basis": "provider_reported_version",
                        }
                        if model_results
                        and all(
                            r.get("provider_model")
                            in ("DeepSeek-V4.1-Flash", "deepseek-v4.1-flash")
                            for r in model_results
                        )
                        else {}
                    ),
                }
                if data["config"]["schema_version"] == 6
                else {}
            ),
            "system_fingerprints": sorted(
                {r["system_fingerprint"] for r in model_results if r.get("system_fingerprint")}
            ),
            "prompt_digest": data["config"]["prompt_digest"],
            "config_digest": data["task"]["config_digest"],
        },
        pricing=trace["pricing"],
        source={
            "trace_digest": digest(trace),
            "schema_version": data["config"]["schema_version"],
            "send_block": trace["send_block"],
            "post_tool_attempt_ids": sorted(post_tool_attempts),
            "post_tool_settled_tokens": sum(
                a["actual_tokens"] or 0
                for a in data["attempts"]
                if a["attempt_id"] in post_tool_attempts
            ),
            "post_tool_settled_cost_nusd": sum(
                a["actual_cost_nusd"] or 0
                for a in data["attempts"]
                if a["attempt_id"] in post_tool_attempts
            ),
            "post_tool_held_tokens": sum(
                a["quote_tokens"]
                for a in data["attempts"]
                if a["attempt_id"] in post_tool_attempts and a["fee_status"] == "HELD"
            ),
            "post_tool_held_cost_nusd": sum(
                a["quote_cost_nusd"]
                for a in data["attempts"]
                if a["attempt_id"] in post_tool_attempts and a["fee_status"] == "HELD"
            ),
        },
    )
