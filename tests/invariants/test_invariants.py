"""Adversarial invariant tests.

These prove the architecture's integrity guarantees hold under attack:
claims can't be forged to VERIFIED, ownership can't be bypassed, raw stdout
can't leak, specs are immutable, orphans/tamper are caught, optional criteria
don't cap the verdict, and PROPOSED is never silently promoted to fact.
"""
import json
from pathlib import Path
from axiom.ir import Spec, Requirement, contract_hash, make_revision, check_ownership
from axiom.ledger import Ledger
from axiom.state import derive_claim_status, derive_verdict
from axiom.harness import Harness


def _spec(nodes=None, steps=None, se=None, dt=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=se or ["S1:R1=claim:C1"], nodes=nodes or {},
        control_flow={"type": "sequence", "steps": steps or []},
        decision_trace=dt or [], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _agent(nid, write_areas=None):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": write_areas or [], "acceptance": ["a"], "failure_policy": {},
    }


# --- VERIFIED cannot be forged ----------------------------------------

def test_agent_cannot_self_certify_verified(tmp_path):
    """An agent_result claiming strength='verified' is NOT VERIFIED."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "node_id": "n1",
               "claims": [{"claim_id": "C1", "strength": "verified"}], "payload": {}})
    assert derive_claim_status("C1", lg) == "PROPOSED"  # not VERIFIED


def test_verified_only_via_verify_survived(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "survived": True, "refuted": False})
    assert derive_claim_status("C1", lg) == "VERIFIED"


def test_last_write_wins_refuted_then_survived(tmp_path):
    """v2 §4 Q9: the LATEST verify_verdict is the current truth about a claim
    (last-write-wins, NOT refuted-dominates). Iterative refinement (defect
    closure) requires a refuted claim to be resurrectable by a later survived
    verdict -- the agent fixed the defect in a subsequent iteration. The v1
    'refuted overrides survived' accumulation was overturned by spec §4 Q9;
    adversarial guard: loop-refine-last-verify-wins."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": True})
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1",
               "survived": True})
    assert derive_claim_status("C1", lg) == "VERIFIED"


def test_proposed_is_not_fact(tmp_path):
    """A claim with no evidence is PROPOSED, never silently elevated."""
    lg = Ledger(tmp_path / "run")
    assert derive_claim_status("C1", lg) == "PROPOSED"


# --- ownership cannot be bypassed -------------------------------------

def test_overlapping_write_areas_rejected():
    spec = _spec(
        nodes={"a": _agent("a", ["src/**"]), "b": _agent("b", ["src/**"])},
        steps=[["parallel", "a", "b"]],
    )
    errs = check_ownership(spec)
    assert errs  # overlap detected


def test_non_overlapping_write_areas_pass():
    spec = _spec(
        nodes={"a": _agent("a", ["src/auth/**"]), "b": _agent("b", ["src/pay/**"])},
        steps=[["parallel", "a", "b"]],
    )
    assert check_ownership(spec) == []


# --- type boundary: raw stdout never downstream -----------------------

def test_dispatch_agent_leaks_nothing_but_validated_output(tmp_path):
    def fake_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"x": 1}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=fake_runner)
    out = h.dispatch_agent(_agent("n1"), {}, "spec.v1")
    assert set(out.keys()) == {"validated_output"}
    assert "result_text" not in out
    assert "raw_stdout" not in out
    assert "session_id" not in out  # no internal worker fields leak


# --- spec immutability ------------------------------------------------

def test_revision_does_not_mutate_parent():
    parent = _spec(nodes={"n1": _agent("n1")}, steps=["n1"])
    parent_hash = contract_hash(parent)
    child = make_revision(parent, "replan", {"gate": "G1"}, {"nodes": "changed"})
    assert parent.revision == 1          # parent untouched
    assert child.revision == 2
    assert child.parent_spec_id == "spec.v1"
    assert parent.contract_drift is False
    # nodes/plan change does NOT drift the contract
    assert child.contract_drift is False
    assert contract_hash(parent) == contract_hash(child) == parent_hash


def test_contract_four_change_drifts_contract():
    parent = _spec(nodes={"n1": _agent("n1")}, steps=["n1"])
    child = make_revision(parent, "drift", {}, {}, new_intent="changed")
    assert child.contract_drift is True
    assert contract_hash(parent) != contract_hash(child)


# --- provenance integrity ---------------------------------------------

def test_orphan_evidence_flagged(tmp_path):
    spec = _spec(dt=[{"decision_id": "D1", "evidence_refs": ["event:E_GHOST"]}])
    lg = Ledger(tmp_path / "run")  # empty: E_GHOST does not exist
    errs = lg.check_refs(spec)
    assert errs and "orphan" in errs[0]


def test_reverse_index_is_derived_not_agent_written(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "payload": {}})
    spec = _spec(dt=[{"decision_id": "D1", "evidence_refs": ["event:E1"]}])
    idx = lg.derive_reverse_index(spec)
    assert idx == {"E1": ["D1"]}            # derived from forward refs
    assert "evidence_for" not in lg.events()[0]  # agents never wrote it


# --- tamper detection -------------------------------------------------

def test_tampered_event_detected(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "node_id": "n1",
               "payload": {"x": 1}})
    # in-place mutation of a stored event
    lines = lg.events_path.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[0])
    ev["payload"]["x"] = 999
    lg.events_path.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    errs = lg.verify_chain()
    assert errs and any("tamper" in e or "event_hash" in e for e in errs)


def test_intact_chain_has_no_breaks(tmp_path):
    lg = Ledger(tmp_path / "run")
    for i in range(5):
        lg.append({"event_id": f"E{i}", "kind": "agent_result", "payload": {"i": i}})
    assert lg.verify_chain() == []


# --- verdict integrity ------------------------------------------------

def test_optional_unmet_does_not_cap_verdict(tmp_path):
    spec = _spec(
        se=["S1:R1=claim:C1", "S2:R2=claim:C2"],
        nodes={},
    )
    spec.requirements = [Requirement("R1", "r", "required"),
                         Requirement("R2", "opt", "optional")]
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "survived": True, "refuted": False})
    # C2 never verified, but R2 is optional -> verdict stays VERIFIED
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_unresolved_gate_blocks_verdict(tmp_path):
    spec = _spec(nodes={"n1": _agent("n1")}, steps=["n1"])
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
               "reason": "risky"})
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1",
               "survived": True, "refuted": False})
    assert derive_verdict(spec, lg) == "BLOCKED"
