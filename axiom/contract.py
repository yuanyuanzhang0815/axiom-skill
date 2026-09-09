"""Delivery Contract + quality-gap/loop-decision protocol (spec v1.1, M1).

Scope (M1: state and protocol only):
- axiom.delivery-contract.v1 schema: task-level versioned contract carrying
  intent, requirements with source (explicit/professional/inferred),
  assumptions, resource limits (nullable cost), quality standards.
- Canonical digest (delivery_contract_digest) over the full contract payload.
- ContractStore: revision files under a task run root + atomic activation via
  a single `delivery_contract_activated` ledger event guarded by
  expected_parent_digest. A new file on disk is NOT active until activated.
- Quality Gap event application: open -> investigating/fixing -> resolved;
  dismissed / deferred (with basis); reopened; duplicate_of. State is a
  projection over ledger events, never a self-reported field.
- Loop Decision validation (continue/probe/replan/conclude/blocked/stop).

NOT in M1 (M2+): standard formation, solution comparison execution,
product-review execution. M1 records / rebuilds / validates only. The
unified-verdict integration (spec §7.4) and the checkpoint `delivery` block
(§13.3) live in M2's `axiom/delivery.py`, which reuses these events.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

CONTRACT_SCHEMA = "axiom.delivery-contract.v1"

SOURCE_TYPES = ("explicit", "professional", "inferred")
SEVERITIES = ("blocking", "material", "polish")
GAP_STATES = ("open", "investigating", "fixing", "resolved",
              "dismissed", "deferred", "reopened")
DISPOSITIONS = ("investigating", "fixing", "resolved",
                "dismissed", "deferred", "reopened", "duplicate_of")
DECISION_ACTIONS = ("continue", "probe", "replan", "conclude", "blocked", "stop")
STOP_REASONS = ("completed", "budget_exhausted", "execution_cap",
                "stagnating", "user_input", "capability_unavailable",
                "cancelled")

KIND_CONTRACT_ACTIVATED = "delivery_contract_activated"
KIND_GAP_RECORDED = "quality_gap_recorded"
KIND_GAP_DISPOSITION = "quality_gap_disposition"
KIND_REVIEW_RECORDED = "quality_review_recorded"
KIND_DECISION_RECORDED = "loop_decision_recorded"
KIND_PLAN_ACTIVATED = "plan_revision_activated"
KIND_EVIDENCE_REVALIDATED = "evidence_revalidated"
NEW_PROTOCOL_KINDS = (
    KIND_CONTRACT_ACTIVATED, KIND_GAP_RECORDED, KIND_GAP_DISPOSITION,
    KIND_REVIEW_RECORDED, KIND_DECISION_RECORDED, KIND_PLAN_ACTIVATED,
    KIND_EVIDENCE_REVALIDATED,
)

# allowed gap-state transitions projected from disposition events
_GAP_TRANSITIONS = {
    "open": {"investigating", "fixing", "resolved", "dismissed",
             "deferred", "duplicate_of"},
    "investigating": {"fixing", "resolved", "dismissed", "deferred",
                      "duplicate_of"},
    "fixing": {"resolved", "dismissed", "deferred", "duplicate_of"},
    "resolved": {"reopened"},
    "dismissed": {"reopened"},
    "deferred": {"reopened", "investigating", "fixing"},
    "reopened": {"investigating", "fixing", "resolved", "dismissed",
                 "deferred", "duplicate_of"},
}
# dispositions that require an explicit basis (spec §7.2: dismissed needs
# proof of invalid/inapplicable/duplicate; deferred never lifts a required
# obligation; downgrades need justification)
_BASIS_REQUIRED = {"dismissed", "deferred", "duplicate_of"}


# --- schema -----------------------------------------------------------------


@dataclass
class QualityStandard:
    standard_id: str
    text: str
    source: Literal["explicit", "professional", "inferred"]
    source_ref: str = ""            # named sample / spec clause / user quote
    applicability: str = ""         # scenario this standard applies to
    necessity: Literal["required", "optional", "advisory"] = "required"
    verification: str = ""          # how the obligation is verified


@dataclass
class Assumption:
    assumption_id: str
    content: str
    basis: str = ""
    impact: str = ""
    disposition: str = "open"       # open / confirmed / falsified / replaced
    superseded_by: str | None = None


@dataclass
class DeliveryContract:
    task_id: str
    contract_revision: int
    parent_digest: str | None
    intent: str
    user_requirements: list[dict[str, Any]] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    authorization_refs: list[str] = field(default_factory=list)
    quality_standards: list[QualityStandard] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    # resource_limits.cost_usd: float | None -- None means "no amount limit
    # set" (still recorded), NOT zero and NOT free (spec §11.1).
    resource_limits: dict[str, Any] = field(default_factory=dict)
    profile_version: str = "software-web-baseline.v1"
    schema: str = CONTRACT_SCHEMA


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def contract_payload(c: DeliveryContract) -> dict:
    d = asdict(c)
    return d


def contract_digest(c: DeliveryContract) -> str:
    """sha256 over the full canonical contract payload. Any managed change to
    standards/assumptions/intent produces a new digest (spec §8.3)."""
    return "sha256:" + hashlib.sha256(
        _canonical(contract_payload(c))).hexdigest()


def validate_contract(c: DeliveryContract) -> list[str]:
    """Schema completeness check. Returns error strings; empty == valid."""
    errs: list[str] = []
    if c.schema != CONTRACT_SCHEMA:
        errs.append(f"schema {c.schema!r} != {CONTRACT_SCHEMA!r}")
    if not c.task_id:
        errs.append("missing task_id")
    if c.contract_revision < 1:
        errs.append(f"contract_revision {c.contract_revision} < 1")
    if c.contract_revision == 1 and c.parent_digest is not None:
        errs.append("revision 1 must have parent_digest=null")
    if c.contract_revision > 1 and not c.parent_digest:
        errs.append(f"revision {c.contract_revision} requires parent_digest")
    if not c.intent:
        errs.append("missing intent")
    seen: set[str] = set()
    for s in c.quality_standards:
        if not s.standard_id:
            errs.append("quality standard missing standard_id")
        elif s.standard_id in seen:
            errs.append(f"duplicate standard_id {s.standard_id!r}")
        seen.add(s.standard_id)
        if s.source not in SOURCE_TYPES:
            errs.append(f"{s.standard_id}: source {s.source!r} not in {SOURCE_TYPES}")
        if not s.text:
            errs.append(f"{s.standard_id}: missing text")
        if s.necessity not in ("required", "optional", "advisory"):
            errs.append(f"{s.standard_id}: bad necessity {s.necessity!r}")
    seen_a: set[str] = set()
    for a in c.assumptions:
        if not a.assumption_id:
            errs.append("assumption missing assumption_id")
        elif a.assumption_id in seen_a:
            errs.append(f"duplicate assumption_id {a.assumption_id!r}")
        seen_a.add(a.assumption_id)
        if not a.content:
            errs.append(f"{a.assumption_id}: missing content")
    cost = c.resource_limits.get("cost_usd", "absent")
    if cost != "absent" and cost is not None:
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            errs.append(f"resource_limits.cost_usd must be number|null, got {cost!r}")
        elif cost != cost or cost in (float("inf"), float("-inf")):
            errs.append("resource_limits.cost_usd must be finite (no NaN/Infinity)")
    return errs


def contract_to_json(c: DeliveryContract) -> str:
    return json.dumps(contract_payload(c), ensure_ascii=False, indent=2)


def contract_from_json(s: str) -> DeliveryContract:
    d = json.loads(s)
    d["quality_standards"] = [QualityStandard(**x)
                              for x in d.get("quality_standards", [])]
    d["assumptions"] = [Assumption(**x) for x in d.get("assumptions", [])]
    return DeliveryContract(**d)


# --- store + atomic activation ----------------------------------------------


class ContractStore:
    """Task-scoped contract persistence rooted at a task run dir.

    Layout: <root>/contracts/contract.v<N>.json. A file on disk is a
    candidate only; activation happens through a single
    delivery_contract_activated ledger event naming digest +
    expected_parent_digest + the ledger length the decision was made at.
    Concurrent stale proposals are rejected and must recompute (spec §8.4).
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.dir = self.root / "contracts"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, revision: int) -> Path:
        return self.dir / f"contract.v{revision}.json"

    def write_candidate(self, c: DeliveryContract) -> Path:
        """Write a revision file (immutable: an existing revision file whose
        content differs is a hard error -- same revision, different bytes
        means the digest chain was recomputed inconsistently)."""
        p = self.path_for(c.contract_revision)
        text = contract_to_json(c)
        if p.exists():
            if p.read_text(encoding="utf-8") != text:
                raise ValueError(
                    f"contract.v{c.contract_revision}.json already exists with "
                    f"different content; revisions are immutable")
            return p
        errs = validate_contract(c)
        if errs:
            raise ValueError(f"invalid contract: {'; '.join(errs)}")
        p.write_text(text, encoding="utf-8")
        return p

    def load(self, revision: int) -> DeliveryContract:
        return contract_from_json(
            self.path_for(revision).read_text(encoding="utf-8"))

    def active(self, events: list[dict]) -> DeliveryContract | None:
        """Project the active contract from activation events (last wins)."""
        digest = None
        for e in events:
            if e.get("kind") == KIND_CONTRACT_ACTIVATED:
                digest = e.get("payload", {}).get("delivery_contract_digest")
        if digest is None:
            return None
        for f in sorted(self.dir.glob("contract.v*.json")):
            c = contract_from_json(f.read_text(encoding="utf-8"))
            if contract_digest(c) == digest:
                return c
        return None  # activated digest but file missing -> caller treats as corrupt

    def activate(self, c: DeliveryContract, ledger) -> dict:
        """Write candidate + append the unique activation event.

        Guards (spec §8.4):
        - expected_parent_digest must equal the digest of the CURRENTLY
          active contract (None for the first activation). A proposal made
          against a stale parent is rejected, not partially applied.
        - the event records ledger_len so the activation position is
          auditable and replay-deterministic.
        """
        events = ledger.events()
        current = self.active(events)
        current_digest = contract_digest(current) if current else None
        if c.parent_digest != current_digest:
            raise ValueError(
                f"stale parent: contract expects parent_digest "
                f"{c.parent_digest!r} but active is {current_digest!r}; "
                f"recompute against the current head")
        self.write_candidate(c)
        event = {
            "event_id": f"ev_contract_v{c.contract_revision}",
            "kind": KIND_CONTRACT_ACTIVATED,
            "task_id": c.task_id,
            "payload": {
                "schema": c.schema,
                "contract_revision": c.contract_revision,
                "delivery_contract_digest": contract_digest(c),
                "expected_parent_digest": c.parent_digest,
                "ledger_len": len(events),
            },
            "claims": [],
        }
        return ledger.append(event)


