import json
from axiom.harness import Harness


class _SpecStub:
    """Minimal spec-like object for retry/stagnation tests."""
    spec_version_id = "spec.v1"
    max_stagnation = 1
    budget_usd = 5.0
    max_agents = 1000


def _agent_node(max_retries=5, guard="requires_new_evidence"):
    return {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries, "retry_guard": guard,
            "on_exhausted": "block",
        },
    }


def _denial_runner():
    """Always returns a cognitive failure (WebSearch denied), identical each call."""
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        }))
    return runner


def test_stagnating_stops_after_budget(tmp_path):
    # identical cognitive failure each retry -> no cognitive delta -> STAGNATING
    # -> honest exit after max_stagnation (does NOT burn max_retries)
    h = Harness(tmp_path / "run", worker_runner=_denial_runner())
    out = h._dispatch_with_retry(_agent_node(max_retries=5), {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(e.get("kind") == "stagnating" for e in h.ledger.events())


def test_delta_allows_retry(tmp_path):
    # first a denial, then a clean success with a different output -> cognitive
    # delta -> not stagnation -> succeeds
    seq = [
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        }),
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 2}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }),
    ]
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0, line)

    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(_agent_node(max_retries=3), {}, "spec.v1", _SpecStub())
    assert out is not None
    assert out["validated_output"] == {"x": 2}


def test_operational_failure_retries_then_exhausts(tmp_path):
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        return (1, "")  # operational failure, no result line

    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(_agent_node(max_retries=2), {}, "spec.v1", _SpecStub())
    assert out is None
    # attempts 0,1,2 -> 3 calls (while attempts <= max_retries=2)
    assert calls["n"] == 3


def test_different_denials_are_not_stagnation(tmp_path):
    # two cognitive failures but with DIFFERENT denial sets -> delta -> no
    # stagnating event, retries until exhausted -> None (on_exhausted=block)
    seq = [
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        }),
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["Glob"], "usage": {},
        }),
    ]
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0, line)

    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(_agent_node(max_retries=1), {}, "spec.v1", _SpecStub())
    assert out is None
    # different denial sets -> no stagnating event logged
    assert not any(e.get("kind") == "stagnating" for e in h.ledger.events())


def test_unretriable_auth_opens_gate(tmp_path):
    # an unauthenticated worker session: exit 0 + is_error=true + "Not logged in"
    # -> classify unretriable -> Gate (NOT retried, NOT stagnation)
    def auth_fail_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "is_error": True, "result": "Not logged in · Please run /login",
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=auth_fail_runner)
    out = h._dispatch_with_retry(_agent_node(max_retries=3), {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(
        e.get("kind") == "gate_open"
        and e.get("reason") == "unretriable_failure"
        for e in h.ledger.events()
    )
