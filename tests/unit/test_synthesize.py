"""A2: synthesize goes through retry+G3 schema-feedback, not a single shot.
Empirical driver: 2/2 real-worker runs returned pure prose -> validated_output {}.
"""
import json
from axiom.harness import Harness


def _env(result_str, cost=0.1, turns=1):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": turns,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _node():
    return {
        "type": "synthesize", "id": "n_report", "inputs": ["{{n_verify.validated_output}}"],
        "output_schema": {
            "type": "object",
            "properties": {"survived_count": {"type": "integer"},
                            "refuted_count": {"type": "integer"},
                            "report": {"type": "string"}},
            "required": ["survived_count", "refuted_count", "report"],
        },
        "acceptance": ["counts + report"],
    }


def test_synthesize_retries_prose_then_returns_structured(tmp_path):
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        # first dispatch: pure prose (the 2/2 empirical failure mode)
        if len(calls) == 1:
            return (0, _env("Both claims passed review: confirmed at visual and code levels."))
        # retry after G3 schema feedback: conforming JSON
        return (0, _env(json.dumps({
            "survived_count": 2, "refuted_count": 0,
            "report": "Both claims passed review"})))
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._synthesize(_node(), {"n_verify": {"validated_output": {"survivors": [], "refuted": []}}},
                       "spec.v1", None)
    assert out is not None
    assert out["validated_output"]["survived_count"] == 2
    assert out["validated_output"]["report"].startswith("Both")
    assert len(calls) == 2  # G3 feedback retry happened


def test_synthesize_exhausts_soft_no_gate(tmp_path):
    # worker keeps returning prose -> after retries, synthesize exhausts.
    # localized=True => soft None, no gate_open (must not drag verdict).
    def runner(args, cwd=None):
        return (0, _env("Still natural language, not returning JSON"))
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._synthesize(_node(), {}, "spec.v1", None)
    assert out is None
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events())


def test_synthesize_success_logs_conforming_result(tmp_path):
    def runner(args, cwd=None):
        return (0, _env(json.dumps({"survived_count": 1, "refuted_count": 0,
                                    "report": "ok"})))
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._synthesize(_node(), {}, "spec.v1", None)
    evs = [e for e in h.ledger.events() if e.get("kind") == "agent_result"
           and e.get("node_id") == "n_report"]
    assert len(evs) == 1
    assert evs[0]["payload"]["validated_output"]["survived_count"] == 1


# --- v1.4-S1: slim inputs + degrade fallback ------------------------------


def _upstream_with_evidence():
    # a verify output whose findings carry a LONG evidence string -- the worker
    # must NOT see that blob (drifts it into prose); only claim_id/aspect/summary.
    long_ev = "VERYLONGEVIDENCE" * 20
    return {"n_verify": {"validated_output": {
        "survivors": [{"claim_id": "C1", "aspect": "visual",
                        "summary": "matches target", "evidence": long_ev}],
        "refuted": []}}}


def test_synthesize_slims_inputs_no_evidence_blob(tmp_path):
    captured = {}
    def runner(args, cwd=None):
        captured["prompt"] = " ".join(str(a) for a in args)
        return (0, _env(json.dumps({"survived_count": 1, "refuted_count": 0,
                                    "report": "ok"})))
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._synthesize(_node(), _upstream_with_evidence(), "spec.v1", None)
    p = captured["prompt"]
    assert "matches target" in p          # summary travels
    assert "VERYLONGEVIDENCE" not in p    # long evidence blob stripped


def test_synthesize_degrade_fallback_when_prose(tmp_path):
    # worker always returns prose -> synthesize exhausts (stagnation).
    # The checkpoint must DERIVE counts from verify evidence + use the worker's
    # prose as the report, labeled degraded=True (no fabricated verdict).
    def runner(args, cwd=None):
        return (0, _env("Both claims passed review: survived=2 refuted=0"))
    from axiom.ir import Spec, Requirement
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes={"n_report": {"type": "synthesize", "id": "n_report",
                        "inputs": ["{{n_verify.validated_output}}"],
                        "output_schema": {"type": "object"}}},
                control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._build_evidence_for(spec)
    # two surviving claims (so the derived counts are 2 / 0)
    h.ledger.append({"event_id": "ev_1", "kind": "verify_verdict",
                     "claim_id": "C1", "survived": True, "refuted": False})
    h.ledger.append({"event_id": "ev_2", "kind": "verify_verdict",
                     "claim_id": "C2", "survived": True, "refuted": False})
    h._synthesize(_node(), {"n_verify": {"validated_output": {
        "survivors": [{"claim_id": "C1"}, {"claim_id": "C2"}], "refuted": []}}},
        "spec.v1", spec)
    cp = h.project_checkpoint(spec)
    so = cp["synthesized_output"]
    assert so.get("degraded") is True
    assert so["survived_count"] == 2
    assert so["refuted_count"] == 0
    assert "Both claims" in so["report"]


def test_synthesize_clean_success_not_degraded(tmp_path):
    # when synthesize returns conforming JSON, no degraded flag
    def runner(args, cwd=None):
        return (0, _env(json.dumps({"survived_count": 2, "refuted_count": 0,
                                    "report": "ok"})))
    from axiom.ir import Spec, Requirement
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes={"n_report": {"type": "synthesize", "id": "n_report",
                        "inputs": [], "output_schema": {"type": "object"}}},
                control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._build_evidence_for(spec)
    h._synthesize(_node(), {}, "spec.v1", spec)
    cp = h.project_checkpoint(spec)
    assert cp["synthesized_output"].get("degraded") is not True
    assert cp["synthesized_output"]["survived_count"] == 2


class _Spec:
    spec_version_id = "spec.v1"; max_stagnation = 1; budget_usd = 5.0; max_agents = 1000


def test_synthesize_honors_node_prompt(tmp_path):
    """v1.3.3: a synthesize node's own prompt is used, not the hardcoded
    fallback. The spec author's synthesis instructions must reach the worker."""
    prompts = []
    def runner(args, cwd=None):
        prompts.append(args[0])  # [host_adapter, ..., prompt, ...]
        return (0, _env(json.dumps({"survived_count":1,"refuted_count":0,"report":"ok"})))
    h = Harness(tmp_path/"run", worker_runner=runner)
    node = _node()
    node["prompt"] = "Custom synthesize instructions {{inputs}}"
    h._synthesize(node, {"n_verify": {"validated_output": {"claim_id":"C1","summary":"s","aspect":"a"}}},
                  "spec.v1", _Spec())
    assert prompts, "synthesize must dispatch"
    assert "Custom synthesize instructions" in prompts[0], \
        "the node's own prompt must reach the worker"
    assert "Synthesize the following review findings" not in prompts[0], \
        "the hardcoded fallback must not override the node prompt"