# --- quality gaps (projection over events) ----------------------------------


def validate_gap_payload(p: dict) -> list[str]:
    errs: list[str] = []
    for k in ("gap_id", "task_id", "severity", "scenario", "problem"):
        if not p.get(k):
            errs.append(f"quality_gap_recorded missing {k}")
    if p.get("severity") and p["severity"] not in SEVERITIES:
        errs.append(f"severity {p['severity']!r} not in {SEVERITIES}")
    if not (p.get("standard_refs") or p.get("user_task_refs")):
        errs.append("gap must reference a standard or a user task")
    return errs


def validate_disposition_payload(p: dict) -> list[str]:
    errs: list[str] = []
    for k in ("gap_id", "disposition"):
        if not p.get(k):
            errs.append(f"quality_gap_disposition missing {k}")
    d = p.get("disposition")
    if d and d not in DISPOSITIONS:
        errs.append(f"disposition {d!r} not in {DISPOSITIONS}")
    if d in _BASIS_REQUIRED and not p.get("basis"):
        errs.append(f"disposition {d} requires basis (spec §7.2)")
    if d == "resolved" and not p.get("evidence_refs"):
        errs.append("resolved requires evidence_refs (actual verification)")
    if d == "duplicate_of" and not p.get("duplicate_of"):
        errs.append("duplicate_of requires the original gap_id")
    return errs


