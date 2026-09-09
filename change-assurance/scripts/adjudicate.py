#!/usr/bin/env python3
"""change-assurance single adjudication entrypoint.

Combines classify_change (machine risk floor) + assurance.derive_assurance_verdict
(the deterministic verdict exit) into ONE subprocess call, so the orchestrator's
harness hook does not have to shell out twice or glue two scripts.

Contract (consumed by the orchestrator seam b):
  in : --receipt <path>  (receipt.json)
       receipt = {
         "write_areas": ["path", ...] or [{"path": "..."}, ...],   # for classify
         "declared_risk": "V0"|"V1"|"V2"|"V3",                    # LLM-proposed, may not be < floor
         "evidence":       [ {claim,type,ai_authored,produced_by,
                              executed_by,adjudicated_by,result,...} ],
         "unresolved":     [ {"id":..,"impact":"high"|"medium"|"low"} ],
         "capability_manifest": {"runtime_mechanisms":[...], ...}  # optional
       }
  out (stdout): derive_assurance_verdict()'s return dict (status/risk/violations/
                missing_evidence/blockers/flaky/unresolved_high_impact/audit)
                + extra "risk_floor" + "floor_reasons" for audit.
  exit: 0 = normal adjudication (any status); 2 = receipt missing/structurally bad
        (the hook judges the result UNVERIFIED).

Design principles unchanged: the oracle is derived by derive_assurance_verdict from
type+ai_authored, ignoring LLM self-fills; the floor is machine-computed by classify;
declared < floor is raised back + violation. The LLM cannot graduate itself through
this path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# same-directory import (change-assurance is a physically independent skill, no package structure)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import classify_change  # noqa: E402
from assurance import derive_assurance_verdict  # noqa: E402
from unresolved_from_manifest import (  # noqa: E402
    derive_unresolved, validate_proposed_unresolved)


def _write_area_paths(receipt: dict) -> list[str]:
    was = receipt.get("write_areas", []) or []
    paths: list[str] = []
    for w in was:
        if isinstance(w, str):
            paths.append(w)
        elif isinstance(w, dict) and w.get("path"):
            paths.append(str(w["path"]))
    return paths


def adjudicate(receipt: dict) -> dict:
    # 1. machine risk floor (classify, no LLM)
    paths = _write_area_paths(receipt)
    file_results = []
    for p in paths:
        surface, signals = classify_change.classify_one(p)
        file_results.append({"path": p, "surface": surface,
                             "floor": classify_change.floor_for(p, surface),
                             "signals": signals})
    agg = classify_change.aggregate(file_results)
    risk_floor = agg["risk_floor"]

    # 2. unresolved: the manifest × analysis machine set-difference is authoritative (no LLM
    #    free-associating). manifest missing -> fall back to receipt.unresolved
    #    (derive_assurance_verdict records a "capability_manifest missing" violation, may not be V0).
    manifest = receipt.get("capability_manifest")
    proposed_violations: list[str] = []
    if manifest:  # non-empty dict
        analysis = receipt.get("analysis_performed") or {}
        unresolved = derive_unresolved(manifest, analysis)
        # classify's unknown-file hint is also merged in (it is a machine-produced fact gap)
        for u in agg.get("unresolved", []):
            unresolved.append({"id": u, "impact": "medium"})
        # LLM-self-filled proposed unresolved is only used to check for fabrication (not part of adjudication)
        proposed_violations = validate_proposed_unresolved(
            receipt.get("unresolved", []) or [], manifest)
        cap_manifest_arg = manifest
    else:
        unresolved = list(receipt.get("unresolved", []) or [])
        for u in agg.get("unresolved", []):
            unresolved.append({"id": u, "impact": "medium"})
        cap_manifest_arg = None

    # 3. adjudication (single exit, deterministic)
    #    surfaces=agg["surfaces_touched"] triggers per-surface accounting (invariant #10):
    #    each evidence item only fills the slot of the surface it declared in addresses;
    #    no borrowing evidence across surfaces.
    verdict = derive_assurance_verdict(
        risk_floor=risk_floor,
        declared_risk=receipt.get("declared_risk", "V1"),
        evidence=receipt.get("evidence", []) or [],
        unresolved=unresolved,
        capability_manifest=cap_manifest_arg,
        surfaces=agg.get("surfaces_touched"),
    )
    if proposed_violations:
        verdict["violations"] = list(verdict.get("violations", [])) + proposed_violations
        verdict["manifest_violations"] = proposed_violations
        # The LLM inventing in unresolved a capability the project lacks (absent_confirmed) or
        # telling an unknown as a concrete bug = receipt integrity violation -> no VERIFIED.
        if verdict["status"] == "VERIFIED":
            verdict["status"] = "PARTIAL"
            verdict["blockers"] = list(verdict.get("blockers", [])) + [
                "manifest_violation: proposed unresolved invents absent/unknown "
                "capability (receipt integrity; unresolved must be machine-derived)"]
    verdict["risk_floor"] = risk_floor
    verdict["floor_reasons"] = agg.get("floor_reasons", [])
    return verdict


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="change-assurance single entry: classify + derive_assurance_verdict")
    ap.add_argument("--receipt", required=True, help="receipt.json path")
    args = ap.parse_args(argv)

    p = Path(args.receipt)
    if not p.is_file():
        print(json.dumps({"status": "UNVERIFIED",
                          "error": f"receipt not found: {p}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        receipt = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "UNVERIFIED",
                          "error": f"receipt not json: {e}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    verdict = adjudicate(receipt)
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
