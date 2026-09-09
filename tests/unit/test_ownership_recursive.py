"""Task 4 (Batch B — Validation): recursive check_ownership + worktree-overlap
exception (spec §5 Q-walk; v1 spec line 551 isolated-merge exception).

v1's check_ownership did a FLAT walk: only `control_flow.steps` parallel
fan-out steps (`["parallel", a, b]`), no descent into node bodies. Two agents
overlapping inside a `condition.then_branch` parallel step were MISSED --
the top-level step is just the condition node id (a str), so no parallel
group was formed. This file proves the recursion catches nested overlap and
that the all-worktree exception allows intentional isolated-merge overlap.

NOTE on `isolation`: read from the node DICT (`node.get("isolation")`), NOT
the AgentNode dataclass -- the `isolation` dataclass field lands in Task 12.
Tests here build agent dicts directly with `"isolation": "worktree"`.
"""
from axiom.ir import Spec, Requirement, check_ownership


def _spec(nodes, cf):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["S1:R1=s"],
        nodes=nodes,
        control_flow=cf,
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def _agent(nid, write_areas, isolation=None):
    """Build an agent node DICT directly (not via AgentNode+as_node_dict) so
    the `isolation` key can be set even though the AgentNode dataclass lacks
    that field until Task 12."""
    n = {
        "type": "agent",
        "id": nid,
        "prompt": "p",
        "output_schema": {"type": "object"},
        "acceptance": ["a"],
        "write_areas": write_areas,
        "failure_policy": {},
    }
    if isolation:
        n["isolation"] = isolation
    return n


def _condition(nid="c1", then_branch=None, else_branch=None):
    return {
        "type": "condition",
        "id": nid,
        "predicate": "claim_status('C1')=='VERIFIED'",
        "then_branch": then_branch or [],
        "else_branch": else_branch or [],
        "gate": False,
        "output_schema": {},
    }


def _repeat(nid="r1", body=None):
    return {
        "type": "repeat",
        "id": nid,
        "body": body or [],
        "max_iterations": 5,
        "until": "dry<2",
        "failure_policy": {"on_exhausted": "block"},
    }


def _until(nid="u1", body=None):
    return {
        "type": "until",
        "id": nid,
        "body": body or [],
        "cond": "claim_status('C1')=='VERIFIED'",
        "max_iterations": 3,
        "budget_aware": True,
        "failure_policy": {"on_exhausted": "degrade"},
    }


def _gate(nid="g1", body=None):
    return {
        "type": "gate",
        "id": nid,
        "trigger": "claim_status('C1')=='REFUTED'",
        "escalation_tiers": [{"model": "claude-opus-4-20250514"}],
        "body": body or [],
        "on_trigger": "pause",
        "output_schema": {},
    }


# --- the 5 required tests (brief Step 2) -----------------------------------


def test_two_nonworktree_overlap_in_condition_branch_rejected():
    """Two non-worktree agents overlapping inside condition.then_branch must
    be REJECTED. v1's flat walk missed this -- top-level step is the condition
    node id (a str), so no parallel group formed. Recursion must descend into
    then_branch and catch it.

    Body shape: then_branch is a LIST OF STEPS (same shape as
    control_flow.steps), so a parallel fan-out is `[["parallel", ...]]` --
    one step that is a parallel list. See Task 3 test_then_branch_ref... and
    test_nested_parallel_ref_unknown_node_rejected for the canonical shape."""
    a1 = _agent("a1", ["src/auth/**"])
    a2 = _agent("a2", ["src/auth/login.py"])  # overlaps a1
    c1 = _condition("c1", then_branch=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1},
        {"type": "sequence", "steps": ["c1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap inside condition.then_branch must be caught (recursion)"
    assert any("a1" in e and "a2" in e for e in errs), errs


def test_two_worktree_overlap_allowed():
    """All-worktree overlap is the intentional isolated-merge exception (v1
    spec line 551). Two worktree-isolated agents may share write_areas."""
    a1 = _agent("a1", ["src/auth/**"], isolation="worktree")
    a2 = _agent("a2", ["src/auth/login.py"], isolation="worktree")
    c1 = _condition("c1", then_branch=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1},
        {"type": "sequence", "steps": ["c1"]},
    )
    errs = check_ownership(spec)
    assert errs == [], f"all-worktree overlap must be allowed (isolated merge): {errs}"


def test_worktree_plus_nonworktree_overlap_rejected():
    """A mix of worktree + non-worktree overlapping nodes is still rejected
    -- the exception only relaxes overlap when ALL overlapping nodes are
    worktree."""
    a1 = _agent("a1", ["src/auth/**"], isolation="worktree")
    a2 = _agent("a2", ["src/auth/login.py"])  # non-worktree
    c1 = _condition("c1", then_branch=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1},
        {"type": "sequence", "steps": ["c1"]},
    )
    errs = check_ownership(spec)
    assert errs, "mixed worktree+non-worktree overlap must be rejected"
    assert any("a1" in e and "a2" in e for e in errs), errs


