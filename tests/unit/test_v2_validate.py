"""v2 validate_spec extensions (spec §6): per-kind rules + referential
integrity + closed-set type check.

These are the fail-fast guards that close the v1 gap (R12): an unknown node
`type` used to pass validate_spec silently, then crash at harness._dispatch
(harness.py:996 `raise ValueError(f"unknown node type {t}")`). Referential
integrity (every referenced node id exists in spec.nodes) was absent for ALL
kinds. This task makes validate_spec reject both at validate time.
"""
import pytest
from axiom.ir import Spec, Requirement, validate_spec


def _spec(nodes, cf):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["S1:R1=s"],
        nodes=nodes,
        control_flow=cf,
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def _agent(nid):
    return {
        "type": "agent",
        "id": nid,
        "prompt": "p",
        "output_schema": {"type": "object"},
        "acceptance": ["a"],
        "write_areas": [],
        "failure_policy": {},
    }


def _condition(nid="c1", **over):
    n = {
        "type": "condition",
        "id": nid,
        "predicate": "claim_status('C1')=='VERIFIED'",
        "then_branch": ["a1"],
        "else_branch": [],
        "gate": False,
        "output_schema": {},
    }
    n.update(over)
    return n


def _repeat(nid="r1", **over):
    n = {
        "type": "repeat",
        "id": nid,
        "body": ["a1"],
        "max_iterations": 5,
        "until": "dry<2",
        "failure_policy": {"on_exhausted": "block"},
    }
    n.update(over)
    return n


def _until(nid="u1", **over):
    n = {
        "type": "until",
        "id": nid,
        "body": ["a1"],
        "cond": "claim_status('C1')=='VERIFIED'",
        "max_iterations": 3,
        "budget_aware": True,
        "failure_policy": {"on_exhausted": "degrade"},
    }
    n.update(over)
    return n


def _gate(nid="g1", **over):
    n = {
        "type": "gate",
        "id": nid,
        "trigger": "claim_status('C1')=='REFUTED'",
        "escalation_tiers": [{"model": "claude-opus-4-20250514"}],
        "body": ["a1"],
        "on_trigger": "pause",
        "output_schema": {},
    }
    n.update(over)
    return n


def _valid_v2_spec():
    """A well-formed v2 spec: all per-kind rules satisfied, all refs exist."""
    return _spec(
        {
            "a1": _agent("a1"),
            "a2": _agent("a2"),
            "c1": _condition("c1", then_branch=["a1"], else_branch=["a2"]),
            "r1": _repeat("r1", body=["a1"]),
            "u1": _until("u1", body=["a1"]),
            "g1": _gate("g1", body=["a1"]),
        },
        {"type": "sequence", "steps": ["c1", "r1", "u1", "g1"]},
    )


# --- per-kind rules: condition ---------------------------------------------


def test_condition_missing_predicate_rejected():
    n = _condition("c1", predicate="")
    errs = validate_spec(_spec({"c1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["c1"]}))
    assert any("condition" in e and "predicate" in e for e in errs), errs


def test_condition_empty_then_branch_rejected():
    n = _condition("c1", then_branch=[])
    errs = validate_spec(_spec({"c1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["c1"]}))
    assert any("then_branch" in e for e in errs), errs


# --- per-kind rules: repeat -------------------------------------------------


def test_repeat_missing_body_rejected():
    n = _repeat("r1", body=[])
    errs = validate_spec(_spec({"r1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["r1"]}))
    assert any("repeat" in e and "body" in e for e in errs), errs


def test_repeat_max_iterations_le_zero_rejected():
    n = _repeat("r1", max_iterations=0)
    errs = validate_spec(_spec({"r1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["r1"]}))
    assert any("max_iterations" in e for e in errs), errs


def test_repeat_missing_until_termination_rejected():
    n = _repeat("r1", until="")
    errs = validate_spec(_spec({"r1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["r1"]}))
    assert any("until" in e for e in errs), errs


# --- per-kind rules: until --------------------------------------------------


def test_until_missing_cond_rejected():
    n = _until("u1", cond="")
    errs = validate_spec(_spec({"u1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["u1"]}))
    assert any("cond" in e for e in errs), errs


def test_until_max_iterations_le_zero_rejected():
    n = _until("u1", max_iterations=0)
    errs = validate_spec(_spec({"u1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["u1"]}))
    assert any("max_iterations" in e for e in errs), errs


# --- per-kind rules: gate ---------------------------------------------------


def test_gate_missing_trigger_rejected():
    n = _gate("g1", trigger="")
    errs = validate_spec(_spec({"g1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["g1"]}))
    assert any("gate" in e and "trigger" in e for e in errs), errs


def test_gate_missing_body_rejected():
    n = _gate("g1", body=[])
    errs = validate_spec(_spec({"g1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["g1"]}))
    assert any("gate" in e and "body" in e for e in errs), errs


# --- referential integrity --------------------------------------------------


def test_then_branch_ref_unknown_node_rejected():
    # then_branch references "ghost" which is not in spec.nodes
    n = _condition("c1", then_branch=["ghost"])
    errs = validate_spec(_spec({"c1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["c1"]}))
    assert any("ghost" in e and "c1" in e for e in errs), errs


def test_nested_parallel_ref_unknown_node_rejected():
    # then_branch is a parallel step ["parallel", "a1", "ghost"] — nested ref
    n = _condition("c1", then_branch=[["parallel", "a1", "ghost"]])
    errs = validate_spec(_spec({"c1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["c1"]}))
    assert any("ghost" in e for e in errs), errs


def test_repeat_body_ref_unknown_node_rejected():
    n = _repeat("r1", body=["ghost"])
    errs = validate_spec(_spec({"r1": n, "a1": _agent("a1")},
                           {"type": "sequence", "steps": ["r1"]}))
    assert any("ghost" in e for e in errs), errs


# --- closed-set type check --------------------------------------------------


def test_unknown_node_type_rejected_not_silent():
    """R12: unknown type must fail at validate, not pass silently then crash
    at harness._dispatch (harness.py:996 `raise ValueError(f"unknown node
    type {t}")`)."""
    n = {"type": "frobnicate", "id": "f1", "prompt": "p",
         "output_schema": {}, "acceptance": ["a"], "write_areas": [],
         "failure_policy": {}}
    errs = validate_spec(_spec({"f1": n}, {"type": "sequence", "steps": ["f1"]}))
    assert any("frobnicate" in e and "unknown type" in e for e in errs), errs


# --- valid v2 spec passes ---------------------------------------------------


def test_valid_v2_spec_passes():
    """A well-formed condition+repeat+until+gate spec validates cleanly."""
    errs = validate_spec(_valid_v2_spec())
    assert errs == [], errs
