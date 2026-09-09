"""debug-projection: axiom `debug` failure-forensic envelope.

Ports comet's debug-gate concept (root-cause first, named recovery) into
axiom's projection layer. The envelope is a pure read-only rebuild from the
ledger: it never appends, never calls an LLM.
"""
import json
from pathlib import Path

import pytest

from axiom.cli import main as cli_main
from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.ledger import Ledger
from axiom.state import derive_debug_envelope, derive_cognitive_signature, \
    derive_schema_fail_keys


def _spec(success_evidence=("S1:R1=claim:C1",), nodes=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i", requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"], success_evidence=list(success_evidence),
        nodes=nodes or {}, control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _ev(kind, **kw):
    e = {"event_id": kw.pop("eid", f"ev_{kind}"), "kind": kind}
    e.update(kw)
    return e


# --- Scenario 1: stagnating node surfaces in the envelope --------------------

def test_stagnating_node_surfaces(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object",
                                                 "required": ["claim_id"]}}})
    svid = "spec.v1"
    # a stagnating event: denials present (cognitive signature = denials set)
    h.ledger.append(_ev("stagnating", spec_version_id=svid, node_id="n1",
                        payload={"attempt": 2,
                                 "denials": ["no_write:src/a.py"],
                                 "conforms": False, "cost_usd": 0.01,
                                 "num_turns": 1,
                                 "result_text_preview": "could not write"}))
    env = derive_debug_envelope(spec, h.ledger)
    assert env["verdict"] == "PARTIAL"  # R1 bound to C1 but not VERIFIED
    assert not env["no_failures"]
    f = next(f for f in env["failures"] if f["node_id"] == "n1")
    assert f["failure_class"] == "stagnating"
    assert f["recovery_action"] == "re-dispatch"
    assert f["cognitive_signature"]["kind"] == "denials"
    assert f["cognitive_signature"]["value"] == ["no_write:src/a.py"]
    assert f["last_preview"] == "could not write"


# --- Scenario 2: unresolved gate_open ---------------------------------------

