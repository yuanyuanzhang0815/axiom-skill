#!/usr/bin/env python3
"""
classify_change.py — machine-classify the write_areas of one change, producing a risk floor + minimum evidence set.

Position: compute "what this change touched" so the LLM cannot vibe-guess it.
Pure stdlib, deterministic, no LLM. The LLM may only upgrade above this floor, never downgrade.

Usage:
  python3 classify_change.py path/to/a.ts path/to/b.ts
  python3 classify_change.py --json '[{"path":"a.ts"},...]'
  cat write_areas.txt | python3 classify_change.py --stdin
  python3 classify_change.py --manifest change.json   # { "write_areas": ["a.ts", ...], "intent": "..." }

Output: JSON to stdout
  {
    "files": [{ "path","surface","floor","signals":[...] }],
    "surfaces_touched": [...],
    "risk_floor": "V2",
    "floor_reasons": [...],
    "minimum_evidence": [...],
    "unresolved": [...]
  }

Risk floor hard rules (cannot be lowered by the LLM):
  secret/auth/payment/irreversible  -> V3
  schema/migration/data-integrity     -> V3
  public-contract / shared-state / cross-module -> V2
  runtime logic                       -> V1
  docs / pure config                  -> V0
  unknown (cannot be determined)      -> V1 (must not default to V0)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---- surface -> floor ----
SURFACE_FLOOR = {
    "secret-auth": "V3",
    "schema-migration": "V3",
    "shared-state": "V2",
    "public-contract": "V2",
    "runtime": "V1",
    "frontend_interactive": "V1",       # component with event handler/state/form/Modal/mutation — needs a behavior oracle
    "frontend_presentational": "V0",    # pure copy/CSS/presentational JSX — static_check suffices
    "type-declaration": "V0",           # .d.ts ambient declaration — no runtime behavior, static_check(tsc) suffices
    "test": None,          # evidentiary; does not independently raise the floor
    "docs": "V0",
    "config": "V0",
}

FLOOR_ORDER = {"V0": 0, "V1": 1, "V2": 2, "V3": 3}
ORDER_FLOOR = {v: k for k, v in FLOOR_ORDER.items()}

MIN_EVIDENCE = {
    "V0": ["targeted-static", "build", "visual-check"],
    "V1": ["reference-scan", "affected-tests", "targeted-behavior"],
    "V2": ["expanded-impact-scan", "regression", "integration", "core-e2e"],
    "V3": ["deep-evidence", "rollback-plan", "authorization-gate"],
}

# ---- path signals (regex on path) ----
# order-sensitive: judge the heaviest first; a hit fixes the surface
PATH_SIGNALS = [
    # V3
    (r"(auth|permission|credential|secret|login|logout|token|oauth|password|otp|2fa)", "secret-auth"),
    (r"(payment|paywall|billing|stripe|charge|refund|price|order|invoice|wallet)", "secret-auth"),
    (r"(migration|/db/|schema|\.sql$|seeds?/|fixtures/)", "schema-migration"),
    # V2 shared-state
    (r"(store|Store|reducer|recoil|zustand|redux|globalstate|global-state|context/provider|Provider|atom|selector)", "shared-state"),
    # V2 public-contract
    (r"(/api/|^api/|/routes?/|router|Router|controller|Controller|resolver|graphql|\.gql$|/handlers?/|endpoints?/|/rpc/)", "public-contract"),
    (r"(index\.ts$|index\.js$|barrel|/public-api/|/exports?/)", "public-contract"),
    # test / docs / config
    (r"(\.test\.|\.spec\.|__tests__/|/tests?/|\.bench\.|/fixtures/.*test)", "test"),
    (r"(\.md$|^docs/|README|CHANGELOG|CONTRIBUTING|LICENSE)", "docs"),
    (r"(\.config\.|\.conf$|\.env|\.eslintrc|\.prettierrc|tsconfig|babel\.config|vite\.config|webpack\.config|jest\.config|pyproject|package\.json$)", "config"),
]

# ---- content signals (grep file content, used to upgrade runtime/public to a heavier surface) ----
# only scan the first 64KB; a hit upgrades (takes the heavier surface)
CONTENT_SIGNALS = [
    (r"\b(drop\s+table|truncate\s+table|delete\s+from|irreversible|destroy_all|DROP\s+COLUMN)\b", "schema-migration"),
    (r"\b(stripe|payment_intent|charge|refund|checkout\.session|subscription)\b", "secret-auth"),
    # tightened: dropped bare permissions? — "permission" appears in many non-auth contexts
    # (the orchestrator's permission_denials is tool-permission rejection, not authz). Keep
    # authz-specific words. PATH_SIGNALS' secret-auth path still catches auth files earnestly.
    (r"\b(role_policy|authorize|has_access|require_permission|policy\.rb|can_can|cancan|guard_clause)\b", "secret-auth"),
    (r"\b(applySchema|createTable|alterTable|db\.migrate|alembic|prisma\s+migrate)\b", "schema-migration"),
]

# extension -> default surface (when no path signal hits)
EXT_DEFAULT = {
    ".ts": "runtime", ".js": "runtime",     # generic TS/JS, mostly backend/shared logic; components use .tsx/.jsx
    ".tsx": "frontend_presentational", ".jsx": "frontend_presentational",
    ".py": "runtime", ".go": "runtime", ".java": "runtime", ".kt": "runtime",
    ".rs": "runtime", ".rb": "runtime", ".php": "runtime", ".cs": "runtime",
    ".css": "frontend_presentational", ".scss": "frontend_presentational",
    ".less": "frontend_presentational", ".html": "frontend_presentational",
    ".vue": "frontend_presentational", ".svelte": "frontend_presentational",
    ".sql": "schema-migration",
    ".md": "docs",
    ".json": "config", ".toml": "config", ".yaml": "config", ".yml": "config",
    ".ini": "config", ".env": "config",
}

CONTENT_READ_LIMIT = 64 * 1024

# ---- frontend interaction signals (grep current file content) ----
# any hit -> frontend_presentational upgrades to frontend_interactive (needs a behavior oracle).
# only scanned for component-class extensions (.tsx/.jsx/.vue/.svelte) — .css/.scss/.html are
# skipped (to avoid false positives like the ".submit-button" CSS class). Phased: this pass
# looks at current file content (not the diff), over-flagging conservatively — a file that
# already contains interaction triggers even when you only changed a comment, but risk-adaptive
# leans safe. The limitation is recorded in the receipt: not diff-aware, may over-flag; upgrade
# to diff-aware only when a real misjudgment case appears.
FRONTEND_INTERACTION_PATTERNS = [
    (r"\bon[A-Z]\w*\s*[=:]", "event-handler"),          # onClick={...} / onSubmit=
    (r"\b(useState|useReducer|useEffect|useMemo|useCallback|useRef)\b", "react-hook"),
    (r"<(form|input|textarea|select)\b", "form-element"),
    (r"\b(Modal|Dialog|Drawer|Popover|Tooltip|Dropdown)\b", "interactive-container"),
    (r"\b(useMutation|useQuery|mutate\b|fetch\(|axios)\b", "api-mutation"),
    (r"\b(useNavigate|useLocation|navigate\(|<Link\b)", "navigation"),
    (r"\b(useDispatch|useSelector|dispatch\(|setState)\b", "store-dispatch"),
    (r"\b(loading\b|disabled\b|isError\b|isLoading\b|errorState\b)", "state-bit"),
]

# extensions that hit the frontend interaction patterns (component class). .css/.scss/.less/.html excluded.
FRONTEND_COMPONENT_EXTS = {".tsx", ".jsx", ".vue", ".svelte"}



def classify_one(path: str) -> tuple[str, list[str]]:
    """Return (surface, signals[]). signals are the judgment clues that hit, for audit."""
    signals: list[str] = []
    p = path.replace("\\", "/")
    surface = None

    # 0) .d.ts / .d.cts / .d.mts — TypeScript ambient declaration files
    # surface is fixed to type-declaration: declaration files have no runtime behavior; the
    # meaningful evidence is static_check (tsc) / contract-diff, NOT a runtime behavior oracle.
    # path signals only raise the floor (risk strength) without changing surface — surface
    # decides "what evidence is meaningful", risk decides "how strong is needed", the two are
    # decoupled (see floor_for). A public API .d.ts is high-risk V2, but its evidence is still
    # tsc/contract-diff, not browser/runtime behavior.
    # (The false-escalation root cause a calibration run hit: the old EXT_DEFAULT mapped
    # .ts->runtime->demanding behavior, structurally unsatisfiable for a declaration file.)
    if p.endswith(".d.ts") or p.endswith(".d.cts") or p.endswith(".d.mts"):
        surface = "type-declaration"
        signals.append("ext:.d.ts(type-declaration)")
        for pat, surf in PATH_SIGNALS:
            if re.search(pat, p, re.IGNORECASE):
                signals.append(
                    f"path-raises-floor:{surf}->{SURFACE_FLOOR.get(surf)}")
                break  # one clue is enough; floor_for computes the actual max
        return surface, signals

    # 1) path signals
    for pat, surf in PATH_SIGNALS:
        if re.search(pat, p, re.IGNORECASE):
            surface = surf
            signals.append(f"path:{pat[:24]}")
            break

    # 2) extension fallback
    if surface is None:
        ext = Path(p).suffix.lower()
        surface = EXT_DEFAULT.get(ext)
        if surface:
            signals.append(f"ext:{ext}")

    # 3) content signals (only scan when the file exists, current surface is lighter than V3,
    #    and it is not test/docs/config)
    if surface in (None, "runtime", "public-contract", "shared-state", "frontend_presentational"):
        try:
            if os.path.exists(path) and os.path.isfile(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(CONTENT_READ_LIMIT)
                for pat, surf in CONTENT_SIGNALS:
                    if re.search(pat, head, re.IGNORECASE):
                        # take the heavier surface (content may not downgrade)
                        if SURFACE_FLOOR.get(surf) and (
                            SURFACE_FLOOR.get(surface) is None
                            or FLOOR_ORDER[SURFACE_FLOOR[surf]] > FLOOR_ORDER[SURFACE_FLOOR[surface]]
                        ):
                            surface = surf
                            signals.append(f"content:{pat[:24]}")
                # frontend interaction upgrade: a component-class file containing event
                # handler/state/form/Modal/mutation -> frontend_presentational upgrades to
                # frontend_interactive (V0->V1). Only scanned for .tsx/.jsx/.vue/.svelte
                # (a CSS class like .submit-button would false-positive).
                if surface == "frontend_presentational" \
                        and Path(p).suffix.lower() in FRONTEND_COMPONENT_EXTS:
                    for pat, label in FRONTEND_INTERACTION_PATTERNS:
                        if re.search(pat, head, re.IGNORECASE):
                            surface = "frontend_interactive"
                            signals.append(f"frontend:{label}")
                            break  # any one hit upgrades
        except OSError:
            signals.append("content:unreadable")

    if surface is None:
        surface = "runtime"  # unknown -> runtime, floor V1, does not default to V0
        signals.append("default:runtime(unknown)")

    return surface, signals


def floor_of(surface: str) -> str | None:
    return SURFACE_FLOOR.get(surface)


def floor_for(path: str, surface: str) -> str | None:
    """Per-file risk floor (decoupled version).

    .d.ts: surface fixed to type-declaration (evidence contract = static_check, no runtime
    behavior), but the floor can be raised by path signals (risk strength). Surface decides
    "what evidence is meaningful", risk decides "how strong is needed" — the two are decoupled.
    A high-impact .d.ts != needs a runtime behavior oracle; its meaningful evidence is
    tsc/contract-diff/reference-scan.

    Other surfaces: floor = floor_of(surface) (original behavior unchanged).
    """
    base = floor_of(surface)
    p = path.replace("\\", "/")
    if not (p.endswith(".d.ts") or p.endswith(".d.cts") or p.endswith(".d.mts")):
        return base
    # .d.ts: take the highest floor implied by path signals as risk (without changing surface)
    raise_floor = base
    for pat, surf in PATH_SIGNALS:
        if re.search(pat, p, re.IGNORECASE):
            fl = SURFACE_FLOOR.get(surf)
            if fl and (raise_floor is None
                       or FLOOR_ORDER[fl] > FLOOR_ORDER[raise_floor]):
                raise_floor = fl
    return raise_floor


def aggregate(files: list[dict]) -> dict:
    touched = sorted({f["surface"] for f in files if f["surface"] != "test"})
    # floor = the highest floor among non-evidentiary surfaces
    floor_lvl = -1
    reasons: list[str] = []
    for f in files:
        # use the per-file floor (set by floor_for, supports .d.ts surface/floor decoupling),
        # falling back to floor_of(surface) only when missing.
        fl = f.get("floor")
        if fl is None:
            fl = floor_of(f["surface"])
        if fl is None:
            continue
        lvl = FLOOR_ORDER[fl]
        if lvl > floor_lvl:
            floor_lvl = lvl
            reasons = [f"{f['surface']}: {f['path']}"]
        elif lvl == floor_lvl and lvl >= 0:
            reasons.append(f"{f['surface']}: {f['path']}")

    if floor_lvl < 0:
        # all test/docs/config
        floor_lvl = 0
        reasons = ["only docs/config/tests touched"]

    risk_floor = ORDER_FLOOR[floor_lvl]
    # unknown files (signals contain default:runtime) trigger unresolved
    unresolved = []
    if any("default:runtime(unknown)" in f.get("signals", []) for f in files):
        unresolved.append("one or more files could not be confidently classified; treat as V1+, do not default V0")

    return {
        "surfaces_touched": touched,
        "risk_floor": risk_floor,
        "floor_reasons": reasons,
        "minimum_evidence": MIN_EVIDENCE[risk_floor],
        "unresolved": unresolved,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="classify write_areas -> risk floor + min evidence")
    ap.add_argument("paths", nargs="*", help="file paths in write_areas")
    ap.add_argument("--json", help="json array of {path} or list of paths")
    ap.add_argument("--manifest", help="json file with write_areas:[...] and optional intent")
    ap.add_argument("--stdin", action="store_true", help="read newline-separated paths from stdin")
    args = ap.parse_args(argv)

    paths: list[str] = list(args.paths)
    if args.json:
        data = json.loads(args.json)
        if isinstance(data, list):
            for item in data:
                paths.append(item["path"] if isinstance(item, dict) else str(item))
    if args.manifest:
        m = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        for w in m.get("write_areas", []):
            paths.append(w["path"] if isinstance(w, dict) else str(w))
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if line:
                paths.append(line)

    if not paths:
        ap.print_usage(sys.stderr)
        return 2

    file_results = []
    for p in paths:
        surface, signals = classify_one(p)
        file_results.append({
            "path": p,
            "surface": surface,
            "floor": floor_for(p, surface),
            "signals": signals,
        })

    agg = aggregate(file_results)
    out = {"files": file_results, **agg}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
