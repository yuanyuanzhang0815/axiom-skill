import json
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _ok_runner(args, cwd=None):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _agent(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }


def _spec(nodes, steps, se=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=se or [], nodes=nodes,
        control_flow={"type": "sequence", "steps": steps},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def test_sequence_runs_nodes_threads_output(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({
        "n1": _agent("n1"),
        "n2": {
            "type": "synthesize", "id": "n2",
            "inputs": ["{{n1.validated_output}}"],
            "output_schema": {"type": "object"}, "acceptance": ["a"],
        },
    }, ["n1", "n2"])
    out = h.run(spec)
    assert "validated_output" in out
    # synthesize received n1's threaded output
    assert out["validated_output"] == {"answer": "42"}


def test_parallel_list_in_sequence(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"a": _agent("a"), "b": _agent("b")}, [["parallel", "a", "b"]])
    out = h.run(spec)
    assert "validated_output" in out
    assert len(out["validated_output"]) == 2


def test_run_rejects_invalid_spec(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # node missing output_schema / acceptance / failure_policy / write_areas
    spec = _spec({"n1": {"type": "agent", "id": "n1", "prompt": "p"}}, ["n1"])
    try:
        h.run(spec)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_empty_steps_returns_empty_output(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"n1": _agent("n1")}, [])
    out = h.run(spec)
    assert out == {"validated_output": {}}
