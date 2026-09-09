# debug-projection Specification

## Purpose
Given the ledger of an axiom run, project a failure-evidence package answering "why did this run not reach VERIFIED", so the orchestrator can make root-cause-driven decisions before drawing the next workflow — the root-cause-first debug-gate principle, landed in axiom's projection layer.
## Requirements
### Requirement: Failed-node evidence package

`axiom debug <run-dir>` SHALL reconstruct, for each failed node, an evidence package from the ledger inside the run-dir, containing node_id, failure_class, cognitive_signature, the last output preview, unmet requirements, recovery_action, and (if present) loop context. This projection MUST be read-only: it MUST NOT mutate the ledger, call any LLM, or introduce external dependencies.

#### Scenario: Stagnating node appears in the report
- **WHEN** the run-dir's ledger contains at least one `stagnating` event
- **THEN** the `axiom debug <run-dir>` report lists that node, marks failure_class as stagnating, derives cognitive_signature from that node's denials set / schema-failure key set, and attaches that node's last result_text_preview

#### Scenario: Node blocked by a gate
- **WHEN** the ledger contains an unresolved `gate_open` event
- **THEN** the report lists that gate's node_id and reason, marks failure_class as gate_open, and marks recovery_action as gate-resolve

#### Scenario: Budget exhausted
- **WHEN** the ledger contains a `budget_exhausted` event
- **THEN** the report lists the budget exhaustion and marks recovery_action as raise-cap-or-replan

#### Scenario: Stagnation inside a loop carries loop context
- **WHEN** the stagnating event payload contains loop_id and iteration
- **THEN** the evidence package MUST carry that loop_id and iteration, so the orchestrator can locate which iteration stalled

### Requirement: Empty report for a fully-VERIFIED run

When a run has no failure-class events at all, `axiom debug` MUST output `no failures` and exit code 0.

#### Scenario: A run with no failures
- **WHEN** the run-dir's derive_verdict is VERIFIED and the ledger has no failure-class events
- **THEN** `axiom debug` outputs `no failures` and exit code 0

### Requirement: Friendly failure for a bad run-dir

For a non-existent run-dir or a missing ledger, `axiom debug` MUST output a one-line explanation and exit code 1, without throwing a stack trace.

#### Scenario: Non-existent run-dir
- **WHEN** the given run-dir does not exist or contains no ledger
- **THEN** `axiom debug` outputs a one-line explanation and exit code 1, without throwing a stack trace

### Requirement: Does not mutate the state machine

`axiom debug` MUST NOT write any event to the ledger; run twice consecutively on the same run-dir, the ledger bytes SHALL be unchanged and the two outputs identical.

#### Scenario: Idempotent and read-only
- **WHEN** `axiom debug` is run twice consecutively on the same run-dir
- **THEN** the ledger file bytes are unchanged and the two outputs are identical

