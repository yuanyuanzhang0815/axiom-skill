from axiom.ir import Spec, Requirement, contract_hash


def _spec(intent="i", reqs=None, boundaries=("b",), success=("s",), nodes=None):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent=intent,
        requirements=reqs or [Requirement("R1", "r", "required")],
        boundaries=list(boundaries),
        success_evidence=list(success),
        nodes=nodes or {},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def test_contract_hash_stable_for_contract_fields():
    # nodes changed — not a contract field — hash must stay the same
    s1 = _spec()
    s2 = _spec(nodes={"n1": {}})
    assert contract_hash(s1) == contract_hash(s2)


def test_contract_hash_changes_when_intent_changes():
    s1 = _spec(intent="i")
    s2 = _spec(intent="changed")
    assert contract_hash(s1) != contract_hash(s2)


def test_contract_hash_changes_on_requirement_change():
    s1 = _spec()
    s2 = _spec(reqs=[Requirement("R1", "changed", "required")])
    assert contract_hash(s1) != contract_hash(s2)


def test_contract_hash_changes_on_boundary_change():
    s1 = _spec(boundaries=("b1",))
    s2 = _spec(boundaries=("b2",))
    assert contract_hash(s1) != contract_hash(s2)


def test_contract_hash_changes_on_success_evidence_change():
    s1 = _spec(success=("s1",))
    s2 = _spec(success=("s2",))
    assert contract_hash(s1) != contract_hash(s2)


def test_decision_trace_change_does_not_drift_contract():
    s1 = _spec()
    s2 = _spec()
    s2.decision_trace.append({"decision_id": "D1", "phase": "replan"})
    assert contract_hash(s1) == contract_hash(s2)
