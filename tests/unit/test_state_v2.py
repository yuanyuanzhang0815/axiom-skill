"""v2 Task 6: state.py loop/branch projections (spec §4).

Tests loop-aware projections in state.py:
  - project_cognitive_state: loop lifecycle (active -> PROGRESSING; closed).
  - exhaustion routing: block -> BLOCKED; degrade -> PARTIAL; replan ->
    checkpoint.open_questions.
  - dry_count re-derivation from events (NOT a cached state; invariant 6).
  - last-write-wins across iterations (Q9): latest verify_verdict is current
    truth about a claim (both directions: survived->refuted AND refuted->
    survived, the iterative-refinement case).
  - branch_decision events are FACTS: derive_claim_status does NOT read them.
  - svid-scoping: v1 VERIFIED + v2 FAILED in same ledger, different svid ->
    v2's verdict not STALE-VERIFIED from v1 (v1 #1 strict-semantic guard).
"""
from axiom.ledger import Ledger
from axiom.ir import Spec, Requirement
from axiom.harness import Harness
from axiom.state import (
    derive_claim_status,
    derive_verdict,
    derive_dry_count,
    project_cognitive_state,
)


def _spec(svid="spec.v1", reqs=None, se=None):
    return Spec(
        spec_version_id=svid,
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=reqs if reqs is not None else [
            Requirement("R1", "r", "required"),
        ],
        boundaries=["b"],
        success_evidence=se if se is not None else ["S1:R1=claim:C1"],
        nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def _loop_open(lg, svid, loop_id="L1", node_id="repeat1", max_iter=3):
    lg.append({
        "event_id": f"lo_{loop_id}", "kind": "loop_open",
        "spec_version_id": svid, "node_id": node_id,
        "payload": {"loop_id": loop_id, "node_id": node_id,
                     "max_iterations": max_iter, "until": "dry<2"},
    })


def _loop_close(lg, svid, loop_id="L1", final_iter=1, exit_reason="converged"):
    lg.append({
        "event_id": f"lc_{loop_id}", "kind": "loop_close",
        "spec_version_id": svid,
        "payload": {"loop_id": loop_id, "final_iteration": final_iter,
                    "exit_reason": exit_reason},
    })


def _vv(lg, svid, claim_id, refuted=False, survived=True, loop_id=None,
        iteration=None, eid=None):
    """Append a verify_verdict event (top-level refuted/survived; loop context
    in payload per Task 5 design)."""
    ev = {
        "event_id": eid or f"vv_{claim_id}_{svid}",
        "kind": "verify_verdict", "spec_version_id": svid,
        "node_id": "verify1", "claim_id": claim_id,
        "refuted": refuted, "survived": survived,
    }
    payload = {}
    if loop_id is not None:
        payload["loop_id"] = loop_id
    if iteration is not None:
        payload["iteration"] = iteration
    if payload:
        ev["payload"] = payload
    lg.append(ev)


def _stagnating(lg, svid, node_id="worker1", loop_id="L1", iteration=1):
    lg.append({
        "event_id": f"stag_{svid}_{loop_id}_{iteration}",
        "kind": "stagnating", "spec_version_id": svid, "node_id": node_id,
        "payload": {"loop_id": loop_id, "iteration": iteration},
    })


# --- project_cognitive_state: loop lifecycle ------------------------------

def test_active_loop_progressing(tmp_path):
    """loop_open without loop_close -> loop active -> PROGRESSING.

    Spec §4: an active loop contributes PROGRESSING; no new cognitive state
    between iterations. v2 exposes loop lifecycle in cognitive_state["loops"].
    """
    lg = Ledger(tmp_path / "run")
    _loop_open(lg, "spec.v1")
    st = project_cognitive_state(_spec(), lg)
    assert st["progress"] == "PROGRESSING"
    loops = st["loops"]
    assert len(loops) == 1
    assert loops[0]["loop_id"] == "L1"
    assert loops[0]["state"] == "active"
    assert loops[0]["exit_reason"] is None


def test_loop_close_converged_not_progressing(tmp_path):
    """loop_open + loop_close(exit_reason=converged) -> loop closed; the loop
    is no longer active (not 'progressing' as a loop)."""
    lg = Ledger(tmp_path / "run")
    _loop_open(lg, "spec.v1")
    _loop_close(lg, "spec.v1", exit_reason="converged")
    st = project_cognitive_state(_spec(), lg)
    loops = st["loops"]
    assert len(loops) == 1
    assert loops[0]["state"] == "closed"
    assert loops[0]["exit_reason"] == "converged"


def test_loop_close_exhausted(tmp_path):
    """loop_close with exit_reason=exhausted -> closed, exit_reason reflected."""
    lg = Ledger(tmp_path / "run")
    _loop_open(lg, "spec.v1")
    _loop_close(lg, "spec.v1", exit_reason="exhausted")
    st = project_cognitive_state(_spec(), lg)
    assert st["loops"][0]["state"] == "closed"
    assert st["loops"][0]["exit_reason"] == "exhausted"


# --- exhaustion routing (state reads events _exhaust emits) ----------------

def test_exhaust_block_blocked(tmp_path):
    """on_exhausted=block -> _exhaust opens a gate_open (unresolved) ->
    derive_verdict == BLOCKED."""
    lg = Ledger(tmp_path / "run")
    # what _exhaust(mode=block) emits via _gate:
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
               "spec_version_id": "spec.v1", "node_id": "n1",
               "reason": "retries_exhausted"})
    # a VERIFIED claim exists, but the unresolved gate caps the verdict.
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True)
    assert derive_verdict(_spec(), lg) == "BLOCKED"


