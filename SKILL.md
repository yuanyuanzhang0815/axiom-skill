---
name: axiom
description: Deterministic multi-agent workflow orchestration. Design a bounded workflow spec (IR), hand it to the axiom harness, read the checkpoint, decide the next workflow — never orchestrate node-by-node.
---

# axiom

> **Invariant:** The orchestrator may reason between workflows, but never between individual workflow nodes. Node-level execution and intermediate state belong exclusively to the harness and ledger.

## Architecture: control layer, not execution layer

axiom owns the **control layer** (spec / IR / ledger / gate / repeat / verify), **not the execution layer**. Execution is provided by **any agent runtime that can fulfill the axiom worker protocol** — axiom is not bound to one runtime.

| Layer | Responsibility | Entity |
|---|---|---|
| **Execution layer** | Agent dispatch, subagent management, LLM calls | Any Agent Runtime (Claude Code, pi, a remote worker, …) |
| **Control layer** | Workflow graph, state machine, convergence judgment, gate approval, hash-chained audit | axiom (this skill) |

**The worker protocol is runtime-agnostic:** axiom writes `dispatch_req_{rid}.json` (prompt/schema/tools/model), then polls for `dispatch_res_{rid}.json` (a `DispatchResult` envelope: `result_text` / `exit_code` / `cost_usd` / `num_turns` / `session_id` / `retry_class`). Any runtime that writes a conforming response back into the run-dir is a legal worker. This is a **symbiosis**, not a lock-in: the runtime solves compute/context; axiom solves *what to work on / what evidence suffices / when to stop / whether cost exceeds budget*. The two are orthogonal. `scripts/host_adapter.py` is the generic adapter for this protocol (its docstring calls itself "the only host-side code any agent runtime needs to plug into axiom").

**Connected execution backends** (`axiom run --runtime <backend>`, unified entry):

| Backend | How it runs | Capabilities |
|---|---|---|
| `cc` (default) | `host_adapter --cc` (claude -p + cc-switch config; file protocol) | tools (no cost channel) |
| `pi` | `host_adapter --pi` (pi NDJSON; file protocol) | cost, tools, cancel |

The three backend axes are separated: **who executes** (backend), **where** (location, currently local), **what it can do** (capabilities). At run start these are recorded into `run_context.json` as `runtime_backend`; **resume inherits the recorded backend and re-spawns the host side automatically**. Explicitly switching backends on resume is a `backend_switch` ledger event **persisted into `run_context`** — the next no-arg resume keeps the new backend; it never silently reverts. A backend whose binary is missing (e.g. `pi` not on PATH) fails fast before the run starts.

The host side is auto-spawned by axiom (pi/cc → `scripts/host_adapter.py` preset). `host_adapter` is also usable as a **standalone manual mode** (debug/e2e: the harness process calls it directly + an external adapter subprocess; see `scripts/e2e_host_adapter.py`). Do not run manual mode and `--runtime pi|cc` at the same time — two dispatch consumers will contend on the same run-dir.

**Symbiosis target (native subagent, not subprocess):** the current session's native subagent tool is in principle the most natural worker — no subprocess, shared session context. The real difficulty in connecting it is **isolation**: a native subagent's final message returns to the parent session (= the orchestrator), breaking the anti-self-audit wall ("the main session never touches intermediate node output"). Before connecting, one must first verify: is the native subagent's `usage.cost` extractable? (The subprocess path proved cc reports cost=0; pi exposes real cost/tokens.) The design constraint is documented alongside the native-agent result channel; the anti-self-audit rule is already enforced as a `validate` machine check (a `verdict_field` node's `write_areas` must be empty; `evidence_from` must point at a script node with `evidence_scope`).

## When to use

Use axiom when a task is **substantive**: fan-out, adversarial verify, multi-phase converge, **or a code change that needs change-assurance** — an independent verify of completed work, not the worker's self-report. Do NOT use it for a single trivial action — just do the action directly.

**Code-change tasks use axiom even when the root cause is known and the edits are designed out.** The value is not exploration here; it is the closing **change-assurance verify node**: an independent agent reads the *finished* edits, runs the real tests (pytest / tsc / TestClient / curl), and adversarially checks "did the worker actually meet every requirement — no missed file, no regression, no inverted guard, no prose-stub." The worker's own "I'm done" + green self-tests do NOT satisfy this — a worker that returns `VERIFIED` on its own output is self-certifying. **Never ship a code-change spec without a verify/agent-verdict node bound to `success_evidence`** (see Hard rules). This is what catches the degradation failure mode of worker LLMs on fine-grained edits.

For bug-fix tasks where the root cause is unknown, first run a read-only **Orient scout** (Step 0 in the walkthrough below), then Frame.

## Your job: graph designer, not runtime

You are the **graph designer**. Your unit of work is one *bounded workflow*:

1. **Frame** — state the Goal, Requirements (obligations, not paths), Boundaries, and Success Evidence bound to required requirements. One sentence each. This prevents drift.
2. **Design the workflow IR** — emit an `axiom` spec (JSON): nodes (`agent`/`parallel`/`pipeline`/`verify`/`synthesize`/`workflow`), `control_flow.steps`, `write_areas` ownership (non-overlapping), `output_schema` per node, `acceptance`, `verification_policy`, `failure_policy`.
3. **Hand off** — `axiom plan --out spec.json` then `axiom run spec.json --run-dir .axiom/run`. The harness executes node-by-node; you do NOT see intermediate node results.
4. **Read the checkpoint** — `axiom checkpoint spec.json --run-dir .axiom/run`. This is the ONLY object that re-enters your context: `{verdict, synthesized_output, evidence_summary, blocked_items, drift, open_questions, cost_usd_total, successful_cost_usd, failed_cost_usd, dispatch_count, dispatch_attempts, successful_dispatches, failed_dispatches, budget_exhausted}`. (`evidence_summary` is the list of event_ids constituting the evidence, not a prose summary; `budget_exhausted` is bool — did cost cross budget_usd. Cost is split BL-6: `cost_usd_total` = ALL spend including failed attempts == what `budget_exhausted` trips on; `successful_cost_usd` = agent_result spend (clean conform or recovered_from_prose); `failed_cost_usd` = stagnating/cognitive_attempt spend — real money, no usable output. An early editing-task relay showed why the split matters: `budget_exhausted:true` beside a small `cost_usd_total` looked contradictory until the failed-attempt spend was made visible.)

> **Why (P-007):** an early checkpoint looked self-contradictory (`budget_exhausted:true` beside a small `cost_usd_total`) because a failed attempt's spend pushed cumulative cost over budget but produced no successful output; `project_checkpoint` now splits cost into `cost_usd_total` (ALL spend, the budget-trip basis) / `successful_cost_usd` (agent_result, incl. recovered) / `failed_cost_usd` (stagnating/cognitive, real money no output).

> **Why (P-017):** the BL-6 split was still blind to three retry-path holes: an `unretriable` return, an `operational` retry (`continue`), and the cognitive `capped → return None` bail each called `_tally` (in-memory cost) but recorded NO event, so an operational-then-success run reported checkpoint $0.02 against $0.08 actually spent, and an over-budget run reported $0 — resume's rebuild (also ledger-summed) under-projected the budget and re-admitted dispatches past the real spend.

> **Why (P-004):** `cmd_checkpoint`/`verdict`/`state` warn to stderr when `--run-dir` holds no `events.jsonl` — pointing at the wrong dir surfaces immediately instead of printing an empty PARTIAL that reads like "it ran."

> **F8: watch progress via `events.jsonl`, not stdout.** The worker's dispatch output does not stream into the launch terminal; `axiom run`'s screen output is only the orchestration-layer summary — watching the screen for progress is wasted time (proven empirically). `axiom run` now tails `<run-dir>/events.jsonl` to stderr on startup.
5. **Decide** — based on the checkpoint:
   - **VERIFIED** → you may deliver.
   - **PARTIAL / UNVERIFIED** → **first `axiom debug spec.json --run-dir .axiom/run`** to project the failure envelope (which node stagnated/exhausted, its `failure_class` + `cognitive_signature`, `unmet_requirements`, deterministic `recovery_action`) — read-only, no LLM, no ledger write. Don't dive the journal by hand when the projection exists. Then design the next bounded workflow to close the gap; emit a new spec revision.
   - **BLOCKED** → a Gate is open (risk:high / secrets / delete node, unretriable failure, or contract_drift). `$LF gate list` to see it, then `$LF gate resolve --gate-id G_.. --decision allow|deny|modify`:
     - `allow` = human approves the risky action; records `gate_resolve` (the node is NOT re-dispatched automatically — re-run `axiom run` to re-dispatch with the gate resolved).
     - `deny` = reject; the node stays blocked.
     - `modify` = record a replan intent + write a stub `spec.v{n}.json` marker — you then design the real `spec.v{n+1}` (`parent_spec_id`=prior, new `spec_version_id`); never edit in place.
   - **CONFLICTED claim** (two verify nodes gave contradictory verdicts on the same `claim_id`) → `axiom claim list` to see which claims are CONFLICTED, then `axiom claim resolve --claim-id <C> --basis-ref evidence:<E> [--note ...]` to record a CLAIM_RESOLVED event pinning it to SUPPORTED. A human resolution can only reach SUPPORTED (cannot self-certify VERIFIED — the claim still needs a verify to reach terminal); `--basis-ref` must point at real evidence. If the CONFLICT is a spec-design flaw (e.g. shared claim_id namespace, see Spec authoring rules), fix the spec + re-run rather than resolving away the symptom.
   - `drift.contract_drift` → the contract changed; re-orient before replanning.
