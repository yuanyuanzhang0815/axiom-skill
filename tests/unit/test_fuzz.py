"""Fuzz tests: randomized property checks over the IR + ledger."""
import json
import random
import pytest
from axiom.ir import Spec, Requirement, spec_to_json, spec_from_json, contract_hash, validate_spec
from axiom.ledger import Ledger


def _rand_spec(rng):
    n = rng.randint(0, 5)
    nodes = {}
    for i in range(n):
        nid = f"n{i}"
        nodes[nid] = {
            "type": "agent", "id": nid, "prompt": f"p{i}", "dispatch": "host",
            "output_schema": {"type": "object"}, "allowed_tools": [],
            "write_areas": [], "acceptance": ["a"], "failure_policy": {},
        }
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"], nodes=nodes,
        control_flow={"type": "sequence", "steps": [f"n{i}" for i in range(n)]},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def test_spec_roundtrip_preserves_contract_hash():
    rng = random.Random(42)
    for _ in range(50):
        s = _rand_spec(rng)
        rt = spec_from_json(spec_to_json(s))
        assert contract_hash(s) == contract_hash(rt)
        assert rt.intent == s.intent
        assert len(rt.nodes) == len(s.nodes)


def test_contract_hash_stable_under_non_contract_changes():
    s1 = _rand_spec(random.Random(1))
    s2 = Spec(**{**s1.__dict__, "nodes": {}})  # nodes only changed
    assert contract_hash(s1) == contract_hash(s2)


def test_validate_never_crashes_on_well_formed_random_specs():
    rng = random.Random(7)
    for _ in range(50):
        s = _rand_spec(rng)
        errs = validate_spec(s)
        assert errs == []  # our generator always produces well-formed specs


def test_ledger_chain_intact_under_random_events(tmp_path):
    rng = random.Random(99)
    lg = Ledger(tmp_path / "run")
    for i in range(20):
        lg.append({
            "event_id": f"E{i}",
            "kind": rng.choice(["agent_result", "verify_verdict"]),
            "claim_id": f"C{rng.randint(0, 3)}",
            "payload": {"v": rng.randint(0, 1000)},
        })
    assert lg.verify_chain() == []
    assert len(lg.events()) == 20


def test_ledger_seal_anchors_final_hash(tmp_path):
    rng = random.Random(13)
    lg = Ledger(tmp_path / "run")
    for i in range(rng.randint(1, 10)):
        lg.append({"event_id": f"E{i}", "kind": "agent_result", "payload": {}})
    lg.seal()
    manifest = json.loads(lg.manifest_path.read_text())
    assert manifest["final_event_hash"] == lg._last_hash()
