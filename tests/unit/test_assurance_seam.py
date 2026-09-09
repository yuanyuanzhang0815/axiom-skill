"""seam (b): change-assurance -> axiom Workflow Enforcement.

Tests the hard boundary the whole design exists for: an agent's text claim of
"done" cannot make axiom advance when change-assurance's machine verdict is
not VERIFIED. The harness re-derives the agent_verdict verdict from
adjudicate.py against the agent's own receipt, overriding the LLM's
verdict_field transcription.

These run the REAL adjudicate.py subprocess (no mock) against real receipt
files in a temp worktree, then assert derive_verdict (the axiom single exit).
"""
import json
from pathlib import Path

import pytest

from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.state import derive_verdict

SKILL_DIR = str(Path(__file__).resolve().parent.parent.parent / "change-assurance")


def _spec_with_assurance_node():
    s = Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i", requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"], success_evidence=["S1:R1=node:n_assurance"],
        nodes={"n_assurance": {
            "type": "agent", "id": "n_assurance", "prompt": "p",
            "verdict_field": "assurance_verdict",
            "assurance_hook": {"receipt_path": "receipt.json"},
        }},
        control_flow={"type": "sequence", "steps": []}, decision_trace=[],
        budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1,
    )
    return s


def _run_hook(tmp_path, validated, receipt_dict):
    """Write receipt into a temp worktree, run _emit_agent_verdict for real,
    return (agent_verdict_event, spec)."""
    (tmp_path / "receipt.json").write_text(json.dumps(receipt_dict), encoding="utf-8")
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    node = _spec_with_assurance_node().nodes["n_assurance"]
    h._emit_agent_verdict(node, validated, "spec.v1", wt_path=tmp_path)
    avs = [e for e in h.ledger.events() if e.get("kind") == "agent_verdict"]
    assert len(avs) == 1, f"expected 1 agent_verdict, got {len(avs)}"
    return avs[0], h.ledger, _spec_with_assurance_node()


RECEIPT_VERIFIED = {
    "write_areas": ["docs/readme.md"], "declared_risk": "V0",
    "evidence": [{"claim": "docs ok", "type": "static-scan",
                  "addresses": ["docs"],
                  "produced_by": "grep", "executed_by": "h",
                  "adjudicated_by": "r", "result": "pass"}],
    "unresolved": [], "capability_manifest": {"runtime_mechanisms": ["http"]}}

RECEIPT_PARTIAL = {
    "write_areas": ["src/components/Sidebar.tsx", "src/stores/workspaceStore.ts"],
    "declared_risk": "V2",
    "evidence": [
        {"claim": "no spread", "type": "static-scan",
         "addresses": ["frontend_presentational", "shared-state"],
         "produced_by": "grep", "executed_by": "h",
         "adjudicated_by": "r", "result": "pass"},
        {"claim": "e2e ok", "type": "e2e",
         "addresses": ["shared-state"],
         "produced_by": "pw", "executed_by": "h",
         "adjudicated_by": "r", "result": "pass"}],
    "unresolved": [{"id": "shared-state baseline", "impact": "medium"}],
    "capability_manifest": {"runtime_mechanisms": ["http", "shared_store"]}}

RECEIPT_UNVERIFIED = {
    "write_areas": ["docs/readme.md"], "declared_risk": "V0",
    "evidence": [{"claim": "flaky", "type": "static-scan",
                  "addresses": ["docs"],
                  "produced_by": "grep", "executed_by": "h",
                  "adjudicated_by": "r", "result": "flaky"}],
    "unresolved": [], "capability_manifest": {"runtime_mechanisms": ["http"]}}


def test_case1_assurance_verified_axiom_advances(tmp_path, monkeypatch):
    """Case 1: assurance = VERIFIED -> axiom may VERIFIED."""
    monkeypatch.setenv("CHANGE_ASSURANCE_SKILL_DIR", SKILL_DIR)
    validated = {"assurance_verdict": "VERIFIED"}  # LLM honest
    ev, ledger, spec = _run_hook(tmp_path, validated, RECEIPT_VERIFIED)
    assert ev["verdict"] == "VERIFIED"
    assert not ev["payload"].get("mismatch")  # LLM honest -> no mismatch key
    assert derive_verdict(spec, ledger) == "VERIFIED"


def test_case2_assurance_partial_axiom_blocked(tmp_path, monkeypatch):
    """Case 2: assurance = PARTIAL -> axiom must NOT VERIFIED."""
    monkeypatch.setenv("CHANGE_ASSURANCE_SKILL_DIR", SKILL_DIR)
    validated = {"assurance_verdict": "PARTIAL"}  # LLM honest
    ev, ledger, spec = _run_hook(tmp_path, validated, RECEIPT_PARTIAL)
    assert ev["verdict"] == "PARTIAL"
    assert derive_verdict(spec, ledger) == "PARTIAL"


def test_case3_assurance_unverified_axiom_blocked(tmp_path, monkeypatch):
    """Case 3: assurance = UNVERIFIED -> axiom must NOT VERIFIED."""
    monkeypatch.setenv("CHANGE_ASSURANCE_SKILL_DIR", SKILL_DIR)
    validated = {"assurance_verdict": "UNVERIFIED"}  # LLM honest
    ev, ledger, spec = _run_hook(tmp_path, validated, RECEIPT_UNVERIFIED)
    assert ev["verdict"] == "UNVERIFIED"
    assert derive_verdict(spec, ledger) != "VERIFIED"


def test_case4_llm_claims_verified_but_assurance_partial(tmp_path, monkeypatch):
    """Case 4 -- the design's reason for existing: the agent's text claims
    done (assurance_verdict="VERIFIED") but its own receipt adjudicates PARTIAL.
    The harness hook overrides with the script's PARTIAL; axiom's
    derive_verdict respects it -> final PARTIAL. LLM prose cannot bypass."""
    monkeypatch.setenv("CHANGE_ASSURANCE_SKILL_DIR", SKILL_DIR)
    validated = {"assurance_verdict": "VERIFIED"}  # LLM lies
    ev, ledger, spec = _run_hook(tmp_path, validated, RECEIPT_PARTIAL)
    # script-derived status wins over the LLM's claimed VERIFIED
    assert ev["verdict"] == "PARTIAL", (
        "LLM claimed VERIFIED but script said PARTIAL; hook must override")
    assert ev["payload"]["llm_claimed"] == "VERIFIED"
    assert ev["payload"]["script_status"] == "PARTIAL"
    assert ev["payload"]["mismatch"] is True
    # axiom single exit honors the machine verdict, not the LLM prose
    assert derive_verdict(spec, ledger) == "PARTIAL"


def test_case5_receipt_missing_safe_default_unverified(tmp_path, monkeypatch):
    """Bonus: no receipt written -> safe default UNVERIFIED, never a fake VERIFIED."""
    monkeypatch.setenv("CHANGE_ASSURANCE_SKILL_DIR", SKILL_DIR)
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    node = _spec_with_assurance_node().nodes["n_assurance"]
    # no receipt.json in tmp_path
    h._emit_agent_verdict(node, {"assurance_verdict": "VERIFIED"}, "spec.v1",
                          wt_path=tmp_path)
    ev = [e for e in h.ledger.events() if e.get("kind") == "agent_verdict"][0]
    assert ev["verdict"] == "UNVERIFIED"
    assert "receipt_missing" in ev["payload"]["note"]
    spec = _spec_with_assurance_node()
    assert derive_verdict(spec, h.ledger) != "VERIFIED"
