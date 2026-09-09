"""Task 7: condition node dispatch (spec §1.1, §2, §3).

`_dispatch` routes a `type:"condition"` node to `_run_condition`, which:
  1. evaluates the deterministic predicate via `_eval_predicate` (Task 2) —
     NEVER an LLM call (invariant 1); a predicate evaluation is a FACT.
  2. emits `branch_decision{node_id, predicate, eval_result, branch_taken,
     degraded, claim_strength}` via the Task 5 helper (top-level node_id is
     authoritative; payload's is redundant — Task 5 parked finding).
  3. executes `then_branch` (value True) or `else_branch` (False / 'undefined'
     safe default) via `_run_steps` (the step-list runner extracted from
     `_run_sequence`).

Binding vs advisory (§2 Q2 — cannot-branch-on-PROPOSED):
  - gate=True            -> BINDING: emit gate_open(reason='binding_branch')
    + raise GateHalt for human approval. On resume, if a gate for this
    (node_id, svid) is already resolved (allow), proceed to the branch.
  - gate=False + verified  -> BINDING: execute the branch directly.
  - gate=False + proposed  -> ADVISORY (degraded=True): execute the branch
    but downstream success_evidence cannot rely on it (Task 16 enforces).
"""
import json
import pytest
from axiom.harness import Harness, GateHalt
from axiom.ir import Spec, Requirement


# ---- helpers ---------------------------------------------------------------

def _ok_runner(args, cwd=None):
    """Mock worker runner: always returns a conforming {answer:'42'}."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _agent(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _condition(nid, predicate, then, else_=(), gate=False):
    return {
        "type": "condition", "id": nid, "predicate": predicate,
        "then_branch": then, "else_branch": list(else_), "gate": gate,
        "output_schema": {},
    }


def _spec(nodes):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=[], nodes=nodes,
        control_flow={"type": "sequence", "steps": ["c1"]},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _branch_decision(ledger):
    """Return the branch_decision event (top-level node_id is authoritative;
    §3 fields live in payload per Task 5's parked finding)."""
    return next(
        e for e in ledger.events() if e.get("kind") == "branch_decision")


def _bd_payload(ledger):
    """Shorthand: the payload of the branch_decision event."""
    return _branch_decision(ledger)["payload"]


def _agent_ran(ledger, nid):
    return any(e.get("kind") == "agent_result" and e.get("node_id") == nid
               for e in ledger.events())


# ---- branch selection -----------------------------------------------------

def test_predicate_true_then_runs_else_skipped(tmp_path):
    """value True -> then_branch runs; else_branch node never dispatches."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "True", ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    out = h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    p = _bd_payload(h.ledger)
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"
    assert p["degraded"] is False
    assert p["claim_strength"] == "deterministic"
    # top-level node_id is authoritative (Task 5 parked finding)
    assert _branch_decision(h.ledger)["node_id"] == "c1"
    assert _agent_ran(h.ledger, "a_then")
    assert not _agent_ran(h.ledger, "a_else")
    assert out["validated_output"] == {"answer": "42"}


def test_predicate_false_else_runs(tmp_path):
    """value False -> else_branch runs; then_branch node never dispatches."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "False", ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    out = h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    p = _bd_payload(h.ledger)
    assert p["eval_result"] is False
    assert p["branch_taken"] == "else"
    assert _agent_ran(h.ledger, "a_else")
    assert not _agent_ran(h.ledger, "a_then")
    assert out["validated_output"] == {"answer": "42"}


def test_undefined_ref_takes_else_safe_default(tmp_path):
    """eval_result='undefined' -> branch_taken='else' (safe default)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "{{missing}}", ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    p = _bd_payload(h.ledger)
    assert p["eval_result"] == "undefined"
    assert p["branch_taken"] == "else"
    assert not _agent_ran(h.ledger, "a_then")
    assert _agent_ran(h.ledger, "a_else")


# ---- cannot-branch-on-PROPOSED (§2 Q2) -------------------------------------

def test_predicate_on_proposed_claim_is_advisory(tmp_path):
    """A {{ref}} to a raw validated_output field (PROPOSED) yields
    claim_strength='proposed' -> degraded=True (advisory, not binding)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1",
                                   "{{n1.validated_output.x}} == 'y'",
                                   ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["c1"], values, "spec.v1", spec)
    p = _bd_payload(h.ledger)
    assert p["claim_strength"] == "proposed"
    assert p["degraded"] is True
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"


