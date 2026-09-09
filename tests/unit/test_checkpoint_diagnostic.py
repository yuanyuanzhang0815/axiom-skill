from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _spec(success, nodes, svid="spec.v1"):
    return Spec(
        spec_version_id=svid, parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=success, nodes=nodes,
        control_flow={"type": "sequence", "steps": []}, decision_trace=[],
        budget_usd=20.0, max_concurrent=16, max_agents=1000, max_stagnation=1,
    )


# --- #7: PARTIAL checkpoint must diagnose which R is unmet and why --------

def test_partial_lists_unmet_agent_failed(tmp_path):
    # n5 emitted agent_verdict FAILED -> R1 unmet -> PARTIAL; the checkpoint's
    # open_questions must name R1 + node:n5 + agent_verdict_FAILED (not empty,
    # which forced the docx-review relay to `axiom journal` to find it).
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_verdict",
                     "spec_version_id": "spec.v1", "node_id": "n5",
                     "verdict": "FAILED",
                     "payload": {"verdict_field": "verdict"}, "claims": []})
    h.ledger.seal()
    spec = _spec(["S1:R1=node:n5"], {"n5": {"type": "agent", "id": "n5",
                                             "verdict_field": "verdict"}})
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "PARTIAL"
    unmet = [q for q in cp["open_questions"] if q.get("requirement_id") == "R1"]
    assert len(unmet) == 1
    assert unmet[0]["status"] == "agent_verdict_FAILED"
    assert unmet[0]["binding"] == "node:n5"


def test_partial_lists_unmet_claim_proposed(tmp_path):
    # C1 never produced (no agent_result with the claim) -> PROPOSED -> R1 unmet
    h = Harness(tmp_path / "run")
    h.ledger.seal()
    spec = _spec(["S1:R1=claim:C1"], {})
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "PARTIAL"
    unmet = [q for q in cp["open_questions"] if q.get("requirement_id") == "R1"]
    assert len(unmet) == 1
    assert unmet[0]["status"] == "claim_proposed"


def test_blocked_lists_gate_no_need_to_gate_list(tmp_path):
    # #7: BLOCKED checkpoint's blocked_items lists the unresolved gate so the
    # orchestrator resolves directly (gate_id + node_id + reason), no journal.
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "gate_open",
                     "spec_version_id": "spec.v1", "gate_id": "G_n1_2",
                     "node_id": "n1", "reason": "risk_high"})
    h.ledger.seal()
    spec = _spec(["S1:R1=node:n5"], {"n5": {"type": "agent", "id": "n5",
                                             "verdict_field": "verdict"}})
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "BLOCKED"
    assert len(cp["blocked_items"]) == 1
    assert cp["blocked_items"][0]["gate_id"] == "G_n1_2"
    assert cp["blocked_items"][0]["node_id"] == "n1"


# --- #1 continuation: project_checkpoint svid-scoped (v2 does not inherit v1)

def test_v2_does_not_inherit_v1_gate_or_unmet(tmp_path):
    # v1 had an open gate + FAILED agent_verdict; v2 has VERIFIED agent_verdict.
    # v2 must project VERIFIED with empty blocked_items + open_questions
    # (does not inherit v1's gate or v1's unmet evidence).
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "gate_open",
                     "spec_version_id": "spec.v1", "gate_id": "G1",
                     "node_id": "n1", "reason": "risk_high"})
    h.ledger.append({"event_id": "E2", "kind": "agent_verdict",
                     "spec_version_id": "spec.v2", "node_id": "n5",
                     "verdict": "VERIFIED",
                     "payload": {"verdict_field": "verdict"}, "claims": []})
    h.ledger.seal()
    spec = _spec(["S1:R1=node:n5"], {"n5": {"type": "agent", "id": "n5",
                                             "verdict_field": "verdict"}},
                 svid="spec.v2")
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "VERIFIED"
    assert cp["blocked_items"] == []   # v1's gate not inherited
    assert cp["open_questions"] == []  # VERIFIED -> no unmet; no v1 CHALLENGED


def test_v2_partial_diagnoses_v2_evidence_only(tmp_path):
    # v1 VERIFIED + v2 FAILED in same ledger; spec.v2 PARTIAL must diagnose
    # v2's FAILED (not be masked by v1's VERIFIED -- #1 stale-leak, projected
    # through the #7 diagnostic).
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_verdict",
                     "spec_version_id": "spec.v1", "node_id": "n5",
                     "verdict": "VERIFIED",
                     "payload": {"verdict_field": "verdict"}, "claims": []})
    h.ledger.append({"event_id": "E2", "kind": "agent_verdict",
                     "spec_version_id": "spec.v2", "node_id": "n5",
                     "verdict": "FAILED",
                     "payload": {"verdict_field": "verdict"}, "claims": []})
    h.ledger.seal()
    spec = _spec(["S1:R1=node:n5"], {"n5": {"type": "agent", "id": "n5",
                                             "verdict_field": "verdict"}},
                 svid="spec.v2")
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "PARTIAL"
    unmet = [q for q in cp["open_questions"] if q.get("requirement_id") == "R1"]
    assert len(unmet) == 1
    assert unmet[0]["status"] == "agent_verdict_FAILED"  # v2's, not v1's VERIFIED
