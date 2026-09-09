"""B2: evidence_for reverse provenance. decision_trace forward refs
(decision -> event) are projected by the harness onto each event as a derived
evidence_for list (event -> decisions). Agents cannot forge it."""
import json
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(result_str='{"x": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _spec(decision_trace):
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"], nodes={},
                control_flow={"type": "sequence", "steps": []},
                decision_trace=decision_trace, budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)


def test_evidence_for_attached_to_cited_event(tmp_path):
    # decision d1 cites event:ev_1 as evidence
    dt = [{"decision_id": "d1", "phase": "frame", "observation": "o",
           "evidence_refs": ["event:ev_1"]}]
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    h._build_evidence_for(_spec(dt))
    h._record({"event_id": "ev_1", "kind": "agent_result", "node_id": "n1",
               "payload": {}})
    ev = [e for e in h.ledger.events() if e["event_id"] == "ev_1"][0]
    assert ev["evidence_for"] == ["d1"]


def test_evidence_for_empty_when_uncited(tmp_path):
    dt = [{"decision_id": "d1", "phase": "frame", "observation": "o",
           "evidence_refs": ["event:ev_1"]}]
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    h._build_evidence_for(_spec(dt))
    h._record({"event_id": "ev_2", "kind": "agent_result", "node_id": "n2",
               "payload": {}})
    ev = [e for e in h.ledger.events() if e["event_id"] == "ev_2"][0]
    assert ev["evidence_for"] == []


def test_evidence_for_cannot_be_forged_by_agent(tmp_path):
    # even if an event tries to set evidence_for, the harness overwrites it
    dt = [{"decision_id": "d_real", "phase": "frame", "observation": "o",
           "evidence_refs": ["event:ev_1"]}]
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    h._build_evidence_for(_spec(dt))
    h._record({"event_id": "ev_1", "kind": "agent_result", "node_id": "n1",
               "payload": {}, "evidence_for": ["FORGED"]})
    ev = [e for e in h.ledger.events() if e["event_id"] == "ev_1"][0]
    assert ev["evidence_for"] == ["d_real"]


def test_bidirectional_provenance_round_trip(tmp_path):
    # forward: decision -> event; reverse: event.evidence_for -> decision
    dt = [{"decision_id": "d1", "phase": "decompose", "observation": "o",
           "alternatives_rejected": [{"option": "x", "reason": "r"}],
           "evidence_refs": ["event:ev_1", "event:ev_2"]}]
    spec = _spec(dt)
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    h._build_evidence_for(spec)
    h._record({"event_id": "ev_1", "kind": "agent_result", "node_id": "n1", "payload": {}})
    h._record({"event_id": "ev_2", "kind": "agent_result", "node_id": "n2", "payload": {}})
    ev1 = [e for e in h.ledger.events() if e["event_id"] == "ev_1"][0]
    ev2 = [e for e in h.ledger.events() if e["event_id"] == "ev_2"][0]
    assert ev1["evidence_for"] == ["d1"]
    assert ev2["evidence_for"] == ["d1"]
    # ledger.derive_reverse_index(spec) agrees (canonical forward rebuild)
    assert h.ledger.derive_reverse_index(spec) == {"ev_1": ["d1"], "ev_2": ["d1"]}
