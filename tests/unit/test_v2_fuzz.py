"""Task 17 (Batch K): 50-seed bounded spec generator + invariant assertions.

A property/fuzz test: a deterministic, seeded random VALID v2 spec generator
(emits agent/condition/repeat/until/gate nodes, some worktree-isolated, with
bounded max_iterations, valid refs, unique write_areas) -> for each of 50
seeds assert the six v2 invariants hold end-to-end:

  1. validate_spec(s) == []               (generator emits VALID specs)
  2. Harness.run completes (no crash)      (fake conforming runner)
  3. ledger.verify_chain() == []          (hash chain intact, invariant 5)
  4. no orphan refs (validate_spec post-run == [])  (referential integrity)
  5. deterministic predicate eval NEVER calls an LLM (invariant 1:
     the runner is only called for AGENT dispatch, never during _eval_predicate)
  6. structural sanity: every branch_decision/loop_close is well-formed
     (no silent branch, no malformed loop exit -- invariants 3/4 provenance)

The generator uses `random.Random(seed)` (deterministic, reproducible -- NOT
`random.random()`). Per-kind rules (Task 3), referential integrity, closed-set
types, and recursive ownership (unique write_areas) are all respected by
construction so validate_spec is clean for every seed.

Non-halting policies are used throughout (on_exhausted='degrade' on loops,
gate on_trigger in {auto,replan}, condition gate=False) so run() completes to a
normal dict rather than a gate_halted state -- the adversarial suite (Task 16)
already exercises the halt paths; this fuzz exercises the clean control-flow
lifecycle (loop convergence/exhaustion, branch selection, gate escalation).
"""
import json
import random

import pytest

from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec


# =====================================================================
# the conforming fake runner (never writes files; zero cost)
# =====================================================================

def _ok_result() -> str:
    """A conforming worker result envelope: subtype success, schema-valid
    {answer:42} payload, zero cost. The runner writes NO files (so the
    _snapshot_artifacts path is trivial), keeping the run hermetic."""
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    })


# =====================================================================
# predicate pool (all deterministic, evaluable, NEVER an LLM call)
# =====================================================================
# Every predicate is a pure ledger-projection read (§2 DSL): True/False are
# deterministic literals; dry_count()/budget_used()/stagnation_count() are
# built-in deterministic projections; claim_status(...) reads VERIFIED truth
# (last-write-wins). None reference a raw {{ref}} PROPOSED field (advisory),
# so every branch/loop-exit here is BINDING (claim_strength in
# {deterministic, verified}) -- the PROPOSED-advisory path is covered by the
# Task 16 adversarial suite, not this fuzz.
_PREDICATES = [
    "True",
    "False",
    "dry_count() < 2",
    "claim_status('C1') == 'VERIFIED'",
    "budget_used() < 1.0",
    "stagnation_count() < 2",
]

# repeat.until must match _DRY_RE (r"^dry<(\d+)$") for the built-in dry metric
# to converge; N in {1,2,3} yields 1-3 dry rounds before convergence.
_DRY_UNTIL = ["dry<1", "dry<2", "dry<3"]


# =====================================================================
# node builders (each emits a valid per-kind node; refs only to existing ids)
# =====================================================================

def _agent(rng, nid):
    """A conforming agent. ~25% are worktree-isolated (exercises §5 worktree
    creation/cleanup via the non-git mkdir fallback). Worktree agents use
    write_areas=[] (read-only worker) so the snapshot path is trivial; non-
    worktree agents get a UNIQUE write_area `wa_{nid}/**` (or []) -- uniqueness
    guarantees check_ownership is clean (no overlap pair)."""
    iso = "worktree" if rng.random() < 0.25 else "none"
    if iso == "worktree":
        wa = []  # read-only worktree worker: snapshot skipped
    else:
        wa = [f"wa_{nid}/**"] if rng.random() < 0.5 else []
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": wa, "acceptance": ["a"], "failure_policy": {},
        "isolation": iso,
        # empty runtime_assets -> auto-detect (empty project_root -> []).
        "runtime_assets": [],
    }


def _condition(rng, nid, agent_ids):
    # gate=False -> the branch executes directly (no GateHalt). A binding
    # branch (predicate is deterministic/verified) or advisory (PROPOSED) --
    # here all predicates are deterministic/verified, so binding.
    return {
        "type": "condition", "id": nid,
        "predicate": rng.choice(_PREDICATES),
        "then_branch": [rng.choice(agent_ids)],
        "else_branch": [rng.choice(agent_ids)] if rng.random() < 0.5 else [],
        "gate": False, "output_schema": {},
    }


def _repeat(rng, nid, agent_ids):
    # on_exhausted='degrade' -> soft None -> PARTIAL (NO gate halt). Bounded
    # max_iterations 1-5 (cannot-loop-forever guard, Q1).
    return {
        "type": "repeat", "id": nid,
        "body": [rng.choice(agent_ids)],
        "max_iterations": rng.randint(1, 5),
        "until": rng.choice(_DRY_UNTIL),
        "failure_policy": {"on_exhausted": "degrade"},
    }