def test_unresolved_gate_surfaces(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec()
    svid = "spec.v1"
    h.ledger.append(_ev("gate_open", spec_version_id=svid, node_id="n1",
                        gate_id="g1", reason="unretriable_failure",
                        payload={}))
    env = derive_debug_envelope(spec, h.ledger)
    assert env["verdict"] == "BLOCKED"
    f = next(f for f in env["failures"] if f["node_id"] == "n1")
    assert f["failure_class"] == "gate_open"
    assert f["recovery_action"] == "gate-resolve"
    assert f["gate_reason"] == "unretriable_failure"


def test_resolved_gate_not_in_envelope(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec()
    svid = "spec.v1"
    h.ledger.append(_ev("gate_open", spec_version_id=svid, node_id="n1",
                        gate_id="g1", reason="risk_high", payload={}))
    h.ledger.append(_ev("gate_resolve", spec_version_id=svid,
                        gate_id="g1", payload={}))
    env = derive_debug_envelope(spec, h.ledger)
    assert all(f.get("failure_class") != "gate_open" for f in env["failures"])


# --- Scenario 3: budget exhausted -------------------------------------------

def test_budget_exhausted_surfaces(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec()
    svid = "spec.v1"
    h.ledger.append(_ev("budget_exhausted", spec_version_id=svid,
                        payload={"cost_total": 6.0}))
    env = derive_debug_envelope(spec, h.ledger)
    f = next(f for f in env["failures"] if f["failure_class"] == "budget_exhausted")
    assert f["recovery_action"] == "raise-cap-or-replan"
    assert f["cost_total"] == 6.0


# --- Scenario 4: loop-tagged stagnation carries loop context ----------------

def test_loop_stagnation_carries_context(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object"}}})
    svid = "spec.v1"
    h.ledger.append(_ev("stagnating", spec_version_id=svid, node_id="n1",
                        payload={"attempt": 1, "denials": [],
                                 "conforms": False, "cost_usd": 0.01,
                                 "num_turns": 1,
                                 "result_text_preview": "stuck",
                                 "loop_id": "loop_fix", "iteration": 2}))
    env = derive_debug_envelope(spec, h.ledger)
    f = next(f for f in env["failures"] if f["node_id"] == "n1")
    assert f["loop_id"] == "loop_fix"
    assert f["iteration"] == 2


# --- Scenario 5: a fully VERIFIED run -> no failures ------------------------

def test_verified_run_no_failures(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec()
    svid = "spec.v1"
    # a verify_verdict that survived -> R1 satisfied -> VERIFIED
    h.ledger.append(_ev("agent_result", spec_version_id=svid, node_id="n_v",
                        claims=[], payload={"validated_output": {},
                                            "cost_usd": 0.0, "num_turns": 1,
                                            "permission_denials": [],
                                            "result_text_preview": "",
                                            "input_hash": None}))
    h.ledger.append(_ev("verify_verdict", spec_version_id=svid, node_id="n_v",
                        claim_id="C1", refuted=False, survived=True,
                        payload={"refute_votes": 0, "abstain_votes": 0,
                                 "skeptic_count": 3,
                                 "survival_rule": "majority_unrefuted"}))
    env = derive_debug_envelope(spec, h.ledger)
    assert env["verdict"] == "VERIFIED"
    assert env["no_failures"] is True
    assert env["failures"] == []


# --- Scenario 6: bad run-dir -> CLI exit 1, no traceback --------------------

def test_bad_run_dir_cli_exit_1(tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "spec_version_id": "spec.v1", "parent_spec_id": None, "revision": 1,
        "intent": "i", "requirements": [{"id": "R1", "text": "r",
                                          "criticality": "required"}],
        "boundaries": ["b"], "success_evidence": ["S1:R1=claim:C1"],
        "nodes": {}, "control_flow": {"type": "sequence", "steps": []},
        "decision_trace": [], "budget_usd": 5.0, "max_concurrent": 16,
        "max_agents": 1000, "max_stagnation": 1,
    }), encoding="utf-8")
    rc = cli_main(["debug", str(spec_path),
                   "--run-dir", str(tmp_path / "nope")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no ledger" in err or "events.jsonl missing" in err


# --- Scenario 7: idempotent read-only (ledger bytes unchanged) -------------

def test_idempotent_read_only(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object"}}})
    svid = "spec.v1"
    h.ledger.append(_ev("stagnating", spec_version_id=svid, node_id="n1",
                        payload={"attempt": 1, "denials": ["d1"],
                                 "conforms": False, "cost_usd": 0.01,
                                 "num_turns": 1,
                                 "result_text_preview": "x"}))
    before = (tmp_path / "run" / "events.jsonl").read_bytes()
    env1 = derive_debug_envelope(spec, h.ledger)
    env2 = derive_debug_envelope(spec, h.ledger)
    after = (tmp_path / "run" / "events.jsonl").read_bytes()
    assert before == after  # ledger untouched
    assert env1 == env2    # deterministic


# --- Scenario 8: signature migration does not regress shape ------------------

def test_cognitive_signature_shape_unchanged():
    # the harness used this exact shape to trip stagnation; after lifting to
    # state.py the shape MUST stay the same so existing stagnation tests hold.
    assert derive_cognitive_signature(["d1", "d2"], True) == ("denials",
                                                              frozenset({"d1", "d2"}))
    assert derive_cognitive_signature([], False) == ("schema",
                                                      frozenset({"schema"}))
    assert derive_cognitive_signature([], True) is None


def test_schema_fail_keys_shape():
    keys = derive_schema_fail_keys({}, {"type": "object", "required": ["claim_id"]})
    assert keys == frozenset({"missing:claim_id"})


# --- CLI happy path: stagnating run -> JSON envelope, exit 1 ----------------

def test_cli_json_envelope(tmp_path, capsys):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object",
                                                 "required": ["claim_id"]}}})
    svid = "spec.v1"
    h.ledger.append(_ev("stagnating", spec_version_id=svid, node_id="n1",
                        payload={"attempt": 2, "denials": ["no_write"],
                                 "conforms": False, "cost_usd": 0.01,
                                 "num_turns": 1,
                                 "result_text_preview": "could not"}))
    h.ledger.seal()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "spec_version_id": "spec.v1", "parent_spec_id": None, "revision": 1,
        "intent": "i", "requirements": [{"id": "R1", "text": "r",
                                          "criticality": "required"}],
        "boundaries": ["b"], "success_evidence": ["S1:R1=claim:C1"],
        "nodes": {"n1": {"type": "agent", "id": "n1",
                         "output_schema": {"type": "object",
                                           "required": ["claim_id"]}}},
        "control_flow": {"type": "sequence", "steps": []},
        "decision_trace": [], "budget_usd": 5.0, "max_concurrent": 16,
        "max_agents": 1000, "max_stagnation": 1,
    }), encoding="utf-8")
    rc = cli_main(["debug", str(spec_path), "--run-dir",
                   str(tmp_path / "run"), "--json"])
    out = capsys.readouterr().out
    assert rc == 1
    env = json.loads(out)
    assert env["no_failures"] is False
    assert any(f["failure_class"] == "stagnating" for f in env["failures"])
