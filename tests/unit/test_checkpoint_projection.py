"""B1: checkpoint projection truth. synthesized_output / cost / open_questions."""
from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.ledger import Ledger


def _spec(nodes=None, decision_trace=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"], nodes=nodes or {},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=decision_trace or [], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def test_checkpoint_synthesized_output_from_last_conforming_synthesize(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result",
                     "spec_version_id": "spec.v1", "node_id": "n_synth",
                     "payload": {"validated_output": {"report": "v1"},
                                 "cost_usd": 0.1}})
    spec = _spec(nodes={"n_synth": {"type": "synthesize", "id": "n_synth",
                                    "inputs": [], "output_schema": {"type": "object"}}})
    cp = h.project_checkpoint(spec)
    assert cp["synthesized_output"] == {"report": "v1"}


def test_checkpoint_synthesized_output_empty_when_missing(tmp_path):
    h = Harness(tmp_path / "run")
    spec = _spec()
    cp = h.project_checkpoint(spec)
    assert cp["synthesized_output"] == {}


def test_checkpoint_synthesized_output_last_wins(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result",
                     "node_id": "n_synth",
                     "payload": {"validated_output": {"report": "old"},
                                 "cost_usd": 0.1}})
    h.ledger.append({"event_id": "E2", "kind": "agent_result",
                     "node_id": "n_synth",
                     "payload": {"validated_output": {"report": "new"},
                                 "cost_usd": 0.2}})
    spec = _spec(nodes={"n_synth": {"type": "synthesize"}})
    cp = h.project_checkpoint(spec)
    assert cp["synthesized_output"] == {"report": "new"}


def test_checkpoint_cost_and_dispatch_count(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result",
                     "node_id": "n1", "payload": {"cost_usd": 0.3}})
    h.ledger.append({"event_id": "E2", "kind": "agent_result",
                     "node_id": "n2", "payload": {"cost_usd": 0.5}})
    h.ledger.append({"event_id": "E3", "kind": "verify_verdict",
                     "claim_id": "C1", "survived": True})
    cp = h.project_checkpoint(_spec())
    assert cp["cost_usd_total"] == 0.8
    assert cp["dispatch_count"] == 2


def test_checkpoint_counts_stagnating_failed_retry(tmp_path):
    # a stagnating event is a real host dispatch with real cost -- it must
    # be counted in both cost_usd_total and dispatch_count (regression: the
    # real A2A run lost the synthesize retry's cost because stagnating had none).
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result",
                     "node_id": "n1", "payload": {"cost_usd": 0.4}})
    h.ledger.append({"event_id": "E2", "kind": "stagnating",
                     "node_id": "n1", "payload": {"cost_usd": 0.25,
                     "attempt": 2, "conforms": False}})
    cp = h.project_checkpoint(_spec())
    assert cp["cost_usd_total"] == 0.65  # 0.4 + 0.25, not just 0.4
    assert cp["dispatch_count"] == 2     # agent_result + stagnating


def test_checkpoint_open_questions_from_challenged_claim(tmp_path):
    h = Harness(tmp_path / "run")
    # a CHALLENGED claim (neither survived nor refuted -> unverifiable) is an
    # open question the next graph must resolve.
    h.ledger.append({"event_id": "E1", "kind": "verify_verdict",
                     "claim_id": "C9", "refuted": False, "survived": False})
    cp = h.project_checkpoint(_spec())
    assert any(q.get("ref") and "C9" in q["ref"] for q in cp["open_questions"])


def test_checkpoint_open_questions_empty_on_clean_verify(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "verify_verdict",
                     "claim_id": "C1", "refuted": False, "survived": True})
    cp = h.project_checkpoint(_spec())
    assert cp["open_questions"] == []


def test_checkpoint_open_questions_from_replan_requested(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "replan_requested",
                     "node_id": "n_find", "payload": {"reason": "stagnation_exhausted"}})
    cp = h.project_checkpoint(_spec())
    assert any(q["ref"] == "replan_requested:n_find" for q in cp["open_questions"])


def test_checkpoint_cost_breakdown_bl6(tmp_path):
    # BL-6: split cost_usd_total into successful/failed so budget_exhausted is
    # coherent next to it. Successful = agent_result (clean or recovered_from_
    # prose -- both record usable work). Failed = stagnating + cognitive_attempt
    # (real spend, no usable output). Money spent on a failed dispatch is a fact.
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "E1", "kind": "agent_result",
                     "node_id": "n_impl", "payload": {"cost_usd": 2.84}})
    h.ledger.append({"event_id": "E2", "kind": "stagnating",
                     "node_id": "n_verify",
                     "payload": {"cost_usd": 3.96, "attempt": 2,
                                 "conforms": False}})
    h.ledger.append({"event_id": "E3", "kind": "cognitive_attempt",
                     "node_id": "n_verify",
                     "payload": {"cost_usd": 1.10, "attempt": 3,
                                 "conforms": False}})
    h.ledger.append({"event_id": "E4", "kind": "agent_result",
                     "node_id": "n_verify", "payload": {"cost_usd": 3.53}})
    cp = h.project_checkpoint(_spec())
    assert cp["cost_usd_total"] == round(2.84 + 3.96 + 1.10 + 3.53, 6)
    assert cp["successful_cost_usd"] == round(2.84 + 3.53, 6)
    assert cp["failed_cost_usd"] == round(3.96 + 1.10, 6)
    assert cp["dispatch_count"] == 4
    assert cp["dispatch_attempts"] == 4
    assert cp["successful_dispatches"] == 2
    assert cp["failed_dispatches"] == 2
    # recovered_from_prose's agent_result counts as successful (work was done; recovery is the purpose of the spend)
    h2 = Harness(tmp_path / "run2")
    h2.ledger.append({"event_id": "E1", "kind": "agent_result",
                      "node_id": "n_impl",
                      "payload": {"cost_usd": 1.0, "recovered_from_prose": True,
                                  "validated_output": {"_recovered": True}}})
    cp2 = h2.project_checkpoint(_spec())
    assert cp2["successful_cost_usd"] == 1.0
    assert cp2["failed_cost_usd"] == 0.0
