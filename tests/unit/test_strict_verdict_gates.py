"""strict-verdict-gates: CONFLICTED claim semantics + CLAIM_RESOLVED event.

Adopts the stricter gating from the original orchestrator design. The old
derive_claim_status used last-write-wins, which erased the conflict signal of
"different verify nodes giving contradictory verdicts on the same claim" --
a real information loss. This change:
  1. CONFLICTED: different verify nodes (different node_id) giving contradictory
     verdicts on the same claim (one survived, one refuted) -> CONFLICTED. The
     same verify node across iterations with survived+refuted is iterative
     refinement (Q9 defect closure), which keeps last-write-wins, not a conflict.
  2. CLAIM_RESOLVED: a human adjudication event (axiom M3 grounded resolution).
     Only CONFLICTED -> SUPPORTED (other states unchanged). A human cannot
     directly stamp VERIFIED (prevents self-deception); conflict resolution
     reaches SUPPORTED, and still requires verify to reach VERIFIED.

No freshness: the verify_verdict is a terminal anchor (the verdict is cast
unless a new verdict/CLAIM_RESOLVED arrives, otherwise it does not move) --
more deterministic and more replayable than freshness decay; svid scope +
last-write-wins already covers cross-version staleness. Forcing freshness
would break determinism with no real failure mode.
"""
from axiom.ledger import Ledger
from axiom.state import derive_claim_status, derive_verdict, derive_unmet_requirements
from axiom.ir import Spec, Requirement


def _spec(svid="spec.v1", ses=None, reqs=None):
    return Spec(
        spec_version_id=svid, parent_spec_id=None, revision=1,
        intent="strict-verdict-gates test",
        requirements=reqs or [Requirement("R1", "criterion", "required")],
        boundaries=[], success_evidence=ses or ["S1:R1=claim:C1"],
        nodes={}, control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _vv(lg, claim, node_id, survived=False, refuted=False, eid=None, svid="spec.v1"):
    """Append a verify_verdict from a specific verify node."""
    lg.append({"event_id": eid or f"vv_{node_id}_{claim}",
               "kind": "verify_verdict", "spec_version_id": svid,
               "node_id": node_id, "claim_id": claim,
               "survived": survived, "refuted": refuted})


def _cr(lg, claim, eid=None, svid="spec.v1", basis_ref="evidence:E1"):
    """Append a CLAIM_RESOLVED event (human grounds the resolution)."""
    lg.append({"event_id": eid or f"cr_{claim}", "kind": "claim_resolved",
               "spec_version_id": svid, "claim_id": claim,
               "basis_ref": basis_ref, "resolved_by": "orchestrator"})


# --- CONFLICTED: different verify nodes contradict ---

def test_conflicted_different_nodes_survived_and_refuted(tmp_path):
    """Two different verify nodes contradict on the same claim (v1 survived +
    v2 refuted) -> CONFLICTED, not last-write-wins erasing the conflict signal."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"


def test_conflicted_reverse_nodes(tmp_path):
    """Order-independent: v1 refuted + v2 survived is also CONFLICTED."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", refuted=True)
    _vv(lg, "C1", "verify_b", survived=True)
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"


def test_no_conflict_same_node_iterations(tmp_path):
    """Same verify node across iterations (k1 refuted, k2 survived) ->
    last-write-wins -> VERIFIED (iterative refinement Q9 defect closure,
    not a conflict)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", refuted=True, eid="vv_k1")
    _vv(lg, "C1", "verify_a", survived=True, eid="vv_k2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"


def test_no_conflict_anonymous_node_single_direction(tmp_path):
    """Multiple same-direction verifies with no node_id (anonymous nodes) ->
    last-write-wins -> VERIFIED."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", None, survived=True, eid="vv1")
    _vv(lg, "C1", None, survived=True, eid="vv2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"


def test_no_conflict_single_refuted(tmp_path):
    """A single verify refuted -> REFUTED (does not report CONFLICTED)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", refuted=True)
    assert derive_claim_status("C1", lg, svid="spec.v1") == "REFUTED"


def test_challenged_when_neither_survived_nor_refuted(tmp_path):
    """Verify ran but both survived/refuted are false -> CHALLENGED (not
    overridden by CONFLICTED)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=False, refuted=False)
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CHALLENGED"


# --- CONFLICTED -> verdict PARTIAL (not BLOCKED) ---

def test_conflicted_drives_verdict_partial(tmp_path):
    """A CONFLICTED claim bound to a required R -> derive_verdict PARTIAL (not
    BLOCKED; BLOCKED is only driven by an unresolved gate, separation of
    concerns)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    assert derive_verdict(_spec(), lg) == "PARTIAL"


# --- derive_unmet_requirements shows claim_conflicted ---

def test_unmet_shows_claim_conflicted(tmp_path):
    """A CONFLICTED claim bound to a required R -> derive_unmet_requirements
    produces status='claim_conflicted' (diagnosis consistent with the verdict
    mirror contract)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    unmet = derive_unmet_requirements(_spec(), lg)
    assert len(unmet) == 1
    assert unmet[0]["status"] == "claim_conflicted"


# --- CLAIM_RESOLVED: only CONFLICTED -> SUPPORTED (axiom M3) ---

