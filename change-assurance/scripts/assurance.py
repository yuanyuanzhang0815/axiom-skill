"""
change-assurance verdict harness — the deterministic core.

This is the one function that turns change-assurance from a "spec skill" (SKILL.md
prose) into an "assurance harness": a machine-derived verdict the Agent cannot bypass.

Authority model:
  - LLM / tools: submit evidence receipts (facts + interpretation)
  - this module: derive_assurance_verdict() — the single verdict exit, deterministic
  - the orchestrator (host): consume verdict via verdict_field (seam b) -> derive_verdict

Layering:
  Verdict Integrity      — change-assurance itself guarantees "no false VERIFIED" (this module)
  Workflow Enforcement   — the orchestrator guarantees "PARTIAL never reaches DONE" (host)

Design principles:
  - oracle tier is derived by this function from evidence.type + ai_authored, IGNORING
    any LLM-self-filled oracle field. The LLM cannot graduate itself by tagging a
    freshly-written test with oracle=independent.
  - risk floor is a machine lower bound (from classify_change); declared_risk may not
    fall below it (A).
  - a high-impact unresolved item exists -> VERIFIED forbidden (C, execution side).
  - V3 missing human/external authorization -> BLOCKED, not PARTIAL.

Usage:
  python3 assurance.py --demo          # run three demo cases
  python3 assurance.py --json REC.json # adjudicate one receipt
"""
from __future__ import annotations

import argparse
import json
import sys

# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------

FLOOR_ORDER = {"V0": 0, "V1": 1, "V2": 2, "V3": 3}
ORDER_FLOOR = {v: k for k, v in FLOOR_ORDER.items()}


def _risk_ge(a: str, b: str) -> bool:
    return FLOOR_ORDER[a] >= FLOOR_ORDER[b]


# ---------------------------------------------------------------------------
# Evidence types + Oracle tier derivation (core of B: not LLM-filled, derived here)
# ---------------------------------------------------------------------------

ALLOWED_TYPES = {
    "compiler",       # tsc / typecheck / pyright — language-rule oracle
    "static-scan",    # rg / LSP refs — current-code-fact oracle
    "schema",         # OpenAPI / drizzle / pydantic — contract oracle
    "unit",           # unit test — strength depends on whether repo-existing
    "integration",    # real component interaction
    "e2e",            # user-visible business loop
    "historical",     # known failure did not recur (repo regression)
    "runtime-trace",  # runtime fact
    "human",          # human acceptance / authorization
    "llm-review",     # another LLM reviewer — weak
}

# types that are independent by themselves (oracle from language/contract/history/runtime
# fact, independent of this implementation's cognitive assumptions)
STRONG_TYPES = {"compiler", "static-scan", "schema", "integration",
                "e2e", "historical", "runtime-trace", "human"}


def derive_oracle_tier(ev: dict) -> str:
    """Derive one evidence item's oracle independence. IGNORES any self-declared oracle field in ev.

    Returns: "strong" | "weak" | "unknown"
    """
    t = ev.get("type", "")
    if t not in ALLOWED_TYPES:
        return "unknown"
    if t in STRONG_TYPES:
        return "strong"
    if t == "unit":
        # repo-existing unit test = historical regression -> strong;
        # AI-freshly-written unit test = weak (oracle from this implementation's same cognition)
        return "weak" if ev.get("ai_authored") else "strong"
    if t == "llm-review":
        return "weak"   # another LLM; blind spots may overlap
    return "unknown"


# ---------------------------------------------------------------------------
# Required evidence "roles" per risk level, and the type->role mapping
# ---------------------------------------------------------------------------

# role names are abstract slots; one evidence item must pass AND have a strong enough
# oracle to fill a slot
ROLES_REQUIRED = {
    "V0": ["static_check"],
    "V1": ["reference_scan", "behavior"],
    "V2": ["impact_scan", "regression", "integration"],
    "V3": ["deep_evidence", "auth_gate"],
}

# V3's auth_gate must be filled by human/external; missing -> BLOCKED (not PARTIAL)
AUTH_GATE_ONLY = {"auth_gate": ("human",)}

# --- per-surface evidence contract (invariant #10: no borrowing evidence across surfaces) ---
# Each touched surface must independently satisfy its own minimum Evidence Contract.
# One evidence item only fills the slot of the surface it EXPLICITLY declared in
# `addresses` — no declaration -> fills no surface's slot (kills the old global pool:
# a backend e2e silently satisfying the frontend's behavior slot).
# secret-auth missing auth_gate -> BLOCKED (AUTH_GATE_ONLY semantics preserved).
SURFACE_REQUIRED_ROLES: dict[str, list[str]] = {
    "frontend_interactive":     ["reference_scan", "behavior"],
    "frontend_presentational":  ["static_check"],
    "type-declaration":         ["static_check"],   # .d.ts ambient declaration: no runtime behavior, tsc satisfies
    "public-contract":          ["reference_scan", "behavior", "integration"],
    "runtime":                  ["reference_scan", "behavior"],
    "shared-state":             ["reference_scan", "behavior", "integration", "regression"],
    "schema-migration":         ["deep_evidence", "regression"],
    "secret-auth":              ["deep_evidence", "auth_gate"],
    "config":                   ["static_check"],
    "docs":                     [],
    "test":                     [],
}

