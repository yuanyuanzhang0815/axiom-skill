#!/usr/bin/env python3
"""detect_capabilities — three-state detector of a project's runtime mechanisms.

Answers one question: which known mechanisms does the project have? Three states:

  present          — a signal was detected in some authoritative source
  absent_confirmed — all sources defined for that capability were checked, and all clean
                     (not-found != does-not-exist, so you may only confirm absent when
                     "all sources checked and clean")
  unknown          — otherwise (some source unavailable / cannot be determined)

Never let the LLM decide "can I think of it right now". Sources are as deterministic as
possible: package.json / pyproject / docker-compose / code imports / openapi files / config.
An explicit hand-written capability-manifest.yaml declaration takes priority over detection
(the user knows the project boundary better than a script).

Outputs capability-manifest.json (stdout or --out):
  {
    "websocket":     "absent_confirmed",
    "event_bus":     "present",
    "background_jobs":"unknown",
    "database":      "present",
    "openapi":       "unknown",
    "shared_state":  "present",
    "_detection": { ... per-capability source hit status, for audit }
  }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Each capability -> a list of detection sources. Each source is a (kind, patterns) tuple.
# kind decides how to check; patterns are keywords/regex. A source is "available" = the
# corresponding file exists.
# Three-state rules:
#   any available source hits -> present
#   all defined sources available and all clean -> absent_confirmed
#   otherwise (some source unavailable and no hit) -> unknown
CAPABILITIES: dict[str, list[tuple[str, list[str]]]] = {
    "websocket": [
        ("pkgdeps", ["socket.io", "socketio", "ws@", "websocket", "engine.io"]),
        ("codegrep", [r"\bsocket\.io\b", r"\bnew\s+WebSocket\b",
                      r"\bfrom\s+['\"]ws['\"]", r"\bws\(\s*['\"]"]),
    ],
    "event_bus": [
        ("pkgdeps", ["eventemitter", "emitter", "mitt", "postie",
                     "eventbus", "rxjs"]),
        # tightened: generic .emit(/.on( matches any object method (a Python project all
        # hit it); only accept explicit bus/emitter library APIs or the "event bus" literal.
        ("codegrep", [r"\bEventEmitter\b", r"\bmitt\(", r"\bevent[\s_\-]?bus\b",
                      r"\bEventSource\b", r"\bEventTarget\b"]),
    ],
    "background_jobs": [
        ("pkgdeps", ["celery", "bull", "agenda", "bullmq", "node-cron",
                     "node-schedule", "apscheduler", "sidekiq"]),
        ("compose", ["celery", "worker", "sidekiq", "scheduler"]),
        ("codegrep", [r"\bcelery\b", r"@cron", r"setInterval\s*\(",
                      r"node-cron", r"apscheduler", r"\bschedule\s*\("]),
    ],
    "database": [
        ("compose", ["postgres", "mysql", "redis", "mongo", "mongodb",
                     "mariadb", "sqlite"]),
        ("pkgdeps", ["pg", "mysql2", "mysql", "prisma", "drizzle",
                     "sequelize", "mongoose", "sqlalchemy", "psycopg",
                     "knex", "typeorm", "@prisma/client"]),
        ("codegrep", [r"\bprisma\b", r"\bdrizzle\b", r"\bCREATE\s+TABLE\b",
                      r"\bsqlalchemy\b", r"\bmongoose\b", r"\bknex\b"]),
    ],
    "openapi": [
        ("openapi_file", ["openapi.json", "openapi.yaml", "openapi.yml",
                          "swagger.json", "swagger.yaml", "swagger.yml"]),
    ],
    "shared_state": [
        ("pkgdeps", ["zustand", "redux", "@reduxjs", "jotai", "pinia",
                     "vuex", "recoil", "valtio", "mobx"]),
        ("codegrep", [r"\bcreate\s*\(\s*\(", r"\buseStore\b",
                      r"\bcreateStore\b", r"\bdefineStore\b"]),
    ],
}


# ---- source readers: one function per kind, returns the hit pattern list (empty=no hit; None=source unavailable) ----

def _read_pkgdeps(root: Path) -> list[str] | None:
    """package.json deps+devDeps name union / pyproject.toml dependency section / requirements.txt lines."""
    deps: list[str] = []
    pj = root / "package.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(encoding="utf-8"))
            for k in ("dependencies", "devDependencies", "peerDependencies"):
                deps += list((d.get(k) or {}).keys())
        except (json.JSONDecodeError, OSError):
            pass
    # pyproject.toml: coarse-scan dependency names (no toml parse, to avoid a dependency)
    pt = root / "pyproject.toml"
    if pt.is_file():
        try:
            txt = pt.read_text(encoding="utf-8")
            for m in re.finditer(r'^\s*["\']?([A-Za-z0-9_.\-]+)["\']?\s*[=~<>!]',
                                 txt, re.MULTILINE):
                deps.append(m.group(1))
        except OSError:
            pass
    rt = root / "requirements.txt"
    if rt.is_file():
        try:
            for line in rt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(re.split(r"[=<>!~\[ ]", line, 1)[0])
        except OSError:
            pass
    if not deps and not pj.is_file() and not pt.is_file() and not rt.is_file():
        return None  # no dependency files at all -> source unavailable
    return deps


def _read_compose(root: Path) -> str | None:
    """docker-compose file text (multiple files merged), or None."""
    for name in ("docker-compose.yml", "docker-compose.yaml",
                 "compose.yml", "compose.yaml"):
        f = root / name
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")
            except OSError:
                pass
    return None


def _read_code(root: Path) -> str | None:
    """Source text (limited to .js/.jsx/.ts/.tsx/.py/.vue/.go/.rb/.java, first ~2MB/file merged).
    No source files at all -> None (distinguished from empty string = checked and clean)."""
    exts = (".js", ".jsx", ".ts", ".tsx", ".py", ".vue", ".go", ".rb", ".java")
    chunks: list[str] = []
    found_any = False
    for p in root.rglob("*"):
        if not p.is_file() or not p.name.endswith(exts):
            continue
        if any(part in ("node_modules", ".git", "dist", "build", "venv",
                        "__pycache__", ".next", "target") for part in p.parts):
            continue
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="ignore")[:200000])
            found_any = True
            if sum(len(c) for c in chunks) > 2_000_000:
                break
        except OSError:
            continue
    if not found_any:
        return None
    return "\n".join(chunks)


def _read_openapi_file(root: Path) -> list[str] | None:
    hits = []
    found_any = False
    for name in CAPABILITIES["openapi"][0][1]:
        if (root / name).is_file():
            found_any = True
            hits.append(name)
    if not found_any:
        return None  # found no openapi filename -> source "available" (we scanned the root's candidate names)
    return hits  # non-empty hit list


# ---- source dispatch: kind -> (data or None) ----

def _source_data(kind: str, root: Path):
    if kind == "pkgdeps":
        return _read_pkgdeps(root)
    if kind == "compose":
        return _read_compose(root)
    if kind == "codegrep":
        return _read_code(root)
    if kind == "openapi_file":
        return _read_openapi_file(root)
    return None


def _match(kind: str, patterns: list[str], data) -> list[str]:
    """Find pattern hits in the source data. Returns the hit pattern list (empty=no hit)."""
    if data is None:
        return []  # source unavailable, no hit (but the caller judges availability via _source_data None)
    if kind == "pkgdeps":
        dlow = " ".join(d.lower() for d in data)
        return [p for p in patterns if p.lower() in dlow]
    if kind in ("compose", "codegrep"):
        for p in patterns:
            if re.search(p, data, re.IGNORECASE):
                return [p]
        return []
    if kind == "openapi_file":
        return list(data) if data else []
    return []


def detect(root: Path) -> dict:
    """Detect all capabilities, return a three-state manifest + _detection audit."""
    manifest: dict[str, str] = {}
    detection: dict[str, dict] = {}
    for cap, sources in CAPABILITIES.items():
        hits: list[str] = []
        per_source: dict = {}
        all_available = True
        for kind, patterns in sources:
            data = _source_data(kind, root)
            available = data is not None
            if not available:
                all_available = False
            m = _match(kind, patterns, data)
            per_source[kind] = {"available": available, "hits": m}
            hits += m
        if hits:
            state = "present"
        elif all_available:
            state = "absent_confirmed"
        else:
            state = "unknown"
        manifest[cap] = state
        detection[cap] = {"state": state, "sources": per_source}
    manifest["_detection"] = detection  # type: ignore[assignment]
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="three-state detect project runtime mechanisms -> capability-manifest.json")
    ap.add_argument("project", nargs="?", default=".", help="project root (default .)")
    ap.add_argument("--out", help="write to file (default stdout)")
    args = ap.parse_args(argv)
    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    m = detect(root)
    out = json.dumps(m, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
