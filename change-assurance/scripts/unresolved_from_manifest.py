#!/usr/bin/env python3
"""unresolved_from_manifest — compute unresolved, don't let the Agent imagine it.

Two pure functions, no LLM:

1. derive_unresolved(manifest, analysis_performed)
   Answers: among the mechanisms the project declares to exist, which were not covered by
   the corresponding analysis this time?
     - present + unscanned    -> coverage_gap unresolved (a real gap)
     - present + scanned      -> none (already covered)
     - absent_confirmed       -> none (the project does not have this mechanism at all; this
                                 is exactly what blocks fabricating websocket_refresh-style
                                 unresolved)
     - unknown                -> capability_unknown unresolved (epistemic uncertainty,
                                 not "there is a websocket bug", but "websocket was not
                                 inspected clearly")
2. validate_proposed_unresolved(proposed, manifest)
   Answers: among the unresolved items the LLM self-filled, is any of it fabricating?
     - references an absent_confirmed capability -> violation (inventing a mechanism the
       project lacks)
     - references an unknown capability -> violation (treating uncertainty as a concrete
       effect/bug; should downgrade to the epistemic "capability unknown", not written as
       "websocket_refresh affected")
     - references a present capability -> pass (a real gap, may keep)

manifest is the three-state manifest produced by detect_capabilities (or hand-written).
analysis_performed is this run's actually-executed analysis scan table {scan_name: bool}.

capability -> required scan mapping (capability × analysis):
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# capability -> which analysis scan must cover it when that capability is present
CAP_TO_SCAN = {
    "websocket":       "websocket_scan",
    "event_bus":       "event_bus_scan",
    "background_jobs": "background_job_scan",
    "database":        "db_schema_check",
    "openapi":         "openapi_diff_check",
    "shared_state":    "shared_state_scan",
}

# capability -> keywords (used to recognize which capability an LLM-proposed unresolved references)
CAP_KEYWORDS = {
    "websocket":       ["websocket", "ws ", "socket.io", "socketio", "ws_refresh"],
    "event_bus":       ["event_bus", "event bus", "eventemitter", "emit"],
    "background_jobs": ["background_job", "background job", "cron", "timer",
                        "setinterval", "scheduler", "worker"],
    "database":        ["database", "db_", "schema", "migration", "postgres",
                       "mysql", "redis"],
    "openapi":         ["openapi", "swagger", "contract"],
    "shared_state":    ["shared_state", "shared state", "store", "zustand",
                       "redux", "sidebar"],
}

ALLOWED_STATES = {"present", "absent_confirmed", "unknown"}


def derive_unresolved(manifest: dict, analysis_performed: dict) -> list[dict]:
    """manifest × analysis -> unresolved list. Each carries a kind:
    coverage_gap (a real gap) / capability_unknown (epistemic, not a bug story).

    manifest is only responsible for capabilities it EXPLICITLY declares — unmentioned
    capabilities are not processed (no fabricating unknown). The manifest comes from
    detect_capabilities (declares all) or is hand-written."""
    out: list[dict] = []
    for cap, scan in CAP_TO_SCAN.items():
        if cap not in manifest:
            continue  # undeclared -> do not invent an unresolved
        state = manifest[cap]
        if state not in ALLOWED_STATES:
            state = "unknown"
        if state == "present":
            if not analysis_performed.get(scan, False):
                out.append({
                    "id": f"{cap}: present but {scan} not performed",
                    "impact": "medium",
                    "kind": "coverage_gap",
                    "capability": cap,
                })
        elif state == "unknown":
            # remember: unknown expresses "epistemic uncertainty", not "there is a bug".
            out.append({
                "id": f"{cap}: capability unknown (not inspected)",
                "impact": "medium",
                "kind": "capability_unknown",
                "capability": cap,
            })
        # absent_confirmed: emits no unresolved. This is exactly where it blocks fabricating
        # websocket_refresh — the project has no such mechanism, no gap to record.
    return out


def _which_capability(text: str) -> str | None:
    """Which capability does a proposed unresolved reference? By keyword hit."""
    low = text.lower()
    best = None
    for cap, kws in CAP_KEYWORDS.items():
        if any(kw in low for kw in kws):
            best = cap
            break
    return best


def validate_proposed_unresolved(proposed: list, manifest: dict) -> list[str]:
    """Is any LLM-self-filled unresolved fabricating? Returns a violation list.

    - references an absent_confirmed capability -> inventing a mechanism the project lacks
      (a real-time app's websocket hallucination is this kind).
    - references an unknown capability and tells it as a concrete effect/bug -> treating
      uncertainty as a conclusion (should downgrade epistemically).
    - references a present capability -> pass (a real gap).
    """
    violations: list[str] = []
    for item in proposed:
        text = item.get("id", item) if isinstance(item, dict) else str(item)
        cap = _which_capability(text)
        if cap is None:
            continue  # no recognized capability keyword; let it pass (may genuinely be capability-unrelated)
        if cap not in manifest:
            violations.append(
                f"proposed unresolved '{text}' references capability '{cap}' "
                f"not declared in the capability manifest (manifest is the "
                f"authority on what mechanisms exist; declare it or drop the item)")
            continue
        state = manifest[cap]
        if state not in ALLOWED_STATES:
            state = "unknown"
        if state == "absent_confirmed":
            violations.append(
                f"proposed unresolved '{text}' references capability '{cap}' "
                f"which is absent_confirmed (project does not have this "
                f"mechanism; you are inventing it — like websocket_refresh "
                f"on a project with no websocket)")
        elif state == "unknown":
            violations.append(
                f"proposed unresolved '{text}' treats capability '{cap}' as a "
                f"concrete effect, but its state is unknown. Express it "
                f"epistemically: '{cap}: capability unknown (not inspected)', "
                f"not a concrete bug story")
        # present: a real gap, may keep
    return violations


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="manifest × analysis -> unresolved[]")
    ap.add_argument("--manifest", required=True, help="capability-manifest.json")
    ap.add_argument("--analysis", help="analysis_performed.json {scan: bool}")
    ap.add_argument("--proposed", help="receipt's proposed unresolved (json list)")
    args = ap.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))  # noqa
    analysis = {}
    if args.analysis:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    unresolved = derive_unresolved(manifest, analysis)
    out = {"unresolved": unresolved}
    if args.proposed:
        proposed = json.loads(Path(args.proposed).read_text(encoding="utf-8"))
        out["proposed_violations"] = validate_proposed_unresolved(proposed, manifest)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main(sys.argv[1:])
