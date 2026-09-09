"""v2 control-flow node dataclasses: ConditionNode / RepeatNode / UntilNode / GateNode.

Verbatim field shapes per v2 spec §1.1–§1.4.  These nodes are OPT-IN: v1 specs
never reference these kinds, so the 278 v1 tests are unaffected.  v2 activates
only when a spec uses one of these node kinds.
"""
import pytest
from axiom.ir import (
    Spec,
    Requirement,
    ConditionNode,
    RepeatNode,
    UntilNode,
    GateNode,
    as_node_dict,
    spec_to_json,
    spec_from_json,
)


def test_condition_node_fields():
    n = ConditionNode(
        id="c1",
        predicate="claim_status('C1')=='VERIFIED'",
        then_branch=["a1", "a2"],
        else_branch=["a3"],
        gate=False,
        output_schema={"type": "object"},
    )
    d = as_node_dict(n)
    assert d["type"] == "condition"
    assert d["predicate"] == "claim_status('C1')=='VERIFIED'"
    assert d["then_branch"] == ["a1", "a2"]
    assert d["else_branch"] == ["a3"]
    assert d["gate"] is False
    assert d["output_schema"] == {"type": "object"}
    # defaults: else_branch=[], gate=False, output_schema={}
    n2 = ConditionNode(id="c2", predicate="p", then_branch=["a1"])
    d2 = as_node_dict(n2)
    assert d2["else_branch"] == []
    assert d2["gate"] is False
    assert d2["output_schema"] == {}


def test_repeat_node_max_iterations_required():
    # max_iterations is REQUIRED (no default) — spec §1.2 forces bounding
    with pytest.raises(TypeError):
        RepeatNode(id="r1", body=["a1"])  # missing max_iterations
    n = RepeatNode(id="r1", body=["a1"], max_iterations=5)
    d = as_node_dict(n)
    assert d["type"] == "repeat"
    assert d["body"] == ["a1"]
    assert d["max_iterations"] == 5
    assert d["until"] == "dry<2"                       # default convergence predicate
    assert d["failure_policy"] == {"on_exhausted": "block"}  # default


def test_until_node_fields():
    # max_iterations is REQUIRED (no default) — spec §1.3 forces bounding
    with pytest.raises(TypeError):
        UntilNode(id="u1", body=["a1"], cond="claim_status('C1')=='VERIFIED'")
    n = UntilNode(
        id="u1",
        body=["a1"],
        cond="claim_status('C1')=='VERIFIED'",
        max_iterations=3,
        budget_aware=True,
    )
    d = as_node_dict(n)
    assert d["type"] == "until"
    assert d["body"] == ["a1"]
    assert d["cond"] == "claim_status('C1')=='VERIFIED'"
    assert d["max_iterations"] == 3
    assert d["budget_aware"] is True                    # default
    assert d["failure_policy"] == {"on_exhausted": "degrade"}  # default


def test_gate_node_fields():
    n = GateNode(
        id="g1",
        trigger="claim_status('C1')=='REFUTED'",
        escalation_tiers=[
            {"model": "claude-opus-4-20250514", "allowed_tools": ["Bash"],
             "skeptic_count": 3}
        ],
        body=["a1"],
        on_trigger="pause",
        output_schema={"type": "object"},
    )
    d = as_node_dict(n)
    assert d["type"] == "gate"
    assert d["trigger"] == "claim_status('C1')=='REFUTED'"
    assert d["escalation_tiers"] == [
        {"model": "claude-opus-4-20250514", "allowed_tools": ["Bash"],
         "skeptic_count": 3}
    ]
    assert d["on_trigger"] == "pause"
    assert d["body"] == ["a1"]
    assert d["output_schema"] == {"type": "object"}
    # defaults: on_trigger="pause", output_schema={}
    n2 = GateNode(id="g2", trigger="t", escalation_tiers=[], body=["a1"])
    d2 = as_node_dict(n2)
    assert d2["on_trigger"] == "pause"
    assert d2["output_schema"] == {}


def test_spec_roundtrip_v2_kinds():
    cond = ConditionNode(id="c1", predicate="p", then_branch=["a1"],
                         else_branch=["a2"])
    rep = RepeatNode(id="r1", body=["a1"], max_iterations=5)
    unt = UntilNode(id="u1", body=["a1"], cond="p", max_iterations=3)
    gate = GateNode(id="g1", trigger="t",
                    escalation_tiers=[{"model": "m"}], body=["a1"])
    spec = Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={
            "c1": as_node_dict(cond),
            "r1": as_node_dict(rep),
            "u1": as_node_dict(unt),
            "g1": as_node_dict(gate),
        },
        control_flow={"type": "sequence", "steps": ["c1", "r1", "u1", "g1"]},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )
    s = spec_to_json(spec)
    back = spec_from_json(s)
    # condition: then_branch / else_branch preserved
    assert back.nodes["c1"]["type"] == "condition"
    assert back.nodes["c1"]["then_branch"] == ["a1"]
    assert back.nodes["c1"]["else_branch"] == ["a2"]
    # repeat: body / max_iterations preserved
    assert back.nodes["r1"]["type"] == "repeat"
    assert back.nodes["r1"]["body"] == ["a1"]
    assert back.nodes["r1"]["max_iterations"] == 5
    # until: body / max_iterations preserved
    assert back.nodes["u1"]["type"] == "until"
    assert back.nodes["u1"]["body"] == ["a1"]
    assert back.nodes["u1"]["max_iterations"] == 3
    # gate: escalation_tiers / body preserved
    assert back.nodes["g1"]["type"] == "gate"
    assert back.nodes["g1"]["escalation_tiers"] == [{"model": "m"}]
    assert back.nodes["g1"]["body"] == ["a1"]
