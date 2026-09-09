"""A3: failure_policy on_exhausted=replan emits a replan_requested boundary
signal (no Gate, soft None); the orchestrator sees it in checkpoint
open_questions to draw the next spec. Not a synchronous callback."""
import json
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(result_str="just prose, no json"):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": 0.01,
        "permission_denials": [], "usage": {},
    })


def _spec():
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"], nodes={},
                control_flow={"type": "sequence", "steps": []}, decision_trace=[],
                budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1)


def _replan_node():
    return {
        "type": "agent", "id": "n_find", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object",
                          "properties": {"z": {"type": "integer"}},
                          "required": ["z"]},
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"],
        "failure_policy": {"max_retries": 0, "retry_guard": "requires_new_evidence",
                           "on_exhausted": "replan"},
    }


def test_replan_exhaust_emits_replan_requested_no_gate(tmp_path):
    def runner(args, cwd=None):
        return (0, _env())  # prose -> schema fail every time
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._run_agent_with_retry(_replan_node(), {}, "spec.v1", _spec(),
                                 localized=False)
    assert out is None  # soft
    kinds = [e.get("kind") for e in h.ledger.events()]
    assert "replan_requested" in kinds
    assert "gate_open" not in kinds  # replan never opens a Gate


def test_replan_request_surfaces_in_checkpoint_open_questions(tmp_path):
    def runner(args, cwd=None):
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    spec = _spec()
    h._build_evidence_for(spec)
    h._run_agent_with_retry(_replan_node(), {}, "spec.v1", spec, localized=False)
    cp = h.project_checkpoint(spec)
    assert any(q["ref"] == "replan_requested:n_find" for q in cp["open_questions"])


def test_block_still_gates_and_replan_does_not(tmp_path):
    # contrast: block opens a gate; replan does not
    def runner(args, cwd=None):
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {**_replan_node(),
            "failure_policy": {"max_retries": 0, "on_exhausted": "block"}}
    h._run_agent_with_retry(node, {}, "spec.v1", _spec(), localized=False)
    assert any(e.get("kind") == "gate_open" for e in h.ledger.events())