def validate_decision_payload(p: dict) -> list[str]:
    errs: list[str] = []
    for k in ("decision_id", "task_id", "action"):
        if not p.get(k):
            errs.append(f"loop_decision_recorded missing {k}")
    a = p.get("action")
    if a and a not in DECISION_ACTIONS:
        errs.append(f"action {a!r} not in {DECISION_ACTIONS}")
    if a == "stop":
        sr = p.get("stop_reason")
        if sr not in STOP_REASONS:
            errs.append(f"stop requires stop_reason in {STOP_REASONS}")
    return errs


def validate_review_payload(p: dict) -> list[str]:
    """quality_review_recorded completeness gate (spec §6.4 / AC-04).

    A product review is evidence only when it says what was actually
    checked, what was observed, and what it found -- a bare PASS/score is
    NOT review evidence and must not close obligations. All four content
    fields are required; a review that genuinely found nothing still
    records what it checked and observed plus an empty gaps_found list.
    """
    errs: list[str] = []
    if not p.get("review_id"):
        errs.append("quality_review_recorded missing review_id")
    for k in ("scenarios_checked", "observations"):
        v = p.get(k)
        if not isinstance(v, list) or not v:
            errs.append(
                f"quality_review_recorded requires non-empty {k} "
                "(spec §6.4: a bare PASS is not review evidence)")
    for k in ("gaps_found", "unchecked_scope"):
        if k not in p or not isinstance(p[k], list):
            errs.append(
                f"quality_review_recorded requires list field {k} "
                "(may be empty, but must be present)")
    return errs


