import json
from axiom.harness import Harness


def _runner_nth(results):
    calls = {"i": 0}

    def runner(args, cwd=None):
        r = results[min(calls["i"], len(results) - 1)]
        calls["i"] += 1
        line = json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps(r),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        })
        return (0, line)

    return runner


def _body_agent(nid="b"):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }


def test_parallel_collects_results(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_runner_nth([{"x": "ok"}, {"x": "bad"}]))
    node = {
        "type": "parallel", "id": "p", "over": "{{items}}",
        "concurrency": 1, "barrier": True, "body": _body_agent(),
    }
    out = h.run_parallel(node, {"items": ["a", "b"]}, "spec.v1")
    assert len(out) == 2
    assert out[0]["validated_output"] == {"x": "ok"}


def test_parallel_failure_localized_to_null(tmp_path):
    seq = [
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": "ok"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }),
        "",  # second call returns empty stdout -> no result -> operational failure
    ]
    i = {"n": 0}

    def runner(args, cwd=None):
        out = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0 if out else 1, out)

    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "parallel", "id": "p", "over": "{{items}}",
        "concurrency": 1, "barrier": True, "body": _body_agent(),
    }
    out = h.run_parallel(node, {"items": ["a", "b"]}, "spec.v1")
    assert out[0]["validated_output"] == {"x": "ok"}
    assert out[1] is None  # failure localized, does not poison sibling


def test_pipeline_drops_failed_item_continues_others(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_runner_nth([{"f": "found"}]))
    node = {
        "type": "pipeline", "id": "pl", "items": "{{items}}",
        "stages": [
            {
                "type": "agent", "id": "s1", "prompt": "find {{item}}",
                "dispatch": "host", "output_schema": {"type": "object"},
                "allowed_tools": [], "write_areas": [],
                "acceptance": ["a"], "failure_policy": {},
            },
            {
                "type": "agent", "id": "s2", "prompt": "fix {{item}} {{prev}}",
                "dispatch": "host", "output_schema": {"type": "object"},
                "allowed_tools": [], "write_areas": [],
                "acceptance": ["a"], "failure_policy": {},
            },
        ],
    }
    out = h.run_pipeline(node, {"items": ["a", "b"]}, "spec.v1")
    assert len(out) == 2
