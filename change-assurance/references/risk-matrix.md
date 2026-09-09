# Risk Matrix — V0–V3 admission / minimum evidence / upgrade rules

This table is the machine lower bound and hard rules. The LLM may upgrade above the machine lower bound, **may not downgrade below it**. The harness enforces the lower bound at the verdict gate.

## Risk dimensions (for the LLM's reference when proposing an upgrade, not a weighted total)

- **blast radius**: scope of affected modules / users / tenants / external consumers
- **reversibility**: whether a failure can roll back quickly without data loss
- **sensitivity**: whether it involves permissions / privacy / funds / data integrity / infrastructure
- **detectability**: whether an error can be caught quickly by tests / monitoring / users
- **novelty**: whether the tech / runtime path / business behavior is unfamiliar
- **uncertainty**: whether the impact surface has unresolved or invisible parts
- **evidence coverage**: how much the existing verification assets cover key behavior

The risk level is not a weighted total; it relies on hard lower bounds + upgrade rules.

## Level table

| Level | Admission condition | Minimum evidence | surface trigger (machine lower bound) |
|---|---|---|---|
| **V0 Quick** | local, reversible, no public contract, clear impact surface | targeted-static + build + visual-check | docs / pure config |
| **V1 Standard** | ordinary function or local interface, enumerable impact | reference-scan + affected-tests + targeted-behavior | runtime logic |
| **V2 Thorough** | public contract / shared state / cross-module linkage | expanded-impact-scan + regression + integration + core-e2e | public-contract / shared-state |
| **V3 Critical** | permissions / data migration / payment / irreversible / critical infrastructure | deep-evidence + rollback-plan + authorization-gate | secret-auth / schema-migration |

## Hard rules (no downgrade)

1. Sensitive domain (permissions/payment/privacy/credentials) at least V3.
2. Public contract or shared state at least V2.
3. Must upgrade when a high-impact unresolved item exists.
4. **May not default to V0 when risk cannot be reliably judged** (unknown -> V1).
5. Only local, reversible, and clear-impact may enter V0.
6. Cross-module linkage (>=2 surface types other than test/docs) at least V2.
7. A schema/migration file appearing -> V3, no matter how light the other files are.
8. Test files do not independently raise the floor (evidentiary), but if the test itself changed assertion semantics -> treat as runtime V1.

## Upgrade triggers (the LLM should proactively upgrade)

- write_areas hit a high fan-out symbol (referenced by >=9 modules) -> upgrade one level
- the change touches an event bus / websocket / dynamic dispatch -> upgrade one level (runtime relationships are not statically visible)
- the change touches a historically high-coupling file (git history shows every change to A is accompanied by a change to B) -> upgrade one level
- the change alters a public function signature / return type -> at least V2
- the change involves irreversible external side effects (send email / charge / delete data) -> V3 + authorization gate
- verification tool unavailable and no alternative -> no VERIFIED, downgrade to UNVERIFIED by risk or trigger BLOCKED

## Downgrade prohibition

The LLM may not downgrade V3 to V2 because "I feel the risk is not high". A downgrade may only come from:
- New evidence proves the previously-judged surface was wrong (e.g. the .sql is actually a fixture, not a migration) -> re-judge the surface, not downgrade the risk.
- A human explicitly accepts the residual risk (human acceptance) -> recorded into evidence; the verdict still follows evidence strength, does not auto-upgrade to VERIFIED.
