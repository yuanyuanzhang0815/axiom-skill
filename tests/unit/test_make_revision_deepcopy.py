"""Task 13: make_revision deep-copies nodes + control_flow (spec §6).

Without deepcopy, `make_revision` assigns `nodes=parent.nodes,
control_flow=parent.control_flow` BY REFERENCE — child and parent share the
same dict objects. A loop-aware replan that adjusts `max_iterations` or
rewrites branch arms in the child then mutates the parent, violating
invariant (6): spec history is immutable.

These tests fail RED against the reference-assignment implementation and pass
GREEN once `copy.deepcopy` is applied to `nodes` + `control_flow`.
"""
from axiom.ir import Spec, Requirement, make_revision


def _spec_with_nodes():
    """Parent spec with a populated nodes dict and a sequence control_flow."""
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={
            "n1": {"type": "agent", "id": "n1", "prompt": "do work"},
        },
        control_flow={"type": "sequence", "steps": ["n1"]},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def _make_revision(parent):
    return make_revision(
        parent,
        "replan: loop tuning",
        trigger={"evidence_refs": ["event:E1"], "broken_assumption": "A1"},
        diff={
            "added_nodes": [],
            "removed_nodes": [],
            "modified_nodes": ["n1"],
            "control_flow_changed": True,
        },
    )


def test_mutate_v2_nodes_parent_unchanged():
    """Mutating a node dict in v2 must not mutate the parent's node dict."""
    s1 = _spec_with_nodes()
    v2 = _make_revision(s1)

    # Child must be a different dict object (deepcopy), not a shared reference.
    assert v2.nodes is not s1.nodes, (
        "v2.nodes is the SAME object as s1.nodes — make_revision must deepcopy"
    )
    # Mutate the child's node entry.
    v2.nodes["n1"]["x"] = 1

    # Parent's node entry must be unchanged.
    assert "x" not in s1.nodes["n1"], (
        "parent node 'n1' was mutated by a child revision — history not immutable"
    )
    assert s1.nodes["n1"] == {"type": "agent", "id": "n1", "prompt": "do work"}


def test_adjust_repeat_max_iterations_parent_unchanged():
    """Adjusting a repeat node's max_iterations in v2 must not mutate parent.

    Loop-aware replans tune `max_iterations` (or rewrite branch arms) on the
    child; the parent's bound must stay intact for audit/replay.
    """
    s1 = Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["s"],
        nodes={
            "r1": {
                "type": "repeat",
                "id": "r1",
                "body": ["a1"],
                "max_iterations": 3,
                "until": "dry<2",
                "failure_policy": {"on_exhausted": "block"},
            },
        },
        control_flow={"type": "sequence", "steps": ["r1"]},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )
    v2 = _make_revision(s1)

    # Loop-aware replan: widen the iteration cap on the child only.
    assert v2.nodes is not s1.nodes, "v2.nodes must be a deepcopy, not a reference"
    v2.nodes["r1"]["max_iterations"] = 10

    # Parent bound unchanged.
    assert s1.nodes["r1"]["max_iterations"] == 3, (
        "parent repeat.max_iterations mutated by child revision — invariant (6) broken"
    )
    assert v2.nodes["r1"]["max_iterations"] == 10


def test_control_flow_append_v2_parent_unchanged():
    """Appending a step to v2.control_flow must not mutate parent control_flow.

    Replans that rewrite branch arms or insert steps touch control_flow.steps;
    the parent's control_flow must remain immutable.
    """
    s1 = _spec_with_nodes()
    v2 = _make_revision(s1)

    # Child control_flow must be a different dict object (deepcopy).
    assert v2.control_flow is not s1.control_flow, (
        "v2.control_flow is the SAME object as s1.control_flow — "
        "make_revision must deepcopy"
    )
    # Also the inner steps list must not be shared.
    assert v2.control_flow["steps"] is not s1.control_flow["steps"], (
        "v2.control_flow['steps'] is the SAME list as parent's — deepcopy must be deep"
    )

    # Append a new step to the child's control_flow.
    v2.control_flow["steps"].append("n2")
    v2.control_flow["new_key"] = "child-only"

    # Parent unchanged.
    assert s1.control_flow == {"type": "sequence", "steps": ["n1"]}, (
        "parent control_flow mutated by child revision — history not immutable"
    )
    assert s1.control_flow["steps"] == ["n1"]
    assert "new_key" not in s1.control_flow
