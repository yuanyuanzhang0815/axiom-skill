from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.ledger import Ledger


def _spec():
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"], nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def test_checkpoint_is_projection(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "verify_verdict",
                     "claim_id": "C1", "refuted": False, "survived": True})
    cp = h.project_checkpoint(_spec())
    assert cp["verdict"] == "VERIFIED"
    assert "E1" in cp["evidence_summary"]


def test_checkpoint_has_all_keys(tmp_path):
    h = Harness(tmp_path / "run")
    cp = h.project_checkpoint(_spec())
    for k in ("verdict", "synthesized_output", "evidence_summary",
              "blocked_items", "drift", "open_questions"):
        assert k in cp


def test_checkpoint_detects_contract_drift(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
                     "reason": "contract_drift"})
    cp = h.project_checkpoint(_spec())
    assert cp["drift"]["contract_drift"] is True


def test_checkpoint_no_drift_when_clean(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result", "payload": {}})
    cp = h.project_checkpoint(_spec())
    assert cp["drift"]["contract_drift"] is False


def test_checkpoint_blocked_when_gate_open(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
                     "reason": "risky"})
    h.ledger.append({"event_id": "E2", "kind": "verify_verdict",
                     "claim_id": "C1", "refuted": False, "survived": True})
    cp = h.project_checkpoint(_spec())
    assert cp["verdict"] == "BLOCKED"