# evidentiary surfaces that neither raise risk_floor nor participate in per-surface accounting
_NON_SCORING_SURFACES = {"test", "docs"}


def evidence_roles(ev: dict) -> set[str]:
    """Which roles a passing evidence item can fill. A weak oracle fills no required slot."""
    t = ev.get("type", "")
    tier = derive_oracle_tier(ev)
    roles: set[str] = set()
    if tier == "weak":
        return roles  # weak evidence does not independently satisfy required slots
    if t in ("compiler", "static-scan"):
        roles |= {"static_check", "reference_scan", "impact_scan"}
    elif t == "schema":
        roles |= {"static_check", "impact_scan"}
    elif t == "unit" and tier == "strong":
        roles |= {"behavior"}
    elif t == "integration":
        roles |= {"integration", "deep_evidence"}
    elif t == "e2e":
        roles |= {"integration", "behavior", "deep_evidence"}
    elif t == "historical":
        roles |= {"regression", "deep_evidence"}
    elif t == "runtime-trace":
        roles |= {"integration", "deep_evidence"}
    elif t == "human":
        roles |= {"auth_gate"}
    return roles


# ---------------------------------------------------------------------------
# Receipt validator (B-min hard checks)
# ---------------------------------------------------------------------------

def validate_receipt(risk: str, evidence: list[dict]) -> list[str]:
    """Hard-check each evidence item's structure + self-deception check. Returns violation list."""
    errs: list[str] = []
    for i, ev in enumerate(evidence):
        prefix = f"evidence[{i}]"
        # 1. type must be an allowed type
        if ev.get("type") not in ALLOWED_TYPES:
            errs.append(f"{prefix}: type '{ev.get('type')}' not in the allowed set")
            continue
        # 2. four-authority required (produced/executed/adjudicated)
        for f in ("produced_by", "executed_by", "adjudicated_by"):
            if not ev.get(f):
                errs.append(f"{prefix}: missing {f}")
        # 3. self-deception check: AI-freshly-written evidence self-tagging a high oracle
        #    — ignored and flagged
        declared = ev.get("oracle") or ev.get("self_declared_oracle")
        derived = derive_oracle_tier(ev)
        if declared and declared in ("independent", "strong", "repo-existing") and derived == "weak":
            errs.append(
                f"{prefix}: ai-authored evidence self-tagged oracle={declared}, "
                f"machine-derived as {derived} (self-tag ignored; cannot graduate yourself)")
        # 4. result is required
        if ev.get("result") not in ("pass", "fail", "flaky", "unavailable"):
            errs.append(f"{prefix}: result missing or invalid")
        # 5. addresses (per-surface binding, invariant #10): if provided must be list[str]
        #    (when absent, the per-surface path judges a separate violation; legacy path ignores)
        addrs = ev.get("addresses")
        if addrs is not None:
            if not isinstance(addrs, list) or any(
                    not isinstance(a, str) for a in addrs):
                errs.append(f"{prefix}: addresses must be a list[str]")
    return errs


# ---------------------------------------------------------------------------
# Main adjudication function — the single exit, deterministic
# ---------------------------------------------------------------------------

