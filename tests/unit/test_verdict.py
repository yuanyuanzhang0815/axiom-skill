from axiom.ledger import Ledger
from axiom.ir import Spec, Requirement
from axiom.state import derive_verdict


def _spec(reqs):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=reqs,
        boundaries=["b"],
        success_evidence=["S1:R1=s"],
        nodes={},
        control_flow={"type": "sequence", "steps": []},
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def test_all_required_met_is_verified(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_optional_unmet_does_not_cap(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    spec = _spec([Requirement("R1", "r", "required"), Requirement("R2", "opt", "optional")])
    spec.success_evidence = ["S1:R1=claim:C1", "S2:R2=claim:C2"]  # C2 never verified
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_required_unmet_is_partial(tmp_path):
    lg = Ledger(tmp_path / "run")  # no evidence
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]
    assert derive_verdict(spec, lg) == "PARTIAL"


def test_gate_unresolved_blocks(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1", "reason": "contract_drift"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    assert derive_verdict(spec, lg) == "BLOCKED"


def test_gate_resolved_does_not_block(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "gate_open", "gate_id": "G1", "reason": "risky"})
    lg.append({"event_id": "E2", "kind": "gate_resolve", "gate_id": "G1", "decision": "allow"})
    lg.append({"event_id": "E3", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_no_required_criteria_bound_is_unverified(tmp_path):
    lg = Ledger(tmp_path / "run")
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1"]  # no :Rn binding
    assert derive_verdict(spec, lg) == "UNVERIFIED"


def test_refuted_required_claim_is_partial(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": True, "survived": False})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]
    assert derive_verdict(spec, lg) == "PARTIAL"


# --- v1.3 agent-verdict: an agent node owns a requirement's verdict ---
# An agent (e.g. a code-review verifier) declares verdict_field; on clean
# conform it emits an agent_verdict event. success_evidence "S1:R1=node:n5"
# binds R1 to n5's verdict (like claim: binds to a verify node's claim).

def test_agent_verdict_node_binding_satisfies_requirement(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict", "node_id": "n5",
               "verdict": "VERIFIED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_agent_verdict_failed_leaves_partial(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict", "node_id": "n5",
               "verdict": "FAILED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "PARTIAL"


def test_agent_verdict_missing_leaves_partial(tmp_path):
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, Ledger(tmp_path / "run")) == "PARTIAL"


# --- stale-evidence leak: a spec.v2 re-run must not inherit spec.v1's verdict ---
# Regression: derive_verdict/_agent_verdict_verified used to scan the whole
# ledger unscoped by spec_version_id, and _agent_verdict_verified returned on
# the FIRST match (append order = spec.v1 before spec.v2). So a spec.v1
# agent_verdict=VERIFIED masked a spec.v2 FAILED in the same run-dir ->
# derive_verdict(spec.v2) wrongly returned VERIFIED. Fixed by scoping all
# verdict projection to spec.spec_version_id.

def test_v2_failed_not_masked_by_v1_verified(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "VERIFIED"})
    lg.append({"event_id": "E2", "kind": "agent_verdict",
               "spec_version_id": "spec.v2", "node_id": "n5", "verdict": "FAILED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.spec_version_id = "spec.v2"
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "PARTIAL"


def test_v2_verified_ignores_v1_failed(tmp_path):
    # symmetric: spec.v1 FAILED must not block spec.v2 VERIFIED in the same dir.
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "FAILED"})
    lg.append({"event_id": "E2", "kind": "agent_verdict",
               "spec_version_id": "spec.v2", "node_id": "n5", "verdict": "VERIFIED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.spec_version_id = "spec.v2"
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "VERIFIED"


def test_v1_verdict_unchanged_when_v2_present(tmp_path):
    # svid scoping is symmetric: spec.v1 still sees only v1 evidence even when
    # a spec.v2 FAILED event sits later in the same ledger.
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "VERIFIED"})
    lg.append({"event_id": "E2", "kind": "agent_verdict",
               "spec_version_id": "spec.v2", "node_id": "n5", "verdict": "FAILED"})
    spec = _spec([Requirement("R1", "r", "required")])  # spec_version_id="spec.v1"
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "VERIFIED"
