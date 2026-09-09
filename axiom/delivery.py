"""M2 autonomous quality loop: task-level projection + unified verdict + delivery block.

Spec: docs/superpowers/specs/2026-09-09-axiom-autonomous-delivery-quality-loop-design.md
(v1.1), §6/§7/§8/§9/§10/§13.3.

M1 (contract.py) records / rebuilds / validates the Delivery Contract and the
quality-gap / decision / review EVENTS. M2 turns those events into the
task-level projection the orchestrator reads, and folds the quality loop into
the SINGLE ruling entry (`derive_verdict`) so a segment VERIFIED can never
mask an open task-level gap (spec §7.4 / AC-20).

Scope discipline (spec §3 / §17): this is a projection + gating layer over
the existing ledger. It does NOT build a new orchestration platform, an
always-on fixed checklist, or a second reviewer store. Legacy (no
delivery-contract binding) tasks are byte-for-byte unaffected -- every entry
point is None-gated on the binding.

The task ledger is the same events.jsonl the workflow uses (spec §8.4:
replan / resume / workflow-switch keep one task_id and one task ledger; no
cross-run-dir ledger federation in v1). Task-level events (contract
activation, gap record/disposition, review, decision) live alongside the
segment's dispatch events and are filtered by task_id.
"""
from __future__ import annotations

import re
from typing import Any

from axiom.contract import (
    KIND_CONTRACT_ACTIVATED, KIND_DECISION_RECORDED, KIND_EVIDENCE_REVALIDATED,
    KIND_GAP_DISPOSITION, KIND_GAP_RECORDED, KIND_PLAN_ACTIVATED,
    KIND_REVIEW_RECORDED, open_blocking_or_material, project_gaps,
    validate_disposition_payload, validate_gap_payload,
    validate_review_payload,
)

# actions that mean the loop decided to finish / halt (vs keep working)
_TERMINAL_ACTIONS = ("conclude", "blocked", "stop")
# actions that constitute real movement for stagnation detection
_PROGRESS_ACTIONS = ("continue", "probe", "replan")


# --- event collection --------------------------------------------------------


def _task_events(events: list[dict], task_id: str) -> list[dict]:
    """Events belonging to this task (task_id carried at top level or in
    payload). The ledger mixes per-segment dispatch events (no task_id) with
    task-level protocol events (task_id set) -- we only project the latter."""
    out = []
    for e in events:
        if e.get("task_id") == task_id or e.get("payload", {}).get("task_id") == task_id:
            out.append(e)
    return out


# --- sub-projections ---------------------------------------------------------


def project_reviews(events: list[dict], task_id: str) -> dict[str, dict]:
    """review_id -> the latest quality_review_recorded payload.

    A review whose payload fails the §6.4 completeness gate is kept but
    flagged review_ok=False so it cannot count as coverage (AC-04).
    """
    reviews: dict[str, dict] = {}
    for e in _task_events(events, task_id):
        if e.get("kind") != KIND_REVIEW_RECORDED:
            continue
        p = e.get("payload", {})
        rid = p.get("review_id")
        if not rid:
            continue
        errs = validate_review_payload(p)
        reviews[rid] = {**p, "review_ok": not errs, "review_errors": errs}
    return reviews


def project_comparison(events: list[dict], task_id: str) -> dict[str, dict]:
    """decision_id -> comparison record for quality-sensitive decisions
    (spec §6.3 / §4.3). Carried on loop_decision_recorded payloads that
    declare `comparison`. A comparison with no named reference samples or no
    substantive candidates is flagged so §10.3(7)/AC-23..25 can gate on it.
    """
    out: dict[str, dict] = {}
    for e in _task_events(events, task_id):
        if e.get("kind") != KIND_DECISION_RECORDED:
            continue
        p = e.get("payload", {})
        comp = p.get("comparison")
        if not isinstance(comp, dict):
            continue
        did = p.get("decision_id")
        refs = comp.get("references") or []
        cands = comp.get("candidates") or []
        out[did] = {
            "decision_id": did,
            "references": refs,
            "candidates": cands,
            "method": comp.get("method", ""),
            "outcome": comp.get("outcome", ""),
            # §4.3: a category description ("like mainstream booking tools") is not a
            # reference; a reference is a concrete, nameable sample.
            "has_named_references": bool(refs),
            # §6.3: candidates that differ only in wording are not a real
            # comparison; we require >=2 declared substantive candidates.
            "has_substantive_candidates": len(cands) >= 2,
        }
    return out


def project_stagnation(events: list[dict], task_id: str) -> dict[str, Any]:
    """Cross-revision stagnation (spec §10.2): consecutive no-progress
    decisions, NOT reset by replan/resume. A decision is no-progress when it
    is not a terminal action and names no gap closure / obligation progress.
    """
    decisions = [e.get("payload", {}) for e in _task_events(events, task_id)
                 if e.get("kind") == KIND_DECISION_RECORDED]
    consecutive_no_progress = 0
    last_action = None
    for p in decisions:
        a = p.get("action")
        last_action = a
        if a in _TERMINAL_ACTIONS:
            break
        closes = p.get("closes_gap_ids") or p.get("progress_made") or []
        if a in _PROGRESS_ACTIONS and closes:
            consecutive_no_progress = 0
        else:
            consecutive_no_progress += 1
    return {
        "decision_count": len(decisions),
        "consecutive_no_progress": consecutive_no_progress,
        "last_action": last_action,
        "stagnating": consecutive_no_progress >= 2,  # spec §10.2 default
    }