def _until(rng, nid, agent_ids):
    # on_exhausted='degrade' (UntilNode default) -> PARTIAL (NO gate halt).
    # Bounded max_iterations 1-5. budget_aware toggled (cost=0 -> never trips).
    return {
        "type": "until", "id": nid,
        "body": [rng.choice(agent_ids)],
        "max_iterations": rng.randint(1, 5),
        "cond": rng.choice(_PREDICATES),
        "budget_aware": rng.choice([True, False]),
        "failure_policy": {"on_exhausted": "degrade"},
    }


def _gate(rng, nid, agent_ids):
    # on_trigger in {auto, replan} -> NEVER raises GateHalt. auto opens+
    # resolves the escalation gate then runs the body; replan emits a boundary
    # signal and returns empty. Both complete the run (no pause-halt).
    return {
        "type": "gate", "id": nid,
        "trigger": rng.choice(_PREDICATES),
        "body": [rng.choice(agent_ids)],
        "on_trigger": rng.choice(["auto", "replan"]),
        "escalation_tiers": [],
        "output_schema": {},
    }


_CF_BUILDERS = {
    "condition": _condition,
    "repeat": _repeat,
    "until": _until,
    "gate": _gate,
}


# =====================================================================
# the deterministic random VALID v2 spec generator
# =====================================================================

def _rand_spec(seed):
    """Build a random VALID v2 spec from `random.Random(seed)` (deterministic).

    Structure:
      - 2-4 agent nodes (leaf workers); ~25% isolation='worktree'.
      - 1-3 control-flow nodes (condition/repeat/until/gate), each referencing
        EXISTING agent ids in body/then_branch/else_branch (valid refs; no
        cycles since CF bodies only reference agents, never other CF nodes).
      - control_flow.steps: EVERY CF node id appears as a step (so each is
        actually dispatched -- the fuzz exercises the real condition/repeat/
        until/gate lifecycle, not unreachable dead nodes), plus 1-N extra
        agent steps and occasional ['parallel', <2-3 agent ids>] fan-outs.
        Shuffled so the CF nodes land at random positions. All refs exist.

    Validity guarantees (validate_spec clean by construction):
      - VALID refs: every body/then_branch/else_branch/step id exists in
        spec.nodes (CF nodes only reference the pre-built agent pool).
      - UNIQUE write_areas: `wa_{nid}/**` per non-worktree agent (no two agents
        share a glob base -> check_ownership clean); worktree agents use [].
      - BOUNDED max_iterations: every repeat/until has max_iterations in 1-5.
      - per-kind fields (Task 3): non-empty predicate/then_branch/body/cond/
        trigger + max_iterations>0 + a termination predicate (repeat.until).
      - closed-set types: only known kinds (agent/condition/repeat/until/gate).
      - non-halting policies so run() completes (see builders above).
    """
    rng = random.Random(seed)
    n_agents = rng.randint(2, 4)
    nodes = {}
    agent_ids = []
    for i in range(n_agents):
        nid = f"a{i + 1}"
        nodes[nid] = _agent(rng, nid)
        agent_ids.append(nid)

    # at least 1 CF node per spec: the fuzz is ABOUT v2 control flow, so every
    # spec exercises at least one condition/repeat/until/gate dispatch.
    n_cf = rng.randint(1, 3)
    cf_ids = []
    for j in range(n_cf):
        kind = rng.choice(["condition", "repeat", "until", "gate"])
        nid = f"cf{j + 1}"
        nodes[nid] = _CF_BUILDERS[kind](rng, nid, agent_ids)
        cf_ids.append(nid)

    # steps: every CF node is reachable (dispatched) + extra agents/parallel,
    # shuffled so CF nodes land at random positions in the sequence.
    steps = list(cf_ids)
    n_extra = rng.randint(1, max(1, len(agent_ids)))
    for _ in range(n_extra):
        if rng.random() < 0.3 and len(agent_ids) >= 2:
            k = rng.randint(2, min(3, len(agent_ids)))
            steps.append(["parallel"] + rng.sample(agent_ids, k))
        else:
            steps.append(rng.choice(agent_ids))
    rng.shuffle(steps)

    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=[],  # no required success_evidence -> verdict is
        # UNVERIFIED/PARTIAL (never VERIFIED); the fuzz asserts invariants, not
        # a specific verdict. (success_evidence=[] -> no verdict-pairing checks.)
        nodes=nodes,
        control_flow={"type": "sequence", "steps": steps},
        decision_trace=[],
        budget_usd=5.0, max_concurrent=4, max_agents=1000, max_stagnation=2,
    )


# =====================================================================
# invariant 1 instrumentation: predicate eval must NEVER call an LLM
# =====================================================================

