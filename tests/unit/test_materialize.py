"""B4: run materializes verdict.json / checkpoints.jsonl / packet.md."""
import json
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, contract_hash


def _spec():
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="review the thing", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b1"], success_evidence=["S1:R1=claim:C1"], nodes={},
                control_flow={"type": "sequence", "steps": []}, decision_trace=[],
                budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1)


def test_materialize_writes_three_files(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "ev_1", "kind": "verify_verdict",
                     "claim_id": "C1", "refuted": False, "survived": True})
    spec = _spec()
    cp = h.materialize(spec)
    assert (tmp_path / "run" / "verdict.json").exists()
    assert (tmp_path / "run" / "checkpoints.jsonl").exists()
    assert (tmp_path / "run" / "packet.md").exists()
    vj = json.loads((tmp_path / "run" / "verdict.json").read_text())
    assert vj["verdict"] == "VERIFIED"
    assert vj["contract_hash"] == contract_hash(spec)
    assert vj["dispatch_count"] == 0  # no agent_result events


def test_checkpoints_jsonl_appends_per_run(tmp_path):
    h = Harness(tmp_path / "run")
    spec = _spec()
    h.materialize(spec)
    h.materialize(spec)
    lines = (tmp_path / "run" / "checkpoints.jsonl").read_text().splitlines()
    assert len(lines) == 2  # append-only across runs


def test_packet_md_is_human_readable(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append({"event_id": "ev_1", "kind": "verify_verdict",
                     "claim_id": "C1", "refuted": False, "survived": True})
    h.materialize(_spec())
    md = (tmp_path / "run" / "packet.md").read_text()
    assert "review the thing" in md
    assert "R1" in md and "required" in md
    assert "VERIFIED" in md
