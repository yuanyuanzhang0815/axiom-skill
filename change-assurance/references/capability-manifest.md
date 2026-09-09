# Capability Manifest — let unresolved be computed, not imagined by the Agent

Static analysis cannot see an event bus / websocket / dynamic dispatch. The question is not "should unresolved be recorded", but **who thinks to list `websocket_refresh` into unresolved** — if it is still the LLM "free-associating what else is invisible", it partially regresses to the original cognitive blind-spot problem.

Solution: the project maintains a three-state capability manifest; the system **computes** unresolved from it, rather than letting the Agent imagine it. **Discovering capabilities (detect) and judging unresolved (set-difference) are two separated pure functions, not mixed into one LLM prompt.**

## Three states (core)

```
present          — a signal was detected in some authoritative source
absent_confirmed — all sources defined for that capability were checked, and all clean
                   (not-found != does-not-exist, so you may only confirm absent when
                   "all sources checked and clean")
unknown          — otherwise (some source unavailable / cannot be determined)
```

**Only `absent_confirmed` can suppress a capability's unresolved generation.** `unknown` expresses epistemic uncertainty ("not inspected clearly"), **not** "there is a websocket bug" — never written as a concrete story like `websocket_refresh affected`.

Not-found != does-not-exist. Lightly writing `false` is dangerous; in the three states `unknown` specifically carries "I am not sure".

## Two scripts (separated responsibilities)

```
detect_capabilities.py <project>          -> capability-manifest.json (three states)
unresolved_from_manifest.py --manifest M --analysis A [--proposed P]
   -> { unresolved[]: machine set-difference, proposed_violations[]: LLM fabrication check }
```

- `detect_capabilities` answers: which known mechanisms does the project have? (deterministic sources: package.json / pyproject / docker-compose / code imports / openapi files / config)
- `unresolved_from_manifest` answers: among these mechanisms, which were not checked this time? And it checks whether the LLM-self-filled proposed unresolved is fabricating.

## capability -> required scan mapping

| capability | analysis scan required when present | missing -> unresolved |
|---|---|---|
| websocket | `websocket_scan` | websocket runtime linkage not inspected (coverage_gap) |
| event_bus | `event_bus_scan` | event bus subscription not inspected |
| background_jobs | `background_job_scan` | scheduled-task side effects not inspected |
| database | `db_schema_check` | db schema change not reconciled |
| openapi | `openapi_diff_check` | contract change not reconciled |
| shared_state | `shared_state_scan` | shared-state cross-component behavior not baseline-compared |

## Three states × analysis unresolved generation rules

| capability state | analysis scan | output |
|---|---|---|
| present | unscanned | coverage_gap unresolved (a real gap) |
| present | scanned | none (already covered) |
| absent_confirmed | — | none (the project has no such mechanism; **this is exactly what blocks fabricating websocket_refresh**) |
| unknown | — | capability_unknown unresolved (epistemic, not a bug story) |

A capability the manifest does not declare -> not processed (the manifest is the authority; undeclared means do not invent).

## LLM proposed unresolved validation (anti-fabrication)

The LLM may still fill proposed unresolved (explaining "which invisible things relate to this change"), but it **does not participate in adjudication**, only gets validated:

| proposed references a capability state | judgment |
|---|---|
| present | pass (a real gap, may keep) |
| absent_confirmed | **violation** — fabricating a mechanism the project lacks (a real-time app's websocket hallucination is this kind) |
| unknown | **violation** — telling uncertainty as a concrete bug, should downgrade to the epistemic `capability unknown` |
| not declared in the manifest | **violation** — the manifest is the authority; undeclared means you may not propose it |

manifest_violation -> receipt integrity violation -> blocks VERIFIED (downgrades to PARTIAL).

## Wired into adjudication

`adjudicate.py`: the receipt carries `capability_manifest` (produced by detect) + `analysis_performed` ->
`derive_unresolved(manifest, analysis)` produces the **machine unresolved** (authoritative, fed to `derive_assurance_verdict`);
`validate_proposed_unresolved(receipt.unresolved, manifest)` produces proposed_violations (wired into verdict.violations).
manifest missing -> does not default to "no runtime mechanisms", records a violation (may not be V0).

## 4 acceptance cases

1. **websocket=absent_confirmed** + LLM proposes `websocket_refresh` -> violation (fabricating).
2. **event_bus=present** + unscanned -> unresolved auto-appears (coverage_gap).
3. **event_bus=present** + scanned -> none.
4. **websocket=unknown** -> `capability unknown (not inspected)` (epistemic); the LLM writing `websocket_refresh affected` -> violation (treating uncertainty as a conclusion).

## Why a manifest, not LLM imagination

- The manifest is a project-level **fact declaration** (whether the project has a websocket is objective), produced by `detect_capabilities` from deterministic sources, or hand-filled once by a human.
- unresolved is **computed** from it, not depending on the LLM "can I think of it right now".
- The LLM's job becomes "explain which of these unresolved relate to this change", not "imagine what unresolved should contain".
- absent_confirmed is a hard block — it makes "fabricating websocket_refresh" structurally impossible.

> This parallels "the graph may be cached but is not Truth": compute what can be computed, honestly mark what cannot be computed as unresolved (unknown), never let the LLM pass off a blind spot as a conclusion.
