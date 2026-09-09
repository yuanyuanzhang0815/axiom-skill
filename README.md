# axiom

> **Deterministic multi-agent workflow orchestration.** Design a bounded workflow spec (IR), hand it to the axiom harness, read the checkpoint, decide the next workflow — never orchestrate node-by-node.

```
You (the orchestrator)        ──reasons between workflows──▶   next spec revision
        │ spec.json (IR)
        ▼
   axiom harness               ──dispatches nodes, gates, converges──▶  checkpoint
        │ dispatch_req_{rid}.json  /  dispatch_res_{rid}.json (file protocol)
        ▼
   any agent runtime           ──compute / context──▶  DispatchResult envelope
   (Claude Code · pi · your worker)
```

## The one invariant

> The orchestrator may reason between workflows, but never between individual workflow nodes. Node-level execution and intermediate state belong exclusively to the harness and ledger.

You design the graph; the harness runs it; you read **one** checkpoint and decide. You never see intermediate node output. This is the anti-self-audit wall — the thing that keeps an agent from grading its own homework and shipping `VERIFIED` on prose.

## Why

Agents drift. They self-certify. They burn budget without converging. Orchestrating them **node-by-node** — dispatch one, read its output, decide the next — puts the orchestrator inside the agent's state, which is exactly where false-verifying and context rot begin.

axiom is the **control layer**, not the execution layer:

| Layer | Owns | Entity |
|---|---|---|
| **Execution** | agent dispatch, subagent mgmt, LLM calls | *any* runtime that speaks the file protocol |
| **Control** | workflow graph, state machine, convergence, gates, hash-chained audit | **axiom** |

The runtime solves compute and context. axiom solves *what to work on, what evidence suffices, when to stop, whether cost exceeds budget*. The two are orthogonal — a symbiosis, not a lock-in. Any runtime that writes a conforming `DispatchResult` back into the run-dir is a legal worker.

## Backends

| Backend | How | Capabilities |
|---|---|---|
| **cc** (default) | `host_adapter --cc` — `claude -p` + cc-switch config | tools |
| **pi** | `host_adapter --pi` — pi NDJSON file protocol | cost, tools, cancel |

The host adapter (`scripts/host_adapter.py`) is the *only* host-side code any agent runtime needs to plug into axiom — it bridges the file protocol to the underlying CLI. Bring your own worker by writing a `dispatch_res_{rid}.json` envelope; see `scripts/host_adapter.py`.

## Install

axiom is a self-contained Python skill — no package install, no daemon.

```bash
# 1. clone
git clone https://github.com/<you>/axiom.git
# 2. put it on PATH (or alias)
export PATH="$PWD/axiom/bin:$PATH"
# 3. preflight — checks your backend binary + provider key + adapter
axiom doctor
```

That's it. `axiom` resolves its own runtime; python3 at `/usr/bin` is enough. No nvm, no `bash -lc`.

> **Skill managers:** if you use a skill-adopt workflow (e.g. a symlink farm), point it at this repo's root. axiom never writes to a global skill list on its own.

## Quickstart — make N edits, then verify them

The copy-paste shape that reaches `VERIFIED` end-to-end: edit agents do the work (no `verdict_field`), a script node produces machine evidence, and one verifier owns the verdict.

```bash
axiom plan --intent "fix the login redirect bug" --out spec.json
# edit spec.json: nodes + write_areas + success_evidence (see SKILL.md)
axiom validate spec.json
axiom run spec.json --run-dir .axiom/run          # synchronous; prints checkpoint
axiom checkpoint spec.json --run-dir .axiom/run   # the ONLY re-entry object
```

Minimal node shape (full template + the runtime-verifier variant in `SKILL.md`):

```json
{
  "nodes": {
    "n1_edit":   {"type":"agent","dispatch":"host","write_areas":["src/auth.ts"],"output_schema":{"required":["files_modified"]}},
    "n_test":    {"type":"script","script_path":"/abs/run_tests.py","evidence_scope":["src/auth.ts"]},
    "n_verify":  {"type":"agent","verdict_field":"verdict","evidence_from":"n_test","allowed_tools":["Read"]}
  },
  "control_flow": {"type":"sequence","steps":[["parallel","n1_edit"],"n_test","n_verify"]},
  "success_evidence": ["S1:R1=node:n_verify"],
  "budget_usd": 20.0
}
```