def test_nested_in_repeat_body_same_rules():
    """Same three rules apply when the parallel step is nested inside
    repeat.body (not just condition.then_branch)."""
    # (a) non-worktree overlap in repeat.body -> rejected
    a1 = _agent("a1", ["src/**"])
    a2 = _agent("a2", ["src/**"])
    r1 = _repeat("r1", body=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "r1": r1},
        {"type": "sequence", "steps": ["r1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap inside repeat.body must be caught"
    assert any("a1" in e and "a2" in e for e in errs), errs

    # (b) all-worktree overlap in repeat.body -> allowed
    b1 = _agent("b1", ["src/**"], isolation="worktree")
    b2 = _agent("b2", ["src/**"], isolation="worktree")
    r2 = _repeat("r2", body=[["parallel", "b1", "b2"]])
    spec2 = _spec(
        {"b1": b1, "b2": b2, "r2": r2},
        {"type": "sequence", "steps": ["r2"]},
    )
    assert check_ownership(spec2) == [], \
        "all-worktree overlap in repeat.body must be allowed (isolated merge)"

    # (c) worktree + non-worktree mix in repeat.body -> rejected
    c_a = _agent("ca", ["src/**"], isolation="worktree")
    c_b = _agent("cb", ["src/**"])  # non-worktree
    r3 = _repeat("r3", body=[["parallel", "ca", "cb"]])
    spec3 = _spec(
        {"ca": c_a, "cb": c_b, "r3": r3},
        {"type": "sequence", "steps": ["r3"]},
    )
    errs3 = check_ownership(spec3)
    assert errs3, "mixed overlap in repeat.body must be rejected"


def test_nonoverlapping_nested_ok():
    """Nested parallel agents whose write_areas don't overlap must pass --
    recursion must not false-positive on every nested parallel step."""
    a1 = _agent("a1", ["src/auth/**"])
    a2 = _agent("a2", ["src/pay/**"])  # no overlap with a1
    c1 = _condition("c1", then_branch=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1},
        {"type": "sequence", "steps": ["c1"]},
    )
    assert check_ownership(spec) == []


# --- bonus: recursion covers until.body / gate.body / else_branch ----------
# (self-audit: recursion covers all 4 new node bodies; not just condition+repeat)


def test_overlap_in_until_body_rejected():
    a1 = _agent("a1", ["docs/**"])
    a2 = _agent("a2", ["docs/intro.md"])
    u1 = _until("u1", body=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "u1": u1},
        {"type": "sequence", "steps": ["u1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap inside until.body must be caught"
    assert any("a1" in e and "a2" in e for e in errs), errs


def test_overlap_in_gate_body_rejected():
    a1 = _agent("a1", ["cfg/**"])
    a2 = _agent("a2", ["cfg/app.yaml"])
    g1 = _gate("g1", body=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "g1": g1},
        {"type": "sequence", "steps": ["g1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap inside gate.body must be caught"
    assert any("a1" in e and "a2" in e for e in errs), errs


def test_overlap_in_condition_else_branch_rejected():
    a1 = _agent("a1", ["db/**"])
    a2 = _agent("a2", ["db/seed.sql"])
    c1 = _condition("c1", then_branch=["a1"], else_branch=[["parallel", "a1", "a2"]])
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1},
        {"type": "sequence", "steps": ["c1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap inside condition.else_branch must be caught"
    assert any("a1" in e and "a2" in e for e in errs), errs


# --- bonus: deep nesting + parallel.body / pipeline.stages ---------------
# (self-audit: recursion covers parallel.body + pipeline.stages too)


def test_deeply_nested_overlap_rejected():
    """repeat.body -> condition.then_branch -> parallel step: deep nesting
    still caught (recursion is transitive across body fields)."""
    a1 = _agent("a1", ["pkg/**"])
    a2 = _agent("a2", ["pkg/mod.py"])
    c1 = _condition("c1", then_branch=[["parallel", "a1", "a2"]])
    r1 = _repeat("r1", body=["c1"])  # repeat body references the condition
    spec = _spec(
        {"a1": a1, "a2": a2, "c1": c1, "r1": r1},
        {"type": "sequence", "steps": ["r1"]},
    )
    errs = check_ownership(spec)
    assert errs, "overlap nested deep in repeat.body->condition.then_branch must be caught"
    assert any("a1" in e and "a2" in e for e in errs), errs


def test_parallel_body_agent_overlaps_sibling_rejected():
    """A parallel node's inline body agent runs concurrently with its sibling
    agents in the same `["parallel", ...]` fan-out step; if their
    write_areas overlap (and not all worktree), reject. Descends into
    parallel.body per spec §5 Q-walk."""
    a1 = _agent("a1", ["src/**"])
    p1 = {
        "type": "parallel", "id": "p1", "over": "{{items}}",
        "concurrency": 2, "barrier": True,
        "body": {
            "type": "agent", "id": "p1_body", "prompt": "p",
            "output_schema": {"type": "object"}, "acceptance": ["a"],
            "write_areas": ["src/foo/**"], "failure_policy": {},
        },
    }
    spec = _spec(
        {"a1": a1, "p1": p1},
        {"type": "sequence", "steps": [["parallel", "a1", "p1"]]},
    )
    errs = check_ownership(spec)
    assert errs, "parallel.body agent overlapping a sibling must be caught"
    assert any("a1" in e and "p1" in e for e in errs), errs


def test_pipeline_stage_overlaps_sibling_rejected():
    """A pipeline node's inline stage agents run concurrently with sibling
    agents in the same fan-out step (stages within one pipeline are
    sequential, but a pipeline node alongside a sibling agent in a
    `["parallel", ...]` step runs concurrently with that sibling). Descends
    into pipeline.stages per spec §5 Q-walk."""
    a1 = _agent("a1", ["lib/**"])
    pl1 = {
        "type": "pipeline", "id": "pl1", "items": "{{items}}",
        "stages": [
            {
                "type": "agent", "id": "s1", "prompt": "p",
                "output_schema": {"type": "object"}, "acceptance": ["a"],
                "write_areas": ["lib/util.py"], "failure_policy": {},
            },
        ],
    }
    spec = _spec(
        {"a1": a1, "pl1": pl1},
        {"type": "sequence", "steps": [["parallel", "a1", "pl1"]]},
    )
    errs = check_ownership(spec)
    assert errs, "pipeline stage agent overlapping a sibling must be caught"
    assert any("a1" in e and "pl1" in e for e in errs), errs


def test_parallel_body_all_worktree_overlap_allowed():
    """parallel.body agent + sibling both worktree-isolated -> allowed."""
    a1 = _agent("a1", ["src/**"], isolation="worktree")
    p1 = {
        "type": "parallel", "id": "p1", "over": "{{items}}",
        "concurrency": 2, "barrier": True,
        "body": {
            "type": "agent", "id": "p1_body", "prompt": "p",
            "output_schema": {"type": "object"}, "acceptance": ["a"],
            "write_areas": ["src/foo/**"], "failure_policy": {},
            "isolation": "worktree",
        },
    }
    spec = _spec(
        {"a1": a1, "p1": p1},
        {"type": "sequence", "steps": [["parallel", "a1", "p1"]]},
    )
    errs = check_ownership(spec)
    assert errs == [], f"all-worktree overlap (parallel.body + sibling) must be allowed: {errs}"


# --- Fix 1: cross-subtree overlap (control-flow node as fan-out sibling) ---
# When a ["parallel", ...] step references a control-flow node (condition/
# repeat/until/gate) alongside an agent, that node's body agents run
# concurrently with the sibling -- they must be MERGED into the fan-out
# group (not collected as an independent nested group) so an agent inside a
# condition.then_branch is overlap-checked against the parallel sibling.
# Branch bodies DO run concurrently with parallel siblings; the previous
# separate-descend formed independent groups and missed cross-subtree
# conflicts (invariant 4 violation).


def test_cross_subtree_overlap_cond_body_vs_sibling_rejected():
    """cond1 is a fan-out sibling of a1; cond1.then_branch's agent a_inner
    overlaps a1's write_areas -> must be rejected (real concurrent-write
    hazard the ownership invariant must prevent)."""
    a1 = _agent("a1", ["src/x/**"])
    a_inner = _agent("a_inner", ["src/x/overlap.py"])  # overlaps a1
    cond1 = _condition("cond1", then_branch=[["parallel", "a_inner"]])
    spec = _spec(
        {"a1": a1, "a_inner": a_inner, "cond1": cond1},
        {"type": "sequence", "steps": [["parallel", "cond1", "a1"]]},
    )
    errs = check_ownership(spec)
    assert errs, "cond1.then_branch agent overlapping fan-out sibling a1 must be caught"
    assert any("a1" in e and "a_inner" in e for e in errs), errs


def test_cross_subtree_overlap_all_worktree_allowed():
    """Same cross-subtree shape but both a_inner and a1 are worktree-isolated
    -> the all-worktree exception (isolated merge) allows it."""
    a1 = _agent("a1", ["src/x/**"], isolation="worktree")
    a_inner = _agent("a_inner", ["src/x/overlap.py"], isolation="worktree")
    cond1 = _condition("cond1", then_branch=[["parallel", "a_inner"]])
    spec = _spec(
        {"a1": a1, "a_inner": a_inner, "cond1": cond1},
        {"type": "sequence", "steps": [["parallel", "cond1", "a1"]]},
    )
    errs = check_ownership(spec)
    assert errs == [], f"all-worktree cross-subtree overlap must be allowed: {errs}"


def test_cross_subtree_overlap_repeat_body_vs_sibling_rejected():
    """Same cross-subtree rule for repeat.body (not just condition.then_branch):
    a repeat node as a fan-out sibling, its body agent overlaps a1 -> rejected."""
    a1 = _agent("a1", ["cfg/**"])
    a_inner = _agent("a_inner", ["cfg/app.yaml"])
    r1 = _repeat("r1", body=[["parallel", "a_inner"]])
    spec = _spec(
        {"a1": a1, "a_inner": a_inner, "r1": r1},
        {"type": "sequence", "steps": [["parallel", "r1", "a1"]]},
    )
    errs = check_ownership(spec)
    assert errs, "repeat.body agent overlapping fan-out sibling a1 must be caught"
    assert any("a1" in e and "a_inner" in e for e in errs), errs


# --- Fix 2: cycle defense (self-referential / cyclic body) ---------------
# Task 3's validate_spec only checks ids EXIST, not acyclicity -- a cyclic
# body (cond1.then_branch referencing cond1) passes validate and reaches
# check_ownership, which without a visited set would infinite-loop. The
# recursive walk must defend against this and return cleanly (no hang).


def test_self_referential_body_does_not_hang():
    """cond1.then_branch references cond1 itself -> a cycle. check_ownership
    must NOT hang (returns within the test timeout). Whether it reports an
    error is secondary; the key assertion is no infinite recursion."""
    import threading
    result = {}

    def _run():
        cond1 = _condition("cond1", then_branch=["cond1"])  # self-cycle
        spec = _spec(
            {"cond1": cond1},
            {"type": "sequence", "steps": ["cond1"]},
        )
        result["errs"] = check_ownership(spec)
        result["done"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10.0)  # hard deadline: a hang means the test fails
    assert result.get("done"), "check_ownership hung on a self-referential body (cycle)"
    # no assertion on result["errs"] content -- returning cleanly is the fix


def test_mutual_cycle_bodies_do_not_hang():
    """cond1.then_branch -> cond2, cond2.then_branch -> cond1 (mutual cycle).
    Two-node cycle must also not hang."""
    import threading
    result = {}

    def _run():
        cond1 = _condition("cond1", then_branch=["cond2"])
        cond2 = _condition("cond2", then_branch=["cond1"])
        spec = _spec(
            {"cond1": cond1, "cond2": cond2},
            {"type": "sequence", "steps": ["cond1"]},
        )
        result["errs"] = check_ownership(spec)
        result["done"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10.0)
    assert result.get("done"), "check_ownership hung on a mutual two-node cycle"


def test_cycle_in_fan_out_subtree_does_not_hang():
    """cond1 in a fan-out step, cond1.then_branch -> cond2, cond2.then_branch
    -> cond1 (cycle reachable through the subtree-merge path). Must not hang."""
    import threading
    result = {}

    def _run():
        a1 = _agent("a1", ["src/**"])
        cond1 = _condition("cond1", then_branch=["cond2"])
        cond2 = _condition("cond2", then_branch=["cond1"])
        spec = _spec(
            {"a1": a1, "cond1": cond1, "cond2": cond2},
            {"type": "sequence", "steps": [["parallel", "cond1", "a1"]]},
        )
        result["errs"] = check_ownership(spec)
        result["done"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10.0)
    assert result.get("done"), "check_ownership hung on a cycle through the fan-out subtree-merge path"