6. **Repeat** bounded workflows until the verdict is VERIFIED or you honestly stop (STAGNATING / budget exhausted). **Never loop node-by-node.**
7. **Verify** — claim strength ≤ evidence strength. A claim is VERIFIED only via a `verify` node (skeptic majority-unrefuted) or an agent node with `verdict_field` + `node:` binding, never by your own assertion or the worker's self-report. In a code-change workflow this step IS the change-assurance gate — the independent adversarial check that the finished edits truly meet every requirement; skipping it on "the task looked simple" is exactly the failure the Hard rule forbids.
8. **Deliver** — state the verdict truthfully (VERIFIED / PARTIAL / BLOCKED / UNVERIFIED). Do not announce PASS unless `derive_verdict` says VERIFIED.

## Hard rules

- **Type boundary:** raw worker stdout never flows downstream; only `validated_output` / `artifact_ref` / `ledger_event`. You never see raw node output.
- **Ownership:** parallel `write_areas` must not overlap; the harness rejects overlapping plans (logical isolation).
- **Claim ≠ Fact:** `PROPOSED → SUPPORTED → CHALLENGED → {REFUTED | VERIFIED}`. VERIFIED only via a verify node (`claim:` binding) OR an agent node with `verdict_field` returning `VERIFIED` (`node:` binding).
- **STAGNATING:** a retry with no cognitive delta does not burn `max_retries` but burns `max_stagnation`; after the budget, honest stop — reframe or seek info, do not mechanically loop. The `plan` template + walkthrough default `max_stagnation:2` (not 1) so G3 schema-feedback gets one injection before a 2nd identical failure trips stagnation — a prose-stubborn worker model needs that 3rd attempt.

> **Why (P-003):** the "default 2" above was prose only; `lint_spec` now warns (`axiom validate` prints `! <warn>`, still VALID) when `max_stagnation<2` on an agent-bearing spec, so a spec.v1 set to 1 trips the warn at validate time instead of wasting a whole run on a conform-fail that exhausts before any G3 schema-feedback injection (an early run: the worker read the code right but returned thousands of chars of prose, conform-failed once, gate).
- **Gate:** high-risk / secrets / delete / irreversible actions trigger a Gate. `modify` → new spec revision (never in-place).

> **Why (P-011):** `check_gate` used to key authorization on `node_id` alone, so a `gate_resolve(allow)` under spec v1 for action A (risk:high, write_areas X) auto-authorized spec v2's action B under the same node_id (different write_areas / prompt) without a new gate — a gate could be inherited across a changed action.

> **Why (P-018):** BLOCKED is a documented flow (gate list → resolve → re-run), but resolving a gate on a SEALED run appends `gate_resolve` after the seal manifest's `final_event_hash`, so `axiom journal` reported a false "seal manifest mismatch" tamper indicator on a healthy ledger.

> **Why (P-024):** P-011's scope binding had a wildcard hole: `stored_scope is None` was treated as "authorized", but NON-risk gates (`unretriable_failure`, `stagnation_exhausted`) never record a scope — so allowing an auth-failure RETRY silently authorized a later, DIFFERENT high-risk action under the same `node_id`.

> **Why (P-028):** P-018's re-anchor was unconditional: if the sealed tail had been TRUNCATED before the resolution, re-sealing over the truncated tail erased the only evidence (the old anchor) of what the tail used to be.
- **Caps:** `max_concurrent` 16, `max_agents` 1000, `budget_usd` enforced as honest exit.

> **Why (P-016):** a `max_agents=1` parallel fan-out of 4 items dispatched all 4: `_tally` only *logged* `agent_cap_exhausted` post-dispatch, and the pre-dispatch guard lived only in `_run_agent_with_retry`, which parallel/pipeline bodies bypass (they call `dispatch_agent` directly, `localized=True`).

> **Why (P-025):** P-016's pre-check was a bare READ of `_agent_count`: two truly concurrent threads both passed it and both dispatched (`max_agents=1, concurrency=2` → 2 real calls).
- **Authorization never expands:** "you handle it" does not authorize destructive or external-write actions; Gate them.
- **Change-assurance is mandatory on code-change specs:** a spec whose `write_areas` touch code files MUST bind every required requirement's `success_evidence` to a `verify` node (`claim:` binding) or an agent node with `verdict_field` returning `VERIFIED` (`node:` binding) that **independently** reads the finished edits + runs the real verification command (tests / build / runtime). A spec where implementer agents return `{files_modified, summary}` and nothing verifies them is self-certifying — the worker's "I'm done" + its own green tests are NOT evidence. This is the rule that prevents the failure mode of skipping axiom on a "no-fan-out, root-cause-known" code task and shipping the worker's self-report as truth. If the task touches endpoints or UI state, the verifier MUST be the runtime-verifier variant (`allowed_tools:["Read","Bash"]`).

> **Why (P-010):** three rechecks showed the same identity family still reproducible: a worker that FAILED (non-zero exit / envelope `is_error`) but happened to emit schema-conforming JSON with `verdict:VERIFIED` / `{refuted:false}` was promoted to success evidence → false VERIFIED → sedimented as a "proven contract" (poisoning the knowledge base).

> **Why (P-022 + P-023 + P-027):** review found three extensions of the same identity family.
- **verify-only specs (`write_areas:[]`) MUST NOT declare `assurance_hook`:** receipt/provenance is for *code-change* specs; a read-only verify writes nothing so a receipt is a category error (without a worktree it goes straight to UNVERIFIED). **Code tasks now use machine evidence**: a script node's `evidence_scope` + a reviewer's `evidence_from` — no change-assurance companion skill or hand-written receipt.json needed. Verdict binding still goes through `verdict_field` + `node:<id>`.

## Coverage before conclusion

> **Why this exists (P-002):** a session asked "does axiom have a knowledge-distillation layer?" and the model answered "no", missing `axiom/wiki.py` + the `## Wiki` and `## Skill self-evolution` sections. The bug was not "didn't scan enough" but **partial visibility → global negation**: "I haven't seen it" compressed into "it doesn't exist."

The most dangerous incompleteness is not "didn't see everything" — it is **not knowing you haven't seen everything, then asserting a negative as global fact.** Three retrieval surfaces (Repo Map navigation, CodeGraph relations, grep local) are all partial; truth is the underlying artifact (source + config + runtime), never any one surface.

- **Negative ≠ positive evidence threshold.** A positive assertion ("the project has X") needs one concrete evidence. A negative assertion ("the project has no X") must survive **multi-path retrieval** (Repo Map + filename/symbol search + semantic equivalents + docs/config/scripts) **and explicitly declare residual uncertainty** (regions not checked). Never output bare "doesn't exist" from "grep found nothing."
- **Coverage before conclusion.** Before any architectural judgment, fill a coverage checklist — checked domains, evidence found, residual uncertainty, confidence — *then* state the conclusion. Conclusion in the same turn as coverage, not before.
- **Conclusion strength ≤ evidence coverage.** If you covered 40% of the relevant surface, your conclusion strength is at most 40%. "Searched three places, not found → 100% absent" is the forbidden move.
- **Repo Map is navigation, not truth.** `axiom/REPO_MAP.md` (regenerate: `python3 scripts/generate_repo_map.py`) is a derived structural-fact index — top-level classes/funcs + intra-import edges + a module-docstring role, stamped with the commit it was generated from. If the stamp ≠ `git rev-parse --short HEAD`, it is **stale — do not depend on it**; regenerate or go to source. It stores structure (what exists, who imports whom), not semantics (what it means) — semantic conclusions must return to source evidence.

**Coverage-before-conclusion template** (fill before any "does the project have X?" / "is there no Y?" judgment):
```
Question: <the thing to judge>
Coverage:
  [x] axiom/*.py module list (read REPO_MAP.md)
  [x] docs/ + SKILL.md section titles
  [x] scripts/
  [ ] historical branches
  [ ] runtime-generated artifacts
Evidence found: - <concrete paths>
Residual uncertainty: not checked <regions>
Confidence: High / Medium / Low
```

**Self-irony anchor.** This section was added because a prior session answered "no three-layer structure" while `wiki.py` + two SKILL.md sections existed. Its author then needed an Explore agent to learn `state.py` and `wiki.py` share no direct import (side-by-side only via `cli.py`), that `harness.py` does not import `wiki.py` (so following the harness chain hides it entirely), and that HEAD had advanced two commits — all invisible to bare grep. The rule exists because the failure is structural to partial retrieval, not because the model was lazy.

## Spec authoring rules (from real editing-task relays)

Each of these cost a real run to learn; none is obvious from the IR schema:

