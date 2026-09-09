from axiom.harness import Harness


def test_high_risk_node_gates(tmp_path):
    h = Harness(tmp_path / "run")
    assert h.check_gate({"type": "agent", "id": "n", "risk": "high",
                         "write_areas": ["src/**"]}) is True


def test_secrets_write_area_gates(tmp_path):
    h = Harness(tmp_path / "run")
    assert h.check_gate({"type": "agent", "id": "n", "risk": "low",
                         "write_areas": ["**/secrets/**"]}) is True


def test_delete_write_area_gates(tmp_path):
    h = Harness(tmp_path / "run")
    assert h.check_gate({"type": "agent", "id": "n", "risk": "low",
                         "write_areas": ["delete/users/**"]}) is True


def test_low_risk_safe_area_no_gate(tmp_path):
    h = Harness(tmp_path / "run")
    assert h.check_gate({"type": "agent", "id": "n", "risk": "low",
                         "write_areas": ["src/auth/**"]}) is False


def test_gate_open_logged(tmp_path):
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v1")
    assert gid.startswith("G_n1_")
    evs = h.ledger.events()
    assert any(e["kind"] == "gate_open" and e["gate_id"] == gid
               and e["reason"] == "contract_drift" for e in evs)


def test_modify_creates_revision_not_inplace(tmp_path):
    import json as _json
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v1")
    h.resolve_gate(gid, "modify", modify_intent="new intent")
    # modify -> spec.v{parent_rev+1} = spec.v2.json, never in-place
    assert (h.run_dir / "spec.v2.json").exists()
    v2 = _json.loads((h.run_dir / "spec.v2.json").read_text())
    assert v2["intent"] == "new intent"
    assert v2["contract_drift"] is True
    assert v2["parent_spec_id"] == "spec.v1"
    # v1 was never written (no in-place mutation)
    assert not (h.run_dir / "spec.v1.json").exists()


def test_allow_resolves_gate(tmp_path):
    h = Harness(tmp_path / "run")
    gid = h._gate("risky_worker", "n1", "spec.v1")
    h.resolve_gate(gid, "allow")
    assert any(e["kind"] == "gate_resolve" and e["gate_id"] == gid
               and e["decision"] == "allow" for e in h.ledger.events())


def test_deny_resolves_gate(tmp_path):
    h = Harness(tmp_path / "run")
    gid = h._gate("risky_worker", "n1", "spec.v1")
    h.resolve_gate(gid, "deny")
    assert any(e["kind"] == "gate_resolve" and e["decision"] == "deny"
               for e in h.ledger.events())


def test_modify_logs_replan(tmp_path):
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v1")
    h.resolve_gate(gid, "modify", modify_intent="x")
    evs = h.ledger.events()
    assert any(e["kind"] == "replan" and e["checkpoint_ref"].startswith("checkpoint:gate_")
               and e["revision_trigger"]["gate"] == gid for e in evs)


def test_modify_off_higher_revision(tmp_path):
    import json as _json
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v3")
    h.resolve_gate(gid, "modify", modify_intent="rev")
    assert (h.run_dir / "spec.v4.json").exists()