def project_gaps(events: list[dict], task_id: str) -> dict[str, dict]:
    """Rebuild gap states from quality_gap_* events for one task.

    Raises ValueError on illegal transitions (a projection that tolerates
    them would let a gap be laundered open->resolved without fixing).
    """
    gaps: dict[str, dict] = {}
    for e in events:
        if e.get("task_id") != task_id and e.get("payload", {}).get("task_id") != task_id:
            continue
        kind = e.get("kind")
        p = e.get("payload", {})
        if kind == KIND_GAP_RECORDED:
            gid = p.get("gap_id")
            if gid in gaps and not p.get("reactivates"):
                # re-recording an existing id without reactivate flag is a
                # duplicate attempt; keep original, mark the event invalid
                continue
            gaps[gid] = {
                "gap_id": gid,
                "state": "open",
                "severity": p.get("severity"),
                "problem": p.get("problem"),
                "standard_refs": list(p.get("standard_refs") or []),
                "user_task_refs": list(p.get("user_task_refs") or []),
                "evidence_refs": list(p.get("evidence_refs") or []),
                "contract_digest": p.get("contract_digest"),
                "duplicate_of": None,
                "history": ["open"],
            }
        elif kind == KIND_GAP_DISPOSITION:
            gid = p.get("gap_id")
            g = gaps.get(gid)
            if g is None:
                raise ValueError(f"disposition for unknown gap {gid!r}")
            d = p.get("disposition")
            if d == "duplicate_of":
                target = p.get("duplicate_of")
                if target not in gaps:
                    raise ValueError(
                        f"duplicate_of target {target!r} is not a known gap")
                g["duplicate_of"] = target
                g["state"] = "dismissed"
                g["history"].append("duplicate_of")
                continue
            allowed = _GAP_TRANSITIONS[g["state"]]
            if d not in allowed:
                raise ValueError(
                    f"illegal gap transition {g['state']} -> {d} for {gid}")
            g["state"] = d
            g["history"].append(d)
            if p.get("severity") and p["severity"] != g["severity"]:
                # severity change requires basis and is preserved in history
                if not p.get("basis"):
                    raise ValueError(
                        f"severity change on {gid} requires basis")
                g["history"].append(
                    f"severity:{g['severity']}->{p['severity']}")
                g["severity"] = p["severity"]
            if p.get("evidence_refs"):
                g["evidence_refs"] += list(p["evidence_refs"])
    return gaps


def open_blocking_or_material(gaps: dict[str, dict]) -> list[str]:
    """gap_ids whose state still blocks delivery (spec §10.3 condition 4)."""
    out = []
    for gid, g in gaps.items():
        if (g["severity"] in ("blocking", "material")
                and g["state"] not in ("resolved", "dismissed", "deferred")):
            out.append(gid)
    return out
