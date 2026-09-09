from axiom.ledger import Ledger
from axiom.state import derive_claim_status


def test_inferred_claim_is_proposed(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "claims": [{"claim_id": "C1", "text": "t", "strength": "inferred"}]})
    assert derive_claim_status("C1", lg) == "PROPOSED"


def test_supported_claim_is_supported(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "claims": [{"claim_id": "C1", "text": "t", "strength": "supported"}]})
    assert derive_claim_status("C1", lg) == "SUPPORTED"


def test_refuted_after_skeptic(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "claims": [{"claim_id": "C1", "text": "t"}]})
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1", "refuted": True})
    assert derive_claim_status("C1", lg) == "REFUTED"


def test_verified_only_via_survived_verify(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "claims": [{"claim_id": "C1", "text": "t", "strength": "supported"}]})
    assert derive_claim_status("C1", lg) == "SUPPORTED"  # not VERIFIED without verify
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    assert derive_claim_status("C1", lg) == "VERIFIED"


def test_unknown_claim_is_proposed(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "claims": [{"claim_id": "C1", "text": "t"}]})
    assert derive_claim_status("C2", lg) == "PROPOSED"  # C2 never mentioned


def test_refuted_dominates_even_if_supported(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "claims": [{"claim_id": "C1", "text": "t", "strength": "supported"}]})
    lg.append({"event_id": "E2", "kind": "verify_verdict", "claim_id": "C1", "refuted": True})
    assert derive_claim_status("C1", lg) == "REFUTED"


# --- svid scoping: spec.v2 evidence must not leak from/into spec.v1 ---

def test_svid_scope_isolates_claim_versions(tmp_path):
    # spec.v1 recorded C1 supported; spec.v2 (same run-dir) refuted C1.
    # svid=spec.v2 -> REFUTED (only v2's verify_verdict counts);
    # svid=spec.v1 -> SUPPORTED (only v1's agent_result counts).
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result", "spec_version_id": "spec.v1",
               "claims": [{"claim_id": "C1", "text": "t", "strength": "supported"}]})
    lg.append({"event_id": "E2", "kind": "verify_verdict", "spec_version_id": "spec.v2",
               "claim_id": "C1", "refuted": True})
    assert derive_claim_status("C1", lg, svid="spec.v2") == "REFUTED"
    assert derive_claim_status("C1", lg, svid="spec.v1") == "SUPPORTED"
