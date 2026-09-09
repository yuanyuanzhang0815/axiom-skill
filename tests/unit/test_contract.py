"""M1 deterministic tests for the delivery-contract / quality-gap protocol.

Spec: docs/superpowers/specs/2026-09-09-axiom-autonomous-delivery-quality-loop-design.md
(v1.1). M1 scope only: record, rebuild, validate. No verdict integration
(that is M2).
"""
import json

import pytest

from axiom.contract import (
    CONTRACT_SCHEMA, Assumption, ContractStore, DeliveryContract,
    QualityStandard, contract_digest, contract_from_json, contract_to_json,
    open_blocking_or_material, project_gaps, validate_contract,
    validate_decision_payload, validate_disposition_payload,
    validate_gap_payload, KIND_CONTRACT_ACTIVATED,
)
from axiom.ir import Requirement, Spec, budget_limit, make_revision, validate_spec
from axiom.ledger import Ledger


def _std(sid="S1", source="professional", necessity="required"):
    return QualityStandard(
        standard_id=sid, text=f"standard {sid}", source=source,
        source_ref="booking.example.com flow", applicability="booking",
        necessity=necessity, verification="walk the flow")


def _contract(rev=1, parent=None, task="task.demo", cost=None):
    return DeliveryContract(
        task_id=task, contract_revision=rev, parent_digest=parent,
        intent="booking system",
        user_requirements=[{"id": "U1", "text": "customers can book"}],
        boundaries=["no payments"],
        authorization_refs=[],
        quality_standards=[_std()],
        assumptions=[Assumption("A1", "services have fixed durations")],
        resource_limits={"cost_usd": cost},
    )


# --- schema + digest --------------------------------------------------------


def test_validate_contract_ok():
    assert validate_contract(_contract()) == []


def test_validate_contract_null_cost_ok():
    # §11.1: null = no amount limit set; it is NOT zero and must not error
    assert validate_contract(_contract(cost=None)) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "20", True])
def test_validate_contract_rejects_non_finite_or_typed_cost(bad):
    errs = validate_contract(_contract(cost=bad))
    assert any("cost_usd" in e for e in errs), errs


def test_validate_contract_revision_chain_rules():
    assert validate_contract(_contract(rev=1, parent="sha256:x")) != []
    assert validate_contract(_contract(rev=2, parent=None)) != []
    assert validate_contract(_contract(rev=2, parent="sha256:p")) == []


def test_validate_contract_source_enum_and_dup_ids():
    c = _contract()
    c.quality_standards.append(_std(sid="S1"))  # duplicate id
    assert any("duplicate standard_id" in e for e in validate_contract(c))
    c2 = _contract()
    c2.quality_standards[0].source = "vibes"  # type: ignore
    assert any("source" in e for e in validate_contract(c2))


def test_digest_stable_and_sensitive():
    c1, c2 = _contract(), _contract()
    assert contract_digest(c1) == contract_digest(c2)
    c2.quality_standards[0].text = "changed"
    assert contract_digest(c1) != contract_digest(c2)
    # json round-trip preserves the digest (canonical serialization)
    c3 = contract_from_json(contract_to_json(c1))
    assert contract_digest(c3) == contract_digest(c1)


# --- store + atomic activation ----------------------------------------------


def test_store_activate_and_project_active(tmp_path):
    store = ContractStore(tmp_path)
    ledger = Ledger(tmp_path)
    c1 = _contract(rev=1)
    ev = store.activate(c1, ledger)
    assert ev["kind"] == KIND_CONTRACT_ACTIVATED
    assert ev["payload"]["expected_parent_digest"] is None
    assert store.active(ledger.events()).contract_revision == 1

    c2 = _contract(rev=2, parent=contract_digest(c1))
    store.activate(c2, ledger)
    assert store.active(ledger.events()).contract_revision == 2


def test_store_rejects_stale_parent(tmp_path):
    store = ContractStore(tmp_path)
    ledger = Ledger(tmp_path)
    c1 = _contract(rev=1)
    store.activate(c1, ledger)
    # a proposal computed against the OLD head (rev1's parent = None) must
    # lose to the activated head
    stale = _contract(rev=2, parent=None)
    with pytest.raises(ValueError, match="stale parent"):
        store.activate(stale, ledger)
    # the active contract is still rev1; no partial activation happened
    assert store.active(ledger.events()).contract_revision == 1


def test_store_candidate_immutable(tmp_path):
    store = ContractStore(tmp_path)
    store.write_candidate(_contract(rev=1))
    changed = _contract(rev=1)
    changed.intent = "different intent, same revision"
    with pytest.raises(ValueError, match="immutable"):
        store.write_candidate(changed)


def test_store_rejects_invalid_candidate(tmp_path):
    store = ContractStore(tmp_path)
    bad = _contract(rev=1)
    bad.quality_standards = []  # fine
    bad.intent = ""             # not fine
    with pytest.raises(ValueError, match="invalid contract"):
        store.write_candidate(bad)


