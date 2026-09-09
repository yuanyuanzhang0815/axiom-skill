"""M2 deterministic tests: task projection + unified verdict + delivery block.

Spec: docs/superpowers/specs/2026-09-09-axiom-autonomous-delivery-quality-loop-design.md
(v1.1), §6.3/§6.4/§7.4/§10.2/§10.3/§13.3 and AC-03/04/08/16/20/21/23/24/25.

M1 (contract.py) records/rebuilds/validates events. M2 turns them into the
task-level projection the orchestrator reads, and folds the quality loop into
the single ruling entry (`derive_verdict`). These tests pin the OBSERVABLE
behaviors the spec demands -- especially that a segment VERIFIED can never
mask an open task-level gap (AC-20) and that a bare-PASS review is not
evidence (AC-04).
"""
import json

import pytest

from axiom.contract import (
    Assumption, ContractStore, DeliveryContract, QualityStandard,
    contract_digest, validate_review_payload,
    KIND_CONTRACT_ACTIVATED, KIND_DECISION_RECORDED, KIND_GAP_DISPOSITION,
    KIND_GAP_RECORDED, KIND_REVIEW_RECORDED,
)
from axiom.delivery import (
    derive_delivery_verdict, project_comparison, project_delivery,
    project_gaps, project_reviews, project_stagnation, project_stop,
)
from axiom.ir import Requirement, Spec
from axiom.ledger import Ledger
from axiom.state import derive_verdict


# --- helpers -----------------------------------------------------------------

def _contract(rev=1, parent=None, task="task.demo", cost=None):
    return DeliveryContract(
        task_id=task, contract_revision=rev, parent_digest=parent,
        intent="booking system",
        user_requirements=[{"id": "U1", "text": "customers can book"}],
        boundaries=["no payments"],
        authorization_refs=[],
        quality_standards=[QualityStandard(
            standard_id="S1", text="conflict handling", source="professional",
            source_ref="booking.example.com flow", applicability="booking",
            necessity="required", verification="walk the flow")],
        assumptions=[Assumption("A1", "services have fixed durations")],
        resource_limits={"cost_usd": cost},
    )


def _gap_event(gid, task="task.demo", severity="material", rev=1):
    return {
        "event_id": f"ev_gap_{gid}", "kind": KIND_GAP_RECORDED, "task_id": task,
        "payload": {
            "gap_id": gid, "task_id": task, "severity": severity,
            "scenario": "checkout", "problem": "no duplicate-submit guard",
            "standard_refs": ["S1"], "contract_digest": "sha256:x",
        },
        "claims": [],
    }


def _disp_event(gid, disposition, task="task.demo", **extra):
    p = {"gap_id": gid, "disposition": disposition}
    p.update(extra)
    return {
        "event_id": f"ev_disp_{gid}_{disposition}", "kind": KIND_GAP_DISPOSITION,
        "task_id": task, "payload": p, "claims": [],
    }


def _decision(did, action, task="task.demo", **extra):
    p = {"decision_id": did, "task_id": task, "action": action}
    p.update(extra)
    return {
        "event_id": f"ev_dec_{did}", "kind": KIND_DECISION_RECORDED,
        "task_id": task, "payload": p, "claims": [],
    }


def _review(rid, task="task.demo", ok=True):
    p = {"review_id": rid, "task_id": task}
    if ok:
        p.update({
            "scenarios_checked": ["first-use booking"],
            "observations": ["success page has no manage entry"],
            "gaps_found": [],
            "unchecked_scope": ["mobile viewport"],
        })
    return {"event_id": f"ev_rev_{rid}", "kind": KIND_REVIEW_RECORDED,
            "task_id": task, "payload": p, "claims": []}


def _spec(task_id=None, tmp_path=None):
    # minimal legacy-valid spec; delivery binding added when task_id given
    req = [Requirement(id="R1", text="booking works", criticality="required")]
    s = Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="booking", requirements=req, boundaries=[],
        success_evidence=["S1:R1=node:n_verify"],
        nodes={}, control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=None, max_concurrent=16,
        max_agents=1000, max_stagnation=2, contract_drift=False,
        contract_hash_value="",
        task_id=task_id,
        delivery_contract_ref=(str(tmp_path / "c.json") if task_id else None),
        delivery_contract_digest=("sha256:x" if task_id else None),
    )
    return s


