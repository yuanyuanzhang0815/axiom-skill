---
name: change-assurance
description: Adjudicate whether a code change can be announced complete — risk grading, evidence receipts, non-binary verdict. Tests are only one kind of evidence; this skill does not generate tests.
---

# change-assurance

Given a code change (intent + write_areas + diff), produce a **risk grade, a non-binary verdict backed by evidence strength**, answering "can this change be announced complete?"

This is not a testing skill. Tests are only one kind of evidence. This skill does **adjudication**, not **test generation**.

> **This is an OPTIONAL companion skill.** For code-change workflows, the recommended path is axiom's **machine-evidence** shape (a `script` node declares `evidence_scope` + a reviewer declares `evidence_from`; after a code change the old evidence auto-invalidates, forcing a re-review — see the `## Workflow IR node types` → `script` section of the parent `SKILL.md`). `change-assurance` is retained for workflows that want a standalone, risk-graded, per-surface evidence adjudicator wired through the `assurance_hook` subprocess contract.

## Position in the stack

```
the orchestrator (axiom harness)   — when to verify, how to handle failure
change-assurance (this skill)      — what evidence suffices, what the verdict is
evidence producers (rg/LSP/tests/E2E/mutation) — what the facts are
```

## The one rule that matters (authority boundary)

The LLM may propose impact, risk, and evidence plans, but it **does not own the authority to emit VERIFIED**.
`scripts/assurance.py`'s `derive_assurance_verdict()` is the **single verdict exit** — deterministic, the agent cannot bypass it: the oracle is derived by this function from `evidence.type` (ignoring any LLM-self-filled value), the floor is machine-set, and high-impact unresolved items hard-block VERIFIED.
An agent "writing its own test, running it, and announcing completion" is structurally impossible.

> The LLM explains and proposes, submits receipts; the harness sets the floor, derives the oracle, and authorizes.

