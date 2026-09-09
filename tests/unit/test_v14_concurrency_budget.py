"""v1.4-S2: per-claim skeptics dispatch concurrently; S3: budget pre-check
refuses dispatch once the boundary is crossed."""
import json
import threading
import time
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(result_str='{"refuted": false, "evidence_ref": "ok"}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _spec(budget=5.0, max_concurrent=16):
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"], nodes={},
                control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=budget, max_concurrent=max_concurrent,
                max_agents=1000, max_stagnation=1)


def test_skeptics_dispatch_concurrently(tmp_path):
    seen = set()
    def runner(args, cwd=None):
        seen.add(threading.get_ident())
        time.sleep(0.02)
        return (0, _env())
    v = {"type": "verify", "id": "n_v", "target": "{{findings}}",
         "skeptic_count": 4, "skeptic_prompt": "ref {{finding}}",
         "survival_rule": "majority_unrefuted", "independent_session": True,
         "output_schema": {"type": "object"}, "verification_policy": "independent"}
    spec = _spec()
    spec.nodes = {"n_v": v}
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._build_evidence_for(spec)
    out = h.run_verify(v, {"findings": [{"claim_id": "C1"}]}, "spec.v1", spec)
    assert out["survivors"] == [{"claim_id": "C1"}]  # 0 refute, 0 abstain -> survived
    assert len(seen) > 1  # skeptics ran on >1 thread
    assert h.ledger.verify_chain() == []


def test_skeptic_concurrency_capped_by_max_concurrent(tmp_path):
    seen = set()
    def runner(args, cwd=None):
        seen.add(threading.get_ident())
        time.sleep(0.02)
        return (0, _env())
    v = {"type": "verify", "id": "n_v", "target": "{{findings}}",
         "skeptic_count": 8, "skeptic_prompt": "ref {{finding}}",
         "survival_rule": "majority_unrefuted", "independent_session": True,
         "output_schema": {"type": "object"}, "verification_policy": "independent"}
    spec = _spec(max_concurrent=3)
    spec.nodes = {"n_v": v}
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._build_evidence_for(spec)
    h.run_verify(v, {"findings": [{"claim_id": "C1"}]}, "spec.v1", spec)
    assert len(seen) <= 3
    assert len(seen) > 1


def test_budget_pre_check_refuses_dispatch_after_crossing(tmp_path):
    # budget=0.05; each dispatch costs 0.04. First: 0.04 ok. Second: 0.08 crosses
    # (post-check logs budget_exhausted, returns None). Third: pre-check sees
    # 0.08 > 0.05 -> refuse WITHOUT dispatch.
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _env('{"x": 1}', cost=0.04))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {"type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
            "output_schema": {"type": "object"}, "allowed_tools": [],
            "write_areas": [], "acceptance": ["a"], "failure_policy": {}}
    spec = _spec(budget=0.05)
    h._build_evidence_for(spec)
    a = h._run_agent_with_retry(node, {}, "spec.v1", spec, localized=False)
    b = h._run_agent_with_retry(node, {}, "spec.v1", spec, localized=False)
    c = h._run_agent_with_retry(node, {}, "spec.v1", spec, localized=False)
    assert a is not None           # first dispatch under budget
    assert b is None               # second crossed -> None (post-check)
    assert c is None               # third refused pre-dispatch
    assert len(calls) == 2         # third NOT dispatched (pre-check refused)
    assert any(e.get("kind") == "budget_exhausted" for e in h.ledger.events())