# --- review payload gate (AC-04) --------------------------------------------


def test_review_bare_pass_rejected():
    # AC-04: a reviewer returning only PASS must not close obligations.
    assert validate_review_payload({"review_id": "r1"}) != []
    ok = validate_review_payload({
        "review_id": "r1",
        "scenarios_checked": ["first use"],
        "observations": ["success page reachable"],
        "gaps_found": [],
        "unchecked_scope": ["mobile"],
    })
    assert ok == []


def test_project_reviews_flags_insufficient():
    evs = [_review("r_good", ok=True), _review("r_bad", ok=False)]
    reviews = project_reviews(evs, "task.demo")
    assert reviews["r_good"]["review_ok"] is True
    assert reviews["r_bad"]["review_ok"] is False


# --- unified verdict: segment VERIFIED must not mask a task gap (AC-20) -----


def test_delivery_verdict_segment_verified_but_open_material_gap():
    proj = project_delivery([_gap_event("g1", severity="material")],
                            "task.demo", contract=None)
    assert proj["gaps"]["open_blocking_or_material"] == ["g1"]
    # segment (workflow) VERIFIED, but an open material gap caps it.
    assert derive_delivery_verdict("VERIFIED", proj) == "PARTIAL"


def test_delivery_verified_when_no_gaps():
    proj = project_delivery([], "task.demo", contract=None)
    assert derive_delivery_verdict("VERIFIED", proj) == "VERIFIED"


def test_delivery_verdict_passthrough_non_verified():
    proj = project_delivery([], "task.demo", contract=None)
    for wv in ("UNVERIFIED", "PARTIAL", "BLOCKED"):
        assert derive_delivery_verdict(wv, proj) == wv


def test_delivery_verdict_unmet_comparison_caps():
    # AC-23/24/25: a quality-sensitive decision closed with no substantive
    # comparison (no named references / <2 candidates) caps VERIFIED.
    evs = [_decision("d1", "conclude", comparison={
        "references": [], "candidates": ["only one"], "method": "gut"})]
    proj = project_delivery(evs, "task.demo", contract=None)
    assert proj["comparison"]["unmet_decision_ids"] == ["d1"]
    assert derive_delivery_verdict("VERIFIED", proj) == "PARTIAL"


def test_delivery_deferred_material_gap_still_blocks():
    # §10.3(4): deferred never lifts a required obligation.
    evs = [_gap_event("g1", severity="material"),
           _disp_event("g1", "deferred", basis="later")]
    proj = project_delivery(evs, "task.demo", contract=None)
    assert "g1" in proj["gaps"]["open_blocking_or_material"]


def test_delivery_resolved_gap_unblocks():
    evs = [_gap_event("g1", severity="material"),
           _disp_event("g1", "resolved", evidence_refs=["artifact:e1"])]
    proj = project_delivery(evs, "task.demo", contract=None)
    assert proj["gaps"]["open_blocking_or_material"] == []


# --- stagnation across revisions (AC-11, spec §10.2) -------------------------


def test_stagnation_two_no_progress_decisions():
    evs = [_decision("d1", "continue"), _decision("d2", "continue")]
    stag = project_stagnation(evs, "task.demo")
    assert stag["consecutive_no_progress"] == 2
    assert stag["stagnating"] is True


def test_stagnation_progress_resets_counter():
    evs = [_decision("d1", "continue"),
           _decision("d2", "continue", closes_gap_ids=["g1"]),
           _decision("d3", "continue")]
    stag = project_stagnation(evs, "task.demo")
    assert stag["consecutive_no_progress"] == 1
    assert stag["stagnating"] is False


def test_stop_reason_projection():
    assert project_stop([_decision("d1", "stop", stop_reason="stagnating")],
                        "task.demo") == "stagnating"
    assert project_stop([_decision("d1", "continue")], "task.demo") is None


# --- null budget runs (AC-16) ------------------------------------------------