Exit codes triage without JSON parsing: **0=VERIFIED**, **3=BLOCKED**, **4=PARTIAL**, **5=UNVERIFIED**, 2=unknown. So `axiom run spec.json && deliver` just works.

## What you get

- **A bounded IR** — `agent` / `parallel` / `pipeline` / `verify` / `synthesize` / `workflow` / `script` node types. Non-overlapping `write_areas` ownership. Per-node `output_schema`. `validate` rejects malformed plans *before* a run.
- **v2 control flow** — `repeat` (loop-until-dry), `until` (predicate convergence), `condition` (binding branches via `gate:true`), `gate` (staged human/auto escalation). A restricted predicate DSL over `claim_status()` / `budget_used()` / `dry_count()`.
- **Non-binary verdicts** — `VERIFIED / PARTIAL / BLOCKED / UNVERIFIED`. A claim is VERIFIED only via a `verify` node (skeptic majority-unrefuted) or an agent with `verdict_field` + `node:` binding — never your assertion, never the worker's self-report.
- **Honest cost** — `budget_usd` enforced as honest exit. Cost is split: `cost_usd_total` (all spend, the budget-trip) / `successful_cost_usd` (clean conform) / `failed_cost_usd` (stagnating, real money no output).
- **Hash-chained ledger** — every dispatch / verify / gate / verdict is an append-only, tamper-evident event. `axiom journal` projects the bidirectional evidence graph.
- **Gates** — high-risk / secrets / delete / irreversible actions halt for human `allow|deny|modify`. `modify` → new spec revision, never in-place.
- **Cross-session resume** — `axiom continue` (no args) reads an active-run pointer and prints a handoff digest, so a `/clear`-wiped context re-lands on a run in seconds, not minutes.
- **Wiki** — append-only, hash-chained cross-run retrieval. `plan --wiki-suggest` returns the proven output *format* before you design the next spec, so the agent gets the shape it needs before producing, not after.
- **Skill self-evolution** — axiom's own changes are sediment-tracked in `skill_patterns.jsonl` (structured, never prose), with `Why this exists: P-NNN` traceability links back into `SKILL.md`. The discipline travels with the skill wherever it's installed.
- **Worktree isolation** — `"isolation":"worktree"` runs an agent in a detached git worktree; the main checkout is never touched (no-leak property). Overlapping `write_areas` allowed when *every* node in the pair is isolated.
- **Machine evidence (R9-3)** — for code tasks, a `script` node declares `evidence_scope` and a reviewer declares `evidence_from`; after a code change the old evidence auto-invalidates, forcing a re-review. No hand-written receipts.

## Optional companion: change-assurance

`change-assurance/` is an **optional** companion skill implementing the `assurance_hook` subprocess contract (the legacy code-change assurance path). For code tasks, the recommended path is the **machine-evidence** shape above (script `evidence_scope` + reviewer `evidence_from`); `assurance_hook` is retained for workflows that want a standalone risk-graded, per-surface evidence adjudicator. See `change-assurance/SKILL.md`.

## The discipline this enforces

- **Coverage before conclusion** — a negative assertion ("the project has no X") must survive multi-path retrieval and declare residual uncertainty. "grep found nothing → doesn't exist" is the forbidden move.
- **Claim strength ≤ evidence strength.**
- **Authorization never expands** — "you handle it" does not authorize destructive or external-write actions; Gate them.
- **Never ship a code-change spec without a verify/agent-verdict node bound to `success_evidence`** — the worker's "I'm done" + green self-tests are NOT evidence.

## Repository layout

```
axiom/            the control layer (ir, harness, dispatch, ledger, state, wiki, cli)
bin/axiom         entry shim (sys.path bootstrap, defeats same-name shadowing)
scripts/          host_adapter.py (pi/cc) + e2e + repo-map generator
change-assurance/ optional companion skill
specs/            example specs (repeat-until-dry, condition-gate-review)
tests/            unit + integration (all workers mocked; ~800 tests)
SKILL.md          the full authoring guide + architecture + provenance
```

## Contributing

axiom's design rationale is sediment-tracked, not prose. When you change a behavior, append a structured `skill_pattern` (`axiom wiki pattern add --id P-NNN ...`) and link it from `SKILL.md` (`Why this exists: P-NNN`) before closing the task. Rejected approaches are recorded so they aren't re-proposed. See `## Skill self-evolution` in `SKILL.md`.

Run the suite (all workers are mocked — no live agent runtime needed):

```bash
python -m pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
