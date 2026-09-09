# format-contract-wiki Specification

## Purpose
Extract a copy-pasteable spec structure template (node skeleton + output_schema essentials + success_evidence binding form + control_flow shape) from a VERIFIED run, as a wiki `format_contract` entry, for `axiom plan --wiki-suggest` to retrieve before designing the next spec — getting "the exact format that has worked" in hand before producing, not just a shape fingerprint. This is the spec-experience wiki's further convergence on the friction root cause of "getting the needed format into the agent's hands before it produces".
## Requirements
### Requirement: Extract a format contract from a VERIFIED run

`axiom wiki extract <spec> --run-dir <dir> --contract` SHALL, when that run's `derive_verdict` is VERIFIED, append a `entry_type=format_contract` entry to wiki.jsonl, containing `node_skeleton` (for each agent node: id/type/output_schema required field set/verdict_field declaration/whether write_areas is empty/failure_policy.on_exhausted), `binding_pattern` (the success_evidence form), `control_flow_shape`, `intent`, and `tags`. A non-VERIFIED run SHALL NOT produce a contract entry (a failed run's lessons go into experience patterns, not into a contract).

#### Scenario: VERIFIED run extracts a contract
- **WHEN** a run's `derive_verdict` is VERIFIED and `wiki extract --contract` is called
- **THEN** wiki.jsonl appends a `entry_type=format_contract` entry, containing node_skeleton (each agent node's type + output_schema required field set + whether verdict_field is declared) and binding_pattern (the success_evidence verbatim)

#### Scenario: A non-VERIFIED run produces no contract
- **WHEN** a run's verdict is PARTIAL/BLOCKED/UNVERIFIED and `wiki extract --contract` is called
- **THEN** no format_contract entry is appended (the experience entry is handled separately by extract without --contract)

### Requirement: plan --wiki-suggest returns a contract structure summary

`axiom plan --wiki-suggest --intent <text>` SHALL, while retrieving same-intent experience, also return a summary of the node_skeleton and binding_pattern of the top `format_contract` entry under that intent, printed to stderr (not polluting stdout's spec template).

#### Scenario: Getting the format that worked, before plan
- **WHEN** `plan --wiki-suggest --intent <text>` is run and the wiki holds a format_contract entry with the same intent
- **THEN** stderr lists the top contract's node_skeleton (node type + output_schema required set) and binding_pattern, while stdout remains a clean spec template

#### Scenario: Degrades gracefully when no matching contract
- **WHEN** the wiki has no format_contract entry with the same intent
- **THEN** stderr prints a "no matching contract" notice, does not block plan, and stdout emits the template normally

### Requirement: search retrieves contract entries

`axiom wiki search <query>` SHALL be able to match `format_contract` entries (by intent + tag keywords), returning a structure summary (node_skeleton + binding_pattern), listed alongside experience entries.

#### Scenario: Match a contract by intent
- **WHEN** `wiki search <query>` is run and a query word matches some format_contract entry's intent
- **THEN** that contract appears in the results, with node_skeleton + binding_pattern attached

### Requirement: Does not break the append-only semantics of experience entries

A format_contract entry SHALL enter the same wiki.jsonl hash chain via `Wiki.append()`, sharing the prev_hash/entry_id/event_hash isomorphic mechanism with experience entries. A format_contract entry SHALL NOT have a dedicated `add_impact` path; if a bad example needs to be flagged, reuse the existing `add_impact(parent_entry_id, kind="format_drift", ...)` amendment mechanism.

#### Scenario: Contract and experience share one chain without mutating each other
- **WHEN** an experience is appended first, then a contract, then another experience
- **THEN** all three share one hash chain (each prev_hash points to the previous entry's event_hash), and the experience entry's sealed fields are not changed by the contract append