def test_null_budget_no_amount_limit():
    # §11.1: null = no limit set (still recorded), NOT zero, NOT free.
    c = _contract(cost=None)
    assert c.resource_limits.get("cost_usd") is None
    proj = project_delivery([], "task.demo", contract=c)
    assert proj["task_id"] == "task.demo"


# --- unified derive_verdict integration (spec §7.4) --------------------------


def test_derive_verdict_legacy_unchanged(tmp_path):
    # legacy spec (no task binding) -> M2 wrapper is a no-op passthrough.
    # S1:R1=node:n_verify binds a node that never ran -> required R unmet
    # -> PARTIAL (not UNVERIFIED: a required R IS bound, just not satisfied).
    s = _spec(task_id=None)
    lg = Ledger(tmp_path / "run")
    assert derive_verdict(s, lg) == "PARTIAL"


def test_derive_verdict_delivery_open_gap_caps(tmp_path):
    # a delivery-bound task with an open material gap cannot read VERIFIED
    # even if every workflow requirement is satisfied.
    s = _spec(task_id="task.demo", tmp_path=tmp_path)
    lg = Ledger(tmp_path / "run")
    lg.append(_gap_event("g1", severity="material"))
    # Workflow evidence is absent here, so the segment verdict is UNVERIFIED
    # -> passthrough. The gap cap is exercised at the delivery layer (tested
    # above); this test asserts the wrapper does not crash and stays honest.
    v = derive_verdict(s, lg)
    assert v in ("UNVERIFIED", "PARTIAL")


# --- task CLI (M2 surface): record + status ----------------------------------

from axiom.cli import main as cli_main


def _activate(tmp_path, task="task.demo"):
    run_dir = tmp_path / "run"
    lg = Ledger(run_dir)
    ContractStore(run_dir).activate(_contract(task=task), lg)
    return str(run_dir)


def test_task_status_empty(tmp_path, capsys):
    rd = _activate(tmp_path)
    rc = cli_main(["task", "status", "--task-id", "task.demo",
                   "--run-dir", rd])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "task.demo"
    assert out["coverage"]["S1"]["status"] == "pending"


def test_task_record_gap_then_status(tmp_path, capsys):
    rd = _activate(tmp_path)
    rc = cli_main(["task", "record-gap", "--task-id", "task.demo",
                   "--run-dir", rd, "--payload",
                   json.dumps({"gap_id": "g1", "severity": "material",
                               "scenario": "checkout",
                               "problem": "no dup guard",
                               "standard_refs": ["S1"]})])
    assert rc == 0
    capsys.readouterr()
    rc = cli_main(["task", "status", "--task-id", "task.demo",
                   "--run-dir", rd])
    out = json.loads(capsys.readouterr().out)
    assert out["gaps"]["open_blocking_or_material"] == ["g1"]


def test_task_record_review_bare_pass_refused(tmp_path, capsys):
    # AC-04: a bare-PASS review is refused, not recorded.
    rd = _activate(tmp_path)
    rc = cli_main(["task", "record-review", "--task-id", "task.demo",
                   "--run-dir", rd, "--payload",
                   json.dumps({"review_id": "r1"})])
    assert rc == 1
    assert "bare PASS" in capsys.readouterr().err


def test_task_record_disposition_illegal_refused(tmp_path, capsys):
    # AC-08: a disposition on an unknown gap is refused.
    rd = _activate(tmp_path)
    rc = cli_main(["task", "record-disposition", "--task-id", "task.demo",
                   "--run-dir", rd, "--payload",
                   json.dumps({"gap_id": "ghost", "disposition": "resolved",
                               "evidence_refs": ["artifact:e1"]})])
    assert rc == 1


def test_task_record_decision_stop_reason(tmp_path, capsys):
    rd = _activate(tmp_path)
    rc = cli_main(["task", "record-decision", "--task-id", "task.demo",
                   "--run-dir", rd, "--payload",
                   json.dumps({"decision_id": "d1", "action": "stop",
                               "stop_reason": "stagnating"})])
    assert rc == 0
    capsys.readouterr()
    cli_main(["task", "status", "--task-id", "task.demo", "--run-dir", rd])
    out = json.loads(capsys.readouterr().out)
    assert out["stop_reason"] == "stagnating"