def project_stop(events: list[dict], task_id: str) -> str | None:
    """Latest stop_reason from a stop/blocked decision; None while running
    (spec §10.4: stop_reason is empty until the task stops)."""
    sr = None
    for e in _task_events(events, task_id):
        if e.get("kind") != KIND_DECISION_RECORDED:
            continue
        p = e.get("payload", {})
        if p.get("action") == "stop":
            sr = p.get("stop_reason") or sr
        elif p.get("action") == "blocked":
            sr = sr or "user_input"
    return sr


# --- unified verdict ---------------------------------------------------------


def derive_delivery_verdict(workflow_verdict: str, projection: dict) -> str:
    """Single ruling entry for a delivery-bound task (spec §7.4).

    The workflow verdict (this segment's managed work) is the floor; the
    task-level quality loop can only lower it, never raise it:
      - open/reopened/error-deferred blocking|material gap  -> cap at PARTIAL
        (AC-20: a segment VERIFIED cannot mask an open task gap)
      - a quality-sensitive decision closed with no substantive comparison
        -> cap at PARTIAL (AC-23..25)
      - workflow UNVERIFIED/BLOCKED/PARTIAL passes through unchanged
    A task VERIFIED requires the segment VERIFIED AND no open blocking/
    material gap AND comparison obligations met (§10.3).
    """
    if workflow_verdict != "VERIFIED":
        return workflow_verdict
    if projection["gaps"]["open_blocking_or_material"]:
        return "PARTIAL"
    if projection["comparison"]["unmet_decision_ids"]:
        return "PARTIAL"
    return "VERIFIED"


# --- the checkpoint `delivery` block (spec §13.3) ----------------------------


def project_delivery(events: list[dict], task_id: str,
                     contract=None) -> dict[str, Any]:
    """Rebuild the task-level delivery block from the ledger. All fields are
    derived from contract + events (spec §13.3: rebuildable, cache may be
    dropped). `contract` is the active DeliveryContract (may be None)."""
    tevents = _task_events(events, task_id)
    gaps = project_gaps(events, task_id)
    reviews = project_reviews(events, task_id)
    comparison = project_comparison(events, task_id)
    stag = project_stagnation(events, task_id)
    stop_reason = project_stop(events, task_id)

    open_bm = open_blocking_or_material(gaps)
    # §10.3(4): a deferred blocking/material gap still blocks unless the
    # deferral carries basis (deferred never lifts a required obligation).
    wrongly_deferred = [
        gid for gid, g in gaps.items()
        if g["severity"] in ("blocking", "material") and g["state"] == "deferred"
    ]
    open_bm = sorted(set(open_bm) | set(wrongly_deferred))

    unmet_comp = sorted(
        did for did, c in comparison.items()
        if not (c["has_named_references"] and c["has_substantive_candidates"]))

    # contract / plan revision (from the latest activation events)
    contract_rev = plan_rev = None
    for e in tevents:
        if e.get("kind") == KIND_CONTRACT_ACTIVATED:
            contract_rev = e.get("payload", {}).get("contract_revision")
        elif e.get("kind") == KIND_PLAN_ACTIVATED:
            plan_rev = e.get("payload", {}).get("plan_revision")

    # open assumptions (important unresolved ones surface in the block)
    open_assumptions = []
    if contract is not None:
        open_assumptions = [
            a.assumption_id for a in contract.assumptions
            if a.disposition == "open"
        ]

    # applicability coverage (§5 / AC-02): each standard is either checked,
    # justified-not-applicable, or pending. Rebuilt from review coverage
    # records; pending means no review has addressed it yet.
    coverage = {}
    for rid, r in reviews.items():
        for sid in (r.get("standards_addressed") or []):
            coverage[sid] = {"status": "checked", "by_review": rid}
        for na in (r.get("not_applicable") or []):
            if isinstance(na, dict) and na.get("standard_id") and na.get("justification"):
                coverage[na["standard_id"]] = {
                    "status": "not_applicable",
                    "justification": na["justification"],
                    "by_review": rid,
                }
    if contract is not None:
        for s in contract.quality_standards:
            coverage.setdefault(s.standard_id, {"status": "pending"})

    return {
        "task_id": task_id,
        "contract_revision": contract_rev,
        "plan_revision": plan_rev,
        "gaps": {
            "open_blocking_or_material": open_bm,
            "by_state": _count_by(gaps, "state"),
            "by_severity": _count_by(gaps, "severity"),
            "total": len(gaps),
        },
        "reviews": {
            "total": len(reviews),
            "ok": sum(1 for r in reviews.values() if r["review_ok"]),
            "rejected": [rid for rid, r in reviews.items()
                         if not r["review_ok"]],
        },
        "comparison": {
            "decisions": len(comparison),
            "unmet_decision_ids": unmet_comp,
        },
        "open_assumptions": open_assumptions,
        "coverage": coverage,
        "stagnation": stag,
        "stop_reason": stop_reason,
        "resource": {},  # filled by caller (cost accounting lives in harness)
    }


def _count_by(gaps: dict[str, dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for g in gaps.values():
        out[g[field]] = out.get(g[field], 0) + 1
    return out


def delivery_block(projection: dict[str, Any]) -> dict[str, Any]:
    """The §13.3 checkpoint `delivery` section."""
    return projection