def derive_assurance_verdict(
    risk_floor: str,
    declared_risk: str,
    evidence: list[dict],
    unresolved: list,
    capability_manifest: dict | None = None,
    surfaces: list[str] | None = None,
) -> dict:
    """Adjudicate. The Agent cannot bypass: oracle is derived here, floor is machine-set,
    high-impact unresolved hard-blocks.

    surfaces (new, invariant #10): adjudicate collects touched_surfaces from classify's
    per-file surface and passes them in. When provided, runs **per-surface accounting** —
    each touched surface independently satisfies its own SURFACE_REQUIRED_ROLES; one
    evidence item only fills the slot of the surface it declared in `addresses`, no
    declaration -> fills nothing (no borrowing evidence across surfaces). None runs the
    legacy global ROLES_REQUIRED (transitional backward-compat; new fixtures always go
    per-surface).

    Returns:
      { status, risk, violations, missing_evidence, blockers, flaky,
        unresolved_high_impact, audit, surfaces_touched?, per_surface? }
    """
    risk_floor = risk_floor if risk_floor in FLOOR_ORDER else "V1"  # unknown -> V1, never V0
    declared_risk = declared_risk if declared_risk in FLOOR_ORDER else "V1"
    violations: list[str] = []

    # --- A: machine risk floor gate ---
    if not _risk_ge(declared_risk, risk_floor):
        violations.append(
            f"declared_risk {declared_risk} below machine floor {risk_floor} (A: no downgrade)")
    effective_risk = declared_risk if _risk_ge(declared_risk, risk_floor) else risk_floor

    # --- B-min: receipt hard checks ---
    violations += validate_receipt(effective_risk, evidence)

    # --- capability manifest missing check (C side) ---
    if capability_manifest is None:
        violations.append("capability_manifest missing: runtime mechanisms unknown; must not default to no-impact")

    # --- required role check ---
    flaky_present = False
    for ev in evidence:
        if ev.get("result") == "flaky":
            flaky_present = True

    if surfaces is not None:
        # per-surface accounting (invariant #10)
        touched = [s for s in dict.fromkeys(surfaces)
                   if s not in _NON_SCORING_SURFACES]
        per_surface_filled: dict[str, set[str]] = {s: set() for s in touched}
        per_surface_missing: dict[str, list[str]] = {}
        for ev in evidence:
            if ev.get("result") != "pass":
                continue
            addrs = ev.get("addresses")
            if not addrs or not isinstance(addrs, list):
                # no surface binding -> fills no surface slot + record violation (force explicit annotation)
                if addrs is None:
                    violations.append(
                        f"evidence[{ev.get('type')}] has no addresses: fills no surface slot "
                        f"(invariant #10: no borrowing evidence across surfaces; declare which "
                        f"surfaces it covers)")
                continue
            roles = evidence_roles(ev)
            for s in addrs:
                if s in per_surface_filled:
                    per_surface_filled[s] |= roles
        missing_evidence: list[dict] = []
        for s in touched:
            req = SURFACE_REQUIRED_ROLES.get(s, ["reference_scan", "behavior"])
            miss = [r for r in req if r not in per_surface_filled[s]]
            if miss:
                per_surface_missing[s] = miss
                for r in miss:
                    missing_evidence.append({"surface": s, "role": r})
        missing = missing_evidence  # list[dict] form
        per_surface = {
            s: {"filled": sorted(per_surface_filled[s]),
                "missing": per_surface_missing.get(s, [])}
            for s in touched
        }
        # secret-auth surface missing auth_gate -> BLOCKED
        auth_gate_blocked = any(
            "auth_gate" in per_surface_missing.get("secret-auth", [])
            for _ in [0])
    else:
        # legacy global ROLES_REQUIRED (transitional)
        required = ROLES_REQUIRED[effective_risk]
        filled: set[str] = set()
        for ev in evidence:
            if ev.get("result") != "pass":
                continue
            filled |= evidence_roles(ev)
        missing = [r for r in required if r not in filled]
        missing_evidence = missing
        per_surface = None
        auth_gate_blocked = (effective_risk == "V3" and "auth_gate" in missing)

    # --- C: high-impact unresolved hard-blocks VERIFIED ---
    def _impact(u):
        if isinstance(u, dict):
            return u.get("impact", "unknown")
        return "unknown"
    high_impact = [u for u in unresolved if _impact(u) == "high"]

    # --- verdict ---
    # audit: record each evidence item's derived oracle (transparent, auditable)
    audit = [{"type": ev.get("type"),
              "derived_oracle": derive_oracle_tier(ev),
              "declared_oracle": ev.get("oracle") or ev.get("self_declared_oracle"),
              "result": ev.get("result")} for ev in evidence]

    blockers: list[str] = []
    status: str

    # whether any evidence filled a slot (used to distinguish PARTIAL vs UNVERIFIED)
    if surfaces is not None:
        any_filled = any(per_surface_filled[s] for s in per_surface_filled)
    else:
        any_filled = bool(filled)

    # secret-auth surface missing auth_gate -> BLOCKED (external condition unmet)
    if auth_gate_blocked:
        status = "BLOCKED"
        blockers.append("authorization gate unmet (secret-auth surface has no human/external evidence)")
    elif high_impact:
        status = "PARTIAL"
        blockers.append(f"high-impact unresolved present: {[ _label(u) for u in high_impact]}")
    elif flaky_present:
        status = "UNVERIFIED"
        blockers.append("flaky evidence present (may not mechanically re-run to green)")
    elif missing:
        status = "PARTIAL" if any_filled else "UNVERIFIED"
        blockers.append(f"required evidence missing: {missing_evidence}")
    elif violations:
        # structural / self-deception violation, but slots filled -> no VERIFIED
        status = "PARTIAL"
        blockers.append(f"receipt violations: {len(violations)}")
    else:
        status = "VERIFIED"

    if violations:
        blockers += violations
    blockers += [f"missing: {m}" for m in missing_evidence]

    out = {
        "status": status,
        "risk": effective_risk,
        "violations": violations,
        "missing_evidence": missing_evidence,
        "blockers": blockers,
        "flaky": flaky_present,
        "unresolved_high_impact": [str(u) for u in high_impact],
        "audit": audit,
    }
    if surfaces is not None:
        out["surfaces_touched"] = touched
        out["per_surface"] = per_surface
    return out