def test_claim_resolved_conflicted_to_supported(tmp_path):
    """CONFLICTED + CLAIM_RESOLVED -> SUPPORTED (conflict resolved; still not
    VERIFIED, requires verify to reach terminal state). A human cannot
    directly stamp VERIFIED (prevents self-deception)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    _cr(lg, "C1")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "SUPPORTED"


def test_claim_resolved_only_on_conflicted_verified_unchanged(tmp_path):
    """CLAIM_RESOLVED only applies to CONFLICTED; a VERIFIED claim +
    CLAIM_RESOLVED -> unchanged VERIFIED (a human cannot change the status of
    an already-VERIFIED claim)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _cr(lg, "C1")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "VERIFIED"


def test_supported_after_resolve_still_verdict_partial(tmp_path):
    """CONFLICTED -> CLAIM_RESOLVED -> SUPPORTED, still not VERIFIED ->
    derive_verdict PARTIAL (human adjudication resolves the conflict but does
    not directly complete; requires verify to reach VERIFIED)."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    _cr(lg, "C1")
    # claim is SUPPORTED now (not VERIFIED) -> required_unmet -> PARTIAL
    assert derive_verdict(_spec(), lg) == "PARTIAL"


# --- post-resolution re-conflict (iteration on the sticky-mask edge) ---

def test_post_resolution_new_refute_re_conflicted(tmp_path):
    """CLAIM_RESOLVED is TEMPORAL: a new verify that re-contradicts AFTER the
    resolution re-surfaces CONFLICTED (the resolution does not stickily mask
    new evidence). The human must re-resolve with fresh basis_ref."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)   # idx 0
    _vv(lg, "C1", "verify_b", refuted=True)    # idx 1 -> CONFLICTED
    _cr(lg, "C1")                               # idx 2 -> SUPPORTED (resolves)
    _vv(lg, "C1", "verify_c", refuted=True)     # idx 3 -> new refute, re-CONFLICTED
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"


def test_post_resolution_new_survived_two_node_re_conflicted(tmp_path):
    """After resolution, a new SURVIVED verify from a fresh node still leaves
    the claim CONFLICTED (verify_b's old refutation + verify_c's survive across
    distinct nodes). Resolution is not a blanket clear of pre-resolution verdicts."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    _cr(lg, "C1")                                # -> SUPPORTED
    _vv(lg, "C1", "verify_c", survived=True)     # new survive, but b still refutes
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"


def test_re_resolve_after_re_conflict_clears_again(tmp_path):
    """After a post-resolution re-conflict, a NEW CLAIM_RESOLVED (fresher than
    the latest verify) clears it again to SUPPORTED -- the human can always
    re-resolve with a newer basis_ref."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True)
    _vv(lg, "C1", "verify_b", refuted=True)
    _cr(lg, "C1", eid="cr1")                      # -> SUPPORTED
    _vv(lg, "C1", "verify_c", refuted=True)       # -> re-CONFLICTED
    _cr(lg, "C1", eid="cr2")                      # idx after verify_c -> SUPPORTED again
    assert derive_claim_status("C1", lg, svid="spec.v1") == "SUPPORTED"


def test_resolution_before_any_verify_not_sticky(tmp_path):
    """A claim_resolved that predates all verify_verdicts does NOT stickily
    mask a later conflict (resolution_idx < latest_verify_idx)."""
    lg = Ledger(tmp_path / "run")
    _cr(lg, "C1", eid="cr_pre")                  # idx 0, before any verify
    _vv(lg, "C1", "verify_a", survived=True)      # idx 1
    _vv(lg, "C1", "verify_b", refuted=True)       # idx 2 -> CONFLICTED, NOT masked
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"


# --- svid scoping ---

def test_conflicted_svid_scoped(tmp_path):
    """CONFLICTED is also svid-scoped: v1's cross-node contradiction does not
    leak into v2."""
    lg = Ledger(tmp_path / "run")
    _vv(lg, "C1", "verify_a", survived=True, svid="spec.v1")
    _vv(lg, "C1", "verify_b", refuted=True, svid="spec.v1")
    _vv(lg, "C1", "verify_a", survived=True, svid="spec.v2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED"
    assert derive_claim_status("C1", lg, svid="spec.v2") == "VERIFIED"


def test_claim_resolved_svid_scoped(tmp_path):
    """CLAIM_RESOLVED is svid-scoped: v1 CONFLICTED+CLAIM_RESOLVED->SUPPORTED;
    v2 CONFLICTED but no claim_resolved -> stays CONFLICTED (v1's human
    adjudication does not leak across svid)."""
    lg = Ledger(tmp_path / "run")
    # v1: conflict + resolution
    _vv(lg, "C1", "verify_a", survived=True, svid="spec.v1")
    _vv(lg, "C1", "verify_b", refuted=True, svid="spec.v1")
    _cr(lg, "C1", svid="spec.v1")
    # v2: conflict, no resolution
    _vv(lg, "C1", "verify_a", survived=True, svid="spec.v2")
    _vv(lg, "C1", "verify_b", refuted=True, svid="spec.v2")
    assert derive_claim_status("C1", lg, svid="spec.v1") == "SUPPORTED"
    assert derive_claim_status("C1", lg, svid="spec.v2") == "CONFLICTED"
