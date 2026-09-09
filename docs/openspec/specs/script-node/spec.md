# script-node Specification

## Purpose
Introduce a deterministic script-execution node for the axiom spec. Let tasks with deterministic answers — running tsc/grep/diff/format conversions — execute directly as a declarative script node via a harness subprocess, without dispatching an LLM subagent — pulling deterministic work out of the LLM loop, cutting token cost, eliminating hallucination, and making it replayable and verifiable.
## Requirements
### Requirement: Script node declarative deterministic execution

When a node declares `type: script` + `script_path` (path to a script inside the repo) + `output_schema` (structured output contract), the harness SHALL execute that script directly via subprocess and MUST NOT dispatch an LLM subagent. The JSON the script prints to stdout SHALL be parsed according to `output_schema`, written to the ledger, and exposed to downstream nodes for reference as `{{this.out}}`.

#### Scenario: Successful script node produces structured output
- **WHEN** a `type: script` node declares `script_path: scripts/run_tsc.py` and `output_schema: {required: [errors]}`, and that script exits with exit 0 and prints valid JSON `{"errors": 3}` to stdout
- **THEN** the harness dispatches no LLM subagent; parses `{"errors": 3}` per output_schema and writes it to the ledger; downstream nodes can reference the output via `{{<script_node_id>.validated_output.errors}}`

#### Scenario: Non-existent script path errors at validate stage
- **WHEN** a `type: script` node's `script_path` points to a non-existent file
- **THEN** `validate_spec` returns an error and the spec does not enter a run; the error message names the missing script_path

### Requirement: Script node failure follows failure_policy

When a Script node exits with non-0, the subprocess times out, or stdout cannot be parsed per output_schema, it is treated as a node failure, SHALL be handled per the node's declared `failure_policy` (retry/block), and MUST NOT be silently treated as success.

#### Scenario: Script exit non-0 retries then blocks
- **WHEN** a `type: script` node's script exits 1, with `failure_policy: {max_retries: 2, on_exhausted: block}`
- **THEN** the harness retries per failure_policy up to max_retries; if still failing, the node verdict is failure and it blocks that branch; no fake-success output is produced into the ledger

#### Scenario: Non-JSON stdout parse failure counts as node failure
- **WHEN** a `type: script` node declares an `output_schema`, but the script stdout is not valid JSON
- **THEN** that execution counts as a failure and is included in the failure_policy retry count; it MUST NOT fall back to "accept the raw stdout string"

### Requirement: Script node input args support upstream references

The `args` field of a Script node SHALL support `{{<upstream_node_id>.validated_output.<field>}}` template references to upstream node output (consistent with the `{{n.validated_output.field}}` convention used in agent prompts / synthesize inputs / the predicate DSL), and `validate_spec` MUST verify these references point to upstream nodes that actually exist.

#### Scenario: args template references upstream node output
- **WHEN** a `type: script` node declares `args: ["--files", "{{n1_edit.validated_output.files_modified}}"]`, where `n1_edit` is an upstream agent node that actually exists in the same spec
- **THEN** before dispatching that script node, the harness replaces `{{n1_edit.validated_output.files_modified}}` with the upstream's actual output value (scalars substituted directly; non-scalars JSON-serialized to a string) and passes it as the script input args

#### Scenario: args template referencing a non-existent node errors at validate
- **WHEN** a `type: script` node's `args` references `{{n_missing.validated_output.x}}`, and `n_missing` is not a node in the same spec
- **THEN** `validate_spec` returns an error and the spec does not enter a run

### Requirement: Script node is a work node, not a verdict node

A Script node SHALL NOT declare a `verdict_field` (it does work, not verdicts). But a downstream `verify` node can read the script node's structured output to make a judgment, and declare completion via a `success_evidence` binding. The script node itself MUST NOT directly bind the `success_evidence` completion judgment.

#### Scenario: verify node reads script output to adjudicate
- **WHEN** a spec contains `n_scan` (type: script, outputs `{violations: N}`) and `n_verify` (type: verify, reads `{{n_scan.validated_output.violations}}` to determine verdict), `success_evidence: ["S1:R1=node:n_verify"]`
- **THEN** the completion judgment is bound to `n_verify`'s verdict, not to `n_scan`; `n_scan` is only responsible for producing the violations count

#### Scenario: script node directly binding success_evidence is rejected
- **WHEN** a `type: script` node appears in a `node:<id>` binding within `success_evidence`
- **THEN** `validate_spec` errors: the completion judgment must bind a verify/agent node, not a script node (script does not produce a verdict)

