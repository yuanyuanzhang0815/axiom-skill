"""A4: resume replays unchanged successful nodes from the journal cache (zero
host dispatch, zero cost) and re-runs only changed/failed nodes. Verdict stays
consistent with the first run."""
import json
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(result_str='{"x": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _agent(nid="n1"):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _spec(steps, svid="spec.v1"):
    return Spec(spec_version_id=svid, parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes={s["id"]: s for s in steps if isinstance(s, dict)},
                control_flow={"type": "sequence",
                              "steps": [s["id"] if isinstance(s, dict) else s
                                        for s in steps]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)


def test_resume_replays_successful_node_no_dispatch(tmp_path):
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    a = _agent("n1")
    spec = _spec([a])
    # first run
    h.run(spec)
    n_after_first = len(calls)
    assert n_after_first == 1
    verdict1 = h.project_checkpoint(spec)["verdict"]
    # resume: build cache, re-walk
    h2 = Harness(tmp_path / "run", worker_runner=runner)
    h2.build_replay_cache(spec)
    h2._run_sequence(spec)
    # zero NEW dispatches (n1 was a success -> replayed from cache)
    assert len(calls) == n_after_first
    # replay_hit event emitted
    assert any(e.get("kind") == "replay_hit" for e in h2.ledger.events())


def test_resume_reruns_changed_spec_version(tmp_path):
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    a = _agent("n1")
    spec_v1 = _spec([a], svid="spec.v1")
    h.run(spec_v1)
    after_first = len(calls)
    # resume with a v2 spec (different spec_version_id) -> cache miss -> re-run
    spec_v2 = _spec([a], svid="spec.v2")
    h2 = Harness(tmp_path / "run", worker_runner=runner)
    h2.build_replay_cache(spec_v2)  # cache keyed on spec.v1 -> nothing for v2
    h2._run_sequence(spec_v2)
    assert len(calls) == after_first + 1  # n1 re-dispatched under v2


def test_resume_verdict_consistent_with_first_run(tmp_path):
    def runner(args, cwd=None):
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    a = _agent("n1")
    spec = _spec([a])
    h.run(spec)
    v1 = h.project_checkpoint(spec)["verdict"]
    h2 = Harness(tmp_path / "run", worker_runner=runner)
    h2.build_replay_cache(spec)
    h2._run_sequence(spec)
    v2 = h2.project_checkpoint(spec)["verdict"]
    assert v1 == v2


def test_resume_skeptic_cache_populated_per_dispatch(tmp_path):
    # sc=2 skeptics both conform -> 2 cached entries -> resume pops both, 0 dispatch
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _env('{"refuted": false, "evidence_ref": "ok"}'))
    v = {"type": "verify", "id": "n_v", "target": "{{findings}}",
         "skeptic_count": 2, "skeptic_prompt": "ref {{finding}}",
         "survival_rule": "majority_unrefuted", "independent_session": True,
         "output_schema": {"type": "object"}, "verification_policy": "independent"}
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes={"n_v": v}, control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._build_evidence_for(spec)
    h.run_verify(v, {"findings": [{"claim_id": "C1"}]}, "spec.v1", spec)
    after_first = len(calls)
    assert after_first == 2  # 2 skeptics dispatched
    h2 = Harness(tmp_path / "run", worker_runner=runner)
    h2.build_replay_cache(spec)
    h2._build_evidence_for(spec)
    h2.run_verify(v, {"findings": [{"claim_id": "C1"}]}, "spec.v1", spec)
    assert len(calls) == after_first  # both skeptics replayed from cache
