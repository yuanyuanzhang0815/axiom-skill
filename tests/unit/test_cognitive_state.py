from axiom.ledger import Ledger
from axiom.ir import Spec, Requirement
from axiom.state import project_cognitive_state


def _spec():
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"],
        nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def test_stagnating_progress(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "stagnating", "node_id": "n1", "payload": {}})
    st = project_cognitive_state(_spec(), lg)
    assert st["progress"] == "STAGNATING"


def test_gate_open_blocks(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1", "reason": "r"})
    st = project_cognitive_state(_spec(), lg)
    assert st["progress"] == "BLOCKED"


def test_progressing_default(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "payload": {}})
    st = project_cognitive_state(_spec(), lg)
    assert st["progress"] == "PROGRESSING"


def test_resolved_gate_not_blocked(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1", "reason": "r"})
    lg.append({"event_id": "E2", "kind": "gate_resolve", "gate_id": "G1", "decision": "allow"})
    st = project_cognitive_state(_spec(), lg)
    assert st["progress"] == "PROGRESSING"