Two hard constraints (different layers — don't conflate them):
- **Verdict Integrity** (change-assurance itself) — guaranteed by `derive_assurance_verdict()` to "never emit a false VERIFIED."
- **Workflow Enforcement** (the orchestrator host) — guarantees "my PARTIAL never reaches DONE." Standalone use only has the first layer.

## The iron rules

1. **The graph may be cached, but the cache is not Truth.** A persistent lightweight symbol index (LSP / CodeGraph cache) is only a retrieval accelerator, not a source of fact. Truth priority: `runtime > current code scan > cached graph`. A stale cached graph gives false certainty; verify against current code.
2. **Unresolved items are computed, not imagined by the agent.** The project maintains a **tri-state** capability manifest (present / absent_confirmed / unknown); the system derives "the project has an event_bus but event_bus_scan was not run this time" automatically, instead of letting the LLM free-associate "what else might be invisible." **Only `absent_confirmed` can suppress a capability's unresolved** (not-found ≠ does-not-exist); `unknown` expresses epistemic uncertainty, not "there is a bug." Discovering capabilities (`detect_capabilities.py`) and judging unresolved (`unresolved_from_manifest.py`) are two separated pure functions, not mixed into one LLM prompt. See `references/capability-manifest.md`.
3. **Cross-agent assurance does not see another worktree's uncommitted diff.** Worktree isolation (`git worktree add --detach` + cleanup that never merges back) is one of the orchestrator's deterministic guarantees that change-assurance relies on: one agent session does not implicitly observe another worktree's uncommitted changes. Therefore evidence must be **independently observable** — from committed artifacts, explicit receipts, or harness-visible output paths, never from "a prior agent's worktree-internal uncommitted output." If a use case truly needs cross-agent visibility into uncommitted diff, design an explicit artifact handoff (receipt-ize / commit / drop into a harness-visible path); **do not loosen isolation.** (See invariant #8 in `references/evidence-schema.md`.)
4. **Assurance is on by default for runtime/code changes; evidence is reconciled per-surface, and borrowing evidence across surfaces is forbidden.** Two things, both exposed by a real relay run:
   - **On by default (invariant #9):** when any spec agent's `write_areas` touch runtime code (`ir.py:_touches_code`, coarse-grained) → the completion node bound to `success_evidence` **must** declare `assurance_hook`. It does not look at whether that node itself has `write_areas` (blocks the impl/verify split: an impl node has `write_areas` but no `verdict_field`, a verdict node has `verdict_field` but no `write_areas` — if neither declares the hook, the machine chain is bypassed → the LLM self-reports VERIFIED and is directly trusted). `assurance_opt_out` (with a reason) can exempt, but lint discloses the bypass.
   - **Per-surface reconciliation (invariant #10):** adjudicate collects `surfaces_touched` from classify and passes it to `derive_assurance_verdict`; each touched surface independently satisfies its own `SURFACE_REQUIRED_ROLES`. Each evidence item only fills the slot of the surface it **explicitly declared** in `addresses`; no `addresses` → fills no slot + violation. **A strong backend e2e (addresses:["runtime"]) must NOT fill the frontend_interactive behavior slot** — in a real relay run: backend HTTP e2e with 6 assertions was strong, but it never touched the frontend component → frontend_interactive lacked behavior → PARTIAL (the old global pool would OR the e2e's behavior into the shared set, masking the frontend gap → false VERIFIED). It does not auto-start browser E2E: missing evidence → PARTIAL + "lacks <surface>:<role> oracle"; whether to supplement is the agent/human's call (adaptive assurance — spend the cost only if VERIFIED is wanted). See the per-surface section of `references/evidence-schema.md`.
   - **Surface and risk are decoupled (invariant #11, a calibration point):** surface decides "what evidence is meaningful," risk decides "how strong is needed," and the two are not fully bound. `.d.ts`/`.d.cts`/`.d.mts` (TypeScript ambient declarations) land on the `type-declaration` surface (contract = `static_check`, no runtime behavior to verify); path signals only raise `floor` (risk), they do not change surface (`classify_change.floor_for`). High-impact ≠ necessarily needs a runtime behavior oracle — a public-API `.d.ts` is high-risk V2, but the evidence is still tsc/contract-diff. Calibration: `vite-env.d.ts` was formerly judged `.ts`→`runtime` demanding `behavior`, structurally unsatisfiable for a declaration file → false-escalation; after the fix, replay reached VERIFIED. **Stop-development principle: only fix misclassifications that actually fire; don't proactively hunt other extensions; diff-aware classification upgrades only when a diff-class misjudgment truly appears.**

## v0.1 core objects (five; don't touch the Impact Graph)

1. **Change Manifest** — intent, acceptance contract, non-goals, write_areas.
2. **Risk Profile** — `classify_change.py` machine-judges a V0–V3 floor from write_areas; the LLM may raise, never lower (harness-enforced).
3. **Evidence Receipt** — each evidence item carries four-authority provenance + an `ai_authored` flag.
4. **Assurance Verdict** — `derive_assurance_verdict()`, the single exit: VERIFIED / PARTIAL / UNVERIFIED / BLOCKED. Strength ≤ evidence strength.
5. **Orchestrator seam** — the verdict feeds `verdict_field` via seam (b) → `derive_verdict` (orchestrator Workflow Enforcement).

Impact Surface (changed symbol → LSP refs → 1–2 hops, tagged `impact_confidence = bounded`, not claiming completeness) is **v0.2**; v0.1 leaves it as an empty shell.

## The single verdict exit — `derive_assurance_verdict()`

`scripts/assurance.py`. Deterministic; the agent cannot bypass it. Core rules:

- **A risk floor:** `declared_risk < risk_floor` → raise back to floor + record violation; may not downgrade.
- **B-min Oracle:** the oracle tier is **derived by this function from `evidence.type + ai_authored`**, ignoring any LLM-self-filled oracle field. compiler/schema/integration/e2e/historical/runtime/human = strong; AI-freshly-written unit = weak; llm-review = weak. AI-freshly-written evidence self-tagged `oracle=independent` → ignored + flagged as a violation.
- **Required roles:** each risk level has required evidence roles (V2 = impact_scan + regression + integration; V3 = deep_evidence + auth_gate). Weak evidence does not fill a required slot. Missing → PARTIAL/UNVERIFIED.
- **C unresolved:** a high-impact unresolved item exists → VERIFIED forbidden → PARTIAL. A missing capability_manifest → record a violation (must not default to "no impact").
- **D Verdict Integrity:** this function is the exit; the LLM does not directly produce a verdict. flaky → UNVERIFIED (may not re-run-to-green). V3 missing human/auth_gate → BLOCKED.

Three demos: `python3 scripts/assurance.py --demo` (an honest walkthrough → PARTIAL, a self-graduation attempt → UNVERIFIED, a downgrade attempt → PARTIAL).

## Workflow

1. **Classify the change** (machine): `scripts/classify_change.py` reads write_areas, outputs touched-surface types (runtime / test / docs / schema / shared-state / secret).
2. **Set risk** (machine floor + LLM proposal): the machine sets the floor (secret/permission → V3, public contract/shared state → V2, …); the LLM may raise, never lower; high-impact unresolved → must upgrade.
3. **Required evidence** (policy): risk level → minimum evidence set (see `references/risk-matrix.md`).
4. **Gather evidence** (evidence producers): run rg/LSP/tests/E2E/mutation; each item tagged with four-authority labels.
5. **Adjudicate** (harness gate): only when every required evidence item exists, is fresh, scope-fit, and its independence matches the risk level, is VERIFIED allowed. Missing → PARTIAL/UNVERIFIED; externally blocked → BLOCKED.

## Four-authority provenance (every evidence item must have)

| Field | Meaning |
|---|---|
| `defined_by` | who declared the claim this evidence is meant to support |
| `produced_by` | who produced the evidence artifact |
| `executed_by` | who ran it |
| `adjudicated_by` | who judged the result |

**The four authorities only determine auditability, not strength on their own.** Independence is mainly about the **Oracle** — whether this evidence's "right answer" is independent of this implementation's cognitive assumptions: compiler/schema/historical-regression/runtime-trace = strong; AI-freshly-written test / another LLM reviewer = weak. **Spawning more agents ≠ more independent** — four agents sharing the same blind spot all miss it together; this specifically prevents degenerating into a heavyweight Reviewer Loop. See `references/evidence-schema.md`.

## Risk floor table

| Level | Applies to | Minimum evidence |
|---|---|---|
| V0 Quick | local, reversible, no public contract, impact clear | targeted static/build/visual |
| V1 Standard | ordinary function, impact enumerable | reference scan + affected tests + targeted behavior |
| V2 Thorough | public contract / shared state / cross-module | expanded impact + regression/integration + core E2E |
| V3 Critical | permission / data migration / payment / irreversible | deep evidence + rollback plan + authorization gate |

Hard rules: sensitive domain ≥ V3; public contract or shared state ≥ V2; high-impact unresolved → upgrade; risk unjudgable → may not be V0; only local, reversible, clear-impact may be V0.

## Verdict

| Verdict | Meaning |
|---|---|
| `VERIFIED` | the acceptance contract is supported by scope-fit, fresh, independence-sufficient evidence |
| `PARTIAL` | some requirements satisfied; remaining items + impact listed explicitly |
| `UNVERIFIED` | implementation may be complete, but evidence is insufficient to support a completion claim |
| `BLOCKED` | a necessary external condition / authorization / gate is unmet; cannot continue converging |

A test command returning success ≠ `VERIFIED`. Verdict strength may not exceed evidence strength.

## Orchestrator seam contract — seam (b)

**Does NOT connect seam (a)** (the verify node's skeptic refutation chain) — that is claim→survived/refuted semantics; forcing it in would turn change-assurance into "a more complex skeptic," regressing to a Reviewer Loop. Leave skeptics as-is; a V3 skeptic serves as additional evidence.

**Connects seam (b) (landed in an earlier axiom version):** an agent node declares `assurance_hook` → the harness, in `_emit_agent_verdict`, uses `adjudicate.py` to recompute the verdict, **overriding the LLM's `verdict_field` transcription** → emits an `agent_verdict` event → `derive_verdict` (the single exit) consumes it. Missing receipt / script failure → safe-default UNVERIFIED, never a false VERIFIED. An LLM-self-reported `assurance_verdict` value is an audit field, not part of the adjudication.

The harness locates this skill's scripts via the `CHANGE_ASSURANCE_SKILL_DIR` environment variable (defaults to the `change-assurance/` directory sibling to the axiom install). See the `assurance_hook` field contract in `references/evidence-schema.md`.

```text
classify_change → risk_floor
   ↓
LLM/tools submit Evidence Receipt (facts + explanation)
   ↓
derive_assurance_verdict()  ← change-assurance single exit, deterministic
   ↓ {status, risk, evidence, blockers, unresolved}
orchestrator verdict_field → agent_verdict → derive_verdict  ← orchestrator Workflow Enforcement
   ↓
VERIFIED → advance to next state; otherwise does not reach DONE
```

Input: a change (intent + write_areas + diff). Output:
```yaml
verdict:
  status: VERIFIED | PARTIAL | UNVERIFIED | BLOCKED
  risk: V0 | V1 | V2 | V3
  evidence:
    - claim: ...
      type: compiler | static-scan | unit | integration | e2e | historical | human | llm-review
      ai_authored: bool
      produced_by / executed_by / adjudicated_by: ...
      result: pass | fail | flaky | unavailable
      # oracle field optional — ignored by derive_assurance_verdict, only audit
  blockers: [...]
  unresolved: [{id, impact}]
  violations: [...]
```

## Standalone use (without the orchestrator)

`python3 scripts/classify_change.py <write_areas>` → get the floor → collect a receipt → `python3 scripts/assurance.py --json receipt.json --floor V? --declared V?`.
Standalone only has Verdict Integrity (does not emit a false VERIFIED), not Workflow Enforcement (the host enforces PARTIAL).

## v0.1 scope / non-scope

**Does:** classify_change (risk floor), assurance.py (single verdict exit + receipt validator + oracle derivation), detect_capabilities (tri-state capability detection), unresolved_from_manifest (machine set-difference + anti-fabrication check), adjudicate (single entry: classify + manifest diff + derive_assurance_verdict), the orchestrator seam (b) contract (landed).
**Does not (v0.2+):** impact surface graph (rg/LSP 1–2 hops), mutation, complexity reset, calibration metrics.

Read the detailed rules on demand:
- `references/risk-matrix.md` — V0–V3 admission / minimum evidence / upgrade rules
- `references/evidence-schema.md` — four-authority provenance + Oracle independence + evidence fields + the invariant #8/#9/#10/#11 contracts
- `references/capability-manifest.md` — making unresolved computed rather than imagined
