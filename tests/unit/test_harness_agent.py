import json
from axiom.harness import Harness


def _ok_runner(args, cwd=None):
    line = json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"files": ["a.py"]}),
        "session_id": "s1", "total_cost_usd": 0.01,
        "permission_denials": [], "usage": {},
    })
    return (0, line)


def test_agent_returns_validated_output(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "find files", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"files": {"type": "array", "items": {"type": "string"}}},
            "required": ["files"],
        },
        "allowed_tools": ["Glob"], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out["validated_output"] == {"files": ["a.py"]}
    # type boundary: no raw stdout leaks downstream
    assert "raw_stdout" not in out
    assert "result_text" not in out


def test_agent_result_logged_to_ledger(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    evs = h.ledger.events()
    assert any(e["kind"] == "agent_result" and e["node_id"] == "n1" for e in evs)
    assert evs[-1]["payload"]["cost_usd"] == 0.01


def test_agent_failure_returns_none(tmp_path):
    def fail_runner(args, cwd=None):
        return (1, "")  # operational failure, no result line

    h = Harness(tmp_path / "run", worker_runner=fail_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None


def test_cognitive_failure_returns_none(tmp_path):
    def denial_runner(args, cwd=None):
        line = json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s1", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        })
        return (0, line)

    h = Harness(tmp_path / "run", worker_runner=denial_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None  # cognitive failure (denials) -> None, not validated
