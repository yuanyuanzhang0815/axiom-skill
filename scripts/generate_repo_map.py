#!/usr/bin/env python3
"""Generate a derived Repo Map (structural-fact navigation index) for axiom.

This is a **retrieval surface, NOT a source of truth.** Stores structural facts, few semantic conclusions:
- Per module: top-level class/def + line + imported-by-whom (intra-import edges)
- Per module one role line: machine-extracted from the module docstring's first line (source fact, not AI semantic judgment)
- Top commit stamp: on entry, compare the stamp with `git rev-parse HEAD`; if mismatch, it is stale — do not depend on it

Why this exists: P-002 (`axiom wiki pattern show P-002`) — fixes the "AI scans part of the codebase → global negation" leap
(missed wiki.py → answered "no three-layer structure"). The Repo Map guarantees modules enter your cognitive space, but is not truth —
semantic conclusions must return to first-hand source evidence.

Usage:
  python3 scripts/generate_repo_map.py --out axiom/REPO_MAP.md
  python3 scripts/generate_repo_map.py            # default: axiom/REPO_MAP.md
  python3 scripts/generate_repo_map.py --check     # only verify whether the stamp is stale, no rewrite
"""
from __future__ import annotations
import argparse
import ast
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PKG = "axiom"
REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / PKG
DEFAULT_OUT = PKG_DIR / "REPO_MAP.md"


# --- git ---------------------------------------------------------------------

def _git(args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(REPO_ROOT)] + args,
                           capture_output=True, text=True, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_stamp() -> dict:
    short = _git(["rev-parse", "--short", "HEAD"]) or "unknown"
    branch = _git(["branch", "--show-current"]) or "detached"
    iso = _git(["log", "-1", "--format=%cI"]) or datetime.now(timezone.utc).isoformat()
    return {"commit": short, "branch": branch, "committed_at": iso}


# --- ast analysis ------------------------------------------------------------

def _module_docstring_first_line(tree: ast.Module) -> str:
    doc = ast.get_docstring(tree)
    if not doc:
        return ""
    first = doc.splitlines()[0].strip()
    return f"{first}  *(from module docstring; may be stale)*"


