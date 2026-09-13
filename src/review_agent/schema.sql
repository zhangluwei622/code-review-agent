CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, config TEXT NOT NULL, config_digest TEXT NOT NULL,
 snapshot_id TEXT NOT NULL, trace_id TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL, send_block_reason TEXT, send_block_attempt_id TEXT,
 CHECK ((send_block_reason IS NULL) = (send_block_attempt_id IS NULL))
);
CREATE TABLE IF NOT EXISTS snapshots (
 snapshot_id TEXT PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_snapshots (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
 source_digest TEXT UNIQUE NOT NULL,
 data TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS preserve_source_snapshot_update
BEFORE UPDATE ON source_snapshots BEGIN SELECT RAISE(ABORT, 'SOURCE_SNAPSHOT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_source_snapshot_delete
BEFORE DELETE ON source_snapshots BEGIN SELECT RAISE(ABORT, 'SOURCE_SNAPSHOT_IMMUTABLE'); END;
CREATE TABLE IF NOT EXISTS review_units (
 unit_id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, data TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', stop_reason TEXT,
 sends INTEGER NOT NULL DEFAULT 0 CHECK(sends >= 0), validation_ref TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES review_units(unit_id),
 request_digest TEXT NOT NULL, request_ref TEXT NOT NULL REFERENCES artifacts(artifact_id),
 result_ref TEXT REFERENCES artifacts(artifact_id)
);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
 attempt_no INTEGER NOT NULL DEFAULT 1,
 call_status TEXT NOT NULL CHECK(call_status IN
 ('RESERVED','DISPATCHED','COMPLETED','UNKNOWN','CANCELLED')),
 result_status TEXT, fee_status TEXT NOT NULL CHECK(fee_status IN ('HELD','SETTLED','RELEASED')),
 quote TEXT NOT NULL, quote_tokens INTEGER NOT NULL CHECK(quote_tokens >= 0),
 quote_cost_nusd INTEGER NOT NULL CHECK(quote_cost_nusd >= 0),
 actual_tokens INTEGER CHECK(actual_tokens >= 0),
 actual_cost_nusd INTEGER CHECK(actual_cost_nusd >= 0),
 result_ref TEXT REFERENCES artifacts(artifact_id), error_code TEXT, span_id TEXT NOT NULL,
 UNIQUE(operation_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS budget_events (
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('RESERVE','SETTLE','RELEASE')),
 tokens INTEGER NOT NULL, cost_nusd INTEGER NOT NULL,
 PRIMARY KEY(attempt_id, event_type)
);
CREATE TABLE IF NOT EXISTS operation_context (
 operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
 unit_id TEXT NOT NULL REFERENCES review_units(unit_id),
 kind TEXT NOT NULL CHECK(kind IN ('REVIEW','REPAIR')), ordinal INTEGER NOT NULL,
 source_operation_id TEXT REFERENCES operations(operation_id),
 source_result_ref TEXT REFERENCES artifacts(artifact_id), prompt_digest TEXT NOT NULL,
 CHECK ((kind='REVIEW' AND ordinal=0 AND source_operation_id IS NULL AND source_result_ref IS NULL)
     OR (kind='REPAIR' AND ordinal=1 AND source_operation_id IS NOT NULL AND source_result_ref IS NOT NULL)),
 UNIQUE(unit_id,kind,ordinal)
);
CREATE TABLE IF NOT EXISTS retry_decisions (
 decision_id TEXT PRIMARY KEY,
 source_attempt_id TEXT UNIQUE NOT NULL REFERENCES attempts(attempt_id),
 action TEXT NOT NULL CHECK(action='RETRY'),
 status TEXT NOT NULL CHECK(status IN ('PENDING','BOUND')),
 bound_attempt_id TEXT UNIQUE REFERENCES attempts(attempt_id), created_at TEXT NOT NULL,
 CHECK ((status='PENDING' AND bound_attempt_id IS NULL)
     OR (status='BOUND' AND bound_attempt_id IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS preserve_operation_context_update
BEFORE UPDATE ON operation_context BEGIN SELECT RAISE(ABORT, 'OPERATION_CONTEXT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_operation_context_delete
BEFORE DELETE ON operation_context BEGIN SELECT RAISE(ABORT, 'OPERATION_CONTEXT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_retry_decision_update
BEFORE UPDATE ON retry_decisions
WHEN OLD.status='BOUND' OR NEW.decision_id IS NOT OLD.decision_id
 OR NEW.source_attempt_id IS NOT OLD.source_attempt_id OR NEW.action IS NOT OLD.action
 OR NEW.created_at IS NOT OLD.created_at OR NEW.status!='BOUND'
BEGIN SELECT RAISE(ABORT, 'RETRY_DECISION_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_retry_decision_delete
BEFORE DELETE ON retry_decisions BEGIN SELECT RAISE(ABORT, 'RETRY_DECISION_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS validate_retry_binding
BEFORE UPDATE OF bound_attempt_id ON retry_decisions
WHEN OLD.status='PENDING' AND NEW.status='BOUND' AND NOT EXISTS (
 SELECT 1 FROM attempts source JOIN attempts target
 ON source.operation_id=target.operation_id
 WHERE source.attempt_id=NEW.source_attempt_id AND source.call_status='UNKNOWN'
 AND target.attempt_id=NEW.bound_attempt_id AND target.call_status='RESERVED'
 AND target.attempt_no=source.attempt_no+1
)
BEGIN SELECT RAISE(ABORT, 'INVALID_RETRY_BINDING'); END;
CREATE TABLE IF NOT EXISTS attempt_timing (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
 dispatched_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pricing_reviews (
 review_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 review_kind TEXT NOT NULL CHECK(review_kind IN ('SETTLEMENT_ESTIMATE','HISTORICAL_REVIEW')),
 pricing_version TEXT NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(attempt_id, review_kind, pricing_version)
);
CREATE TABLE IF NOT EXISTS findings (
 finding_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES review_units(unit_id),
 result_ref TEXT NOT NULL REFERENCES artifacts(artifact_id),
 validation_ref TEXT NOT NULL REFERENCES artifacts(artifact_id),
 candidate_index INTEGER NOT NULL, data TEXT NOT NULL,
 UNIQUE(result_ref, candidate_index)
);
CREATE TABLE IF NOT EXISTS finding_evidence (
 finding_id TEXT NOT NULL REFERENCES findings(finding_id), evidence_ref TEXT NOT NULL,
 role TEXT NOT NULL, PRIMARY KEY(finding_id, evidence_ref, role)
);
CREATE TABLE IF NOT EXISTS trace_spans (
 span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, kind TEXT NOT NULL,
 operation_id TEXT, attempt_id TEXT, unit_id TEXT, status TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS preserve_send_block
BEFORE UPDATE OF send_block_reason, send_block_attempt_id ON tasks
WHEN OLD.send_block_reason IS NOT NULL AND (
 NEW.send_block_reason IS NOT OLD.send_block_reason OR
 NEW.send_block_attempt_id IS NOT OLD.send_block_attempt_id
)
BEGIN
 SELECT RAISE(ABORT, 'TASK_SEND_BLOCK_IMMUTABLE');
END;
CREATE TRIGGER IF NOT EXISTS preserve_pricing_review_update
BEFORE UPDATE ON pricing_reviews
BEGIN
 SELECT RAISE(ABORT, 'PRICING_REVIEW_IMMUTABLE');
END;
CREATE TRIGGER IF NOT EXISTS preserve_pricing_review_delete
BEFORE DELETE ON pricing_reviews
BEGIN
 SELECT RAISE(ABORT, 'PRICING_REVIEW_IMMUTABLE');
END;

-- v5 is additive. The v4 operation_context table and its immutable rows stay intact.
CREATE TABLE IF NOT EXISTS model_turns (
 operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
 unit_id TEXT NOT NULL REFERENCES review_units(unit_id),
 kind TEXT NOT NULL CHECK(kind IN ('REVIEW','REPAIR')),
 ordinal INTEGER NOT NULL CHECK(ordinal IN (0,1)), turn_no INTEGER NOT NULL CHECK(turn_no>=0),
 source_operation_id TEXT REFERENCES operations(operation_id),
 source_result_ref TEXT REFERENCES artifacts(artifact_id), prompt_digest TEXT NOT NULL,
 input_tool_call_id TEXT REFERENCES tool_calls(tool_call_id),
 input_tool_result_ref TEXT REFERENCES artifacts(artifact_id),
 CHECK ((input_tool_call_id IS NULL) = (input_tool_result_ref IS NULL)),
 CHECK ((kind='REVIEW' AND ordinal=0 AND source_operation_id IS NULL AND source_result_ref IS NULL)
     OR (kind='REPAIR' AND ordinal=1 AND source_operation_id IS NOT NULL AND source_result_ref IS NOT NULL)),
 UNIQUE(unit_id,turn_no,kind), UNIQUE(input_tool_call_id,kind)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_repair_per_unit ON model_turns(unit_id) WHERE kind='REPAIR';
CREATE TRIGGER IF NOT EXISTS preserve_model_turn_update BEFORE UPDATE ON model_turns
BEGIN SELECT RAISE(ABORT, 'MODEL_TURN_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_model_turn_delete BEFORE DELETE ON model_turns
BEGIN SELECT RAISE(ABORT, 'MODEL_TURN_IMMUTABLE'); END;
CREATE TABLE IF NOT EXISTS tool_calls (
 tool_call_id TEXT PRIMARY KEY,
 unit_id TEXT NOT NULL REFERENCES review_units(unit_id),
 source_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
 source_result_ref TEXT UNIQUE NOT NULL REFERENCES artifacts(artifact_id),
 request_ref TEXT UNIQUE NOT NULL REFERENCES artifacts(artifact_id), request_digest TEXT NOT NULL,
 slot_no INTEGER NOT NULL CHECK(slot_no BETWEEN 1 AND 4),
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','REJECTED','FAILED',
 'TIMED_OUT','INTERRUPTED','TRUNCATED','BLOCKED_SECURITY')),
 result_ref TEXT UNIQUE REFERENCES artifacts(artifact_id), result_digest TEXT,
 span_id TEXT UNIQUE NOT NULL,
 CHECK ((status IN ('PENDING','RUNNING') AND result_ref IS NULL AND result_digest IS NULL)
     OR (status NOT IN ('PENDING','RUNNING') AND result_ref IS NOT NULL AND result_digest IS NOT NULL)),
 UNIQUE(unit_id,slot_no)
);
CREATE TRIGGER IF NOT EXISTS preserve_tool_call_update BEFORE UPDATE ON tool_calls
WHEN OLD.result_ref IS NOT NULL OR NEW.tool_call_id IS NOT OLD.tool_call_id
 OR NEW.unit_id IS NOT OLD.unit_id OR NEW.source_operation_id IS NOT OLD.source_operation_id
 OR NEW.source_result_ref IS NOT OLD.source_result_ref OR NEW.request_ref IS NOT OLD.request_ref
 OR NEW.request_digest IS NOT OLD.request_digest OR NEW.slot_no IS NOT OLD.slot_no
 OR NEW.span_id IS NOT OLD.span_id OR NEW.status='PENDING'
BEGIN SELECT RAISE(ABORT, 'TOOL_CALL_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS preserve_tool_call_delete BEFORE DELETE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'TOOL_CALL_IMMUTABLE'); END;
