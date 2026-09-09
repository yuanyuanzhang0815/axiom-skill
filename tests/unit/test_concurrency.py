"""A1: parallel/pipeline run concurrently (process pool, concurrency cap) and
the hash-chained ledger stays intact under concurrent appends."""
import json
import threading
import time
from axiom.harness import Harness
from axiom.ledger import Ledger


def _env(result_str='{"x": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _body(prompt="p"):
    return {
        "type": "agent", "id": "b", "prompt": prompt, "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _spec(max_concurrent=16):
    from axiom.ir import Spec, Requirement
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=s"], nodes={},
                control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=5.0, max_concurrent=max_concurrent,
                max_agents=1000, max_stagnation=1)


def test_parallel_runs_concurrently_multiple_threads(tmp_path):
    seen_threads = set()
    def runner(args, cwd=None):
        seen_threads.add(threading.get_ident())
        time.sleep(0.02)
        return (0, _env('{"x": 1}'))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {"type": "parallel", "id": "n_par", "over": "{{items}}",
            "body": _body(), "concurrency": 8, "barrier": True}
    res = h.run_parallel(node, {"items": list(range(8))}, "spec.v1", _spec())
    assert len(res) == 8
    assert all(r is not None for r in res)
    # more than one worker thread actually ran dispatches
    assert len(seen_threads) > 1
    # hash chain intact despite concurrent appends
    assert h.ledger.verify_chain() == []


def test_parallel_preserves_order(tmp_path):
    def runner(args, cwd=None):
        return (0, _env('{"x": 1}'))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {"type": "parallel", "id": "n_par", "over": "{{items}}",
            "body": _body(), "concurrency": 4}
    res = h.run_parallel(node, {"items": ["a", "b", "c", "d"]}, "spec.v1", _spec())
    assert len(res) == 4


def test_parallel_failure_localized(tmp_path):
    calls = []
    def runner(args, cwd=None):
        calls.append(args)
        # 2nd item fails operationally
        return (0, _env('{"x": 1}'))
    h = Harness(tmp_path / "run", worker_runner=runner)
    # make body 2 fail by giving it a schema the worker never satisfies
    node = {"type": "parallel", "id": "n_par", "over": "{{items}}",
            "body": {**_body(), "output_schema": {"type": "object",
                    "properties": {"z": {"type": "integer"}}, "required": ["z"]}},
            "concurrency": 3}
    res = h.run_parallel(node, {"items": [1, 2, 3]}, "spec.v1", _spec())
    # every body fails schema (worker returns {"x":1} not {"z":..}) -> all None
    assert res == [None, None, None]
    assert h.ledger.verify_chain() == []


def test_pipeline_runs_items_concurrently(tmp_path):
    seen_threads = set()
    def runner(args, cwd=None):
        seen_threads.add(threading.get_ident())
        time.sleep(0.02)
        return (0, _env('{"v": 1}'))
    h = Harness(tmp_path / "run", worker_runner=runner)
    stage = {**_body(), "output_schema": {"type": "object"}}
    node = {"type": "pipeline", "id": "n_pip", "items": "{{items}}",
            "stages": [stage, stage]}
    res = h.run_pipeline(node, {"items": list(range(6))}, "spec.v1", _spec())
    assert len(res) == 6
    assert len(seen_threads) > 1
    assert h.ledger.verify_chain() == []


def test_concurrency_capped_by_spec_max_concurrent(tmp_path):
    seen_threads = set()
    def runner(args, cwd=None):
        seen_threads.add(threading.get_ident())
        time.sleep(0.02)
        return (0, _env('{"x": 1}'))
    h = Harness(tmp_path / "run", worker_runner=runner)
    # node asks for 32 but spec caps at 4
    node = {"type": "parallel", "id": "n_par", "over": "{{items}}",
            "body": _body(), "concurrency": 32}
    h.run_parallel(node, {"items": list(range(16))}, "spec.v1", _spec(max_concurrent=4))
    # at most 4 concurrent worker threads
    assert len(seen_threads) <= 4
    assert len(seen_threads) > 1
