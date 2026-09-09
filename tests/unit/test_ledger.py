import json
import pathlib
from axiom.ledger import Ledger, _GENESIS_HASH


def test_append_chains_hashes(tmp_path):
    lg = Ledger(tmp_path / "run")
    e1 = lg.append({"event_id": "E1", "kind": "agent_result", "payload": {"x": 1}})
    e2 = lg.append({"event_id": "E2", "kind": "agent_result", "payload": {"x": 2}})
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e2["prev_event_hash"] == e1["event_hash"]
    assert e1["prev_event_hash"] == _GENESIS_HASH
    assert lg.verify_chain() == []


def test_first_event_anchors_genesis(tmp_path):
    lg = Ledger(tmp_path / "run")
    e1 = lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    assert e1["prev_event_hash"] == _GENESIS_HASH


def test_tamper_detected(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    lg.append({"event_id": "E2", "kind": "x", "payload": {"y": 2}})
    # tamper E2's payload in place
    lines = pathlib.Path(lg.events_path).read_text().splitlines()
    bad = json.loads(lines[1])
    bad["payload"]["y"] = 999
    lines[1] = json.dumps(bad)
    pathlib.Path(lg.events_path).write_text("\n".join(lines) + "\n")
    errs = lg.verify_chain()
    assert any("tamper" in e for e in errs)


def test_seq_gap_detected(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    lg.append({"event_id": "E2", "kind": "x", "payload": {}})
    # tamper E2's seq
    lines = pathlib.Path(lg.events_path).read_text().splitlines()
    bad = json.loads(lines[1])
    bad["seq"] = 99
    lines[1] = json.dumps(bad)
    pathlib.Path(lg.events_path).write_text("\n".join(lines) + "\n")
    errs = lg.verify_chain()
    assert any("seq break" in e for e in errs)


def test_seal_writes_root_hash(tmp_path):
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "x", "payload": {}})
    lg.seal()
    manifest = json.loads(pathlib.Path(lg.manifest_path).read_text())
    assert manifest["final_event_hash"] == lg.events()[-1]["event_hash"]


def test_empty_ledger_verifies_clean(tmp_path):
    lg = Ledger(tmp_path / "run")
    assert lg.verify_chain() == []
    lg.seal()
    assert json.loads(pathlib.Path(lg.manifest_path).read_text())["final_event_hash"] == _GENESIS_HASH
