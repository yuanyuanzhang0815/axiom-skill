import json
from axiom.harness import Harness


def _skeptic_refute(args, cwd=None):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"refuted": True, "evidence_ref": "e"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _skeptic_survive(args, cwd=None):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"refuted": False, "evidence_ref": "e"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _verify_node(sc=3):
    return {
        "type": "verify", "id": "v", "target": "{{findings}}",
        "skeptic_count": sc, "skeptic_prompt": "refute",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }


def test_refuted_finding_dropped(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute)
    out = h.run_verify(_verify_node(3), {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == []
    assert any(f["claim_id"] == "C1" for f in out["refuted"])


def test_survived_finding_kept(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive)
    out = h.run_verify(_verify_node(3), {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert any(f["claim_id"] == "C1" for f in out["survivors"])


def test_verify_verdict_events_logged(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive)
    h.run_verify(_verify_node(1), {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert any(
        e["kind"] == "verify_verdict" and e["claim_id"] == "C1"
        for e in h.ledger.events()
    )


def test_majority_threshold_boundary(tmp_path):
    # majority_unrefuted: a claim survives only if a STRICT majority did NOT
    # refute it (refute_votes < sc/2). The tie / intermediate cases are what
    # distinguish the correct rule from the off-by-one `+1` formula that let a
    # near-majority refute survive.
    calls = {"n": 0}

    def one_refute(args, cwd=None):
        calls["n"] += 1
        # first skeptic refutes, second does not -> 1 refute of sc=2 (a tie)
        refuted = (calls["n"] % 2 == 1)
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"refuted": refuted}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=one_refute)
    out = h.run_verify(_verify_node(2), {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    # 1 refute of sc=2 = a 1-1 tie -> only 1 non-refute, NOT a majority -> KILLED.
    # (The old `+1` formula let this tie survive: 1 < 2/2+1=2 -> True.)
    assert out["survivors"] == [], "a 1-1 tie must NOT survive majority_unrefuted"
    assert any(f["claim_id"] == "C1" for f in out["refuted"])