def test_exhaust_degrade_partial(tmp_path):
    """on_exhausted=degrade -> soft None (no gate); the claim stays un-VERIFIED
    -> derive_verdict == PARTIAL (required criterion unmet)."""
    lg = Ledger(tmp_path / "run")
    # degrade emits nothing -- the gap surfaces via the verdict machine.
    # C1 was refuted by a skeptic -> not VERIFIED -> required R1 unmet.
    _vv(lg, "spec.v1", "C1", refuted=True, survived=False)
    assert derive_verdict(_spec(), lg) == "PARTIAL"


def test_exhaust_replan_open_questions(tmp_path):
    """on_exhausted=replan -> _exhaust emits replan_requested -> checkpoint
    open_questions non-empty (the boundary signal for the next spec)."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    h._record({
        "event_id": h._ev_id(), "kind": "replan_requested",
        "spec_version_id": "spec.v1", "node_id": "n1",
        "payload": {"reason": "retries_exhausted"}, "claims": [],
    })
    cp = h.project_checkpoint(spec)
    oq = cp["open_questions"]
    assert any(q.get("status") == "replan_requested" for q in oq)


# --- dry_count re-derivation (invariant 6) --------------------------------

def test_dry_count_rederived(tmp_path):
    """dry_count is a RE-DERIVATION from events (verify_verdict/stagnating
    tagged loop_id+iteration), NOT the cached dry_count field on
    iteration_close. 2 dry rounds -> derive_dry_count(...,iteration=2) == 2."""
    lg = Ledger(tmp_path / "run")
    svid = "spec.v1"
    _loop_open(lg, svid, loop_id="L1")
    # iteration 1: dry (no verify_verdict survived=false, no stagnating)
    # iteration 2: dry
    # iteration_close events record dry_count IN PAYLOAD for audit -- the
    # projection must IGNORE that cached field and re-derive from events.
    lg.append({"event_id": "ic1", "kind": "iteration_close",
                "spec_version_id": svid,
                "payload": {"loop_id": "L1", "iteration": 1,
                            "condition_eval": "dry<2", "dry_count": 999}})
    lg.append({"event_id": "ic2", "kind": "iteration_close",
                "spec_version_id": svid,
                "payload": {"loop_id": "L1", "iteration": 2,
                            "condition_eval": "dry<2", "dry_count": 999}})
    # re-derived: 2 consecutive dry rounds ending at iteration=2.
    assert derive_dry_count(lg, svid, "L1", 2) == 2


def test_dry_count_reset_by_survived_false(tmp_path):
    """A verify_verdict with survived=false in iteration 2 -> iteration 2 is
    non-dry -> streak resets. iteration 3 dry -> dry_count == 1."""
    lg = Ledger(tmp_path / "run")
    svid = "spec.v1"
    _loop_open(lg, svid, loop_id="L1")
    # iteration 1: dry
    # iteration 2: verify_verdict survived=false -> non-dry (streak resets)
    _vv(lg, svid, "C1", refuted=True, survived=False,
        loop_id="L1", iteration=2, eid="vv2")
    # iteration 3: dry -> streak = 1
    assert derive_dry_count(lg, svid, "L1", 3) == 1


def test_dry_count_reset_by_stagnating(tmp_path):
    """A stagnating event in iteration 2 -> non-dry -> streak resets."""
    lg = Ledger(tmp_path / "run")
    svid = "spec.v1"
    _loop_open(lg, svid, loop_id="L1")
    _stagnating(lg, svid, loop_id="L1", iteration=2)
    # iteration 1: dry; iteration 2: stagnating -> reset; iteration 3: dry -> 1
    assert derive_dry_count(lg, svid, "L1", 3) == 1


def test_dry_count_no_loop_context(tmp_path):
    """loop_id=None or iteration=None -> dry_count == 0 (no loop context)."""
    lg = Ledger(tmp_path / "run")
    assert derive_dry_count(lg, "spec.v1", None, 3) == 0
    assert derive_dry_count(lg, "spec.v1", "L1", None) == 0


# --- last-write-wins across iterations (Q9) ---------------------------------

def test_last_write_wins_survived_then_refuted(tmp_path):
    """Q9 (brief's case): C1 verify_verdict survived=True at k=1, refuted=True
    at k=2 -> latest wins -> REFUTED."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True,
        loop_id="L1", iteration=1, eid="vv_k1")
    _vv(lg, "spec.v1", "C1", refuted=True, survived=False,
        loop_id="L1", iteration=2, eid="vv_k2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "REFUTED"


def test_last_write_wins_refuted_then_survived(tmp_path):
    """Q9 reverse (iterative refinement): C1 refuted at k=1, survived at k=2
    (agent fixed the defect) -> latest wins -> VERIFIED. v1's accumulation
    (refuted=True OR survived=True -> REFUTED) was WRONG for refinement; the
    spec says the LATEST verify_verdict IS the current truth."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "spec.v1", "C1", refuted=True, survived=False,
        loop_id="L1", iteration=1, eid="vv_k1")
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True,
        loop_id="L1", iteration=2, eid="vv_k2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"


def test_last_write_wins_three_iterations(tmp_path):
    """Q9 multi-iteration: C1 survived at k=1, refuted at k=2, survived at k=3
    -> latest (k=3) wins -> VERIFIED."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True,
        loop_id="L1", iteration=1, eid="vv_k1")
    _vv(lg, "spec.v1", "C1", refuted=True, survived=False,
        loop_id="L1", iteration=2, eid="vv_k2")
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True,
        loop_id="L1", iteration=3, eid="vv_k3")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"


