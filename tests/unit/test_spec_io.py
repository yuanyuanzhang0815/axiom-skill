import json
from axiom.ir import Spec, Requirement, AgentNode, as_node_dict, spec_to_json, spec_from_json


def test_spec_roundtrip_preserves_node_type_and_fields():
    n = AgentNode(
        id="n1",
        prompt="p",
        dispatch="host",
        model=None,
        write_areas=["src/**"],
        read_areas=[],
        output_schema={"type": "object"},
        acceptance=["a"],
        allowed_tools=["Read"],
        risk="low",
        verification_policy="self_declared",
        failure_policy={
            "max_retries": 2,
            "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        },
    )
    spec = Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={"n1": as_node_dict(n)},
        control_flow={"type": "sequence", "steps": ["n1"]},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )
    s = spec_to_json(spec)
    back = spec_from_json(s)
    assert back.spec_version_id == "spec.v1"
    assert back.nodes["n1"]["type"] == "agent"
    assert back.nodes["n1"]["write_areas"] == ["src/**"]
    assert back.nodes["n1"]["failure_policy"]["retry_guard"] == "requires_new_evidence"


def test_spec_json_is_valid_json_with_contract_four():
    spec = Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
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
    d = json.loads(spec_to_json(spec))
    assert d["intent"] == "i"
    assert d["requirements"] == [{"id": "R1", "text": "r", "criticality": "required"}]