def test_active_none_without_activation(tmp_path):
    store = ContractStore(tmp_path)
    store.write_candidate(_contract(rev=1))  # file on disk, never activated
    assert store.active([]) is None


# --- quality gap projection --------------------------------------------------


def _gap_events(task="t1"):
    return [
        {"kind": "quality_gap_recorded", "task_id": task,
         "payload": {"gap_id": "G1", "task_id": task, "severity": "material",
                     "scenario": "success page", "problem": "no manage entry",
                     "standard_refs": ["S1"]}},
        {"kind": "quality_gap_disposition", "task_id": task,
         "payload": {"gap_id": "G1", "disposition": "fixing",
                     "basis": "reproduced"}},
        {"kind": "quality_gap_disposition", "task_id": task,
         "payload": {"gap_id": "G1", "disposition": "resolved",
                     "evidence_refs": ["event:ev9"]}},
    ]


def test_gap_lifecycle_open_fixing_resolved():
    gaps = project_gaps(_gap_events(), "t1")
    assert gaps["G1"]["state"] == "resolved"
    assert gaps["G1"]["history"] == ["open", "fixing", "resolved"]
    assert open_blocking_or_material(gaps) == []


def test_gap_illegal_transition_raises():
    evs = [
        {"kind": "quality_gap_recorded", "task_id": "t1",
         "payload": {"gap_id": "G1", "task_id": "t1", "severity": "polish",
                     "scenario": "s", "problem": "p", "standard_refs": ["S1"]}},
        {"kind": "quality_gap_disposition", "task_id": "t1",
         "payload": {"gap_id": "G1", "disposition": "resolved",
                     "evidence_refs": ["event:e1"]}},
        # resolved -> fixing is NOT allowed (must reopen first)
        {"kind": "quality_gap_disposition", "task_id": "t1",
         "payload": {"gap_id": "G1", "disposition": "fixing"}},
    ]
    with pytest.raises(ValueError, match="illegal gap transition"):
        project_gaps(evs, "t1")


def test_gap_reopen_from_resolved():
    evs = _gap_events() + [
        {"kind": "quality_gap_disposition", "task_id": "t1",
         "payload": {"gap_id": "G1", "disposition": "reopened",
                     "basis": "regression in booking flow"}},
    ]
    gaps = project_gaps(evs, "t1")
    assert gaps["G1"]["state"] == "reopened"
    # a reopened material gap blocks delivery again
    assert open_blocking_or_material(gaps) == ["G1"]


def test_gap_validation_requires_basis_and_evidence():
    assert validate_disposition_payload(
        {"gap_id": "G1", "disposition": "dismissed"}) != []
    assert validate_disposition_payload(
        {"gap_id": "G1", "disposition": "deferred"}) != []
    assert validate_disposition_payload(
        {"gap_id": "G1", "disposition": "resolved"}) != []  # no evidence
    assert validate_disposition_payload(
        {"gap_id": "G1", "disposition": "resolved",
         "evidence_refs": ["event:e1"]}) == []


def test_gap_severity_downgrade_requires_basis():
    evs = [
        {"kind": "quality_gap_recorded", "task_id": "t1",
         "payload": {"gap_id": "G1", "task_id": "t1", "severity": "material",
                     "scenario": "s", "problem": "p", "standard_refs": ["S1"]}},
        # trying to downgrade material->polish without basis (AC-08 shape)
        {"kind": "quality_gap_disposition", "task_id": "t1",
         "payload": {"gap_id": "G1", "disposition": "fixing",
                     "severity": "polish"}},
    ]
    with pytest.raises(ValueError, match="severity change"):
        project_gaps(evs, "t1")


def test_gap_duplicate_of_links_original():
    evs = [
        {"kind": "quality_gap_recorded", "task_id": "t1",
         "payload": {"gap_id": "G1", "task_id": "t1", "severity": "material",
                     "scenario": "s", "problem": "p", "standard_refs": ["S1"]}},
        {"kind": "quality_gap_recorded", "task_id": "t1",
         "payload": {"gap_id": "G2", "task_id": "t1", "severity": "material",
                     "scenario": "s2", "problem": "p2", "standard_refs": ["S1"]}},
        {"kind": "quality_gap_disposition", "task_id": "t1",
         "payload": {"gap_id": "G2", "disposition": "duplicate_of",
                     "basis": "same root cause", "duplicate_of": "G1"}},
    ]
    gaps = project_gaps(evs, "t1")
    assert gaps["G2"]["state"] == "dismissed"
    assert gaps["G2"]["duplicate_of"] == "G1"
    assert open_blocking_or_material(gaps) == ["G1"]  # only the original


def test_gap_payload_validation():
    assert validate_gap_payload({"gap_id": "G1"}) != []
    assert validate_gap_payload(
        {"gap_id": "G1", "task_id": "t", "severity": "material",
         "scenario": "s", "problem": "p", "user_task_refs": ["U1"]}) == []