# --- branch_decision is a fact (claim != fact, invariant 3) ----------------

def test_branch_decision_does_not_change_claim_status(tmp_path):
    """branch_decision events are deterministic predicate evaluations = FACTS
    (§4). derive_claim_status does NOT read them; a claim's status comes only
    from agent_result/verify_verdict. A branch_decision that mentions C1 must
    NOT flip C1 to VERIFIED/REFUTED."""
    lg = Ledger(tmp_path / "run")
    # C1 supported via an agent_result
    lg.append({"event_id": "E1", "kind": "agent_result",
               "spec_version_id": "spec.v1", "node_id": "worker1",
               "claims": [{"claim_id": "C1", "text": "t",
                           "strength": "supported"}]})
    # a branch_decision event that 'evaluated' C1's predicate to True --
    # this is a FACT, not a verify_verdict; it must NOT change C1's status.
    lg.append({"event_id": "E2", "kind": "branch_decision",
               "spec_version_id": "spec.v1", "node_id": "cond1",
               "payload": {"node_id": "cond1", "predicate": "claim_status(C1)",
                           "eval_result": True, "branch_taken": "then",
                           "degraded": False, "claim_strength": "verified"}})
    assert derive_claim_status("C1", lg, svid="spec.v1") == "SUPPORTED"


# --- svid-scoping (v1 #1 strict-semantic guard) ---------------------------

def test_svid_scoping_v1_verified_v2_failed(tmp_path):
    """v1 VERIFIED + v2 FAILED (refuted) in same ledger, different svid ->
    v2's verdict is NOT STALE-VERIFIED from v1. Loop events are svid-scoped;
    verify_verdict scoping is symmetric (v1 sees only v1, v2 sees only v2)."""
    lg = Ledger(tmp_path / "run")
    # spec.v1: C1 verified
    _vv(lg, "spec.v1", "C1", refuted=False, survived=True, eid="vv_v1")
    # spec.v2 (same run-dir): C1 refuted
    _vv(lg, "spec.v2", "C1", refuted=True, survived=False, eid="vv_v2")
    # v2's C1 is REFUTED (not stale-VERIFIED from v1)
    assert derive_claim_status("C1", lg, svid="spec.v2") == "REFUTED"
    # v1's C1 stays VERIFIED (v2's refutation does not leak back)
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"
    # derive_verdict for spec.v2 -> PARTIAL (R1 unmet: C1 REFUTED, not VERIFIED)
    spec_v2 = _spec(svid="spec.v2")
    assert derive_verdict(spec_v2, lg) == "PARTIAL"


def test_svid_scoping_loop_events(tmp_path):
    """Loop events are svid-scoped: a loop_open in spec.v1 must NOT appear as
    an active loop in spec.v2's cognitive_state projection."""
    lg = Ledger(tmp_path / "run")
    _loop_open(lg, "spec.v1", loop_id="L1")
    # spec.v1 sees the active loop
    st_v1 = project_cognitive_state(_spec(svid="spec.v1"), lg)
    assert any(l["state"] == "active" for l in st_v1["loops"])
    # spec.v2 sees NO loops (v1's loop is not v2's)
    st_v2 = project_cognitive_state(_spec(svid="spec.v2"), lg)
    assert st_v2["loops"] == []