def _top_level_defs(tree: ast.Module):
    classes, funcs = [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append((node.name, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append((node.name, node.lineno))
    return classes, funcs


def _intra_imports(tree: ast.Module) -> list[str]:
    """axiom-internal module names this file imports (e.g. 'ledger' from 'axiom.ledger')."""
    edges: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == PKG or a.name.startswith(f"{PKG}."):
                    parts = a.name.split(".")
                    if len(parts) > 1:
                        edges.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == PKG or node.module.startswith(f"{PKG}.")):
                parts = node.module.split(".")
                if len(parts) > 1:
                    edges.add(parts[1])
    return sorted(edges)


def analyze(f: Path) -> dict:
    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(f))
    classes, funcs = _top_level_defs(tree)
    modname = f.stem  # ledger.py -> ledger
    return {
        "file": f.name,
        "modname": modname,
        "role": _module_docstring_first_line(tree),
        "classes": classes,
        "funcs": funcs,
        "imports": _intra_imports(tree),
    }


# --- layering ----------------------------------------------------------------

def compute_layers(modules: list[dict]) -> dict[str, int]:
    """Topological layer: leaf (no intra-import) = 0; N = max(dep layers)+1."""
    by_name = {m["modname"]: m for m in modules}
    layer: dict[str, int] = {}
    # leaves: no intra-import, or imports only missing modules
    for m in modules:
        deps = [d for d in m["imports"] if d in by_name]
        if not deps:
            layer[m["modname"]] = 0
    remaining = [m["modname"] for m in modules if m["modname"] not in layer]
    changed = True
    while remaining and changed:
        changed = False
        for name in list(remaining):
            deps = [d for d in by_name[name]["imports"] if d in layer]
            if deps and all(d in layer for d in by_name[name]["imports"] if d in by_name):
                layer[name] = max(layer[d] for d in deps) + 1
                remaining.remove(name)
                changed = True
    # unresolved (cyclic?) → put at max+1
    for name in remaining:
        layer[name] = max(layer.values(), default=-1) + 1
    return layer


# --- render ------------------------------------------------------------------

def render(modules: list[dict], stamp: dict) -> str:
    layers = compute_layers(modules)
    by_name = {m["modname"]: m for m in modules}
    # imported-by index
    imported_by: dict[str, list[str]] = {m["modname"]: [] for m in modules}
    for m in modules:
        for dep in m["imports"]:
            if dep in imported_by:
                imported_by[dep].append(m["modname"])

    L: list[str] = []
    L.append("# axiom Repo Map — derived navigation index")
    L.append("")
    L.append("> **Retrieval surface, NOT a source of truth.** Stores structural facts, few semantic conclusions.")
    L.append(f"> Generated from commit `{stamp['commit']}` | branch `{stamp['branch']}` | "
             f"committed `{stamp['committed_at']}`.")
    L.append(f"> On entry, compare the commit above with `git rev-parse --short HEAD` — if mismatch, it is **stale, do not depend on it**.")
    L.append(f"> Regenerate: `python3 scripts/generate_repo_map.py --out axiom/REPO_MAP.md`")
    L.append("")
    L.append("## Dependency layering")
    L.append("```")
    max_l = max(layers.values()) if layers else 0
    for lv in range(0, max_l + 1):
        names = sorted(n for n, l in layers.items() if l == lv)
        tag = "leaf, no intra-import" if lv == 0 else f"layer {lv}"
        if names:
            inner = []
            for n in names:
                deps = [d for d in by_name[n]["imports"] if d in by_name]
                inner.append(f"{n} → [{', '.join(deps)}]" if deps else n)
            L.append(f"L{lv} ({tag}): {' | '.join(inner)}")
    L.append("```")
    L.append("")
    L.append("## Module inventory")
    for m in sorted(modules, key=lambda x: (layers.get(x["modname"], 99), x["modname"])):
        L.append(f"### `{m['file']}`  (L{layers.get(m['modname'], 99)})")
        if m["role"]:
            L.append(f"- role: {m['role']}")
        by = imported_by.get(m["modname"], [])
        L.append(f"- imported by: {', '.join(sorted(by)) if by else '— (leaf or only entry point)'}")
        if m["classes"]:
            L.append(f"- classes: {', '.join(f'{n} (L{ln})' for n, ln in m['classes'])}")
        if m["funcs"]:
            L.append(f"- top-level funcs: {', '.join(f'{n} (L{ln})' for n, ln in m['funcs'][:14])}"
                     + (f"  *+{len(m['funcs'])-14} more*" if len(m['funcs']) > 14 else ""))
        L.append("")
    L.append("## Discipline reminder")
    L.append("- This Map only guarantees modules **enter your cognitive space** (prevents missing wiki.py); it does not prove semantics.")
    L.append("- Any architectural judgment goes through `## Coverage before conclusion` (SKILL.md): fill the coverage checklist before concluding.")
    L.append("- A negative conclusion (\"there is no X\") must declare residual uncertainty; it cannot stand just because the Map/grep doesn't list it.")
    return "\n".join(L) + "\n"


# --- main --------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate axiom derived Repo Map.")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output path (default: axiom/REPO_MAP.md)")
    p.add_argument("--check", action="store_true", help="only verify whether the existing Map's stamp is stale, no rewrite")
    args = p.parse_args(argv)

    if not PKG_DIR.is_dir():
        print(f"ERROR: {PKG_DIR} not found", file=sys.stderr)
        return 2

    if args.check:
        return _check_stale(Path(args.out))

    files = sorted(PKG_DIR.glob("*.py"))
    modules = [analyze(f) for f in files]
    out = render(modules, git_stamp())
    out_path = Path(args.out)
    out_path.write_text(out, encoding="utf-8")
    print(f"wrote {out_path} ({len(modules)} modules, HEAD={git_stamp()['commit']})")
    return 0


def _check_stale(out_path: Path) -> int:
    if not out_path.exists():
        print(f"STALE: {out_path} does not exist (run without --check to generate)")
        return 1
    text = out_path.read_text(encoding="utf-8")
    # extract the commit from the stamp line
    import re
    m = re.search(r"commit `([^`]+)`", text)
    stamped = m.group(1) if m else None
    head = _git(["rev-parse", "--short", "HEAD"])
    if not head:
        # No git repo (deployed copy, bare install): freshness is UNVERIFIABLE.
        # A map generated in this same env carries stamp 'unknown' — self-consistent,
        # accept (rc 0) but say the stamp proves nothing. A map stamped with a real
        # commit was generated elsewhere; it cannot be checked here — treat as
        # stale/untrusted (rc 1) so nobody depends on it silently.
        if stamped in (None, "unknown"):
            print(f"UNVERIFIABLE: no git HEAD here and stamp is {stamped!r} — "
                  f"generated in a git-less env; freshness cannot be proven")
            return 0
        print(f"STALE: stamped {stamped!r} but no git HEAD available to compare — "
              f"do not depend on it; re-run generate_repo_map.py")
        return 1
    if stamped and stamped == head:
        print(f"FRESH: stamped {stamped} == HEAD {head}")
        return 0
    print(f"STALE: stamped {stamped!r} != HEAD {head!r} — re-run generate_repo_map.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
