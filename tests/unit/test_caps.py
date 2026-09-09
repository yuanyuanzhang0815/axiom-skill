import json
from axiom.harness import Harness


class _SpecStub:
    spec_version_id = "spec.v1"
    max_stagnation = 1
    budget_usd = 5.0
    max_agents = 1000


def _cost_runner(cost=0.01):
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": cost,
            "permission_denials": [], "usage": {},
        }))
    return runner


def _agent_node():
    return {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 3, "retry_guard": "requires_new_evidence",
                           "on_exhausted": "block"},
    }


def test_budget_exhausted_is_honest_exit(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_cost_runner(0.01))
    spec = type("S", (), {"spec_version_id": "spec.v1", "max_stagnation": 1,
                          "budget_usd": 0.005, "max_agents": 1000})()
    out = h._dispatch_with_retry(_agent_node(), {}, "spec.v1", spec)
    assert out is None
    assert any(e.get("kind") == "budget_exhausted" for e in h.ledger.events())


def test_check_caps_budget_exceeded(tmp_path):
    h = Harness(tmp_path / "run")
    h._cost_total = 10.0
    spec = _SpecStub()
    assert h._check_caps(spec) is True
    assert any(e.get("kind") == "budget_exhausted" for e in h.ledger.events())


def test_check_caps_agent_count_exceeded(tmp_path):
    h = Harness(tmp_path / "run")
    h._agent_count = 1001
    spec = _SpecStub()
    assert h._check_caps(spec) is True
    assert any(e.get("kind") == "agent_cap_exhausted" for e in h.ledger.events())


def test_check_caps_under_limits(tmp_path):
    h = Harness(tmp_path / "run")
    h._cost_total = 0.01
    h._agent_count = 1
    assert h._check_caps(_SpecStub()) is False


def test_check_caps_none_spec_noop(tmp_path):
    h = Harness(tmp_path / "run")
    h._cost_total = 1e9
    assert h._check_caps(None) is False


def test_conform_dispatch_records_result_when_budget_trips(tmp_path):
    """A real relay's n4 fix: the worker ran, conformed, and edited the file, but
    budget tripped in _tally AFTER the run. The old _tally-bail-before-_record
    form discarded the completed work (no agent_result event). _record must run
    BEFORE the capped bail so the ledger keeps the work that already cost money.
    """
    h = Harness(tmp_path / "run", worker_runner=_cost_runner(0.10))  # 0.10 > budget 0.05
    spec = type("S", (), {"spec_version_id": "spec.v1", "max_stagnation": 1,
                          "budget_usd": 0.05, "max_agents": 1000})()
    out = h._dispatch_with_retry(_agent_node(), {}, "spec.v1", spec)
    assert out is None, "budget-tripped dispatch does not flow downstream"
    assert any(e.get("kind") == "agent_result" and e.get("node_id") == "n1"
               for e in h.ledger.events()), \
        "a conforming dispatch that tripped budget must still record agent_result"
    assert any(e.get("kind") == "budget_exhausted" for e in h.ledger.events())