- **F5/F5b: reference harness code by function name, not line number.** Line numbers drift with every commit (proven: a handoff's `_conforms` 964 → actually 1015, drifted 50 lines in a week); function names are stable. When writing docs/handoffs/triage notes, always write `harness.py's _conforms` or `harness.py::_conforms`, and verify on the spot with `grep -n "def _conforms"` — never paste a remembered line number. This document has purged existing line-number references.

- **Every agent node declares `output_schema` (with `required` fields).** The worker CLI's `--json-schema` is not enforced by the worker; the harness G1 directive keys off the schema. A node with no schema → the worker returns prose → conform-fail → retry/budget waste.

- **F4: schema is a type boundary, not a data-quality tool — declare only constraints that downstream *needs to flow through*.** The stricter the schema, the higher the worker conform-fail rate (proven: early runs demanded exhaustive schema fields; the worker conform-failed repeatedly burning money on retries; after simplification it passed first try). Discipline: put only fields downstream templates really `{{ref}}` to in `required`; avoid enums/value-ranges/deep nested structure unless unavoidable; declare array `items` only when downstream consumes them by field. Test: if this constraint is unmet, does downstream **break** or just **look bad**? Only what breaks goes in schema; what looks bad is left to the verify verdict. Quality shortfalls are returned by verify (fix_loop re-grind), not the boundary's job. After F1 the verdict node defaults to strict JSON, paired with G3 fail_keys feedback giving the worker a clear correction path — don't stuff loose fallbacks into the schema to "make the worker's life easy"; that smuggles quality adjudication from verify into the boundary.
- **A parallel step MUST start with the keyword `"parallel"`.** Write `["parallel", "n2", "n3"]`, NOT `["n2", "n3"]`. `validate` now rejects a bare list (validate/run parity); a bare list was silently skipped by `_run_sequence` in older versions, dropping the whole fan-out.
- **A requirement's verdict comes from EITHER a `verify` node (`claim:` binding) OR an agent node with `verdict_field` (`node:` binding).** An agent that checks work and returns `{verdict: VERIFIED|FAILED}` should declare `verdict_field: "verdict"`, and the matching `success_evidence` binds with `node:<id>` (e.g. `S1:R1=node:n5`). Without this, worker-all-succeeded + agent-verdict-VERIFIED still projects PARTIAL — the agent's verdict is not a verify claim by default.
- **Set `budget_usd` with retry headroom (≥ estimated × 2-3).** Estimate: `dispatches ≈ Σ_nodes (1 + max_retries)`; per-dispatch cost ≈ 0.5–1.2 USD for a worker-model read+edit task (a real relay's n1 was 1.16 USD/6-turns). So a 4-node spec with max_retries=2 → ~12 dispatches × 1 USD ≈ 12 USD worst-case; set `budget_usd` ≥ 15–20. The worker model is non-deterministic: the front-G1 directive is not 100%, so a node may conform-fail and retry, and retry cost accumulates. A tight budget trips `budget_exhausted` mid-run. One relay needed budget 20 for an ~5-cost task (retry swelled cost to 16.3 at budget 10 → budget_exhausted, dispatch 0; at budget 20 → all conform, VERIFIED, cost 4.46).
- **`acceptance` is required by `validate` but decorative at runtime.** Every agent node must declare a non-empty `acceptance` list (validate rejects empty) — use it to state the success criteria in plain text (e.g. `["file modified", "verdict field present"]`); it is NOT enforced by the harness (no runtime check), it documents intent for the verifier agent + the orchestrator.
- **Multi-agent specs must partition claim_id namespaces.** When several agent nodes each emit findings with `claim_id`s, sharing one prefix space (e.g. both the spec/plan agent and the security agent use C1-C4) makes two different verify nodes give contradictory verdicts for the same id → false CONFLICTED (a spec-design flaw, not a semantic conflict; the strict-verdict-gates CONFLICTED detection then correctly fires but on a self-inflicted collision). Partition by domain prefix (SP1-4 for spec/plan, SEC1-4 for security) so a `claim_id` never collides across verify targets.
- **No agent prompt runs `axiom run` or drives the worker dispatch itself.** Those are orchestrator actions, not worker actions. A worker's cwd is its run-dir; a prompt that executes `axiom run --run-dir .axiom/run` recurses infinitely (run-dir/.axiom/run/.axiom/run/..., each layer exit 0 so the orchestrator sees success with only nested run-dirs and no output — a docx-review relay hit exactly this). `axiom run`/`resume` now refuses a run-dir nested inside an existing run-dir, but don't rely on the guard: keep worker prompts about the task (read/edit/verify files), not about driving axiom itself.
- **Prefer `script` nodes for deterministic verify (not `agent`).** If a verify step is deterministic — grep a structural assertion, run pytest/tsc + parse counts, compute whether a tsc error falls inside a diff hunk — use a `script` node. See the `script` node-type entry below (proven: script 23s/zero conform/zero fabricated-evidence vs pi-agent 94s vs cc-agent 410s). Reserve `agent` for genuine judgment.

- **Scout / inventory nodes MUST declare coverage on negative findings.** A scout or any agent node that answers "does the project have X?" / "is there a mechanism for Y?" must declare `output_schema` with a `coverage` field (checked domains / residual uncertainty) and return negative findings as `"not found in <checked>; unchecked: <regions>"` — never a bare `"no"`. This binds the `## Coverage before conclusion` discipline into spec authoring, so the worker cannot ship "grep found nothing → doesn't exist" past the verdict. A scout that omits `coverage` on a negative is self-certifying in the same way an implementer without a verifier is.

> **Why (P-008):** an agent-verdict `n_impl→n_verify` chain hit a real bug (verify FAILED on a sync-method `await`) but the run went straight to PARTIAL with no re-grind; v2's `until`/`repeat` loops were built for the verify-claim path (cond reads `claim_status`), so the agent-verdict "cond reads verdict" road was broken.

- **n_verify credibility three-layer discipline (F9/F10/F12: n_verify's PASS cannot be trusted; the blind spot is at the spec layer, not the verify-capability layer).** Proven in a real run: a callback endpoint had no state-machine precondition (any-state record was force-signed), and n_verify's 8-step check all PASSED without catching it — it only verified the happy path. When writing any verify node's prompt/acceptance, all three layers must be **explicitly listed**: ① **happy path** — run the normal flow end-to-end for real (not "read the code and infer it should pass"); ② **negative path** — did the things that should reject actually reject? non-X-state calling Y endpoint should 400, unauthenticated should 401/403, invalid input should 4xx not crash, duplicate submit should be idempotent; ③ **real-run real-check** — DB state must really run `init_db` + `PRAGMA`/`SELECT` to verify (don't trust a static claim "should have this field"); service must really start and really curl; assertions must be based on real execution output. If real-run is needed, set `allowed_tools` to `["Read","Bash"]` — pure Read can't do ① and ③; `lint_spec` warns on "runtime task with a static verify" but the spec author must proactively write out all three layers.

- **`impl→verify` fix loop: declare `fix_loop` on the verify node.** When verify judges FAILED, the harness automatically re-dispatches `n_impl` with its `issues` to fix, then re-verifies, up to `max_rounds`. Necessary condition: impl's prompt **must** explicitly reference `{{_fix_feedback.issues}}` — the harness injects the values, but if the prompt template doesn't render them the worker can't see them (proven: without referencing, 3 rounds rewrite the same error). Don't add it for one-shot verify.

> **Why (P-006):** a worker LLM often finishes the file work but returns prose/partial JSON (`--json-schema` is not enforced worker-side), so the harness used to treat "report format wrong" as "cognitive failure" → conform-fail → stagnation → gate, burning money on work that was actually done (a run: 3 conform-fails burned $7.5 then gate, but verify was VERIFIED).

- **When a worker returns pure prose/partial JSON, an impl node recovers via P-006 conform-fail recovery.** For impl nodes with non-empty `write_areas` and no `verdict_field`, the harness compares the sha256 of `write_areas` files before/after dispatch — files really changed = work was done, rebuild `validated_output` (`_recovered:true`) and pass it to verify, no stagnation, no gate. No file change is the real failure. `verdict_field` nodes (verify) **never recover** — the verdict must be real JSON. Downstream verify adjudicates whether the recovery is correct.

> **Why (P-013):** P-006 recovery's rebuilt output only had the three semantic fields `files_modified`/`summary`/`_recovered`; other fields the node `output_schema` required were never produced by the worker, but downstream couldn't tell "field missing" from "field empty" — the recovered object masqueraded as complete. Full recovery_schema (#8) now closes as **three-layer explicit disclosure**: (1) on recovery, compute `missing_required_fields` against `output_schema.required`, write into `recovered._recovery{}` + the `agent_result` event payload (queryable at the fact layer); (2) `Harness._recovery_disclosure(values)` scans upstream wrapped/par-list/bare `_recovered` outputs, generating a disclosure block (which node lacks which fields + "missing ≠ empty/default/confirmed fact"); (3) `_build_worker_prompt` auto-appends that disclosure to all downstream node prompts — a G1-style harness force, **not depending on the spec author referencing the template variable** (P-008 proved injected-but-unreferenced values = worker can't see them). When verify adjudicates a recovery, the most-needed fact is which fields never existed.

- **F1 `conform_strict_json` (a different road from P-006).** P-006: "not JSON but files changed → pass" (impl, file evidence speaks); strict: "not pure JSON → judge failed, retry". Three states: explicit `true`/`false` takes priority; **when omitted, verdict nodes (with `verdict_field`) auto-strict, impl nodes auto-tolerant**. Strict forbids extracting a first-object from prose (extracted might be an example, not a conclusion); after conform-fail, G3 feedback switches to strict form, skipping P-006 recovery. A verdict node with explicit `false` = actively weakening verdict integrity; validate lints a warning.

## Verdict model pairings (do not mix)

A requirement's verdict comes from exactly ONE of two paths — the parts must pair. Mixing them yields PARTIAL despite correct work:

| Path | node shape | success_evidence binding | what flows |
|---|---|---|---|
| **agent-verdict** | agent declares `verdict_field: "verdict"` | `S1:R1=node:<id>` | agent returns `{verdict: "VERIFIED"\|"FAILED", ...}` → emits `agent_verdict` event |
| **verify-claim** | `verify` node, `target` = a SINGLE `{{ref}}` to an upstream producing findings | `S1:R1=claim:<claim_id>` | upstream agents produce findings with `claim_id`; skeptics refute/survive each |

Catches for the common mistakes:
- `node:<id>` REQUIRES the named agent to declare `verdict_field` and return `{verdict:"VERIFIED"}`. An agent returning `{fixed_bugs, files_modified}` (no `verdict_field`) + a `node:` binding → no `agent_verdict` event → R unmet → PARTIAL. Either add `verdict_field`+`verdict`, or verify it via a verify node.
- `verify` `target` is a SINGLE `{{ref}}` (e.g. `{{findings}}` pointing at a node that produces a findings list). Do NOT write `{{n1.validated_output}}, {{n2.validated_output}}` (comma-separated multi-ref) — `_resolve` parses one ref; a comma string is not a valid lookup path.
- `claim:<x>` binds a **claim_id** (e.g. `C1`, produced by an agent finding and adjudicated by a verify node), NOT a node id. `claim:n4_verify_fixes` is wrong (that's a node id).
- For a "did the edits fix the bugs?" verifier, prefer the **agent-verdict** path: one agent reads the changed files, returns `{verdict:VERIFIED, issues:[]}` with `verdict_field:"verdict"` + `node:<id>` binding. The verify path adjudicates upstream *claims* (skeptic refutation), not file checks.
- For a "does the fix actually work in the running service?" verifier, use the **agent-verdict path with `allowed_tools:["Read","Bash"]`** — NOT just Read/Glob. The worker dispatch passes `--allowedTools` through unfiltered, and `check_gate` only triggers on `risk:high` or `secrets`/`delete` `write_areas`, so a default `risk:low` runtime verifier runs with no Gate. Its prompt restarts the service (kill + start), curls the affected endpoints, asserts on live output, and returns `{verdict:"VERIFIED"|"FAILED", issues:[...]}`. Without `Bash`, VERIFIED means "files look right", not "bug is gone at runtime" — and you end up re-verifying by hand (a flight-price-monitor relay ran ~9 shell commands after a VERIFIED checkpoint for exactly this reason).

## Commands

> **Why this exists (P-009):** `bin/axiom` uses `python3 -m axiom` + PYTHONPATH, but under `-m` the cwd's `sys.path[0]('') precedes PYTHONPATH, so any cwd with a same-named `axiom/` package (e.g. a dev sandbox worktree) shadows the installed skill → `wiki pattern list` reads an empty store. The shim now uses a `-c` bootstrap that does `sys.path.insert(0, SKILL_DIR)` in-process to force the installed version first, with `sys.argv[0]='axiom'` to preserve the argparse prog name.

> **Why this exists (P-026):** P-009 fixed the main entry but not the subprocess the main entry regenerates: `axiom run --runtime cc` auto-spawns the host adapter, which still used `python -m axiom`, and in a plain project dir (no PYTHONPATH) hit `No module named axiom` (main process fine, child dead). `cmd_run` now uses the same `-c + sys.path.insert(0, <install parent>)` bootstrap to spawn children — when fixing an entry-class bug, the fix surface must cover the whole process tree. The PYTHONPATH approach was rejected (it sits after cwd in sys.path, can't defeat P-009's shadowing).

```
# axiom's bin lives at <skill_root>/bin/axiom — <skill_root> = the directory
# containing this SKILL.md. Resolve it relative to THIS file, never assume a
# fixed absolute path — or export PATH="<skill_root>/bin:$PATH" first, then
# bare `axiom`. No nvm export / bash -lc needed — axiom resolves its own
# runtime, and python3 is at /usr/bin.
LF=<skill_root>/bin/axiom   # e.g. LF=~/.claude/skills/axiom/bin/axiom
$LF plan --intent "..." --out spec.json              # scaffold a spec template
$LF validate spec.json                                # validate + contract_hash
$LF run spec.json --run-dir .axiom/run             # SYNCHRONOUS: blocks until all nodes dispatch, then prints output + checkpoint. The checkpoint IS the return value. For short specs, foreground and wait. For LONG workflows (>~2 min, many nodes, big worker tasks) you cannot foreground (tool-call timeout) — background the run (`nohup ... &` / your harness background task) and poll `axiom checkpoint spec.json --run-dir .axiom/run` ~every 60s until the run-dir is sealed (`verdict.json` exists or the run process has exited), then read the final checkpoint. This background+checkpoint-poll is the legitimate pattern for long runs — the run stays synchronous+deterministic; the poll only bridges agent tool-call timeouts.
$LF resume spec.json --run-dir .axiom/run          # re-walk + re-project a run
$LF continue                                         # cross-session resume after /clear — no args; reads active-run pointer, prints checkpoint + next-step guidance
$LF continue --resume                                # same, but re-dispatch unfinished nodes (re-seals the run)
$LF state spec.json --run-dir .axiom/run           # cognitive state (goal/claims/progress/completion)
$LF verdict spec.json --run-dir .axiom/run          # VERIFIED|PARTIAL|BLOCKED|UNVERIFIED
$LF checkpoint spec.json --run-dir .axiom/run      # the ONLY re-entry object
$LF gate list --run-dir .axiom/run                 # open/resolved gates
$LF gate resolve --gate-id G_.. --decision allow|deny|modify [--modify-intent "..."]
$LF journal --run-dir .axiom/run                   # ledger events (hash-chained) + tamper check
$LF doctor                                          # preflight: pi/cc binary on PATH? provider key set? host_adapter.py present? run this FIRST on a fresh/bare host
```

Exit codes: `run`/`verdict`/`checkpoint`/`resume`/`continue` return **granular** codes so automation (`axiom run && deliver`, CI `case $?`) can triage without parsing JSON: **0=VERIFIED**, **3=BLOCKED** (resolve a gate then re-run), **4=PARTIAL** (replan), **5=UNVERIFIED** (gather evidence), 2=unknown/invalid (also: `continue` with no active-run pointer, or a stale pointer). Non-zero still means not-VERIFIED, so `axiom run && deliver` is unaffected. `validate`/`journal` return 1 on invalid/tamper.

## Workflow IR node types

- `script` — deterministic, **zero LLM dispatch**. Runs `script_path` (a `.py`) with `args`, parses stdout as JSON against `output_schema`. For evidence a script produces deterministically: `grep` a structural assertion, run `pytest`/`tsc` and parse counts, compute git-diff hunk line ranges. Output flows downstream via `{{<id>.validated_output}}` (same template ref as agent nodes). `failure_policy` same shape as agent; non-zero exit / bad JSON → `script_result` with a `failure_class`. **Prefer `script` over `agent` for deterministic checks** — an agent (LLM) doing deterministic work pays conform-fail retry (prose-not-JSON) + fabricated-evidence risk (may invent evidence); a script does it in seconds, zero conform, zero fabrication (runs real commands, can't fabricate). Reserve `agent` for genuine judgment (is the logic *correct given intent*). Proven by `scripts/e2e_script_node.py` (script→agent→script→agent chain, zero LLM for script nodes).

  **Empirical (a relay's verify stages, three ways)** — same verify three ways: script nodes (3 script + 1 minimal verdict agent) = **23s, zero conform-fail, zero fabricated evidence**; pi-agent (pure) = 94s with conform-fail retries; cc-agent (pure) = 410s with conform-fail retries. 4-17× faster; the deterministic evidence (grep/pytest/tsc hunk) is more trustworthy than an agent's prose claim. The conform/fabrication frictions are *symptoms* of "using an LLM where determinism sufficed" — script nodes remove the conditions that produce them, not work around them.

  Two reference patterns (from a real run's `struct.py` / `tsc_run.py`), copy into a spec-author script body:

```python
# grep ONLY +lines of a diff — deleted lines mislead.
# Friction: git diff HEAD~1 contained -multiple={false} (deleted); a naive
# 'multiple' in full_diff → false pass. Fix: grep added lines only.
def diff_added_lines(diff_text):
    return "\n".join(l[1:] for l in diff_text.splitlines()
                     if l.startswith("+") and not l.startswith("+++"))

# classify tsc errors: new (line inside a diff hunk) vs pre-existing.
# Friction: tsc errors at pre-existing lines made an
# agent FAILED unless taught to read git-diff hunk headers — a script
# computes it deterministically in ms.
def hunk_added_ranges(diff_text):
    ranges = []
    for line in diff_text.splitlines():
        m = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@", line)
        if m:
            s, n = int(m.group(1)), int(m.group(2) or 1)
            ranges.append((s, s + n - 1))
    return ranges
# then: error line in any range → new_error; else → pre_existing (not counted)
```

  These stay **reference snippets in script bodies** (not an `axiom.diff_scope` lib) — one reuse site doesn't warrant a module yet (REDUCE: verify before building, don't add a layer first; extract when a 2nd project reuses them).
- `agent` — one worker dispatch with a structured-output schema. `failure_policy`: `{max_retries, retry_guard, on_exhausted}` — **defaults if omitted/empty**: `max_retries=1`, `retry_guard="requires_new_evidence"`, `on_exhausted="block"`. `on_exhausted`: `block` (Gate→BLOCKED) | `degrade` (soft None, gap surfaces via verdict) | `replan` (emit `replan_requested` boundary signal, no Gate). **`on_stagnation`** (F3 cause-level override, inside `failure_policy`): when stagnation_exhausted (same failure signature repeated = truly stuck) follow this instead of `on_exhausted` — for impl→verify chains, recommend `"on_stagnation":"degrade"`, so stagnation auto-passes to downstream verify to adjudicate (real runs proved stagnation gate human-resolves almost always allow; verify is the true adjudicator; with fix_loop, verify FAILED auto re-grinds impl), while retries_exhausted (operational/cognitive failure exhausted) still goes to on_exhausted human gate. Omitting it = stagnation also follows on_exhausted. Set `max_retries=2` for retry headroom against worker non-determinism. Optionally declare `verdict_field` (a key into `validated_output`) to let this agent OWN a requirement's verdict — on clean conform, if `validated_output[verdict_field] == "VERIFIED"` it emits an `agent_verdict` event; bind it via `success_evidence` `node:<id>`. Use this for a code-review verifier that checks files and returns `{verdict: VERIFIED|FAILED}`. A **runtime/smoke verifier** uses the same shape but declares `allowed_tools:["Read","Bash"]` (or `["Read","Bash","WebFetch"]`) — it restarts the service, curls endpoints, asserts on live output, and owns the verdict via the same `verdict_field` + `node:<id>` binding. See the runtime-verifier variant under the Quick-start walkthrough.
- `parallel` — fan-out `body` over `over` (a `{{ref}}` to a list, e.g. `"over":"{{items}}"`); each iteration injects `{{item}}` (**singular** — `vu["item"]=item`, so the body prompt uses `{{item}}` not `{{items}}`). Real concurrency (`concurrency`, capped by `max_concurrent`); barrier; null on failure (localization). Ledger appends are lock-serialized so the hash chain stays intact under concurrent writes. In `control_flow.steps` a parallel fan-out is written `["parallel", "n2", "n3"]` (the `"parallel"` keyword FIRST — a bare `["n2","n3"]` is rejected by `validate`).
- `pipeline` — each item through `stages` with no stage barrier; items run concurrently, each item's stages run sequentially; item drops to null on stage failure.
- `verify` — `skeptic_count` independent skeptic agents prompted to REFUTE. A skeptic that fails to return conforming JSON **abstains** (not a refutation — kills the false-refutation failure mode). A claim survives iff a strict majority of the **conforming** skeptics did NOT refute (`refute_votes < effective/2`); over-half abstention → CHALLENGED (unverifiable, neither survived nor refuted). A verify node may declare `allowed_tools`/`read_areas` to grant skeptics evidence access (e.g. `Read` a screenshot) — default is `WebSearch`/`WebFetch`.
- `synthesize` — merge `inputs` (refs like `{{n1.validated_output}}`) via an agent that retries with G3 schema-feedback (not a single shot). On persistent failure it degrades softly to `{}` (never a Gate — the verdict is driven by verify evidence, not the report node).
- `workflow` — load `spec.<name>.json` from the run dir; sub-harness.

### v2 control-flow node types

These nodes add **looping, branching, and gated escalation** — the three patterns that required orchestrator-level iteration in v1. Use them when a single pass cannot guarantee convergence.

#### `repeat` — loop-until-dry convergence

Iterate a body until the output stops changing (`dry<N`). The canonical pattern for "implement → verify → fix → re-verify" loops.

```json
"loop_fix": {
  "type": "repeat",
  "id": "loop_fix",
  "body": ["n_implement", "n_verify"],
  "max_iterations": 3,
  "until": "dry<2",
  "failure_policy": {"on_exhausted": "block"}
}
```

- `body` — ordered list of node ids to execute each iteration.
- `max_iterations` — hard cap (required, ≥1). The loop exits after this many iterations regardless of convergence.
- `until` — convergence condition. Currently supports `"dry<N"` (converge when `dry_count ≥ N`). `dry_count` is re-derived from ledger events after each iteration — never cached — so it reflects real cognitive delta.
- Each iteration emits `iteration_open` / `iteration_close` events with `loop_id`, `iteration`, `exit_reason`. The whole loop emits `loop_open` / `loop_close`.
- **Budget mid-body**: if budget trips mid-iteration, the partial iteration's work and cost are recorded (`iteration_close` with `exit_reason='budget_exhausted'`), then the loop closes. No further iterations.
- **Stagnation cap**: consecutive iterations with no cognitive delta (`stagnating` events) count toward `spec.max_stagnation`; exceeding it breaks the loop.
- `failure_policy.on_exhausted`: `block` (default) → BLOCKED verdict; `degrade` → UNVERIFIED with partial evidence; `replan` → `replan_requested` event.

#### `until` — do-until with arbitrary predicate

Like `repeat`, but converges on an **arbitrary predicate** instead of dry-count. Use for "keep going until a claim is VERIFIED or budget runs out".

```json
"loop_verify": {
  "type": "until",
  "id": "loop_verify",
  "body": ["n_fix", "n_check"],
  "cond": "claim_status('C1') == 'VERIFIED'",
  "max_iterations": 3,
  "budget_aware": true,
  "failure_policy": {"on_exhausted": "degrade"}
}
```

- `cond` — predicate DSL expression (see below). Evaluated **after** the body runs (do-until semantics: body always executes at least once).
- `budget_aware` (default `true`) — refuse to enter iteration k if budget is already exhausted. Set `false` to allow one more iteration past budget.
- All loop/iteration events, stagnation, and budget-trip behavior are the same as `repeat`.

#### `condition` — deterministic branch

Evaluate a predicate and run one of two branches. Use for "if the scout says the code exists, refactor it; otherwise implement from scratch".

```json
"n_branch": {
  "type": "condition",
  "id": "n_branch",
  "predicate": "claim_status('C_existing') == 'VERIFIED'",
  "then_branch": ["n_refactor"],
  "else_branch": ["n_greenfield"],
  "degraded": false,
  "gate": false
}
```

- `predicate` — DSL expression. If truthy → `then_branch`; falsy/`'undefined'` → `else_branch`.
- `degraded` — set `true` (readonly) when the predicate read a `PROPOSED` claim (advisory, not binding). The branch still executes; this flag signals the orchestrator that the decision was advisory.
- `gate` — **cannot-branch-on-PROPOSED rule**: set `true` to make this a **binding branch**. If the predicate reads a PROPOSED (unverified) claim, a Gate opens (`binding_branch` gate) and the run halts with `GateHalt`. Resolve with `$LF gate resolve --gate-id G_.. --decision allow` to proceed, or `deny` to abort the branch (returns empty output → PARTIAL/UNVERIFIED). **Always set `gate:true` when the branch decision must be based on verified evidence, not agent prose.**
- Emits `branch_decision` event with `{predicate_value, claim_strength, branch_taken, degraded, gate}`.

#### `gate` — staged escalation

Explicit human-in-the-loop gate with optional auto-escalation tiers. Use for "if the review flags a critical issue, pause for human judgment; if auto, escalate to a stronger model".

```json
"n_gate": {
  "type": "gate",
  "id": "n_gate",
  "trigger": "claim_status('C_review') == 'REFUTED'",
  "on_trigger": "pause",
  "escalation_tiers": [{"model": "claude-opus-4-20250514", "skeptic_count": 3}],
  "body": ["n_fix"]
}
```

- `trigger` — predicate DSL. Not triggered → `body` runs directly (gate is invisible).
- `on_trigger`: `pause` (default) → open gate + `GateHalt` (human must resolve); `auto` → open gate + immediately resolve with `escalate` + run body with tier-0 config merged; `replan` → emit `replan_requested`, body NOT executed.
- `escalation_tiers` — list of `{model?, skeptic_count?, allowed_tools?}`. On `auto` or after `gate resolve --decision allow`, tier-0 config is shallow-merged into body agents (immutable spec copy — production spec is never mutated). Use stronger models / higher skeptic counts for escalated retries.
- `trigger` is evaluated before the body — if not triggered, the gate is a no-op passthrough.

### Predicate DSL

Control-flow nodes (`condition`, `until`, `gate`) evaluate predicates in a **restricted eval sandbox** (`{"__builtins__": {}}` — no imports, no file access, no network). Available context functions:

| Function | Returns | Example |
|---|---|---|
| `claim_status(id)` | `'PROPOSED'` / `'SUPPORTED'` / `'CHALLENGED'` / `'REFUTED'` / `'VERIFIED'` | `claim_status('C1') == 'VERIFIED'` |
| `verdict()` | final verdict string | `verdict() == 'VERIFIED'` |
| `budget_used()` | float, cumulative USD | `budget_used() < 0.8` |
| `budget_remaining()` | float | `budget_remaining() > 0.1` |
| `dry_count()` | int, re-derived from ledger | `dry_count() >= 2` |
| `iteration()` | current loop iteration (1-based) | `iteration() <= 3` |
| `stagnation_count()` | consecutive stagnating dispatches | `stagnation_count() < 2` |

Expressions can use `{{ref}}` to read upstream `validated_output` fields (e.g. `{{n_scout.exists}} == true`). **Caveat**: `{{ref}}` reads raw agent output — `claim_strength` is `proposed` (advisory). For binding decisions, use `claim_status(...)` (which reads verify-adjudicated state → `verified` strength) and set `gate:true` on the condition node.

Unresolvable `{{ref}}` → `'undefined'` → else-branch (safe default).

### Worktree isolation

An agent node with `"isolation": "worktree"` executes in a **git worktree** — an isolated checkout where the worker's file edits never touch the main working tree. Use when multiple agents write overlapping areas in parallel (e.g. two implementers touching the same module) or when you want to inspect/reject a worker's changes before merging.

```json
"n_impl": {
  "type": "agent", "id": "n_impl", ...,
  "isolation": "worktree",
  "runtime_assets": ["data/**", ".env", "src/**"]
}
```

- `runtime_assets` — globs for files/dirs the worktree needs but are gitignored (DB files, `.env`, config, data). The harness **auto-symlinks** the root-relative prefix into the worktree (e.g. `data/**` → symlinks `data/` from the main checkout). If omitted, `_auto_detect_runtime_assets` auto-detects: `data/**` if `data/` exists, `.env` if present, and every top-level dir with `__init__.py` (namespace packages), excluding tests/node_modules/venv/dist/build.
- Worker writes inside the worktree; `_snapshot_artifacts` pins them content-addressed into `run_dir/artifacts/`. The main checkout is untouched (**no-leak property**). There is no auto-merge — artifact refs are the merge unit.
- Worktree is cleaned up after dispatch (unless `keep_worktrees=True` for debugging). Symlinks are unlinked before removal so `rmtree` cannot follow them into the main checkout.
- **Ownership relaxation**: overlapping `write_areas` are allowed when *every* node in the overlapping pair has `isolation=="worktree"` (intentional isolated-merge). A mixed pair (one worktree, one not) is still rejected.

### Resume cache (v2)

`axiom resume` replays successful nodes from the ledger (zero dispatch, zero cost). v2 extends the cache key to `(svid, node_id, loop_id, iteration)` for loop body nodes, and `(svid, node_id, None, 0)` for non-loop nodes (v1-compatible). A **secondary `input_hash` check** ensures upstream inputs match — if they changed (e.g. a prior node's output differs), the cache misses and the node re-dispatches. This means: resuming a converged loop with no upstream changes replays instantly; changing an upstream output forces re-evaluation even for previously-successful nodes.

### Cross-session resume (after /clear)

> **Why this exists (P-001):** the `/clear` → multi-minute state-reconstruction friction that motivated the active-run pointer.

`axiom run` writes an **active-run pointer** the moment a run starts, so a `/clear`-wiped context can re-land on the run without reconstructing state from memory + git + disk (which is what produces the multi-minute "thinking" on a bare `axiom continue`):

- **Project-scoped:** `<project_root>/.axiom/active.json`
- **Global mirror:** `~/.axiom/active.json` (last-active run across all projects — the one `continue` with no args reads)
- **Fields:** `{project_root, spec_path, run_dir, spec_version_id, started_at, sealed, verdict}` — all absolute paths, so it survives a cwd change. Written atomically (tmp + `os.replace`).
- On run **start** → pointer written `sealed:false`. On run **end** → pointer refreshed `sealed:true, verdict:<final>`. `resume` does the same refresh around its re-walk.

`axiom continue` (no `spec.json` / `--run-dir` args) is the no-arg discovery command that was missing:

- **No pointer found** → exit 2, telling you which two files it checked and how to start a run.
- **Pointer found (sealed or not)** → prints a **handoff digest** to stderr (and the full checkpoint JSON to stdout). The digest = `spec.intent` + verdict/cost/dispatch-count + `derive_debug_envelope`'s per-node failure detail (failure_class, cognitive signature = WHY each node failed, deterministic `recovery_action`, unmet requirements) + a mechanical next-step line. This replaces the old one-line guidance that told you to run a separate `axiom debug` — the second hop IS what produced the multi-minute "thinking" after /clear; the digest inlines it so you read off stderr and resume. add `--resume` to re-dispatch unfinished nodes (re-seals the run, refreshes the pointer); on a sealed run `--resume` is a no-op digest print, not a re-dispatch — start a new run with `axiom run <spec>` instead.
- **Stale pointer** (spec_path no longer a file) → exit 2 with the stale path, so you don't act on a run whose spec moved.

You can still pass `--spec` / `--run-dir` explicitly to bypass the pointer.

> **Orchestrator behavior on resume (why the digest exists, not just the pointer):** the pointer solved the FIND layer (which run to re-land on), but the multi-minute "thinking" after /clear was also the UNDERSTAND layer — re-reading the full spec + trawling `events.jsonl` to rebuild "what's this run, where's it stuck, what next". The handoff digest collapses that into one stderr block. **Resume from the digest:**
> - Read the digest, act on its next-step line. Do NOT re-read the full spec. Do NOT trawl `events.jsonl` / `journal`. That re-read IS the multi-minute thinking the digest replaces.
> - The digest names WHICH node failed (by `node_id`). If you need that node's prompt/binding detail, read ONLY that node's slice in the spec — not the whole spec.
> - The mechanical `recovery_action` is a STARTING point, not a verdict. Judge the real next step from the cognitive signature — `re-dispatch` is wrong when the signature shows a structural (not stochastic) failure (e.g. cc worker's web tools structurally unavailable), where re-dispatch just re-fails.

The entry is just typing `axiom continue` — the host invokes the skill and runs `axiom continue` (no-arg, reads the pointer). No slash command, no flags for the common case.

### v2 authoring rules

- **Loop nodes (`repeat`/`until`) MUST declare `max_iterations` ≥ 1.** `validate` rejects missing or zero `max_iterations`. Without it, the loop could run forever.
- **`condition` + `gate:true` for binding branches.** If the predicate reads `{{ref}}` (raw agent output), the decision is advisory (`claim_strength=proposed`). Set `gate:true` to enforce that the branch only proceeds on verified evidence (or human override via gate resolve).
- **`until` predicates: prefer `claim_status(...)` over `{{ref}}`.** `claim_status('C1') == 'VERIFIED'` reads verify-adjudicated state; `{{n_check.verdict}} == 'VERIFIED'` reads raw agent output (the agent might be wrong — that's what verify exists to check).
- **`repeat` `until:"dry<2"` is the most common convergence criterion.** `dry<1` means "any non-empty change" (converges on first no-change iteration); `dry<2` means "two consecutive no-change iterations" (more stable — resists a single stale retry). Prefer `dry<2` for fix-verify loops.
- **Set `budget_usd` with loop headroom.** A 3-iteration loop with 2 body nodes × 1 USD/dispatch × 3 iterations = 6 USD minimum. With retries: budget ≥ 12–15 USD. Budget exhaustion mid-loop records partial work and exits cleanly.
- **`gate` escalation tiers: list stronger models/capabilities first.** Tier 0 is applied on `auto` trigger or `allow` resolve. Typical: `[{"model": "claude-opus-4-20250514", "skeptic_count": 3}]` — a more capable model with more skeptics for the escalated retry.

## Provenance & materialized outputs

- Every event carries a derived `evidence_for` reverse index (event → decisions); agents cannot forge it. `axiom journal` shows the bidirectional graph; `ledger.derive_reverse_index(spec)` rebuilds it from forward refs.
- On a successful agent dispatch, files matching `write_areas` (relative to the run dir / worker cwd) are sha256-pinned into `run_dir/artifacts/` and recorded in an `artifact_write` event's `artifact_refs`. Read-only scouts (empty `write_areas`) produce none.
- `axiom run`/`resume` materialize `verdict.json`, `checkpoints.jsonl` (append-per-run), and `packet.md` (human-readable contract summary) into the run dir.
- `decision_trace` iron rules are enforced at `validate`: `decompose`/`replan` require non-empty `alternatives_rejected`; `replan` requires `replan_reason`; `evidence_refs` must use `event:`/`artifact:`/`decision:`/`checkpoint:` prefixes; assumption ids must be unique.

## Wiki: sediment run experience for cross-run retrieval

A run dies when it finishes. Spec-design experience — which shape worked, which cognitive signature stagnated, which replan was denied and why — lives only in the caller's head and is lost. The wiki (`axiom/wiki.py`, store at `<wiki-dir>/wiki.jsonl`) is the append-only, hash-chained cross-run retrieval layer. `plan --wiki-suggest` / `axiom wiki search` retrieves "how did a similar intent fare last time" BEFORE you design the next spec — format/shape arrive before output, not after. This is the friction-elimination-at-source thesis: the agent gets the format it needs before producing, not after.

**Three asymmetric layers — decide which one a fact belongs in BEFORE you sediment:**

| Layer | Carrier | Semantics | What goes in |
|---|---|---|---|
| **Raw** | ledger `events.jsonl` | append-only, hash-chained, never rolls back | every dispatch / verify_verdict / agent_verdict / gate — automatic, nothing to do |
| **Wiki** | `wiki.jsonl` | append-only, hash-chained, never rolls back | DISTILLED cross-run lessons the ledger alone doesn't surface: which shape worked, which signature stagnated, which replan was denied, proven format contracts |
| **Skills** | spec.v{n} version chain | rollable back | the spec itself, versioned |

The wiki's value is NOT re-deriving truth the ledger holds — `extract_entry` reuses `derive_verdict` + `derive_debug_envelope` + `derive_spec_shape`. It is the **distilled + retrievable-before-design** form. Do not dump raw events; sediment the lesson.

**When to sediment:** after any real run that taught a shape, a friction, or a denied path finishes (VERIFIED or not), before closing the task. A run that taught nothing needs no entry.

**What is worth wiki-ing (judgment, not reflex):**
- ✅ **spec-shape lessons** — "agent×3|verify×2|synthesize worked for competitive-research intents" / "multi-agent specs sharing one claim_id prefix collide into false CONFLICTED — partition namespaces". These help design the NEXT spec.
- ✅ **cognitive signatures of stagnation/exhaustion** — `patterns` distilled automatically from `derive_debug_envelope` failures (node-level, NOT claim-level). A VERIFIED run usually has `patterns=[]` — correct, not a gap; don't hand-write patterns.
- ✅ **denied replans / failed gates / verify failures** — appended as `impact` amendments (`wiki impact <entry_id> --kind replan_denied|gate_denied|verify_failed|format_drift --reason "..."`), never rewriting the parent entry.
- ❌ **host/infra bugs** (e.g. a retry-classifier mis-routing 429s) — these belong in code + the host_adapter's issue surface, NOT wiki. `plan --wiki-suggest` cannot help a future spec avoid a host bug; sedimenting it pollutes the experience store with non-spec knowledge. Fix the code.
- ❌ **invocation patterns that aren't spec-shape** (e.g. "use Harness.run() not `axiom run`") — borderline; prefer the code/docs home unless they change how you design specs.

**format_contract admittance + caveat (hard):** `wiki extract --contract` only appends a format_contract entry IF `derive_verdict == VERIFIED` (the proven structure: node skeleton + `output_schema` required fields + binding pattern, so the next spec copies the exact format). BUT VERIFIED at the requirement level can mask a known claim-level structural flaw — advisory requirements don't cap the verdict, and CONFLICTED claims don't auto-BLOCK. **Do not sediment a format_contract from a run you know has a structural defect without an `impact` amendment** (`wiki impact <contract_entry_id> --kind format_drift --reason "<the flaw>"`) flagging it — otherwise `plan --wiki-suggest` returns a broken template and the bug propagates. If the flaw is fixable in the spec, fix + re-run VERIFIED, then sediment the clean contract.

> **Why (P-021):** the skeleton used to keep only `type==agent` nodes, so a source+verify VERIFIED run's contract dropped the verify node: `plan --wiki-suggest` recommended copying a "proven structure" with NO verification stage — the exact shape that produces self-certified false VERIFIEDs.

**Symbiosis loop: adoption declaration + outcome attribution (the symbiosis loop's recording surface).** After `plan --wiki-suggest` retrieves a contract/experience, **the adopt judgment stays with the orchestrator** (which entry fits the current intent is the orchestrator's cognition; axiom does no similarity heuristic — a heuristic false-positive would treat "resembles" as "adopt", polluting attribution data); axiom provides the recording surface: a spec declares `"adopted_from": ["<entry_id or unique prefix>"]` (durable, versioned along the spec.v chain; child versions do NOT inherit, must re-declare) or `run/resume/continue --adopted-from <id>` (merge into the in-memory spec, the spec file stays immutable). On run seal (`--auto-wiki`), `_maybe_sediment` appends an `impact(kind=adoption_outcome)` (incl. run_dir/svid/verdict) for each adopted entry, and `search()`'s impact aggregation lets the next retrieval see "this contract was adopted N times, with what verdict". Unknown entry_id is non-fatal (stderr warning, doesn't block sediment).

> **Why (P-014):** the symbiosis loop used to break at attribution: an adopted entry never knew it was adopted or what verdict it produced, the retrieval surface had no outcome feedback, and the orchestrator's next adopt had no historical basis; plus `plan --wiki-suggest` only printed a truncated entry_id while `add_impact` only accepted full ids, so the orchestrator couldn't declare even if it wanted to. Now `add_impact` supports unique-prefix resolution (exact first, ambiguous reports candidates), the adoption edge is queryable both ways (adopted entry's impact + the run's own experience entry's `adopted_from` field). **An effect-decay/retirement mechanism (a contract that consecutively PARTIALs should be down-weighted) is an intentional open question — not enough volume yet; designing it now = designing governance for an empty store.**

> **Why (P-012):** the sediment *loop* had three breaks independent of the fact-layer fixes (P-010/P-011).

> **Why (P-019 + P-020):** two more sediment/resume breaks closed after review.

> **Why (P-029 + P-030):** review hardened P-019's context on two edges: the persisted `wiki_dir` was stored RELATIVE (`.axiom/wiki`) and re-interpreted against the RESUMER's cwd — cross-directory resume sedimented into B while the run's own entries lived in A (paths that must survive across invocations must be persisted absolute); and `--adopted-from` was never persisted, so a resume lost the CLI-declared adoption — the adopted contract received the BLOCKED outcome but never the resumed VERIFIED (attribution broken on exactly the path P-014 built; in-spec `adopted_from` was unaffected — it lives in the file).

**Commands:**
```
# at run time: --auto-wiki distills this run into the wiki right after seal
# (non-fatal: a wiki write failure only logs). Use this so the next plan can
# retrieve this run — no separate extract step needed.
axiom run spec.json --runtime ... --auto-wiki

# manual extract (only for an already-existing run-dir, or to add --contract / --learned):
axiom wiki extract <spec.json> --run-dir <ledger dir> --wiki-dir <wiki dir> \
  --contract --tag <domain> --learned "<spec-shape lesson, not a code bug>"
axiom wiki verify --wiki-dir <wiki dir>          # chain integrity
axiom wiki list --wiki-dir <wiki dir>            # all experience entries (+ impact counts)
axiom wiki show <entry_id> --wiki-dir <wiki dir> # one entry full JSON (patterns/impact)
axiom wiki search "<intent keywords>" --wiki-dir <wiki dir>  # BEFORE designing next spec
```

`run --auto-wiki` appends an experience entry only (no format_contract — use `wiki extract --contract` for that). `--learned` is free text — put only spec-shape lessons there, never code bugs. `patterns` come from the ledger automatically; do not hand-write them. Add `--project-root <path>` to `run` when the orchestrator runs outside the worker's target tree (worker cwd + write_areas glob root; default = cwd).

## Skill self-evolution: the three-layer discipline (portable, structured)

The `## Wiki` section above is the three-layer sediment system for **spec-RUN experience** (Raw = `events.jsonl`, Wiki = `wiki.jsonl`, Skills = `spec.v{n}`). That covers runs. **axiom's own changes** — a feature added, a friction fixed, a behavior changed — have a separate three layers, and the discipline to maintain them travels WITH axiom (this `SKILL.md` + `skill_patterns.jsonl`), so it holds wherever axiom is installed. It is **never** stored in machine-local memory.

**Sediment is for the agent, not humans.** All three layers are STRUCTURED (fielded, machine-parseable, agent-consumable) — never prose markdown. The agent retrieves a pattern by `pattern_id` and reads typed fields (`friction`/`fix`/`commit_sha`/`rejected`/`open_questions`), not a document it must re-read. Human readability is explicitly NOT a goal; convenience-at-retrieval is.

| Layer | Carrier | Semantics |
|---|---|---|
| **Raw** | git history (source repo) | commits + diffs; the immutable trajectory of skill changes |
| **Wiki** | `skill_patterns.jsonl` (skill root) | one STRUCTURED entry per pattern (`entry_type=skill_pattern`: `pattern_id`/`feature`/`friction`/`fix`/`commit_sha`/`rejected[]`/`open_questions[]`/`sub_lessons[]`/`status`); hash-chained, append-only, never rolls back |
| **Skills** | `SKILL.md` feature sections | each carries a `Why this exists: P-NNN` link back to its pattern (`axiom wiki pattern show P-NNN`) — the traceability layer |

**Autonomous maintenance — the rule, not a suggestion.** When you modify axiom itself — resolve a friction, add a feature, change a behavior — **before closing the task**:

1. Append a STRUCTURED skill_pattern: `axiom wiki pattern add --id P-NNN --feature "..." --friction "..." --fix "..." --commit <sha> [--rejected "approach::reason"]... [--open-question "..."]... [--sub-lesson "..."]...`. This writes a typed entry to `skill_patterns.jsonl` (the portable store, resolved from the axiom install root). Never hand-write a markdown doc.
2. Add a `Why this exists: P-NNN` line to the feature's `SKILL.md` section (the traceability link — `axiom wiki pattern show P-NNN` retrieves it).
3. Git commit. The skill-impact layer = git history; rejected approaches are recorded as structured `rejected` entries (`{approach, reason}`) so they are not re-proposed blindly.

A friction that taught nothing needs no entry (judgment, not reflex — same rule as the run-experience wiki). This is what prevents "losing the context of why a design decision was made when modifying the skill": the *why* lives in `skill_patterns.jsonl`, which never rolls back, even when `SKILL.md` (the Skills layer) does. The public distribution ships with an **empty store** — the sediment mechanism is the value, not the personal history; re-sediment the lessons as you adapt axiom to your own runs. The conceptual seed is the `### Cross-session resume` section above (the /clear → multi-minute thinking friction) — re-sediment it as `P-001` on your first real change to axiom.

**Daydream — self-summarization trigger (structured output).** After closing a task that ran axiom, autonomously surface sediment gaps so the run-experience wiki stays current:
```
axiom wiki daydream --run-root .axiom --wiki-dir .axiom/wiki [--json]
```
Read-only. It surfaces TWO gaps against `wiki.jsonl`: (a) **contract gap** (highest priority) — a VERIFIED run with no `format_contract` entry, i.e. the proven structured-output structure (node skeleton + `output_schema` required + binding pattern) was left on the floor; (b) **experience gap** — no experience entry for the svid. Contract gaps sort first because structured output is the point of the wiki (`plan --wiki-suggest` returns the contract so format arrives BEFORE output, not after). `--json` emits a machine-consumable gap list for agent consumption / chaining. `--auto-wiki` at run time is the non-daydream path (sediments automatically on seal); daydream catches runs that finished without it, and flags VERIFIED runs missing their contract.

## v1 limitations (honest)

> **Why (P-005):** verify cost scales as `O(findings × skeptic_count / max_concurrent)` and can blow the budget with no warning; `lint_spec` now warns when a verify `target` points at a parallel/pipeline fan-out node (its output is a LIST of findings, so cost = N × skeptic_count) or `skeptic_count`>5, closing the static no-warning half.

- Concurrency is real (`ThreadPoolExecutor`, `concurrency`/`max_concurrent`). As of v1.4-S2 the `sc` skeptics **within a single claim** dispatch concurrently (`conc = min(skeptic_count, max_concurrent)`); the **per-claim `for f in findings` loop is still sequential** — a verify node targeting N findings runs N batches of `skeptic_count` concurrent dispatches. Verify cost scales as `O(findings × skeptic_count / max_concurrent)`: pointing verify at a large findings list with high `skeptic_count` can blow the budget with no warning. **Partial (P-005): `lint_spec` now warns when a verify `target` points at a parallel/pipeline fan-out node (its output is a LIST of findings, so cost = N × skeptic_count) or `skeptic_count`>5 — the static no-warning half is closed; the runtime findings-count half stays a harness/budget concern.** Parallel/pipeline bodies ARE concurrent.
- Workers run via the **host adapter file protocol**: axiom writes `dispatch_req_{rid}.json` (prompt/schema/tools/model) into the run-dir; `host_adapter.py` (pi or cc preset) consumes it, drives the underlying CLI, and writes `dispatch_res_{rid}.json` (a `DispatchResult` envelope). The underlying CLI's `--json-schema` is not reliably enforced by the worker, so the harness owns the type boundary: G1 injects a hard JSON directive, G2 extracts JSON from prose/fences, G3 feeds schema errors back on retry, G4 abstains on non-conforming skeptics. An unauthenticated/stale session returns `is_error=true`: "Not logged in"/"login"/"api key" → **unretriable → Gate**; "Unable to connect"/"ConnectionRefused"/"timeout" → operational (backoff retry); other → cognitive.
- `resume` replays unchanged successful nodes from the journal cache (zero dispatch, zero cost); only changed (new `spec_version_id`) or originally-failed nodes re-run. Verdict stays consistent with the first run. Per-node checkpoint restore of *failed* nodes is v2.

## Prerequisites (by backend)

- **pi backend**: `pi` on PATH; a provider key exported in your shell profile (the `host_adapter --pi` preset auto-sources it). Cost: when the GLM provider reports cost=0, `tokens_total` is the real-usage signal.
- **cc backend**: the cc-switch desktop app running (auto-detected via its launcher tmpfile); otherwise falls back to bare `claude -p`.

## Quick-start walkthrough (from task to VERIFIED)

A copy-paste shape for "make N edits, then verify them" — the editing-task pattern that reached VERIFIED end-to-end. Other agents: substitute your files/requirements and run it.

**Step 0 — Orient (bug-fix tasks only):** if the root cause is unknown, do NOT grep blindly — dispatch ONE read-only scout first. Empty `write_areas` (so no `artifact_write`), `allowed_tools:["Read","Glob","Grep"]`, `output_schema` with `root_cause` + `affected_files` + `evidence`. Its `validated_output` feeds Step 1 Frame via `{{n_orient.root_cause}}`. This replaces ~5 ad-hoc search rounds with one bounded dispatch.
```json
"n_orient":{"type":"agent","id":"n_orient","prompt":"Locate the root cause of <symptom>. Read the relevant files, grep for <terms>. Return {root_cause, affected_files, evidence}.","dispatch":"host","output_schema":{"type":"object","properties":{"root_cause":{"type":"string"},"affected_files":{"type":"array","items":{"type":"string"}},"evidence":{"type":"string"}},"required":["root_cause","affected_files"]},"allowed_tools":["Read","Glob","Grep"],"write_areas":[],"acceptance":["root_cause identified"],"failure_policy":{"max_retries":2,"retry_guard":"requires_new_evidence","on_exhausted":"degrade"}}
```

**Step 1 — Frame:** Goal / Requirements (obligations, not paths) / Boundaries / Success Evidence. One sentence each. For **UI/role tasks** ("admin interface", "user dashboard"), frame the COMPLETE role experience — login → landing → actions + what the role must NOT see (routing/menu isolation) — not just a component refactor. The requirement is the role's end-to-end experience; do NOT let `boundaries` exclude routing (`App.tsx`/router) or menu (`Layout.tsx`) files when the requirement is role-level experience isolation — that cuts the necessary work and ships a half-experience (one admin task narrowed "admin interface" to "AdminPage component" and excluded `App.tsx`/`Layout.tsx`, so admin still saw the unguarded pages).

**Step 2 — Design one agent per edit target (non-overlapping `write_areas`) + ONE verifier that owns the verdict.** For code tasks, the recommended shape is "script evidence + reviewer verdict" machine-acceptance (R9-3): a script node runs the real tests and declares `evidence_scope` (the runtime auto-records command/exit-code/output/file-fingerprint, no hand-written receipt needed), and a reviewer declares `evidence_from` to receive the evidence and judge — after a code change the old evidence auto-invalidates, forcing a re-review. The example below contains both forms (machine-evidence preferred; pure agent-verdict also works):

```json
{
  "spec_version_id":"spec.v1","parent_spec_id":null,"revision":1,
  "intent":"...","requirements":[{"id":"R1","text":"...","criticality":"required"}],
  "boundaries":["only touch these files"],
  "success_evidence":["S1:R1=node:n_verify"],
  "contract_drift":false,"contract_hash_value":"",
  "nodes":{
    "n1_edit":{"type":"agent","id":"n1_edit","prompt":"edit X to do Y","dispatch":"host",
      "output_schema":{"type":"object","properties":{"files_modified":{"type":"array","items":{"type":"string"}}},"required":["files_modified"]},
      "allowed_tools":["Read","Edit","Write","Glob"],"write_areas":["path/to/file"],"acceptance":["file modified"],"failure_policy":{"max_retries":2,"retry_guard":"requires_new_evidence","on_exhausted":"block"}},
    "n_test":{"type":"script","id":"n_test","script_path":"/abs/path/test_target.py","args":[],
      "output_schema":{"type":"object","required":["ok"]},
      "evidence_scope":["path/to/file"]},
    "n_verify":{"type":"agent","id":"n_verify","verdict_field":"verdict","evidence_from":"n_test",
      "prompt":"Review the machine evidence below + read the files. Return {verdict, issues}.",
      "output_schema":{"type":"object","properties":{"verdict":{"type":"string"},"issues":{"type":"array","items":{"type":"string"}}},"required":["verdict"]},
      "allowed_tools":["Read"],"write_areas":[],"acceptance":["verdict grounded in evidence"],"failure_policy":{"max_retries":2,"retry_guard":"requires_new_evidence","on_exhausted":"degrade"}}
  },
  "control_flow":{"type":"sequence","steps":[["parallel","n1_edit"],"n_test","n_verify"]},
  "decision_trace":[],"budget_usd":20.0,"max_concurrent":16,"max_agents":1000,"max_stagnation":2
}
```

The anti-self-audit rule is enforced by `validate` as a machine check (R9-4): a `verdict_field` node's `write_areas` must be empty (an implementer cannot self-audit and pass); `evidence_from` must point at a script node with `evidence_scope`. The checkpoint's `evidence_grade` (machine/none/claim) labels the VERIFIED's evidence tier. **From R10-2 the evidence declaration is a gate, not a label**: a VERIFIED that declared `evidence_from` but whose source node produced no pack (evidence_id=None) no longer projects through — verdict/checkpoint always PARTIAL, open_questions records `agent_verdict_evidence_missing`; code-change-induced evidence staleness works the same (`agent_verdict_evidence_stale`). R10-1: `verdict`/`state`/`debug` share the same freshness check as `checkpoint`; the two entries can no longer disagree (one PARTIAL, one VERIFIED).

**Runtime-verifier variant** — swap `n_verify` for this when VERIFIED must mean "the bug is gone at runtime", not just "the files look right":
```json
"n_runtime_verify":{"type":"agent","id":"n_runtime_verify","verdict_field":"verdict",
  "prompt":"First load the project env the service needs (`set -a; . backend/.env; set +a` + export any shell-env secret like JWT_SECRET — the verify subprocess does NOT inherit them). Restart the service (kill old, start fresh with that env). Then curl the affected endpoints and assert on the live output (e.g. python -c counting the relevant records). Scope TS checks to only the changed files (not a full build — pre-existing errors in untouched files would mask yours). Return {verdict, issues}; verdict is VERIFIED only if every assertion passes.",
  "output_schema":{"type":"object","properties":{"verdict":{"type":"string"},"issues":{"type":"array","items":{"type":"string"}}},"required":["verdict"]},
  "allowed_tools":["Read","Bash"],"write_areas":[],"acceptance":["service restarted","endpoints asserted"],"failure_policy":{"max_retries":2,"retry_guard":"requires_new_evidence","on_exhausted":"degrade"}}
```
`Bash` is NOT gated (only `risk:high` or `secrets`/`delete` `write_areas` open a Gate), so a `risk:low` runtime verifier runs without human approval. Use `risk:high` only if the restart is genuinely destructive in your env.

**Runtime-verifier env pitfalls (must read):** the verify worker is a bare subprocess; it does NOT inherit the service's `.env` or shell env — in the prompt, `set -a; . backend/.env; set +a` before running any import. Scope TS checks to the changed files (a full build is masked by pre-existing errors). Endpoint/UI tasks MUST runtime-verify (a static verify only proves "files look right"); `axiom validate` lints a warning on a static verify + runtime write_areas combination.

**Step 3 — validate → run (foreground, synchronous) → read checkpoint:**
```
$LF validate .axiom/spec.json
$LF run .axiom/spec.json --run-dir .axiom/run
# blocks until all nodes dispatch, then prints {"output":..., "checkpoint":{verdict,...}}.
# `output` = the LAST node's validated_output (auxiliary); `checkpoint` is the ONLY re-entry object — read verdict/blocked_items/budget_exhausted there.
# LONG workflows (>~2 min) can't be foregrounded (tool-call timeout) — background the run
# (nohup ... &) and poll `axiom checkpoint ... --run-dir .axiom/run` ~every 60s until
# `verdict.json` exists / the run process exits; that final checkpoint is the same object.
```

**Step 4 — Decide on the checkpoint:**
- **VERIFIED** → deliver.
- **PARTIAL** → the verifier returned FAILED or abstained. `axiom journal --run-dir .axiom/run` to see why; fix the gap; emit `spec.v2` (`parent_spec_id":"spec.v1"`, new `spec_version_id`); re-run. Never edit the spec in place.
- **BLOCKED** → a Gate is open. `axiom gate list`, resolve, continue.
- Never loop node-by-node. Replan only at the workflow boundary.

**Why this shape works:**
- Edit agents have NO `verdict_field` — they just do the work and return `{files_modified}`. They are not bound to requirements.
- The verifier has `verdict_field` + returns `{verdict:"VERIFIED"}` → emits `agent_verdict` → `success_evidence` `node:n_verify` satisfies all R's. One verifier can own the whole task's verdict (simplest); split into per-requirement verifiers only if you need per-R evidence.
- `parallel` step has the keyword; `write_areas` non-overlap; `budget_usd` has retry headroom; `run` is foreground.