# --- loop decision validation ------------------------------------------------


def test_decision_payload_validation():
    assert validate_decision_payload(
        {"decision_id": "D1", "task_id": "t", "action": "replan"}) == []
    assert validate_decision_payload(
        {"decision_id": "D1", "task_id": "t", "action": "stop"}) != []
    assert validate_decision_payload(
        {"decision_id": "D1", "task_id": "t", "action": "stop",
         "stop_reason": "stagnating"}) == []
    assert validate_decision_payload(
        {"decision_id": "D1", "task_id": "t", "action": "yolo"}) != []


# --- spec binding (ir.py) ----------------------------------------------------


def _spec(**kw):
    base = dict(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i", requirements=[Requirement("R1", "r", "required")],
        boundaries=[], success_evidence=[], nodes={},
        control_flow={"type": "sequence", "steps": []}, decision_trace=[],
        budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1,
    )
    base.update(kw)
    return Spec(**base)


def test_binding_partial_is_invalid():
    s = _spec(task_id="t1")  # only one of the three fields
    errs = validate_spec(s)
    assert any("binding is partial" in e for e in errs)


def test_binding_complete_passes_structural_validation():
    s = _spec(task_id="t1", delivery_contract_ref="contracts/contract.v1.json",
              delivery_contract_digest="sha256:" + "0" * 64)
    assert validate_spec(s) == []


def test_binding_absent_is_legacy_and_valid():
    assert validate_spec(_spec()) == []


def test_revision_preserves_binding_and_assurance_fields():
    parent = _spec(task_id="t1", delivery_contract_ref="c.json",
                   delivery_contract_digest="sha256:" + "0" * 64,
                   adopted_from=["entry:abc"], assurance_opt_out="reason",
                   budget_usd=None)
    child = make_revision(parent, "replan", {"kind": "evidence"}, {"nodes": []})
    assert child.task_id == "t1"
    assert child.delivery_contract_ref == "c.json"
    assert child.delivery_contract_digest == parent.delivery_contract_digest
    assert child.adopted_from == ["entry:abc"]
    assert child.assurance_opt_out == "reason"
    assert child.budget_usd is None  # null budget survives a replan


def test_budget_limit_null_maps_to_inf():
    assert budget_limit(_spec(budget_usd=None)) == float("inf")
    assert budget_limit(_spec(budget_usd=3.5)) == 3.5


# --- cli binding enforcement (refuse, never silent legacy) --------------------


def _write_bound_pair(tmp_path, cost=None, spec_budget=None):
    from axiom.contract import contract_digest as digest
    c = _contract(rev=1, cost=cost)
    cpath = tmp_path / "contract.v1.json"
    cpath.write_text(contract_to_json(c), encoding="utf-8")
    spec = {
        "spec_version_id": "spec.v1", "parent_spec_id": None, "revision": 1,
        "intent": "i",
        "requirements": [{"id": "R1", "text": "r", "criticality": "required"}],
        "boundaries": [], "success_evidence": [], "nodes": {},
        "control_flow": {"type": "sequence", "steps": []},
        "decision_trace": [], "budget_usd": spec_budget,
        "max_concurrent": 16, "max_agents": 1000, "max_stagnation": 1,
        "task_id": "task.demo", "delivery_contract_ref": "contract.v1.json",
        "delivery_contract_digest": digest(c),
    }
    spath = tmp_path / "spec.json"
    spath.write_text(json.dumps(spec), encoding="utf-8")
    return spath, c


def test_load_bound_spec_ok(tmp_path):
    from axiom.cli import _load_spec
    spath, _ = _write_bound_pair(tmp_path, cost=None, spec_budget=None)
    s = _load_spec(str(spath))
    assert s.task_id == "task.demo"


def test_load_refuses_digest_mismatch(tmp_path):
    from axiom.cli import _load_spec
    spath, _ = _write_bound_pair(tmp_path)
    d = json.loads(spath.read_text())
    d["delivery_contract_digest"] = "sha256:" + "f" * 64
    spath.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="digest mismatch"):
        _load_spec(str(spath))


def test_load_refuses_budget_mirror_conflict(tmp_path):
    from axiom.cli import _load_spec
    spath, _ = _write_bound_pair(tmp_path, cost=10.0, spec_budget=5.0)
    with pytest.raises(ValueError, match="budget mirror conflict"):
        _load_spec(str(spath))


def test_load_refuses_missing_contract_file(tmp_path):
    from axiom.cli import _load_spec
    spath, _ = _write_bound_pair(tmp_path)
    d = json.loads(spath.read_text())
    d["delivery_contract_ref"] = "nope.json"
    spath.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="not found"):
        _load_spec(str(spath))


def test_load_refuses_task_id_mismatch(tmp_path):
    from axiom.cli import _load_spec
    spath, _ = _write_bound_pair(tmp_path)
    d = json.loads(spath.read_text())
    d["task_id"] = "task.other"
    spath.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="task_id mismatch"):
        _load_spec(str(spath))
