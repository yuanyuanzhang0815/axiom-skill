# Evidence Schema — four authorities + evidence fields + independence judgment

Every evidence item must carry the following fields. The receipt is the input to a verdict, it does **not self-announce completion**.

## Four-authority provenance

| Field | Meaning | Who fills it |
|---|---|---|
| `defined_by` | who declared the claim this evidence is meant to support | the claim's proposer |
| `produced_by` | who produced the evidence artifact (wrote the test / wrote the script / recorded the trace) | the artifact author |
| `executed_by` | who ran it (which run / which session) | the executor |
| `adjudicated_by` | who judged the result pass/fail | the adjudicator |

## Independence judgment (two layers; Oracle is primary)

Evidence independence is **not** "the more distinct the participating Agents, the more independent." Four independent Agents that share the same blind spot (none of them realized that deleting a workspace affects the ADE selector) all miss it together. **Spawning more Agents != more independent** — this specifically prevents degenerating into a heavyweight "add yet another Reviewer for independence" Quality Loop.

### Oracle Independence (primary) — where does this evidence's "right answer" come from?

Criterion: **whether the oracle is independent of this implementation's cognitive assumptions.**

| Evidence | Independence | Reason |
|---|---|---|
| AI-freshly-written unit test | low | the oracle comes from this implementation's same cognitive assumptions |
| repo-existing regression | high | the oracle comes from a historical real failure, independent of this implementation |
| TypeScript compiler / type check | very high | the oracle is the language rules, unrelated to implementation cognition |
| OpenAPI / JSON schema | very high | the oracle is the contract definition |
| browser against an existing-behavior baseline | high | the oracle is real behavior recorded before the change |
| production trace / runtime | very high | the oracle is a runtime fact |
| another LLM reviewer | medium-low | the oracle is still LLM cognition; blind spots may overlap |

### Provenance Independence (auxiliary, auditable)

The four-authority provenance is still recorded, used to audit "who defined / produced / ran / judged", but it only determines **auditability**, not strength on its own.

- The same agent that both defined and produced and executed and adjudicated, with the oracle also being itself (a freshly-written test) -> weak evidence, even if it runs green it is not independent evidence.
- But if executed_by is the same coding agent, yet the oracle is a compiler / repo-existing-tests -> still strong. **Head-count overlap != weak evidence; look at the oracle.**

> In one line: **look at the oracle, not the head-count.**

## Complete evidence fields

```yaml
- claim: "after deleting a workspace the sidebar refreshes correctly"   # which claim this evidence supports
  type: compiler | static-scan | unit | integration | e2e | historical | human
  addresses: [shared-state]                          # which surfaces this evidence covers (required, see below)
  defined_by: orient-scout-session
  produced_by: repo-test-suite                       # who produced the artifact
  executed_by: harness                               # who ran it
  adjudicated_by: rule                               # who judged: rule | llm | human
  scope: affected-files                              # evidence coverage scope
  freshness: 2026-08-31T12:00:00Z                    # when produced / freshness
  result: pass | fail | flaky | unavailable
  limitations: "only covers static references, not websocket linkage"  # known not-covered
```

## per-surface evidence accounting (invariant #10: no borrowing evidence across surfaces)

Every evidence item **must** explicitly declare in `addresses` which surfaces it covers (using the surface names from classify_change, e.g. `runtime` / `frontend_interactive` / `public-contract` / `shared-state` / `secret-auth` / `schema-migration` / `frontend_presentational` / `type-declaration` / `config` / `docs`).

At adjudication **each touched surface independently satisfies its own minimum Evidence Contract**:

| surface | required role |
|---|---|
| `frontend_interactive` | reference_scan, behavior |
| `frontend_presentational` | static_check |
| `type-declaration` | static_check |
| `public-contract` | reference_scan, behavior, integration |
| `runtime` | reference_scan, behavior |
| `shared-state` | reference_scan, behavior, integration, regression |
| `schema-migration` | deep_evidence, regression |
| `secret-auth` | deep_evidence, auth_gate |
| `config` | static_check |
| `docs` / `test` | (do not independently raise the floor, do not participate in accounting) |