def _label(u) -> str:
    if isinstance(u, dict):
        return str(u.get("id", u))
    return str(u)


# ---------------------------------------------------------------------------
# Demo cases
# ---------------------------------------------------------------------------

def _demo():
    print("=" * 70)
    print("CASE 1 — an earlier version: honest receipt (I hand-walked it and said VERIFIED, machine says PARTIAL)")
    print("=" * 70)
    # classify gives floor=V0 (config), LLM declared V2 -> effective V2
    # evidence: e1 static-scan(grep, pass) + e3 e2e(runtime, pass)
    # V2 requires: impact_scan + regression + integration
    # e1 fills impact_scan; e3 fills integration; regression missing -> PARTIAL
    receipt = [
        {"claim": "change does not ripple to other modules", "type": "static-scan",
         "produced_by": "grep", "executed_by": "harness", "adjudicated_by": "rule",
         "result": "pass"},
        {"claim": "the v2 flow passes end-to-end", "type": "e2e",
         "produced_by": "orchestrator-harness", "executed_by": "orchestrator-harness",
         "adjudicated_by": "rule", "result": "pass"},
    ]
    v = derive_assurance_verdict(
        risk_floor="V0", declared_risk="V2", evidence=receipt,
        unresolved=[{"id": "shared-state behavior baseline not compared", "impact": "medium"}],
        capability_manifest={"runtime_mechanisms": ["http", "shared_store"]})
    print(json.dumps(v, indent=2, ensure_ascii=False))
    print(f"\n-> machine verdict {v['status']} (I hand-said VERIFIED and was loose; the machine caught the missing regression)")

    print("\n" + "=" * 70)
    print("CASE 2 — LLM writes its own test + self-tags oracle=independent + outputs VERIFIED (bypass attempt)")
    print("=" * 70)
    receipt = [
        {"claim": "deletion logic is correct", "type": "unit", "ai_authored": True,
         "oracle": "independent",  # <- LLM self-fills a high oracle
         "produced_by": "coding-agent", "executed_by": "coding-agent",
         "adjudicated_by": "coding-agent", "result": "pass"},
    ]
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1", evidence=receipt,
        unresolved=[], capability_manifest={"runtime_mechanisms": ["http"]})
    print(json.dumps(v, indent=2, ensure_ascii=False))
    print(f"\n-> LLM's self-tagged oracle=independent is ignored, derived as weak; verdict={v['status']}")
    print("  Agent graduating itself -> structurally impossible")

    print("\n" + "=" * 70)
    print("CASE 3 — LLM pushes declared_risk below the floor (A bypass attempt)")
    print("=" * 70)
    # floor=V2 (shared-state), LLM declared V0 attempting a downgrade
    v = derive_assurance_verdict(
        risk_floor="V2", declared_risk="V0",
        evidence=[{"claim": "x", "type": "static-scan",
                   "produced_by": "rg", "executed_by": "h", "adjudicated_by": "r",
                   "result": "pass"}],
        unresolved=[], capability_manifest={"runtime_mechanisms": ["http"]})
    print(json.dumps(v, indent=2, ensure_ascii=False))
    print(f"\n-> declared V0 raised back to {v['risk']}, violation recorded, verdict={v['status']}")


def main(argv):
    ap = argparse.ArgumentParser(description="change-assurance deterministic verdict")
    ap.add_argument("--demo", action="store_true", help="run three demo cases")
    ap.add_argument("--json", help="adjudicate one receipt json file")
    ap.add_argument("--floor", default="V0", help="machine risk floor V0-V3")
    ap.add_argument("--declared", default="V0", help="LLM-proposed risk V0-V3")
    args = ap.parse_args(argv)
    if args.demo:
        _demo()
        return 0
    if args.json:
        rec = json.loads(open(args.json).read())
        v = derive_assurance_verdict(
            risk_floor=args.floor, declared_risk=args.declared,
            evidence=rec.get("evidence", []),
            unresolved=rec.get("unresolved", []),
            capability_manifest=rec.get("capability_manifest"))
        print(json.dumps(v, indent=2, ensure_ascii=False))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