def _instrument_predicate_eval(h, state):
    """Wrap h._eval_predicate so the shared `state` flag is True only during a
    predicate evaluation. The recording runner reads the same flag: if a
    runner call lands with in_predicate=True, an LLM was invoked to decide a
    branch/loop-exit (invariant 1 violation). _eval_predicate is synchronous
    and runs in the loop-driving thread; body dispatches (which call the
    runner) always JOIN before the next predicate eval (do-until: body, then
    cond; condition: eval, then branch; gate: eval, then body), so the flag is
    False during every agent dispatch."""
    orig = h._eval_predicate

    def wrapped(*a, **kw):
        state["in_predicate"] = True
        try:
            return orig(*a, **kw)
        finally:
            state["in_predicate"] = False

    h._eval_predicate = wrapped


# =====================================================================
# the 50-seed fuzz test
# =====================================================================

@pytest.mark.parametrize("seed", list(range(50)))
def test_fuzz_v2_spec_invariants(seed, tmp_path):
    """For each of 50 deterministic seeds: the generator emits a valid v2 spec;
    a fake-runner Harness.run completes; the hash chain is intact; referential
    integrity holds post-run; deterministic predicate eval never calls an LLM;
    every branch_decision/loop_close is well-formed."""
    spec = _rand_spec(seed)

    # (1) the generator MUST produce a VALID spec (validate_spec clean).
    errs = validate_spec(spec)
    assert errs == [], f"seed {seed}: generator emitted invalid spec: {errs}"

    # (2) Harness.run with a conforming fake runner completes (no crash).
    # project_root is an empty non-git dir: worktree agents fall back to mkdir
    # (no git pollution of the host repo); auto-detect runtime_assets -> [].
    state = {"in_predicate": False, "calls": []}

    def runner(args, cwd=None):
        state["calls"].append({
            "in_predicate": state["in_predicate"],
            "cwd": cwd,
        })
        return (0, _ok_result())

    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    h = Harness(tmp_path / "run", worker_runner=runner,
                project_root=project_root)
    _instrument_predicate_eval(h, state)

    out = h.run(spec)
    assert isinstance(out, dict), (
        f"seed {seed}: run did not return a dict: {out!r}")
    # non-halting policies -> no gate_halted (clean run to completion).
    assert "gate_halted" not in out, (
        f"seed {seed}: run halted on a gate (generator must use non-halting "
        f"policies: degrade/auto/replan): {out!r}")

    # (3) hash chain intact (invariant 5: seq+prev_hash+event_hash, tamper-free).
    chain_errs = h.ledger.verify_chain()
    assert chain_errs == [], f"seed {seed}: hash chain broken: {chain_errs}"

    # (4) no orphan refs (referential integrity holds post-run; spec unmutated).
    errs_post = validate_spec(spec)
    assert errs_post == [], (
        f"seed {seed}: post-run validate failed (spec mutated or orphan refs): "
        f"{errs_post}")

    # (5) invariant 1: deterministic predicate eval NEVER calls an LLM.
    #     The runner is only called for AGENT dispatch; a call with
    #     in_predicate=True would mean an LLM decided a branch/loop-exit.
    llm_during_eval = [c for c in state["calls"] if c["in_predicate"]]
    assert not llm_during_eval, (
        f"seed {seed}: runner called during _eval_predicate (invariant 1 "
        f"violation -- an LLM decided a branch/loop-exit): {llm_during_eval}")

    # (6) structural sanity (invariants 3/4 provenance): every branch_decision
    #     carries a valid branch_taken; every loop_close carries a valid
    #     exit_reason. No silent branch, no malformed loop exit.
    for e in h.ledger.events():
        if e.get("kind") == "branch_decision":
            assert e["payload"]["branch_taken"] in ("then", "else"), (
                f"seed {seed}: malformed branch_decision: {e}")
        elif e.get("kind") == "loop_close":
            assert e["payload"]["exit_reason"] in (
                "converged", "exhausted", "budget_exhausted",
                "stagnation_cap"), (
                f"seed {seed}: malformed loop_close exit_reason: {e}")


# =====================================================================
# determinism guard: same seed -> identical spec (proves seeded Random)
# =====================================================================

def test_generator_is_deterministic():
    """The generator is reproducible: random.Random(seed) (NOT random.random())
    means the same seed yields the same spec nodes + control_flow. A drift
    would indicate accidental use of module-global RNG."""
    from axiom.ir import spec_to_json
    s_a = spec_to_json(_rand_spec(7))
    s_b = spec_to_json(_rand_spec(7))
    assert s_a == s_b, "same seed must produce an identical spec (determinism)"
    # and two different seeds differ (sanity: not all seeds collapse to one spec)
    s_c = spec_to_json(_rand_spec(8))
    assert s_c != s_a, "different seeds should produce different specs"