def test_predicate_on_verified_claim_is_binding(tmp_path):
    """claim_status('C1')=='VERIFIED' reads VERIFIED truth -> claim_strength
    'verified', degraded=False (binding branch, executes directly)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # pre-seed a survived verify_verdict so claim_status('C1') == 'VERIFIED'
    h.ledger.append({
        "event_id": "E0", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1", "survived": True,
    })
    spec = _spec({"c1": _condition("c1",
                                   "claim_status('C1') == 'VERIFIED'",
                                   ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    p = _bd_payload(h.ledger)
    assert p["claim_strength"] == "verified"
    assert p["degraded"] is False
    assert p["branch_taken"] == "then"
    assert _agent_ran(h.ledger, "a_then")


# ---- gate=True binding branch ---------------------------------------------

def test_gate_true_condition_emits_gate_open(tmp_path):
    """gate=True -> branch is BINDING: emit gate_open(reason='binding_branch')
    + raise GateHalt; the chosen branch does NOT execute until resolved."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "True", ["a_then"], gate=True),
                  "a_then": _agent("a_then")})
    with pytest.raises(GateHalt) as ei:
        h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    gate_id = ei.value.gate_id
    # gate_open emitted with reason='binding_branch'
    go = next(e for e in h.ledger.events()
              if e.get("kind") == "gate_open" and e.get("gate_id") == gate_id)
    assert go["reason"] == "binding_branch"
    assert go["node_id"] == "c1"
    # branch_decision was emitted BEFORE the gate (FACT recorded first)
    assert _bd_payload(h.ledger)["branch_taken"] == "then"
    # the branch body did NOT execute (gate halted first)
    assert not _agent_ran(h.ledger, "a_then")


def test_gate_true_after_resolve_executes_branch(tmp_path):
    """After gate_resolve(allow), re-dispatch skips re-gating and runs the
    chosen branch (resume flow)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "True", ["a_then"], gate=True),
                  "a_then": _agent("a_then")})
    # first dispatch: gate_open + GateHalt
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    # human resolves the gate (allow)
    gate_open = next(e for e in h.ledger.events()
                     if e.get("kind") == "gate_open" and e.get("node_id") == "c1")
    h.resolve_gate(gate_open["gate_id"], "allow")
    # second dispatch: gate already resolved -> branch executes
    out = h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    assert _agent_ran(h.ledger, "a_then")
    assert out["validated_output"] == {"answer": "42"}
    # no second gate_open (resume skips re-gating)
    gate_opens = [e for e in h.ledger.events()
                  if e.get("kind") == "gate_open" and e.get("node_id") == "c1"]
    assert len(gate_opens) == 1


def test_gate_true_deny_does_not_execute_branch(tmp_path):
    """gate_resolve(decision='deny') is TERMINAL: the binding branch does NOT
    execute; the condition returns empty output (deny blocks the binding
    path, not "try the other branch"). derive_verdict then sees the
    requirement unmet (PARTIAL/UNVERIFIED) -- the honest deny outcome."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "True", ["a_then"], gate=True),
                  "a_then": _agent("a_then")})
    # first dispatch: gate_open + GateHalt
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    # human DENIES the binding branch
    gate_open = next(e for e in h.ledger.events()
                     if e.get("kind") == "gate_open" and e.get("node_id") == "c1")
    h.resolve_gate(gate_open["gate_id"], "deny")
    # second dispatch: deny is terminal -> branch does NOT execute
    out = h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    assert out == {"validated_output": {}}
    assert not _agent_ran(h.ledger, "a_then")
    # gate_resolve(deny) recorded
    assert any(e.get("kind") == "gate_resolve" and e.get("decision") == "deny"
               for e in h.ledger.events())
    # no second gate_open (deny is terminal, not re-gate)
    gate_opens = [e for e in h.ledger.events()
                  if e.get("kind") == "gate_open" and e.get("node_id") == "c1"]
    assert len(gate_opens) == 1


# ---- event shape ----------------------------------------------------------

def test_branch_decision_event_recorded(tmp_path):
    """branch_decision carries all §3 fields; top-level node_id authoritative."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"c1": _condition("c1", "dry_count() < 2",
                                   ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")})
    h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)
    bd = _branch_decision(h.ledger)
    # top-level fields (canonical read location per Task 5 parked finding)
    assert bd["kind"] == "branch_decision"
    assert bd["spec_version_id"] == "spec.v1"
    assert bd["node_id"] == "c1"
    # payload fields
    p = bd["payload"]
    assert p["node_id"] == "c1"  # redundant copy (kept for §3 shape parity)
    assert p["predicate"] == "dry_count() < 2"
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"
    assert p["degraded"] is False
    assert p["claim_strength"] == "deterministic"
