from axiom.ledger import Ledger
from axiom.ir import Spec, Requirement


def _spec(evidence_refs):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[{"decision_id": "D1", "evidence_refs": evidence_refs}],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def test_orphan_evidence_detected(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    errs = lg.check_refs(_spec(["event:E1", "event:E2"]))  # E2 doesn't exist
    assert any("E2" in e for e in errs)


def test_valid_refs_no_error(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    lg.append({"event_id": "E2", "kind": "x", "payload": {}})
    assert lg.check_refs(_spec(["event:E1", "event:E2"])) == []


def test_reverse_index_derived_from_stored_field(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}, "evidence_for": ["D1"]})
    idx = lg.derive_reverse_index()
    assert idx["E1"] == ["D1"]


def test_reverse_index_rebuildable_from_spec_forward_refs(tmp_path):
    lg = Ledger(tmp_path / "run")
    # event stored WITHOUT evidence_for; spec decision_trace declares D1 -> E1
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    spec = _spec(["event:E1"])
    idx = lg.derive_reverse_index(spec)  # rebuild purely from forward refs
    assert idx["E1"] == ["D1"]


def test_non_event_refs_ignored(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    # artifact:sha refs should not be treated as event refs
    spec = _spec(["artifact:sha:abc", "event:E1"])
    assert lg.check_refs(spec) == []