**surface and risk are decoupled (a calibration point)**: surface decides "what evidence is meaningful", risk decides
"how strong is needed", the two are not fully bound. `type-declaration` (.d.ts ambient declaration) has no runtime
behavior to verify — its meaningful evidence is tsc/contract-diff/reference-scan, **not** a runtime behavior oracle.
A public API `.d.ts` is high-risk V2, but the evidence is still static_check, not behavior. High-impact != necessarily
needs a runtime behavior oracle.

- One evidence item only fills the slot of the surface it declared in `addresses`. **No declared `addresses` -> fills no
  surface slot + records a violation** (forcing explicit annotation, killing the old global pool: a backend e2e silently
  satisfying the frontend's behavior slot).
- Any touched surface with a missing slot -> overall PARTIAL; `secret-auth` missing `auth_gate` -> BLOCKED.
- Does not auto-start browser E2E: missing evidence -> PARTIAL + tells the Agent "lacks <surface>:<role> oracle"; whether
  to supplement is the Agent/human's call (adaptive assurance — spend the cost to supplement only if VERIFIED is wanted).

**A real relay run case**: a backend HTTP e2e (6 assertions, addresses:["runtime"]) was strong, but it never touched the
frontend component -> did not fill the `frontend_interactive` behavior slot -> frontend_interactive lacked behavior ->
PARTIAL. The old global-pool rule would OR the same e2e's behavior role into the shared set, masking the frontend gap ->
VERIFIED (false).

**A calibration case (surface/risk decoupling)**: `vite-env.d.ts` is a TypeScript ambient declaration; the old
EXT_DEFAULT `.ts`->`runtime` demanded `behavior` — structurally unsatisfiable for a declaration file (tsc only fills
static_check) -> false-escalation (the agent's real work was blocked at PARTIAL). Fix: `.d.ts`->`type-declaration`
surface (contract = static_check); path signals only raise the floor (risk) without changing surface. Replay proved the
same change upgraded to VERIFIED. This is the calibration phase's first failure -> policy adjustment -> replay proof.

## Evidence type vs. the claim it primarily supports

Evidence is classified by "what claim it primarily supports"; **type does not directly equal strength**. Strength is
jointly determined by directness + scope-fit + freshness + independence + reproducibility.

| type | primarily supports |
|---|---|
| compiler/type/schema | structure and interface consistency |
| static/reference scan | visible code dependencies |
| unit/property test | local logic or invariants |
| integration/runtime trace | real component interaction |
| e2e/behavior baseline | user-visible business loop |
| historical regression | a known failure did not recur |
| human acceptance | business intent or residual risk accepted |

## Verdict vs. evidence strength binding

| Verdict | required evidence condition |
|---|---|
| `VERIFIED` | every required evidence exists + fresh + scope-fit + independence matches the risk level |
| `PARTIAL` | some required evidence satisfied, remaining items explicitly listed |
| `UNVERIFIED` | implementation may be complete, but evidence is insufficient (includes flaky/tool-unavailable downgrade) |
| `BLOCKED` | external condition / authorization / gate unmet, cannot continue converging |

**A test command returning success != VERIFIED.** Verdict strength may not exceed Evidence strength.

## Handling flaky / conflicting / missing (no mechanically re-running to green)

- **flaky**: flag unstable, diagnose / replace / downgrade verdict, do not re-run to green.
- **evidence conflict**: mark the corresponding claim conflicted, no VERIFIED.
- **tool unavailable**: record the absence, downgrade to UNVERIFIED by risk or trigger BLOCKED.
- **evidence stale**: freshness does not meet the risk level's requirement -> treat as missing.
