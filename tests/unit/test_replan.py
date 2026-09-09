from axiom.ir import Spec, Requirement, make_revision, contract_hash


def _spec(intent="i"):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent=intent,
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def test_revision_preserves_contract_when_nodes_change():
    s1 = _spec()
    v2 = make_revision(
        s1,
        "added node",
        trigger={"evidence_refs": ["event:E1"], "broken_assumption": "A1"},
        diff={
            "added_nodes": ["n2"],
            "removed_nodes": [],
            "modified_nodes": [],
            "control_flow_changed": True,
        },
    )
    assert v2.parent_spec_id == "spec.v1"
    assert v2.spec_version_id == "spec.v2"
    assert v2.revision == 2
    assert contract_hash(v2) == contract_hash(s1)  # nodes change -> no drift
    assert v2.contract_drift is False


def test_revision_drifts_when_intent_changes():
    s1 = _spec()
    v2 = make_revision(
        s1,
        "intent changed",
        trigger={"evidence_refs": ["event:E2"], "broken_assumption": "A1"},
        diff={
            "added_nodes": [],
            "removed_nodes": [],
            "modified_nodes": [],
            "control_flow_changed": False,
        },
        new_intent="changed intent",
    )
    assert v2.contract_drift is True
    assert contract_hash(v2) != contract_hash(s1)


def test_revision_does_not_mutate_parent():
    s1 = _spec()
    before = s1.decision_trace.copy()
    make_revision(
        s1,
        "x",
        trigger={"evidence_refs": [], "broken_assumption": "A1"},
        diff={"added_nodes": [], "removed_nodes": [], "modified_nodes": [], "control_flow_changed": False},
    )
    assert s1.decision_trace == before  # parent immutable
    assert s1.revision == 1


def test_revision_chain_threads_parent_spec_id():
    s1 = _spec()
    s2 = make_revision(s1, "r2", {"evidence_refs": [], "broken_assumption": "A1"},
                       {"added_nodes": [], "removed_nodes": [], "modified_nodes": [], "control_flow_changed": False})
    s3 = make_revision(s2, "r3", {"evidence_refs": [], "broken_assumption": "A1"},
                       {"added_nodes": [], "removed_nodes": [], "modified_nodes": [], "control_flow_changed": False})
    assert s3.parent_spec_id == "spec.v2"
    assert s2.parent_spec_id == "spec.v1"
