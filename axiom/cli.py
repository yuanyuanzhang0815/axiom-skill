"""axiom CLI -- the command surface of the deterministic workflow harness.

The orchestrator (an AI agent) designs a bounded workflow spec, hands it to
`axiom run`, and reads back ONLY the checkpoint. Node-level execution and
intermediate state live in the ledger; they never re-enter orchestrator
context. See SKILL.md for the orchestrator loop.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from axiom.ir import spec_from_json, validate_spec, lint_spec, contract_hash
from axiom.ledger import Ledger
from axiom.state import derive_verdict, project_cognitive_state, derive_debug_envelope
from axiom.harness import Harness, GateHalt
from axiom.wiki import Wiki, extract_entry, extract_contract_entry


def _augment_loops_with_iteration(loops: list[dict], ledger, svid) -> list[dict]:
    """Add the current `iteration` to each ACTIVE loop in the `loops` projection.

    project_cognitive_state's `loops` field (Task 6) exposes lifecycle
    (loop_id/state/exit_reason/node_id) but NOT the current iteration. Spec §4
    says the orchestrator should see loop progress (PROGRESSING + loop_id +
    iteration) without diving the journal, so the CLI augments each active loop
    with the latest `iteration_open` event's iteration for that loop_id. Closed
    loops get iteration=None (the loop is no longer iterating).

    This is a CLI rendering concern (honest projection, invariant 6), not a
    state.py truth-source change — the loops field stays as Task 6 defined it.
    """
    # latest iteration_open per loop_id (svid-scoped, append order = ledger order)
    latest_iter: dict[str, int] = {}
    for e in ledger.events():
        if e.get("spec_version_id", svid) != svid:
            continue
        if e.get("kind") == "iteration_open":
            p = e.get("payload", {})
            lid = p.get("loop_id")
            if lid is not None:
                latest_iter[lid] = p.get("iteration", latest_iter.get(lid))
    for loop in loops:
        if loop.get("state") == "active":
            loop["iteration"] = latest_iter.get(loop.get("loop_id"))
        else:
            loop["iteration"] = None
    return loops


def _add_run_dir(sp):
    sp.add_argument("--run-dir", default=".axiom/run",
                    help="run directory holding events.jsonl + spec revisions")


def _add_wiki_dir(sp):
    sp.add_argument("--wiki-dir", default=None,
                    help="wiki directory holding wiki.jsonl (append-only). "
                         "Default: <project-root>/.axiom/wiki when "
                         "--project-root is given, else .axiom/wiki (cwd).")


def _resolve_wiki_dir(wiki_dir, project_root=None):
    """P-020: the wiki belongs to the PROJECT the run works on, not to the
    caller's cwd. `axiom run --project-root A` invoked from directory B used
    to sediment into B/.axiom/wiki (cwd-relative default), so the experience
    never landed in project A's store and `plan --wiki-suggest` inside A
    could not retrieve it (the project_root_wiki_location probe). Explicit
    --wiki-dir always wins; else project-root-scoped; else cwd-relative."""
    if wiki_dir:
        return wiki_dir
    if project_root:
        return str(Path(project_root) / ".axiom" / "wiki")
    return ".axiom/wiki"


def _add_adopted_from(sp):
    # P-014 co-evolution loop: the host's adoption judgment (which wiki experience /
    # format_contract this spec was derived from) is recorded durably on the
    # spec and attributed back as adoption_outcome impact amendments at seal.
    sp.add_argument("--adopted-from", action="append", default=None,
                    metavar="ENTRY_ID",
                    help="wiki entry_id (unique prefix OK) this spec adopts; "
                         "repeatable. Merged into spec.adopted_from; at seal "
                         "each adopted entry gets an adoption_outcome impact "
                         "with the run's verdict")


def _merge_adopted_from(spec, cli_list) -> None:
    """Merge --adopted-from flags into spec.adopted_from (dedupe, spec order
    first then CLI order). Mutates the in-memory spec only -- the spec FILE
    is never rewritten (immutable; the CLI declaration is run-scoped).

    R6-2: entries already present on the loaded spec are adopted as-is and
    are NEVER re-validated as declarations (the spec file is immutable --
    the host cannot strip them to satisfy a context gate). Use
    spec.adopted_pending (set here) for the entries this invocation
    actually declared; gate checks must read adopted_pending, not
    adopted_from."""
    base = list(getattr(spec, "adopted_from", None) or [])
    merged = list(base)
    for eid in (cli_list or []):
        if eid and eid not in merged:
            merged.append(eid)
    spec.adopted_from = merged
    spec.adopted_pending = [e for e in merged if e not in set(base)]


def _load_spec(path):
    spec = spec_from_json(Path(path).read_text(encoding="utf-8"))
    _enforce_delivery_contract_binding(spec, Path(path))
    return spec


def _enforce_delivery_contract_binding(spec, spec_path: Path) -> None:
    """New-protocol binding check (spec §13.1): once any delivery-contract
    field is present the binding must be complete, the referenced contract
    must load, its digest must match, and budget_usd must mirror the task
    contract's resource_limits.cost_usd (equal values, or both null). Any
    failure is a hard refusal -- never a silent fallback to the legacy path.
    """
    bound = [spec.task_id is not None,
             spec.delivery_contract_ref is not None,
             spec.delivery_contract_digest is not None]
    if not any(bound):
        return  # legacy path: untouched
    if not all(bound):
        raise ValueError(
            "delivery-contract binding is partial (need task_id + "
            "delivery_contract_ref + delivery_contract_digest); refusing to "
            "run/verdict as legacy")
    from axiom.contract import contract_from_json, contract_digest, validate_contract
    ref = Path(spec.delivery_contract_ref)
    if not ref.is_absolute():
        ref = spec_path.parent / ref
    if not ref.is_file():
        raise ValueError(
            f"delivery_contract_ref {spec.delivery_contract_ref!r} not found "
            f"(resolved {ref}); refusing to guess")
    contract = contract_from_json(ref.read_text(encoding="utf-8"))
    errs = validate_contract(contract)
    if errs:
        raise ValueError(f"invalid delivery contract: {'; '.join(errs)}")
    if contract.task_id != spec.task_id:
        raise ValueError(
            f"task_id mismatch: spec {spec.task_id!r} vs contract "
            f"{contract.task_id!r}")
    actual = contract_digest(contract)
    if actual != spec.delivery_contract_digest:
        raise ValueError(
            f"delivery_contract_digest mismatch: spec declares "
            f"{spec.delivery_contract_digest!r} but contract file digests to "
            f"{actual!r}; re-bind to the current contract revision")
    task_cost = contract.resource_limits.get("cost_usd", None)
    if spec.budget_usd != task_cost:
        raise ValueError(
            f"budget mirror conflict: spec.budget_usd={spec.budget_usd!r} vs "
            f"contract resource_limits.cost_usd={task_cost!r}; the workflow "
            f"budget is a compatibility mirror, not a separate allowance "
            f"(spec §13.1)")


def _check_run_dir_nesting(run_dir: str) -> None:
    """Refuse a run-dir nested inside another run-dir.

    A worker whose cwd is its own run-dir (the dispatch path cwd=run_dir) can
    execute `axiom run --run-dir .axiom/run` from its prompt and
    recursively create run-dir/.axiom/run/.axiom/run/... infinitely --
    each layer exits 0, so the outer orchestrator sees success and finds only
    nested run-dirs, no output (the docx-review relay hit this). Block it: if
    any ANCESTOR of the run-dir already holds events.jsonl / run_manifest.json,
    this run-dir is nested inside an existing one -> refuse.

    Resume re-running the SAME run-dir is unaffected: only ancestors are
    checked, never the run-dir itself (which legitimately holds events.jsonl
    on resume).
    """
    p = Path(run_dir).resolve()
    for parent in p.parents:
        if (parent / "events.jsonl").exists() or (parent / "run_manifest.json").exists():
            raise ValueError(
                f"run-dir {run_dir!r} is nested inside an existing run-dir "
                f"({parent}). A worker likely re-invoked `axiom run` from "
                f"inside its own cwd (the run-dir), which recurses infinitely. "
                f"Move the run-dir outside any existing run-dir, or fix the "
                f"spec so no agent prompt runs `axiom run` (no recursion)."
            )


_HELD_LOCKS: "dict[str, int]" = {}  # resolved run_dir -> fd (reentrance)


def _acquire_run_lock(run_dir: str) -> "int | None":
    """Cross-process lock so two `axiom run` on the same run-dir can't
    corrupt the hash chain. flock(LOCK_EX|LOCK_NB) on run_dir/lock; returns
    the held fd (keep it open for the run's lifetime) or None if already
    locked by another process.

    Reentrant within a process: if the same process already holds the lock on
    this run_dir (a test simulating run + resume in-process, or a future
    nested dispatch), the held fd is returned without a second flock. A second
    flock would self-deadlock on BSD -- flock is per-fd, not per-process, so a
    second LOCK_EX|LOCK_NB on the same file in the same process returns
    EWOULDBLOCK. Cross-process semantics are unchanged: the second PROCESS
    still gets None."""
    p = Path(run_dir)
    p.mkdir(parents=True, exist_ok=True)
    key = str(p.resolve())
    if key in _HELD_LOCKS:
        return _HELD_LOCKS[key]
    fd = os.open(str(p / "lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _HELD_LOCKS[key] = fd
    return fd


def _cross_task_contaminating(events, spec_svid, spec_parent_svid):
    """Return the set of existing event spec_version_ids that are neither the
    current spec's svid nor its parent (i.e. from a different task), or None if
    the run-dir is clean / same-task. cmd_run refuses cross-task run-dir
    contamination (a fresh task silently inheriting an old task's hash
    chain/journal -> spec filename collisions + scattered run-dirs). --fresh
    bypasses (caller resets before this); revision resume (parent svid) +
    same-svid re-append are allowed (return None)."""
    if not events:
        return None
    existing_svids = {
        e.get("spec_version_id") for e in events if e.get("spec_version_id")
    }
    allowed = {spec_svid, spec_parent_svid}
    allowed.discard(None)
    contaminating = existing_svids - allowed
    return contaminating or None


def _exit_for_verdict(verdict: str) -> int:
    """Granular exit codes so automation (`axiom run && deliver`, CI
    `case $?`) can triage without parsing the checkpoint JSON: 0=VERIFIED,
    3=BLOCKED (resolve a gate then re-run), 4=PARTIAL (replan), 5=UNVERIFIED
    (gather evidence), 2=unknown/invalid. Non-zero still means not-VERIFIED,
    so `axiom run && deliver` is unaffected; only the 2-vs-3/4/5 distinction
    is new."""
    return {"VERIFIED": 0, "BLOCKED": 3, "PARTIAL": 4, "UNVERIFIED": 5}.get(verdict, 2)


# --- cross-session active-run pointer (ambient resume) ----------------------
# `axiom run`/`resume` write a pointer recording WHERE the active run lives
# (absolute paths + svid + seal state), so a fresh session after /clear can
# `axiom continue` (no args) and land on the right checkpoint instead of
# scanning disk / reading memory to rediscover it. Two locations:
#   <project_root>/.axiom/active.json  -- project-scoped (authoritative)
#   ~/.axiom/active.json               -- global last-active mirror (wrong-cwd)
# `continue` reads cwd-local first, then global. The pointer is advisory
# resumption state; events.jsonl (the ledger) remains the truth source.

def _active_global_path() -> Path:
    return Path.home() / ".axiom" / "active.json"


def _active_project_path(project_root: str | None) -> Path | None:
    if not project_root:
        return None
    return Path(project_root) / ".axiom" / "active.json"


def _write_active(spec_path, run_dir, spec_version_id, project_root=None,
                  sealed=False, verdict=None, started_at=None):
    """Record the active run's location. Absolute paths; idempotent overwrite.
    Returns the started_at timestamp used (callers pass it back on the sealed
    update to preserve the original start). Non-fatal: a write failure is
    logged to stderr, never blocks the run."""
    ts = started_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    spec_abs = str(Path(spec_path).resolve()) if spec_path else None
    run_abs = str(Path(run_dir).resolve()) if run_dir else None
    pr_abs = (str(Path(project_root).resolve()) if project_root
              else str(Path.cwd().resolve()))
    entry = {
        "project_root": pr_abs,
        "spec_path": spec_abs,
        "run_dir": run_abs,
        "spec_version_id": spec_version_id,
        "started_at": ts,
        "sealed": sealed,
        "verdict": verdict,
    }
    for p in (_active_global_path(), _active_project_path(pr_abs)):
        if p is None:
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            print(f"active-pointer: could not write {p}: {e}", file=sys.stderr)
    return ts


def _read_active() -> dict | None:
    """Find the active-run pointer: cwd-local project-scoped first, then the
    global ~/.axiom mirror. Returns the entry dict or None."""
    cwd_local = Path.cwd() / ".axiom" / "active.json"
    for p in (cwd_local, _active_global_path()):
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _run_context_path(run_dir) -> Path:
    return Path(run_dir) / "run_context.json"


def _write_run_context(run_dir, project_root=None, auto_wiki=False,
                       wiki_dir=None, adopted_from=None,
                       adopted_from_svid=None, merge_prior=False,
                       runtime_backend=None) -> None:
    """P-019: persist the run's invocation context INTO the run-dir at start.

    The global active pointer answers "which run was last active"; it cannot
    answer "which project does run-dir X belong to" for an EXPLICIT
    `axiom resume <spec> --run-dir X` (the pointer may point at a different
    project B, and resuming run A then dispatches workers with cwd=B -- the
    explicit_resume probe). Same for sediment intent: a run started with
    --auto-wiki must keep sedimenting across resume/continue, or a
    BLOCKED->resume->VERIFIED sequence leaves the wiki at BLOCKED
    (resume_leaves_blocked_experience). Recording the invocation context in
    the run-dir itself makes both answers local to the run being resumed.
    Non-fatal: advisory metadata, never blocks the run."""
    ctx = {
        "project_root": (str(Path(project_root).resolve())
                         if project_root else str(Path.cwd().resolve())),
        "auto_wiki": bool(auto_wiki),
        # P-029: persist the RESOLVED ABSOLUTE wiki_dir. A relative
        # ".axiom/wiki" survives verbatim into run_context and is then
        # re-interpreted against the RESUMER's cwd -- resume from directory
        # B sediments into B/.axiom/wiki while the original run's entries
        # live in A (review4 test_relative_wiki_path_stays_with_original_run).
        "wiki_dir": (str(Path(wiki_dir).resolve()) if wiki_dir else None),
        # P-030: run-scoped adoption declarations (--adopted-from / in-spec
        # adopted_from) are invocation context too. They were only merged
        # into the in-memory spec at run start, so a resume lost the CLI
        # declaration: the adopted entry got its BLOCKED outcome impact but
        # never the resumed VERIFIED (review4
        # test_cli_adoption_is_retained_on_resume).
        # R5-5: the declaration is now BOUND TO a spec version
        # (adopted_from_svid) -- a resume under a DIFFERENT svid (child
        # spec.v2 in the same run-dir) must not inherit the parent
        # version's adoption relationship; SKILL.md: child versions
        # re-declare explicitly (review5
        # test_child_spec_requires_explicit_adoption_declaration).
        # R5-4: cmd_resume persists back declarations first made AT resume
        # time (same svid), so a second resume inherits them (review5
        # test_adoption_declared_during_resume_persists).
        # R6-2: merge_prior=True folds the context's PRIOR declarations
        # (other svids') into the write -- declarations are per-svid state
        # and must not be erased when a different svid's invocation rewrites
        # the context (parent ctx overwritten by a child resume lost the
        # parent's declaration, and a later parent resume then failed
        # attribution silently). The flat adopted_from/adopted_from_svid
        # pair is kept as the LATEST writer's declaration (back-compat for
        # readers); adopted_from_map[{svid: [...]}] (R6-3) carries every
        # svid's declaration.
        "adopted_from": list(adopted_from or []),
        "adopted_from_svid": adopted_from_svid,
    }
    # R9-2: the execution backend is invocation context too. WHO executed
    # (pi/cc), WHERE (location), and WITH WHAT capabilities
    # must survive resume -- a resume that silently re-resolves the backend
    # from the environment can dispatch to a DIFFERENT host than the
    # original run (review9: the recorded backend is the run's fact; a
    # caller override is a switch and must leave a ledger event).
    if runtime_backend is not None:
        ctx["runtime_backend"] = dict(runtime_backend)
    elif merge_prior:
        try:
            p = _run_context_path(run_dir)
            if p.is_file():
                prior = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(prior, dict) and prior.get("runtime_backend"):
                    ctx["runtime_backend"] = prior["runtime_backend"]
        except (OSError, json.JSONDecodeError):
            pass
    if merge_prior:
        try:
            p = _run_context_path(run_dir)
            if p.is_file():
                prior = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(prior, dict):
                    merged_map = {
                        str(k): list(v)
                        for k, v in (prior.get("adopted_from_map")
                                     or {}).items()
                        if isinstance(v, list)
                    }
                    prior_svid = prior.get("adopted_from_svid")
                    prior_list = prior.get("adopted_from")
                    if prior_svid and prior_list:
                        merged_map.setdefault(str(prior_svid),
                                              list(prior_list))
                    if adopted_from_svid:
                        merged_map[str(adopted_from_svid)] = list(
                            adopted_from or [])
                    ctx["adopted_from_map"] = merged_map
        except (OSError, json.JSONDecodeError):
            pass  # advisory merge -- never blocks the write
    try:
        p = _run_context_path(run_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ctx, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        print(f"run-context: could not write {run_dir}: {e}", file=sys.stderr)


def _read_run_context(run_dir) -> dict:
    """P-019: read the run-dir's persisted invocation context ({} if absent
    -- pre-P-019 run-dirs simply have no context to inherit)."""
    try:
        p = _run_context_path(run_dir)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _persist_run_context_backend(run_dir, backend_record: dict) -> None:
    """R10-3: update ONLY the runtime_backend field of run_context.json,
    preserving every other field (project_root / auto_wiki / wiki_dir /
    adoption declarations) byte-for-value.

    _write_run_context cannot do this: it rebuilds the whole context from
    its parameters, so a backend-only update would clobber the flat
    adopted_from pair to [] and adopted_from_svid to None. A backend switch
    at resume time (R9-2) must be DURABLE -- the ledger event records THAT
    the switch happened; the run-context record is what the NEXT no-arg
    resume actually reads. Advisory: never raises."""
    try:
        p = _run_context_path(run_dir)
        ctx = {}
        if p.is_file():
            ctx = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(ctx, dict):
                ctx = {}
        ctx["runtime_backend"] = dict(backend_record)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ctx, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except (OSError, json.JSONDecodeError) as e:
        print(f"run-context: could not persist backend switch: {e}",
              file=sys.stderr)


def _query_harness(run_dir):
    """Harness for READ-ONLY projections (verdict/state/checkpoint/debug).

    R10-1: the evidence freshness probe hashes evidence_scope files under
    project_root -- defaulting it to cwd makes a query run from the WRONG
    directory see a different tree (or an empty signature) and report the
    evidence stale/fresh incorrectly. Anchor the probe to the project_root
    recorded in run_context.json (P-019); fall back to cwd only for
    pre-P-019 run-dirs with no record."""
    ctx = _read_run_context(run_dir)
    return Harness(run_dir, project_root=ctx.get("project_root"))


def _ctx_declared_for(ctx: dict, svid) -> list:
    """R6-3: the adoption declaration persisted for THIS spec version.

    Reads adopted_from_map first (multi-svid store); falls back to the flat
    adopted_from pair for pre-R6-3 contexts. Returns [] when the context
    holds no declaration for `svid` (child versions must not inherit the
    parent version's declaration -- R5-5)."""
    if not svid:
        return []
    amap = ctx.get("adopted_from_map")
    if isinstance(amap, dict):
        entries = amap.get(str(svid))
        if isinstance(entries, list):
            return [e for e in entries if e]
    if ctx.get("adopted_from_svid") == svid:
        return [e for e in (ctx.get("adopted_from") or []) if e]
    return []


def _ctx_foreign_svid(ctx: dict, svid) -> bool:
    """R6-3: True when the context holds a declaration for a DIFFERENT svid
    but none for `svid` -- the only case an undeclared invocation is
    rejected (silent-inheritance guard, R5-5). An explicit same-version
    declaration is always accepted (R6-3: a child may adopt a different
    contract; the declaration is per-version state, not a
    parent-reaffirmation duty).

    R7-2: key PRESENCE in adopted_from_map is the record, not list
    non-emptiness. ``{svid: []}`` means "this version explicitly declared
    no adoptions" -- a recorded fact, not a missing declaration -- so it
    must NOT trigger the silent-inheritance rejection (review7 P2: a
    --fresh run of v2 left {v1: [A], v2: []}; the next plain v2 run was
    rejected as if v2 had never declared anything)."""
    if _ctx_declared_for(ctx, svid):
        return False
    amap = ctx.get("adopted_from_map")
    if isinstance(amap, dict):
        if str(svid) in amap:
            return False  # explicit record for THIS version (possibly empty)
        return any(amap.values())
    return bool(ctx.get("adopted_from_svid") and ctx.get("adopted_from"))


def _maybe_sediment(spec, ledger, run_dir, wiki_dir, auto_wiki):
    """P1-1: shared run-end sediment. Both `run` and `resume`/`continue --resume`
    must distill the finished run's experience into the wiki, or a BLOCKED→resume→
    VERIFIED sequence leaves the wiki stuck at the old BLOCKED verdict (knowledge
    goes stale). Append-only (a resumed run appends a REVISED entry; the old
    entry stays). Non-fatal: a wiki write failure is logged, not on the run path.

    P-014 co-evolution loop: when the spec declares adopted_from (spec field or
    --adopted-from), seal-time sediment also attributes the outcome BACK to
    each adopted entry as an adoption_outcome impact amendment -- this is
    what lets `plan --wiki-suggest` / `wiki search` answer "this contract was
    adopted N times and the runs went VERIFIED/PARTIAL". The adoption
    JUDGMENT stays with the host (which entry fits the new intent); axiom
    only provides the recording surface. Each amendment is non-fatal
    (unknown entry_id -> stderr warning, sediment continues)."""
    if not auto_wiki:
        return
    try:
        entry = extract_entry(spec, ledger, run_dir)
        w = Wiki(wiki_dir)
        sealed = w.append(entry)
        print(f"wiki: extracted {sealed['entry_id']} "
              f"verdict={sealed['verdict']}", file=sys.stderr)
        for adopted_id in (getattr(spec, "adopted_from", None) or []):
            try:
                amend = w.add_impact(
                    adopted_id, kind="adoption_outcome",
                    reason=(f"adopted by run {run_dir} "
                            f"(svid {spec.spec_version_id}): "
                            f"verdict {sealed['verdict']}"))
                print(f"wiki: adoption_outcome -> {amend['amends'][:14]} "
                      f"verdict={sealed['verdict']}", file=sys.stderr)
            except KeyError as e:
                print(f"wiki: adoption attribution skipped: {e}",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- wiki is advisory, not on the run path
        print(f"wiki: extract failed (non-fatal): {e}", file=sys.stderr)


def _run_resume(spec, run_dir, project_root=None, auto_wiki=False,
                wiki_dir=None, _lock_fd=None) -> "tuple[dict, int]":
    """Shared resume body used by `axiom resume` and `axiom continue --resume`:
    nesting check + run lock + replay cache + re-walk + seal + materialize.
    Returns (checkpoint, exit_code); ({}, 1) on a nesting reject, ({}, 2) if
    the run-dir is locked by an in-flight process. The caller validates the
    spec and writes the active pointer around this call.

    P1-5 fix: (a) project_root is restored from the active pointer (or
    --project-root) and passed to Harness -- without it, Harness falls back
    to cwd, so a `continue --resume` from project B re-dispatches workers
    with cwd=B even though the run was started from project A. (b) the run
    lock is held for the whole re-walk -- without it, a resume into a
    run-dir still held by an in-flight `axiom run` appends to the hash
    chain concurrently and corrupts it.

    R6-2: callers that already hold the run lock (cmd_resume acquires it
    before merging/persisting the run context) pass _lock_fd so the
    re-entrant acquisition returns the same fd instead of reporting a
    self-deadlock as LOCKED. When _lock_fd is None the lock is acquired
    here, unchanged from the pre-R6-2 behavior."""
    try:
        _check_run_dir_nesting(run_dir)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return {}, 1
    if _lock_fd is None:
        _lock_fd = _acquire_run_lock(run_dir)
        if _lock_fd is None:
            print(f"LOCKED: run-dir {run_dir!r} is held by another axiom process. "
                  f"Wait for it to finish or use a different --run-dir.",
                  file=sys.stderr)
            return {}, 2
    h = Harness(run_dir, project_root=project_root)
    h.build_replay_cache(spec)
    try:
        h._run_sequence(spec)
    except GateHalt:
        # gate-halted: seal the partial journal + materialize so BLOCKED + the
        # unresolved gate surface in the checkpoint (matches cmd_run).
        pass
    h.ledger.seal()
    cp = h.materialize(spec)
    # P1-1: resume must sediment the (possibly changed) verdict too, or a
    # BLOCKED→resume→VERIFIED sequence leaves the wiki at the old BLOCKED
    # verdict (the resume_leaves_blocked_experience probe).
    if auto_wiki:
        _maybe_sediment(spec, h.ledger, run_dir, wiki_dir, True)
    return cp, _exit_for_verdict(cp["verdict"])


def _continue_guidance(cp: dict, sealed: bool) -> str:
    """One-line human-readable next-step for `axiom continue`."""
    v = cp.get("verdict", "?")
    if sealed:
        return (f"last run sealed (verdict={v}); nothing to re-dispatch -- "
                f"start a new run with `axiom run <spec>`.")
    return {
        "VERIFIED": "VERIFIED -- ready to deliver.",
        "BLOCKED": "BLOCKED -- a gate is open. `axiom gate list`, resolve, then re-run.",
        "PARTIAL": "PARTIAL -- `axiom debug` for the failure envelope, then replan spec.v{n}.",
        "UNVERIFIED": "UNVERIFIED -- gather evidence; `axiom journal` for events.",
    }.get(v, f"verdict={v} -- see checkpoint.")


def _continue_handoff(spec, cp: dict, env: dict, sealed: bool) -> str:
    """Human-readable resume digest for `axiom continue` (no-arg, after /clear).

    Compresses spec.intent + checkpoint verdict/cost + the derive_debug_envelope
    failure-forensic (why each node failed, its cognitive signature,
    deterministic recovery_action, unmet requirements) into a few lines the
    orchestrator reads off stderr and resumes from -- instead of re-reading the
    spec's full text or running a separate `axiom debug` to rebuild a mental model (the
    minutes-long thinking the bare one-line checkpoint forced).

    Scope contract (honest): this does NOT replace reading the failing node's
    spec slice for its prompt/binding detail. It (a) locates WHICH node slice to
    read, (b) gives the deterministic recovery_action as a STARTING point. The
    orchestrator still judges the real next step from the signature -- a
    re-dispatch is wrong when the signature shows a structural (not stochastic)
    failure (e.g. cc worker's web tools structurally unavailable).
    """
    intent = getattr(spec, "intent", "") or "(no intent)"
    svid = getattr(spec, "spec_version_id", "?")
    verdict = cp.get("verdict", "?")
    cost = cp.get("cost_usd_total")
    disp = cp.get("dispatch_count")
    cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
    disp_str = f", {disp} dispatch" if disp is not None else ""
    lines = [
        "=== HANDOFF SUMMARY ===",
        f"Doing: {intent}",
        f"Last result: {verdict} ({svid}), cost {cost_str}{disp_str}",
    ]
    failures = env.get("failures") or []
    if not failures:
        if sealed:
            lines.append("Status: sealed, cannot re-dispatch -- start a new run with `axiom run <spec>`.")
        elif verdict == "VERIFIED":
            out = cp.get("synthesized_output")
            lines.append("Status: VERIFIED achieved, deliverable." + (
                " Output is in synthesized_output." if out else ""))
        else:
            lines.append(f"Status: {verdict} but no failed-node projection -- "
                        f"see checkpoint JSON, `axiom journal` for the event stream.")
        return "\n".join(lines)
    for f in failures:
        nid = f.get("node_id") or "<run-level>"
        fc = f.get("failure_class", "?")
        rec = f.get("recovery_action", "?")
        lines.append(f"Stuck at: [{fc}] node={nid}  recovery={rec}")
        cs = f.get("cognitive_signature")
        if cs and isinstance(cs, dict):
            val = cs.get("value")
            sig = "; ".join(str(x) for x in val) if isinstance(val, (list, tuple)) else str(val)
            lines.append(f"  signature: {cs.get('kind', '?')}={sig}")
        lp = f.get("last_preview")
        if lp:
            lp = str(lp)
            lines.append(f"  last output: {lp[:200]}{'…' if len(lp) > 200 else ''}")
        if f.get("gate_reason"):
            lines.append(f"  gate reason: {f['gate_reason']}")
        if f.get("replan_reason"):
            lines.append(f"  replan reason: {f['replan_reason']}")
    unmet = env.get("unmet_requirements") or []
    if unmet:
        parts = [f"{u.get('requirement_id')} [{u.get('status')}] "
                 f"binding={u.get('binding')}" for u in unmet]
        lines.append(f"Unmet: {'; '.join(parts)}")
    rec0 = failures[0].get("recovery_action") if failures else None
    hint = {
        "re-dispatch": "Re-dispatch per recovery (but read the signature first to judge: worth re-dispatch vs change route).",
        "replan": "replan spec.v{n} (edit spec, re-run).",
        "gate-resolve": "First `axiom gate list` to resolve the gate, then re-run.",
        "raise-cap-or-replan": "Raise budget or replan.",
    }.get(rec0, f"Handle per {rec0}.")
    lines.append(f"Next step (mechanical): {hint}")
    lines.append("* Main session: judge the real next step from the signature; read only the failed node's spec slice to resume -- do not read the full spec or skim the journal.")
    return "\n".join(lines)


def cmd_validate(args):
    try:
        spec = _load_spec(args.spec)
    except Exception as e:  # malformed JSON / missing fields
        print(f"INVALID: could not parse spec: {e}", file=sys.stderr)
        return 1
    errs = validate_spec(spec)
    if errs:
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        print(f"INVALID: {len(errs)} error(s)", file=sys.stderr)
        return 1
    warns = lint_spec(spec)
    for w in warns:
        print(f"  ! {w}", file=sys.stderr)
    extra = f"  ({len(warns)} warning(s))" if warns else ""
    print(f"VALID  contract_hash={contract_hash(spec)}  nodes={len(spec.nodes)} "
          f"requirements={len(spec.requirements)}{extra}")
    return 0


def cmd_plan(args):
    template = {
        "spec_version_id": "spec.v1", "parent_spec_id": None, "revision": 1,
        "intent": args.intent or "<one-sentence goal>",
        "requirements": [
            {"id": "R1", "text": "<obligation that must hold>", "criticality": "required"}
        ],
        "boundaries": ["<fence: do not cross>"],
        # agent-verdict path: bind R to the verifier via node: (see SKILL.md
        # Verdict model pairings). The verifier owns the verdict; edit agents
        # just do the work (no verdict_field).
        "success_evidence": ["S1:R1=node:n_verify"],
        "contract_drift": False, "contract_hash_value": "",
        # P-014 co-evolution loop: when `plan --wiki-suggest` above returned an
        # experience / format_contract worth copying, declare the adoption
        # here (or via `run --adopted-from <entry_id>`). At seal, each adopted
        # entry gets an adoption_outcome impact with this run's verdict, so
        # retrieval later answers "this contract was adopted N times, runs
        # went VERIFIED/PARTIAL". The adoption JUDGMENT is yours (the host's);
        # axiom only records + attributes it.
        "adopted_from": [],
        # P2-11/R9-3: the default template opts out of the LEGACY
        # change-assurance sub-skill (adjudicate.py + hand-written
        # receipt.json). Since R9-3 the runtime generates machine evidence
        # itself: give a script node `evidence_scope` and bind the reviewer
        # with `evidence_from` -- no receipt, no extra sub-skill. Keep the
        # opt_out only as the ir.py escape hatch (a non-empty reason), NOT a
        # silent bypass.
        "assurance_opt_out": "default template: machine evidence via script evidence_scope + reviewer evidence_from (R9-3); the legacy change-assurance sub-skill + hand-written receipt.json flow is not configured by default. Remove this opt_out when the spec declares evidence nodes.",
        "nodes": {
            "n1_edit": {
                "type": "agent", "id": "n1_edit",
                "prompt": "<edit prompt: do the work>",
                "dispatch": "host",
                "output_schema": {"type": "object",
                    "properties": {"files_modified": {"type": "array", "items": {"type": "string"}},
                                   "summary": {"type": "string"}},
                    "required": ["files_modified"]},
                "allowed_tools": ["Read", "Edit", "Write", "Glob"],
                "write_areas": ["src/**"], "acceptance": ["<file modified>"],
                "failure_policy": {"max_retries": 2, "retry_guard": "requires_new_evidence", "on_exhausted": "block"},
                # F3 on_stagnation (inside failure_policy, cause-level override):
                # when stagnating (the same failure signature recurs) it applies
                # instead of on_exhausted. On an impl->verify chain, consider
                # adding "on_stagnation": "degrade" -- a stagnating gate auto-passes
                # downstream for verify adjudication (relay runs 1-7 showed manual
                # resolve of a stagnating gate is almost always allow; paired with
                # fix_loop, a FAILED verify auto-routes back to impl), while
                # retries_exhausted still goes through on_exhausted for a human gate.
                # Not declaring it = stagnation also goes through on_exhausted
                # (block = every stagnation opens a human gate). Add it in failure_policy:
                # "failure_policy": {..., "on_exhausted": "block",
                #                    "on_stagnation": "degrade"},
            },
            "n_verify": {
                "type": "agent", "id": "n_verify", "verdict_field": "verdict",
                # F9/F10/F12 verify credibility three-layer spec (run 7 lesson:
                # n_verify's 8 steps all passed but the callback state-machine
                # bypass bug was not caught -- a fully-passing happy path does NOT
                # mean the implementation is friction-free). When writing n_verify's
                # prompt/acceptance, all three layers must be explicitly listed;
                # a missing layer = a blind spot at the spec layer, not at the
                # verify-capability layer:
                #   1) happy path: actually run the normal flow end-to-end (not
                #      infer from reading code).
                #   2) negative paths: did it reject what it should reject -- a
                #      wrong-state call to an endpoint should 400, an unauthorized
                #      call should 401/403, invalid input should 4xx without
                #      crashing, a duplicate submit should be idempotent.
                #   3) real-run real-check: DB state must be checked by really
                #      running init_db+PRAGMA/SELECT (do not trust the static claim
                #      "this field should exist"); the service must be really
                #      started and really curl'd; assertions must be based on real
                #      execution output, not on "the code looks right".
                # If a real run is needed, change allowed_tools to ["Read","Bash"]
                # (only Read cannot do layers 1 and 3).
                "prompt": "<read the changed files, check each requirement is met; verify in three layers: (1) happy path — actually run the normal flow end-to-end; (2) negative paths — wrong-state/wrong-permission/invalid-input calls must be rejected properly; (3) real checks — DB via real init_db+PRAGMA, service via real start+curl; assertions from real execution output, never from static claims; return {verdict, issues}>",
                "dispatch": "host",
                "output_schema": {"type": "object",
                    "properties": {"verdict": {"type": "string"}, "issues": {"type": "array", "items": {"type": "string"}}},
                    "required": ["verdict"]},
                "allowed_tools": ["Read", "Glob"], "write_areas": [],
                "acceptance": ["verdict field present"],
                # P2-11 fix: assurance_hook is OMITTED on this verify-only
                # node (write_areas:[]) per the SKILL.md verify-only rule.
                # The hook machine-gates the verdict via change-assurance's
                # adjudicate.py against a receipt.json the worker wrote into a
                # git worktree -- but (a) a verify-only node has no
                # write_areas to trace, so receipt resolves to no_worktree /
                # receipt_missing -> UNVERIFIED (discarding the agent's
                # VERIFIED), and (b) the default template cannot assume the
                # change-assurance sub-skill + adjudicate.py is installed
                # (adjudicate_script_missing -> UNVERIFIED). Together these
                # made the default template unable to self-close (agent said
                # VERIFIED, machine overrode to UNVERIFIED). The verifier's
                # {verdict} is trusted directly here (no reliability lost for
                # a read-only verify: receipt is code-change provenance, and
                # there is no code to trace in a read-only verify).
                # To machine-gate a real code-change spec: install the
                # change-assurance sub-skill (so adjudicate.py resolves),
                # have n1_edit write receipt.json (sha256 of files matching its
                # write_areas), then add
                #   "assurance_hook": {"receipt_path": "receipt.json"}
                # here so adjudicate.py validates the receipt before the
                # verdict is recorded.
                # fix_loop (P-008): if this verify returns verdict:FAILED and
                # the failure is something n1_edit can repair, uncomment the
                # line below to auto re-dispatch n1_edit (with the verify's
                # issues injected via {{_fix_feedback.issues}}) and re-verify,
                # up to max_rounds times. Opt-in — do NOT add to one-shot /
                # external-state verifies where impl can't act on the issues.
                # REQUIRED for fix_loop to actually repair: n1_edit's prompt
                # must reference {{_fix_feedback.issues}} explicitly — harness
                # injects _fix_feedback into the render context, but if the
                # prompt never renders it, the re-dispatched prompt is identical
                # to round 1 and the impl repeats the same buggy output
                # (fixloop_probe 2026-09-07: impl wrote a-b 3 rounds straight).
                # "fix_loop": {"impl": "n1_edit", "max_rounds": 2},
                "failure_policy": {"max_retries": 2, "retry_guard": "requires_new_evidence", "on_exhausted": "degrade"},
            },
            # ── v2 control-flow: uncomment the nodes below for common patterns ──
            # CONDITION: branch on verify result
            # "n_branch": {
            #     "type": "condition", "id": "n_branch",
            #     "predicate": "claim_status('C1') == 'VERIFIED'",
            #     "then_branch": ["n1_edit"], "else_branch": [],
            #     "gate": True,   # binding branch — require verified evidence
            # },
            # REPEAT: implement → verify loop until convergence
            # "loop_fix": {
            #     "type": "repeat", "id": "loop_fix",
            #     "body": ["n1_edit", "n_verify"],
            #     "max_iterations": 3, "until": "dry<2",
            #     "failure_policy": {"on_exhausted": "block"},
            # },
            # UNTIL: loop with arbitrary predicate
            # "loop_until": {
            #     "type": "until", "id": "loop_until",
            #     "body": ["n1_edit", "n_verify"],
            #     "cond": "claim_status('C1') == 'VERIFIED'",
            #     "max_iterations": 3, "budget_aware": True,
            #     "failure_policy": {"on_exhausted": "degrade"},
            # },
            # GATE: human escalation on critical findings
            # "n_gate": {
            #     "type": "gate", "id": "n_gate",
            #     "trigger": "claim_status('C1') == 'REFUTED'",
            #     "on_trigger": "pause",
            #     "escalation_tiers": [{"model": "claude-opus-4-20250514", "skeptic_count": 3}],
            #     "body": ["n1_edit"],
            # },
        },
        "control_flow": {"type": "sequence", "steps": ["n1_edit", "n_verify"]},
        # For a repeat loop, change steps to: ["loop_fix"]
        # For a condition branch, change steps to: ["n_branch"]
        "decision_trace": [],
        "budget_usd": 20.0, "max_concurrent": 16, "max_agents": 1000,
        "max_stagnation": 2,  # default 2 (not 1): lets G3 schema-feedback inject
                              # before a 2nd identical failure trips stagnation;
                              # a prose-stubborn worker model needs the 3rd attempt
    }
    text = json.dumps(template, indent=2, ensure_ascii=False)
    # wiki-suggest: retrieve similar-intent experience BEFORE emitting the
    # template, so the author (agent) has prior shape/verdict/impact in hand
    # while filling it in. Format arrives before output, not after.
    if getattr(args, "wiki_suggest", False) and args.intent:
        w = Wiki(_resolve_wiki_dir(args.wiki_dir))
        results = w.search(query=args.intent, limit=6)
        # domain_contract is skill-level adjudication knowledge (cross-project);
        # merge skill root's domain_contracts.jsonl into results so plan in any
        # project can recall surface/risk_floor/verdict_rule, not just the
        # current project's run experience.
        try:
            dc_results = _domain_contracts_wiki().search(query=args.intent, limit=6)
            results = results + dc_results
        except Exception:
            pass
        contracts = [r for r in results if r.get("entry_type") == "format_contract"]
        domain_contracts = [r for r in results if r.get("entry_type") == "domain_contract"]
        experiences = [r for r in results
                       if r.get("entry_type") not in ("format_contract",
                                                       "domain_contract")]
        if experiences:
            print("# wiki experience (similar intent prior runs):", file=sys.stderr)
            for r in experiences:
                print(f"#   [{r['entry_id'][:14]}] verdict={r['verdict']} "
                      f"shape={r['spec_shape']}", file=sys.stderr)
                if r.get("learned"):
                    print(f"#     learned: {r['learned']}", file=sys.stderr)
                for imp in (r.get("impact") or []):
                    print(f"#     impact: [{imp['kind']}] {imp['reason']}",
                          file=sys.stderr)
        if contracts:
            print("# wiki format-contract (proven structure to copy):", file=sys.stderr)
            for r in contracts:
                # P1-3: surface impact (format_drift / replan_denied /
                # verify_failed) on contracts too, not just experiences. A
                # contract with a format_drift amendment is NOT safe to copy
                # as-is; the warning lived in search() but was dropped here,
                # so the author copied a known-broken structure.
                impacts = r.get("impact") or []
                defective = any(i.get("kind") in ("format_drift", "replan_denied",
                                                   "verify_failed")
                                for i in impacts)
                flag = "  [KNOWN DEFECT - address before copying:]" if defective else ""
                print(f"#   [{r['entry_id'][:14]}] "
                      f"shape={r.get('control_flow_shape')}{flag}",
                      file=sys.stderr)
                for ns in (r.get("node_skeleton") or []):
                    req = ns.get("output_schema_required") or []
                    print(f"#     node {ns.get('id')}: type={ns.get('type')} "
                          f"required={req}", file=sys.stderr)
                if r.get("binding_pattern"):
                    print(f"#     binding: {r['binding_pattern']}", file=sys.stderr)
                for imp in impacts:
                    print(f"#     impact: [{imp['kind']}] {imp['reason']}",
                          file=sys.stderr)
            print("# (copy the proven structure into the template below)",
                  file=sys.stderr)
        if domain_contracts:
            print("# wiki domain-contract (assurance verdict rule for this "
                  "intent class):", file=sys.stderr)
            for r in domain_contracts:
                print(f"#   [{r.get('contract_id') or r['entry_id'][:14]}] "
                      f"domain={r.get('domain')} "
                      f"surfaces={r.get('surfaces')} "
                      f"risk_floor={r.get('risk_floor')}", file=sys.stderr)
                for surf, roles in (r.get("required_roles") or {}).items():
                    print(f"#     surface {surf}: required_roles={roles}",
                          file=sys.stderr)
                if r.get("auth_gate_required"):
                    print(f"#     auth_gate: REQUIRED — {r.get('auth_gate_rule')}",
                          file=sys.stderr)
                if r.get("binding_pattern"):
                    print(f"#     binding: {r['binding_pattern']}", file=sys.stderr)
                if r.get("verdict_rule"):
                    print(f"#     verdict_rule: {r['verdict_rule']}", file=sys.stderr)
                if r.get("learned"):
                    print(f"#     learned: {r['learned']}", file=sys.stderr)
                for imp in (r.get("impact") or []):
                    print(f"#     impact: [{imp['kind']}] {imp['reason']}",
                          file=sys.stderr)
        if not experiences and not contracts and not domain_contracts:
            print("# wiki: no prior experience, contract, or domain-contract "
                  "for this intent", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote spec template to {args.out}")
    else:
        print(text)
    return 0


# R9-2 (review9 P1): backend registry. The three choices are separate axes:
#   WHO executes (backend: pi / cc), WHERE (location: local today; cloud
#   reserved), and WHAT IT CAN DO (capabilities: cost reporting, tools,
#   cancellation). pi/cc are host-adapter subprocesses that speak the file
#   protocol (AXIOM_RUNTIME=host under the hood) via scripts/host_adapter.py.
#   A direct-subprocess runner path exists for tests / custom wrappers
#   (dispatch.run_via_runner) but is not a production backend.
_BACKENDS = {
    "pi": {
        "binary": "pi",
        "capabilities": ["cost", "tools", "cancel"],
        "host": "pi",   # host_adapter.py --pi
    },
    "cc": {
        "binary": "claude",
        "capabilities": ["tools"],  # no cost channel under cc-switch
        "host": "cc",    # host_adapter.py --cc
    },
}


def _resolve_backend(requested):
    """Resolve + capability-check the execution backend. Returns the
    backend record dict (with 'name'), or exits the run with a clear
    error when the backend's binary is not on PATH -- a backend that
    cannot execute must fail BEFORE the ledger opens, not after a
    timeout swallow. host-style backends (pi/cc) spawn their adapter child
    eagerly, so a missing binary there WOULD be a silent timeout -- checked
    hard here."""
    import shutil
    name = requested or "cc"
    rec = _BACKENDS.get(name)
    if rec is None:
        raise ValueError(
            f"unknown runtime backend {name!r}; "
            f"choose from {sorted(_BACKENDS)}")
    binary = rec.get("binary")
    if binary and rec.get("host") and shutil.which(binary) is None:
        raise RuntimeError(
            f"backend {name!r} requires {binary!r} on PATH but it was not "
            f"found. Install it or choose another backend "
            f"({sorted(_BACKENDS)}). The run was NOT started.")
    return {"name": name, **rec}


def _resolve_runtime(args_runtime):
    """Determine execution runtime: pi / cc.

    Priority: --runtime flag > AXIOM_RUNTIME env > interactive prompt > default.

    pi — pi backend (host_adapter --pi, local file protocol; reports cost)
    cc — Claude Code backend (host_adapter --cc + cc-switch config; no cost channel)
    """
    # 1. Explicit flag
    if args_runtime is not None:
        return args_runtime
    # 2. Env var (accepts the backend name directly; "host" is the file-protocol
    #    runtime string set by cmd_run, not a backend choice — ignored here)
    env_rt = os.environ.get("AXIOM_RUNTIME")
    if env_rt in ("pi", "cc"):
        return env_rt
    # 3. Interactive prompt — always ask, even over SSH (read /dev/tty)
    try:
        tty = open("/dev/tty", "r")
        print("Select execution backend:", file=sys.stderr)
        print("  1) pi — pi backend (host_adapter, local file protocol)", file=sys.stderr)
        print("  2) cc — Claude Code (host_adapter + cc-switch config)", file=sys.stderr)
        print("Enter [1/2, default 2]: ", end="", file=sys.stderr, flush=True)
        choice = tty.readline().strip()
        tty.close()
    except (OSError, IOError):
        # /dev/tty not available (e.g. non-interactive ssh/cron/automation).
        # Default to cc — this is load-bearing: resume/continue/dispatch
        # flows rely on a runtime being resolved — but warn loudly so the
        # default is not hidden. Set AXIOM_RUNTIME to silence.
        print("WARNING: no /dev/tty — defaulting backend to cc. "
              "Set AXIOM_RUNTIME=pi|cc to choose explicitly "
              "and silence this warning.",
              file=sys.stderr)
        return "cc"
    mapping = {"1": "pi", "2": "cc", "pi": "pi", "cc": "cc"}
    return mapping.get(choice, "cc")


def _spawn_host(backend_name, run_dir, axiom_pid, enable_tools=False):
    """R9-2: spawn the backend's host side as a child process.

    pi/cc spawn host_adapter.py with the preset (the host agent over the
    file protocol). The child's stderr is inherited so its startup
    diagnostics (backend missing, cc-switch not found) reach the operator
    verbatim.

    enable_tools: any spec node with a non-empty allowed_tools needs the
    host's tools on (e.g. pi --tools) or a code-editing dispatch silently
    cannot write files -- the worker then REPORTS success it never did
    (review9 acceptance: pi claimed csv_tool.py modified, it wasn't).
    """
    import subprocess as _sp
    if backend_name in ("pi", "cc"):
        adapter = Path(__file__).resolve().parent.parent / "scripts" / \
            "host_adapter.py"
        cmd = [sys.executable, str(adapter), f"--{backend_name}",
               "--run-dir", str(run_dir), "--axiom-pid", str(axiom_pid)]
        if enable_tools and backend_name == "pi":
            cmd.append("--tools")
        proc = _sp.Popen(cmd)
        print(f"host_adapter[{backend_name}] spawned (PID={proc.pid}"
              f"{', tools on' if enable_tools else ''})", file=sys.stderr)
        return proc
    return None


def _spec_needs_tools(spec) -> bool:
    """True when any agent node declares a non-empty allowed_tools -- the
    host backend must then run with tools enabled (pi --tools), or a
    code-editing dispatch cannot write files and the worker REPORTS work
    it never did."""
    nodes = getattr(spec, "nodes", {}) if spec is not None else {}
    if not isinstance(nodes, dict):
        return False
    for n in nodes.values():
        if isinstance(n, dict) and (n.get("allowed_tools") or []):
            return True
    return False


def _resume_backend(run_dir, spec, requested_runtime=None):
    """R9-2: resolve the backend for a resume and spawn its host side.

    The run's recorded runtime_backend wins (resume keeps the launch config); an
    explicit --runtime that DIFFERS is a backend switch and must leave a
    ledger event (backend_switch) -- never a silent re-resolution from the
    caller's environment. A switch is allowed deliberately; a MISSING
    record (pre-R9-2 run-dir) falls back to the requested/default backend.
    Returns the backend record, or None when the run must not proceed
    (unknown backend binary absent -- the error is printed here).
    """
    ctx = _read_run_context(run_dir)
    recorded = (ctx.get("runtime_backend") or {})
    recorded_name = recorded.get("backend")
    # An explicit --runtime is a deliberate choice and wins (a DIFFERENT one
    # is a ledger-recorded switch); absent that, the recorded backend is
    # inherited; neither -> the cc default.
    name = requested_runtime or recorded_name or "cc"
    # Test/direct-subprocess mode: the run recorded backend="runner" (the
    # AXIOM_RUNTIME=runner bypass in cmd_run). No host adapter to spawn, no
    # backend binary to resolve — just keep the runner path alive on resume.
    if name == "runner":
        os.environ["AXIOM_RUNTIME"] = "runner"
        return {"name": "runner", "host": None, "capabilities": []}
    switched = bool(recorded_name and requested_runtime
                    and requested_runtime != recorded_name)
    # R10-3: a first-time explicit --runtime over a run-dir with NO recorded
    # backend (pre-R9-2) also becomes the run's persisted fact -- otherwise
    # the next no-arg resume re-resolves from the environment again.
    adopting = bool(not recorded_name and requested_runtime)
    try:
        backend = _resolve_backend(name)
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None
    protocol = "host" if backend["host"] else backend["name"]
    os.environ["AXIOM_RUNTIME"] = protocol
    if switched or adopting:
        if switched:
            print(f"NOTE: backend switch {recorded_name!r} -> {name!r} "
                  f"(recorded in ledger + run context)", file=sys.stderr)
            from axiom.ledger import Ledger
            Ledger(run_dir).append({
                "event_id": f"ev_switch_{os.getpid()}",
                "kind": "backend_switch",
                "spec_version_id": getattr(spec, "spec_version_id", None),
                "payload": {"from": recorded_name, "to": name,
                            "capabilities": backend["capabilities"]},
                "claims": [],
            })
        # R10-3: persist the switch so the NEXT no-arg resume inherits the
        # new backend. Previously only the ledger event was written -- the
        # run context kept the old backend and the next resume silently
        # reverted (switch logged but never durable). The ledger event is
        # the audit; the run-context record is the behavior.
        _persist_run_context_backend(run_dir, {
            "backend": backend["name"],
            "protocol": protocol,
            "location": "local",
            "capabilities": backend["capabilities"],
        })
    elif recorded_name:
        print(f"Runtime: {name} (resumed from run record, "
              f"protocol={protocol})", file=sys.stderr)
    _spawn_host(backend["name"], run_dir, os.getpid(),
                enable_tools=_spec_needs_tools(spec))
    return backend


def cmd_run(args):
    # Resolve runtime environment (R9-2: backend = WHO executes; the file
    # protocol underneath pi/cc is the host file protocol).
    # AXIOM_RUNTIME=runner: test/direct-subprocess mode. dispatch routes to an
    # injected runner (tests patch _default_runner; custom wrappers pass a
    # callable). No host adapter spawned, no backend binary required. Production
    # runs use pi/cc (host file protocol via host_adapter.py).
    if os.environ.get("AXIOM_RUNTIME") == "runner":
        runtime = "runner"
        backend = {"name": "runner", "host": None, "capabilities": []}
        protocol = "runner"
    else:
        runtime = _resolve_runtime(getattr(args, "runtime", None))
        try:
            backend = _resolve_backend(runtime)
        except (ValueError, RuntimeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        # pi/cc speak the host file protocol through host_adapter; AXIOM_RUNTIME
        # carries the PROTOCOL for dispatch.dispatch, while run_context records
        # the BACKEND (pi/cc) for resume.
        protocol = "host" if backend["host"] else runtime
        os.environ["AXIOM_RUNTIME"] = protocol  # propagate to harness / dispatch
    os.environ["AXIOM_RUN_DIR"] = str(Path(args.run_dir).resolve())
    print(f"Runtime: {runtime} (protocol={protocol}, "
          f"capabilities={backend['capabilities']})", file=sys.stderr)
    try:
        spec = _load_spec(args.spec)
    except Exception as e:
        print(f"ERROR: could not parse spec: {e}", file=sys.stderr)
        return 1
    # P-014: fold the run-scoped adoption declaration into the in-memory spec
    # (spec file stays immutable) so seal-time sediment can attribute outcome.
    # R5-5: and REJECT the run if the declaration disagrees with the
    # run-dir's persisted context -- a run re-using a run-dir whose context
    # was written by another spec version must not silently inherit that
    # version's adoption relationship at a later resume.
    _merge_adopted_from(spec, getattr(args, "adopted_from", None))
    try:
        _check_run_dir_nesting(args.run_dir)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    # R6-2: the run lock is taken BEFORE reading/validating/writing the
    # run-dir's persisted context -- a rejected invocation (lock contest or
    # the declaration guard below) must leave run_context.json byte-identical
    # (review6: a LOCKED resume used to overwrite wiki_dir + adopted_from).
    _lock_fd = _acquire_run_lock(args.run_dir)
    if _lock_fd is None:
        print(
            f"ERROR: run-dir {args.run_dir!r} is locked by another axiom "
            f"process. Wait for it, or use a different --run-dir.",
            file=sys.stderr,
        )
        return 1
    # R6-3: reject only the SILENT-INHERITANCE shape -- the context holds a
    # declaration for a DIFFERENT svid and this invocation declares nothing
    # new (spec-file entries are adopted as-is, never gated). An explicit
    # declaration for THIS svid is accepted even when it differs from the
    # persisted one: adoption declarations are per-version state (a child
    # version may adopt a different contract; resume always allowed this).
    _prior_ctx = _read_run_context(args.run_dir)
    _declared = (list(getattr(spec, "adopted_pending", None) or [])
                 or list(getattr(spec, "adopted_from", None) or []))
    if (not getattr(args, "fresh", False)
            and not _declared
            and _ctx_foreign_svid(_prior_ctx, spec.spec_version_id)):
        print(
            f"INVALID: run-dir {args.run_dir!r} has adoption declarations "
            f"from {_prior_ctx.get('adopted_from_svid')!r}; this spec is "
            f"{spec.spec_version_id!r} and declares none of its own. "
            f"Use --fresh, a different --run-dir, or declare "
            f"--adopted-from explicitly.",
            file=sys.stderr,
        )
        return 1
    h = Harness(args.run_dir, project_root=args.project_root)
    # F8: progress is in events.jsonl, not stdout -- worker dispatch output does
    # not enter this terminal; staring at the screen will waste your time. When
    # starting a run, tell the observer where to look.
    print(f"Watch progress: tail -f {Path(args.run_dir) / 'events.jsonl'} "
          f"(events.jsonl lags process start by a few seconds — its absence "
          f"right after launch is NOT 'run never started')", file=sys.stderr)
    if getattr(args, "fresh", False):
        h.ledger.reset()
    else:
        # Surface the silent-append hazard. Verdict projection is now scoped by
        # spec_version_id (a v2 re-run won't inherit v1's VERIFIED), but
        # cost_usd_total / dispatch_count in the checkpoint still scan ALL
        # events in the dir, and a re-run of the SAME svid appends duplicate
        # events. Warn (don't block) so the orchestrator picks --fresh or
        # `axiom resume` deliberately.
        existing = h.ledger.events()
        if existing:
            contaminating = _cross_task_contaminating(
                existing, spec.spec_version_id, spec.parent_spec_id)
            if contaminating:
                print(
                    f"INVALID: run-dir {args.run_dir!r} has events from a different "
                    f"task (spec_version_id={sorted(contaminating)}); current spec is "
                    f"{spec.spec_version_id!r} (parent={spec.parent_spec_id!r}). "
                    f"Appending would cross-contaminate the hash chain / journal. "
                    f"Use --fresh to start clean, or --run-dir to a different dir.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"NOTE: run-dir {args.run_dir} has {len(existing)} existing event(s); "
                f"appending. cost/dispatch totals will accumulate across runs; "
                f"use --fresh to start clean, or `axiom resume` to replay "
                f"unchanged nodes.",
                file=sys.stderr,
            )
    # R9-2: spawn the backend's host side (pi/cc -> host_adapter preset,
    # pi/cc -> host_adapter (host side). pi/cc were previously a
    # SEPARATE manual path (start host_adapter yourself in a second
    # terminal) -- the unified entrypoint spawns them here so `--runtime pi`
    # is one command.
    ds_proc = _spawn_host(backend["name"], args.run_dir, os.getpid(),
                          enable_tools=_spec_needs_tools(spec))
    _active_started = _write_active(
        args.spec, args.run_dir, spec.spec_version_id,
        project_root=args.project_root, sealed=False)
    # P-019: persist invocation context into the run-dir so a later EXPLICIT
    # resume/continue inherits this run's project_root + auto-wiki intent
    # instead of falling back to the global last-active pointer (which may
    # belong to a different project's run).
    # R7-2: --fresh resets the ledger AND the run context -- a fresh run
    # starts a new adoption lineage, so the prior context's per-svid
    # declarations must NOT be merged in (review7 P2: fresh v2 inherited
    # v1's adopted_from_map, and the next plain v2 run was rejected as a
    # silent-inheritor of the version the fresh run had wiped). Resume
    # paths below keep merge_prior=True: resume preserves other svids'
    # declarations (R6-2/R6-3).
    _write_run_context(args.run_dir, project_root=args.project_root,
                       auto_wiki=getattr(args, "auto_wiki", False),
                       wiki_dir=_resolve_wiki_dir(getattr(args, "wiki_dir", None),
                                                  args.project_root),
                       adopted_from=list(getattr(spec, "adopted_from", None) or []),
                       adopted_from_svid=spec.spec_version_id,
                       merge_prior=not getattr(args, "fresh", False),
                       runtime_backend={
                           "backend": backend["name"],
                           "protocol": protocol,
                           "location": "local",
                           "capabilities": backend["capabilities"],
                       })
    try:
        out = h.run(spec)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    finally:
        # Clean up host_adapter child
        if ds_proc is not None:
            ds_proc.terminate()
            try:
                ds_proc.wait(timeout=5)
            except _sp.TimeoutExpired:
                ds_proc.kill()
            ds_out, ds_err = ds_proc.communicate()
            if ds_err:
                for line in ds_err.decode(errors="replace").strip().splitlines():
                    print(f"host_adapter: {line}", file=sys.stderr)
    h.ledger.seal()
    cp = h.materialize(spec)  # B4: verdict.json / checkpoints.jsonl / packet.md
    _write_active(
        args.spec, args.run_dir, spec.spec_version_id,
        project_root=args.project_root, sealed=True,
        verdict=cp["verdict"], started_at=_active_started)
    # auto-wiki: distill this run's experience into the wiki right after seal,
    # so the next `plan --wiki-suggest` retrieves it. Append-only, never blocks
    # the run's exit code (a wiki write failure is logged, not fatal).
    # P1-1: shared sediment (was inlined here; resume/continue --resume now
    # reuse the same _maybe_sediment helper so a resumed run also updates the
    # wiki instead of staying stale at the pre-resume verdict).
    if getattr(args, "auto_wiki", False):
        _maybe_sediment(spec, h.ledger, args.run_dir,
                        _resolve_wiki_dir(args.wiki_dir, args.project_root),
                        True)
    print(json.dumps({"output": out, "checkpoint": cp},
                     ensure_ascii=False, indent=2))
    return _exit_for_verdict(cp["verdict"])


def cmd_resume(args):
    try:
        spec = _load_spec(args.spec)
    except Exception as e:
        print(f"ERROR: could not parse spec: {e}", file=sys.stderr)
        return 1
    errs = validate_spec(spec)
    if errs:
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        print(f"INVALID: {len(errs)} error(s)", file=sys.stderr)
        return 1
    # P-014: adoption declared at resume time also attributes (the resumed
    # verdict may differ from the original run's). Fold the resume-scoped
    # declaration into the in-memory spec (spec file stays immutable).
    # P-030: ...and the run-context declaration persists across resume.
    # R6-2: the context merge moved INSIDE the run lock (below) -- a resume
    # rejected by the lock used to mutate run_context.json anyway (review6:
    # LOCKED + exit 2, but wiki_dir flipped to B and the new declaration
    # persisted). _merge_adopted_from only tags this invocation's
    # declaration as spec.adopted_pending; inheritance happens in-lock.
    _merge_adopted_from(spec, getattr(args, "adopted_from", None))
    # P-019/R6-2: acquire the run lock BEFORE reading the persisted context.
    try:
        _check_run_dir_nesting(args.run_dir)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    _lock_fd = _acquire_run_lock(args.run_dir)
    if _lock_fd is None:
        print(f"LOCKED: run-dir {args.run_dir!r} is held by another axiom process. "
              f"Wait for it to finish or use a different --run-dir.",
              file=sys.stderr)
        return 2
    _rctx = _read_run_context(args.run_dir)
    # R5-5/R6-3: inherit only the declaration persisted for THIS spec
    # version (adopted_from_map first, flat pair as legacy fallback). A
    # child spec resumed into the same run-dir must not silently adopt the
    # parent version's relationships -- child versions declare explicitly.
    _ctx_decl = _ctx_declared_for(_rctx, spec.spec_version_id)
    if _ctx_decl:
        _merge_adopted_from(spec, _ctx_decl)
    elif _rctx.get("adopted_from"):
        print(f"NOTE: run-context adoption declarations belong to "
              f"{_rctx.get('adopted_from_svid')!r}; not inherited by "
              f"{spec.spec_version_id!r} (child versions re-declare "
              f"adoption explicitly).", file=sys.stderr)
    _ptr = _read_active() or {}
    _ptr_pr = (_ptr.get("project_root")
               if _ptr.get("run_dir") and args.run_dir
               and Path(_ptr["run_dir"]).resolve() == Path(args.run_dir).resolve()
               else None)
    pr = (getattr(args, "project_root", None) or _rctx.get("project_root")
          or _ptr_pr)
    if pr is None:
        # P-019 last-resort anchor: the resumed run's own artifacts. An
        # explicit `resume <spec> --run-dir X` names both paths; their common
        # ancestor is the project they belong to (e.g. projA/spec.json +
        # projA/run -> projA). Without this, a resume invoked while the
        # global pointer tracks project B (and no run_context.json exists,
        # e.g. the run pre-dates P-019) falls through to Harness' cwd
        # fallback and dispatches workers into project B (the
        # explicit_resume probe). Only trust anchors deeper than the
        # filesystem root; disjoint paths -> None -> cwd (honest fallback).
        try:
            spec_dir = Path(args.spec).resolve().parent
            run_parent = Path(args.run_dir).resolve().parent
            common = Path(os.path.commonpath([str(spec_dir), str(run_parent)]))
            # only trust an anchor deeper than the filesystem root
            if len(common.parts) > 1:
                pr = str(common)
        except (ValueError, OSError):
            pass
    _active_started = _write_active(
        args.spec, args.run_dir, spec.spec_version_id, sealed=False,
        project_root=pr)
    # _run_resume holds the nesting check, run lock, replay cache, GateHalt
    # catch, seal, and materialize -- shared with `axiom continue --resume` so
    # the two paths cannot drift. GateHalt (a gate node pause / binding-branch
    # on PROPOSED) is sealed as a partial journal + materialized so BLOCKED +
    # the unresolved gate surface in the checkpoint (exit 3), matching cmd_run.
    # F8: same as cmd_run -- progress is in events.jsonl, not stdout.
    print(f"Watch progress: tail -f {Path(args.run_dir) / 'events.jsonl'}",
          file=sys.stderr)
    # P-019: auto-wiki/wiki_dir inherit from the run's persisted context
    # unless explicitly re-specified -- a run started with --auto-wiki keeps
    # sedimenting across resume without the caller repeating the flag.
    _auto_wiki = (bool(getattr(args, "auto_wiki", False))
                  or bool(_rctx.get("auto_wiki")))
    _wiki_dir = (getattr(args, "wiki_dir", None) or _rctx.get("wiki_dir")
                 or _resolve_wiki_dir(None, pr))
    # R5-4/R6-2: persist the merged declaration back so a declaration first
    # made AT resume time survives the NEXT resume. Run-scoped and
    # svid-bound; the original spec file stays immutable. Still done BEFORE
    # _run_resume's re-walk so the context is durable even if the resume
    # gate-halts again -- but now INSIDE the run lock (_lock_fd acquired
    # above), so a lock-rejected resume leaves the context byte-identical
    # (review6 test_lock_rejection_leaves_run_context_unchanged).
    # merge_prior=True keeps OTHER svids' declarations in the store (R6-3).
    _write_run_context(args.run_dir, project_root=pr,
                       auto_wiki=_auto_wiki, wiki_dir=_wiki_dir,
                       adopted_from=list(getattr(spec, "adopted_from", None) or []),
                       adopted_from_svid=spec.spec_version_id,
                       merge_prior=True)
    # R9-2: resume uses the backend RECORDED at run start (or a deliberate,
    # ledger-recorded switch via --runtime) and spawns its host side --
    # previously resume re-resolved from the caller's AXIOM_RUNTIME env and
    # spawned NO host, so a pi/cc run resumed without its adapter timed out
    # at the first uncached dispatch.
    _backend = _resume_backend(args.run_dir, spec,
                               requested_runtime=getattr(args, "runtime", None))
    if _backend is None:
        return 1
    # R9-2 fix: the file-protocol dispatch path (run_cli_dispatch) resolves
    # the request directory from AXIOM_RUN_DIR. cmd_run sets it; resume
    # never did -- a resumed pi/cc/cli run wrote dispatch_req into
    # $CWD/.axiom/run (the CALLER's directory), which the spawned adapter
    # (watching the real run_dir) never saw -> every uncached dispatch hung
    # forever. Anchor it to the run being resumed.
    os.environ["AXIOM_RUN_DIR"] = str(Path(args.run_dir).resolve())
    cp, code = _run_resume(spec, args.run_dir, project_root=pr,
                            auto_wiki=_auto_wiki,
                            wiki_dir=_wiki_dir, _lock_fd=_lock_fd)
    if cp:
        _write_active(args.spec, args.run_dir, spec.spec_version_id,
                      sealed=True, verdict=cp.get("verdict"),
                      started_at=_active_started, project_root=pr)
    print(json.dumps({"checkpoint": cp}, ensure_ascii=False, indent=2))
    return code


def _warn_empty_ledger(lg, run_dir):
    """Advise when --run-dir holds no events.jsonl (missing/empty). cmd_debug
    bails hard on this (forensic tool), but cmd_checkpoint/verdict/state silently
    derive from an empty ledger — printing a garbage UNVERIFIED/empty checkpoint
    that looks like "it ran." Surface the mismatch so a wrong --run-dir (common in
    Path A e2e, where the dispatch dir differs from the ledger dir) is caught
    immediately instead of misread as success.

    2026-09-05: a real relay pointed debug at .axiom/run (dispatch dir) while the
    ledger lived at .axiom/ledger; debug bailed but checkpoint silently printed
    an empty checkpoint, misread as "found the ledger" — wasted a diagnosis round."""
    ep = getattr(lg, "events_path", None)
    if ep is None:
        return
    try:
        if not ep.exists() or ep.stat().st_size == 0:
            print(f"warning: no ledger events at --run-dir {run_dir!r} "
                  f"(events.jsonl missing/empty). F11: a run started seconds "
                  f"ago may not have created events.jsonl yet — check for a "
                  f"live axiom process / active.json before concluding it "
                  f"never ran; poll with a grace window (events.jsonl can "
                  f"lag process start). Otherwise: in Path A e2e the ledger dir "
                  f"may differ from the dispatch dir — point --run-dir at the "
                  f"dir holding events.jsonl. Deriving a verdict from an empty "
                  f"ledger.", file=sys.stderr)
    except OSError:
        pass


def cmd_state(args):
    spec = _load_spec(args.spec)
    # R10-1: same freshness-gated projection as checkpoint/verdict (the
    # Harness carries the evidence probe; a bare Ledger cannot see stale
    # evidence and disagreed with the gated verdict).
    h = _query_harness(args.run_dir)
    _warn_empty_ledger(h.ledger, args.run_dir)
    st = project_cognitive_state(spec, h.ledger,
                                 evidence_probe=h._evidence_freshness_probe)
    # v2 (spec §4): augment active loops with the current iteration so the
    # orchestrator sees PROGRESSING + loop_id + iteration without diving the
    # journal. Honest projection (invariant 6).
    _augment_loops_with_iteration(st.get("loops", []), h.ledger,
                                  spec.spec_version_id)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_verdict(args):
    try:
        spec = _load_spec(args.spec)
    except (ValueError, OSError) as e:
        print(f"ERROR: could not load spec: {e}", file=sys.stderr)
        return 1
    # R10-1: share the checkpoint path's evidence freshness probe. The bare
    # Ledger + derive_verdict call skipped R9-3 staleness (the probe guard in
    # _agent_verdict_verified requires evidence_probe), so a code change
    # flipped `axiom checkpoint` to PARTIAL while `axiom verdict` kept
    # printing VERIFIED + exit 0 -- two entry points disagreeing on the same
    # ledger. verdict must be the SAME projection the checkpoint gates on.
    h = _query_harness(args.run_dir)
    _warn_empty_ledger(h.ledger, args.run_dir)
    v = derive_verdict(spec, h.ledger,
                       evidence_probe=h._evidence_freshness_probe)
    print(v)
    return _exit_for_verdict(v)


def cmd_checkpoint(args):
    try:
        spec = _load_spec(args.spec)
    except (ValueError, OSError) as e:
        print(f"ERROR: could not load spec: {e}", file=sys.stderr)
        return 1
    h = _query_harness(args.run_dir)
    _warn_empty_ledger(h.ledger, args.run_dir)
    cp = h.project_checkpoint(spec)
    # v2 (spec §4): render loop progress in the checkpoint so the orchestrator
    # sees active loops + current iteration from the checkpoint alone (the only
    # object that re-enters orchestrator context). project_checkpoint in
    # harness.py predates v2 loops; the CLI merges the cognitive-state loops
    # projection here (honest projection, invariant 6). No harness.py change.
    st = project_cognitive_state(spec, h.ledger,
                                 evidence_probe=h._evidence_freshness_probe)
    cp["loops"] = _augment_loops_with_iteration(
        st.get("loops", []), h.ledger, spec.spec_version_id)
    print(json.dumps(cp, ensure_ascii=False, indent=2))
    return _exit_for_verdict(cp["verdict"])


def cmd_continue(args):
    """Cross-session resume after /clear. No args: read the active-run pointer
    (written by `axiom run`/`resume` at <project>/.axiom/active.json +
    ~/.axiom/active.json) and print the checkpoint + cognitive state + a
    one-line next-step. Read-only by default (zero dispatch); `--resume`
    re-dispatches unfinished nodes. Override the pointer with `--spec` +
    `--run-dir` if it's stale. Exit code matches the verdict (0/3/4/5), so
    `axiom continue && deliver` works; 2 = no active run found."""
    spec_path = getattr(args, "spec", None)
    run_dir = getattr(args, "run_dir", None)
    do_resume = getattr(args, "resume", False)
    entry = None
    if not (spec_path and run_dir):
        entry = _read_active()
        if entry is None:
            print("No active axiom run found. Pointers checked:\n"
                  "  <cwd>/.axiom/active.json   (project-scoped)\n"
                  "  ~/.axiom/active.json        (global last-active mirror)\n"
                  "Start one with: axiom run <spec.json> --run-dir .axiom/run",
                  file=sys.stderr)
            return 2
        spec_path = entry.get("spec_path")
        run_dir = entry.get("run_dir")
        if not spec_path or not run_dir or not Path(spec_path).is_file():
            print(f"active pointer is stale (spec_path={spec_path!r} not found). "
                  f"Re-run `axiom run <spec>` to refresh it, or pass "
                  f"--spec/--run-dir explicitly.", file=sys.stderr)
            return 2
    try:
        spec = _load_spec(spec_path)
    except Exception as e:
        print(f"ERROR: could not parse spec: {e}", file=sys.stderr)
        return 1
    # P-014: adoption declared at continue --resume time also attributes.
    _merge_adopted_from(spec, getattr(args, "adopted_from", None))
    # P-030: the run-context declaration persists across resume (merge after
    # reading the context below, inside the do_resume branch).
    sealed = bool(entry.get("sealed")) if entry else False
    if do_resume and not sealed:
        # re-dispatch unfinished nodes; the pointer is refreshed as sealed so a
        # subsequent `continue` sees the completed run instead of re-dispatching.
        # P-019: inherit the run's persisted context (auto-wiki/wiki_dir; the
        # project_root comes from the pointer -- it IS this run by definition
        # of continue, but the persisted context wins when present, e.g. the
        # pointer was overwritten by another project's run after this one).
        # R6-2: the run lock is held across the context read/merge/write AND
        # the re-walk -- a lock-rejected continue --resume must leave
        # run_context.json byte-identical (same review6 bug as cmd_resume).
        try:
            _check_run_dir_nesting(run_dir)
        except ValueError as e:
            print(f"INVALID: {e}", file=sys.stderr)
            return 1
        _lock_fd = _acquire_run_lock(run_dir)
        if _lock_fd is None:
            print(f"LOCKED: run-dir {run_dir!r} is held by another axiom process. "
                  f"Wait for it to finish or use a different --run-dir.",
                  file=sys.stderr)
            return 2
        _rctx = _read_run_context(run_dir)
        # R5-5/R6-3: only inherit declarations made under THIS spec version
        # (see cmd_resume -- a child version re-declares adoption explicitly).
        _ctx_decl = _ctx_declared_for(_rctx, spec.spec_version_id)
        if _ctx_decl:
            _merge_adopted_from(spec, _ctx_decl)
        elif _rctx.get("adopted_from"):
            print(f"NOTE: run-context adoption declarations belong to "
                  f"{_rctx.get('adopted_from_svid')!r}; not inherited by "
                  f"{spec.spec_version_id!r}.", file=sys.stderr)
        _pr = (_rctx.get("project_root")
               or (entry or {}).get("project_root"))
        _auto_wiki = (bool(getattr(args, "auto_wiki", False))
                      or bool(_rctx.get("auto_wiki")))
        _wiki_dir = (getattr(args, "wiki_dir", None) or _rctx.get("wiki_dir")
                     or _resolve_wiki_dir(None, _pr))
        # R5-4: persist the merged declaration back (same svid) so it
        # survives the next resume; spec file stays immutable. R6-2: inside
        # the run lock; merge_prior keeps OTHER svids' declarations (R6-3).
        _write_run_context(run_dir, project_root=_pr,
                           auto_wiki=_auto_wiki, wiki_dir=_wiki_dir,
                           adopted_from=list(getattr(spec, "adopted_from", None) or []),
                           adopted_from_svid=spec.spec_version_id,
                           merge_prior=True)
        _backend = _resume_backend(run_dir, spec)
        if _backend is None:
            return 1
        os.environ["AXIOM_RUN_DIR"] = str(Path(run_dir).resolve())
        cp, code = _run_resume(
            spec, run_dir, project_root=_pr,
            auto_wiki=_auto_wiki, wiki_dir=_wiki_dir, _lock_fd=_lock_fd)
        if cp:
            _write_active(spec_path, run_dir, spec.spec_version_id,
                          project_root=_pr,
                          sealed=True, verdict=cp.get("verdict"))
        print(f"resumed -- verdict={cp.get('verdict', '?')} "
              f"(run re-sealed, active pointer refreshed).", file=sys.stderr)
        print(json.dumps({"checkpoint": cp, "resumed": True},
                         ensure_ascii=False, indent=2))
        return code
    # read-only: project checkpoint + cognitive state (loops augmented, same
    # rendering as `axiom checkpoint`) so the orchestrator sees verdict +
    # blocked_items + active loops in one object without diving the journal.
    h = _query_harness(run_dir)
    cp = h.project_checkpoint(spec)
    st = project_cognitive_state(spec, h.ledger,
                                 evidence_probe=h._evidence_freshness_probe)
    cp["loops"] = _augment_loops_with_iteration(
        st.get("loops", []), h.ledger, spec.spec_version_id)
    # derive the failure-forensic envelope inline (not a separate `axiom debug`
    # call) so the orchestrator reads off the resume digest and continues
    # without rebuilding a mental model from the spec's full text + journal -- the
    # envelope carries WHY each node failed + its cognitive signature +
    # deterministic recovery_action + unmet requirements in one stderr block.
    env = derive_debug_envelope(spec, h.ledger,
                                evidence_probe=h._evidence_freshness_probe)
    cp["handoff"] = _continue_handoff(spec, cp, env, sealed)
    print(cp["handoff"], file=sys.stderr)
    print(json.dumps({"checkpoint": cp, "active": entry},
                     ensure_ascii=False, indent=2))
    return _exit_for_verdict(cp["verdict"])


def cmd_gate(args):
    lg = Ledger(args.run_dir)
    if args.action == "list":
        # show only UNRESOLVED gates (gate_open whose gate_id is not in
        # gate_resolve). Use `axiom journal` for the full history.
        resolved = {e["gate_id"] for e in lg.events() if e.get("kind") == "gate_resolve"}
        for e in lg.events():
            if e.get("kind") == "gate_open" and e["gate_id"] not in resolved:
                print(json.dumps(e, ensure_ascii=False))
        return 0
    # resolve
    if not args.gate_id or not args.decision:
        print("ERROR: gate resolve requires --gate-id and --decision", file=sys.stderr)
        return 1
    h = Harness(args.run_dir)
    h.resolve_gate(args.gate_id, args.decision, modify_intent=args.modify_intent)
    print(f"resolved {args.gate_id}: {args.decision}")
    return 0


def cmd_claim(args):
    """List CONFLICTED claims or resolve one via CLAIM_RESOLVED (axiom M3).

    `axiom claim list` shows claims whose derive_claim_status is CONFLICTED
    (different verify nodes contradicting). `axiom claim resolve --claim-id C1
    --basis-ref evidence:E1` grounds a CONFLICTED claim to SUPPORTED (NOT
    VERIFIED -- still needs verify; a human cannot self-certify).
    """
    from axiom.state import derive_claim_status
    lg = Ledger(args.run_dir)
    if args.action == "list":
        # collect (claim_id, svid) pairs from verify_verdict events
        seen = set()
        pairs = []
        for e in lg.events():
            if e.get("kind") != "verify_verdict":
                continue
            cid = e.get("claim_id")
            svid = e.get("spec_version_id")
            key = (cid, svid)
            if cid and key not in seen:
                seen.add(key)
                pairs.append((cid, svid))
        for cid, svid in pairs:
            if derive_claim_status(cid, lg, svid=svid) == "CONFLICTED":
                print(json.dumps({"claim_id": cid, "spec_version_id": svid,
                                  "status": "CONFLICTED"}, ensure_ascii=False))
        return 0
    # resolve
    if not args.claim_id or not args.basis_ref:
        print("ERROR: claim resolve requires --claim-id and --basis-ref",
              file=sys.stderr)
        return 1
    h = Harness(args.run_dir)
    h.resolve_claim(args.claim_id, args.basis_ref,
                    svid=args.spec_version_id, note=args.note)
    print(f"resolved {args.claim_id}: CONFLICTED -> SUPPORTED "
          f"(basis={args.basis_ref})")
    return 0


def cmd_journal(args):
    lg = Ledger(args.run_dir)
    for e in lg.events():
        print(json.dumps(e, ensure_ascii=False))
    errs = lg.verify_chain()
    if errs:
        print(f"\nCHAIN WARN: {len(errs)} tamper indicator(s):", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    # explicit chain-intact confirmation (kind-agnostic — works for ALL event
    # kinds including v2 loop/iteration/branch; invariant 5).
    print("CHAIN OK")
    return 0


# --- M2: task-level delivery protocol CLI (spec v1.1 §7.2/§7.3/§6.4) ---------
# The orchestrator records quality-loop events through these commands instead
# of hand-writing ledger JSON. Every write is validated against the M1/M2
# payload schema BEFORE it is appended -- an invalid gap/review/decision is
# refused, not recorded. The task ledger is the same events.jsonl the workflow
# uses (spec §8.4: one task_id, one task ledger across replan/resume).

def _load_active_contract(run_dir, task_id):
    from axiom.contract import ContractStore
    lg = Ledger(run_dir)
    c = ContractStore(run_dir).active(lg.events())
    if c is None:
        raise ValueError(
            f"no active delivery contract in {run_dir!r}; activate one first "
            "(M1 ContractStore.activate) before recording task events")
    if c.task_id != task_id:
        raise ValueError(
            f"task_id {task_id!r} does not match active contract "
            f"{c.task_id!r}")
    return c, lg


def _append_task_event(lg, kind, task_id, payload):
    return lg.append({
        "event_id": f"ev_{kind}_{payload.get('gap_id') or payload.get('review_id') or payload.get('decision_id') or 'x'}",
        "kind": kind, "task_id": task_id, "payload": payload, "claims": [],
    })


def cmd_task(args):
    from axiom.contract import (
        KIND_DECISION_RECORDED, KIND_GAP_DISPOSITION, KIND_GAP_RECORDED,
        KIND_REVIEW_RECORDED, validate_decision_payload,
        validate_disposition_payload, validate_gap_payload,
        validate_review_payload,
    )
    from axiom.delivery import project_delivery
    sub = args.task_action
    try:
        contract, lg = _load_active_contract(args.run_dir, args.task_id)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if sub == "status":
        # the task-level delivery block (spec §13.3) -- what the orchestrator
        # reads. Rebuilt from the ledger; never a self-reported field.
        proj = project_delivery(lg.events(), args.task_id, contract=contract)
        print(json.dumps(proj, ensure_ascii=False, indent=2))
        return 0

    if sub == "record-gap":
        payload = json.loads(args.payload)
        payload.setdefault("task_id", args.task_id)
        errs = validate_gap_payload(payload)
        if errs:
            print(f"INVALID gap: {'; '.join(errs)}", file=sys.stderr)
            return 1
        ev = _append_task_event(lg, KIND_GAP_RECORDED, args.task_id, payload)
        print(json.dumps({"recorded": ev["event_id"],
                          "gap_id": payload["gap_id"]}, ensure_ascii=False))
        return 0

    if sub == "record-disposition":
        payload = json.loads(args.payload)
        errs = validate_disposition_payload(payload)
        if errs:
            print(f"INVALID disposition: {'; '.join(errs)}", file=sys.stderr)
            return 1
        # gate the transition against the projected state (AC-08: an illegal
        # transition or a basis-less downgrade is refused, not recorded)
        from axiom.delivery import project_gaps
        gaps = project_gaps(lg.events(), args.task_id)
        gid = payload.get("gap_id")
        if gid not in gaps:
            print(f"INVALID: gap {gid!r} not found in task {args.task_id}",
                  file=sys.stderr)
            return 1
        try:
            _append_task_event(lg, KIND_GAP_DISPOSITION, args.task_id, payload)
            # re-project to surface any illegal transition as a hard error
            project_gaps(lg.events(), args.task_id)
        except ValueError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"recorded": True, "gap_id": gid,
                          "disposition": payload["disposition"]},
                         ensure_ascii=False))
        return 0

    if sub == "record-review":
        payload = json.loads(args.payload)
        payload.setdefault("task_id", args.task_id)
        errs = validate_review_payload(payload)
        if errs:
            # AC-04: a bare-PASS review is NOT review evidence -- refuse it.
            print(f"INVALID review (spec §6.4): {'; '.join(errs)}",
                  file=sys.stderr)
            return 1
        ev = _append_task_event(lg, KIND_REVIEW_RECORDED, args.task_id,
                                payload)
        print(json.dumps({"recorded": ev["event_id"],
                          "review_id": payload["review_id"]},
                         ensure_ascii=False))
        return 0

    if sub == "record-decision":
        payload = json.loads(args.payload)
        payload.setdefault("task_id", args.task_id)
        errs = validate_decision_payload(payload)
        if errs:
            print(f"INVALID decision: {'; '.join(errs)}", file=sys.stderr)
            return 1
        ev = _append_task_event(lg, KIND_DECISION_RECORDED, args.task_id,
                                payload)
        print(json.dumps({"recorded": ev["event_id"],
                          "decision_id": payload["decision_id"]},
                         ensure_ascii=False))
        return 0

    print(f"ERROR: unknown task action {sub!r}", file=sys.stderr)
    return 1


def cmd_doctor(args):
    """Preflight: verify execution-backend prerequisites in one command.

    Collapses the ad-hoc discovery an orchestrator otherwise does by failing
    (binary on PATH? provider key set? host adapter runnable?) into one
    pass/fail sweep per backend (pi / cc). Run this FIRST on a fresh host.
    """
    import shutil

    checks = []

    # --- pi backend ---
    pi = shutil.which("pi")
    checks.append(("pi binary on PATH", bool(pi), pi or "<not found>"))
    pi_key = any(os.environ.get(k) for k in (
        "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
    checks.append(("pi provider key set (DASHSCOPE/OPENAI/ANTHROPIC)",
                   pi_key,
                   (os.environ.get("DASHSCOPE_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", ""))[:4] + "..." if pi_key else "<unset>"))

    # --- cc backend ---
    cc = shutil.which("claude")
    checks.append(("claude (cc backend) on PATH", bool(cc), cc or "<not found>"))

    # host adapter present (the pi/cc responder script)
    adapter = Path(__file__).resolve().parent.parent / "scripts" / "host_adapter.py"
    checks.append(("scripts/host_adapter.py present",
                   adapter.exists(), str(adapter)))

    all_ok = True
    for name, ok, detail in checks:
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if all_ok:
        print("doctor: all checks passed")
        return 0
    print("doctor: one or more checks FAILED -- fix before `axiom run`",
          file=sys.stderr)
    return 1


def cmd_debug(args):
    """Project a failure-forensic envelope for a non-VERIFIED run (comet
    debug-gate, ported into axiom's projection layer).

    Read-only: never appends to the ledger, never calls an LLM. For each node
    the ledger records a failure for, surface the failure_class, cognitive
    signature, last output preview, bound unmet requirements, loop context and
    a deterministic recovery_action -- so the orchestrator draws the next
    workflow from root cause, not from a journal dive. A VERIFIED run with no
    failure events prints `no failures` and exits 0.
    """
    run_dir = Path(args.run_dir)
    if not (run_dir / "events.jsonl").exists():
        print(f"debug: run-dir {args.run_dir!r} has no ledger "
              f"(events.jsonl missing). Run `axiom run` first.",
              file=sys.stderr)
        return 1
    try:
        spec = _load_spec(args.spec)
    except (ValueError, OSError) as e:
        print(f"ERROR: could not load spec: {e}", file=sys.stderr)
        return 1
    # R10-1: the forensic verdict must match the acceptance gates
    # (checkpoint/verdict) -- a debug envelope reporting stale VERIFIED
    # sends the orchestrator down a fixed-already path.
    h = _query_harness(args.run_dir)
    lg = h.ledger
    env = derive_debug_envelope(spec, lg,
                                evidence_probe=h._evidence_freshness_probe)
    if getattr(args, "json", False):
        print(json.dumps(env, ensure_ascii=False, indent=2))
        return 0 if env["no_failures"] else 1
    if env["no_failures"]:
        print("no failures")
        return 0
    verdict = env["verdict"]
    print(f"verdict: {verdict}  ({len(env['failures'])} failure node(s))")
    if env.get("unmet_requirements"):
        print(f"unmet requirements ({len(env['unmet_requirements'])}):")
        for u in env["unmet_requirements"]:
            print(f"  - {u.get('requirement_id')} [{u.get('status')}] "
                  f"binding={u.get('binding')}")
    for f in env["failures"]:
        nid = f.get("node_id") or "<run-level>"
        print(f"\n[{f.get('failure_class')}] node={nid}  "
              f"recovery={f.get('recovery_action')}")
        if f.get("loop_id") is not None:
            print(f"  loop: loop_id={f['loop_id']} iteration={f['iteration']}")
        if f.get("cognitive_signature"):
            cs = f["cognitive_signature"]
            print(f"  cognitive_signature: {cs['kind']}={cs['value']}")
        if f.get("last_preview"):
            print(f"  last_preview: {f['last_preview']}")
        if f.get("gate_reason"):
            print(f"  gate_reason: {f['gate_reason']}")
        if f.get("unmet_requirements"):
            for u in f["unmet_requirements"]:
                print(f"  unmet: {u.get('requirement_id')} [{u.get('status')}]")
    return 1


def cmd_wiki_extract(args):
    """Distill a run's spec + ledger into a wiki experience entry (append-only).

    Reuses derive_verdict + derive_debug_envelope + derive_spec_shape (the
    Raw->Wiki distiller) so the wiki never re-derives truth the ledger holds.
    Appends one sealed entry to .axiom/wiki/wiki.jsonl; --auto-wiki on `run`
    calls this after a run finishes.
    """
    try:
        spec = _load_spec(args.spec)
    except Exception as e:
        print(f"ERROR: could not parse spec: {e}", file=sys.stderr)
        return 1
    lg = Ledger(args.run_dir)
    if not (Path(args.run_dir) / "events.jsonl").exists():
        print(f"ERROR: run-dir {args.run_dir!r} has no ledger (events.jsonl "
              f"missing). Run `axiom run` first.", file=sys.stderr)
        return 1
    tags = args.tag or []
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    sealed = w.append(extract_entry(spec, lg, args.run_dir, tags=tags,
                                    learned=args.learned))
    print(f"extracted {sealed['entry_id']}  verdict={sealed['verdict']}  "
          f"shape={sealed['spec_shape']}  patterns={len(sealed['patterns'])}")
    # --contract: if the run is VERIFIED, also append a format_contract entry
    # (the concrete proven structure, not just the shape fingerprint). Non-
    # VERIFIED runs produce no contract (their lessons are in the experience
    # entry's patterns, not in a contract). This is the deepening of "format
    # in hand before output": plan --wiki-suggest returns this concrete
    # structure next time, not just a shape string.
    if getattr(args, "contract", False):
        ce = extract_contract_entry(spec, lg, args.run_dir, tags=tags,
                                    learned=args.learned)
        if ce is None:
            print(f"contract: skipped (verdict not VERIFIED)")
        else:
            csealed = w.append(ce)
            print(f"contract: {csealed['entry_id']}  "
                  f"nodes={len(csealed.get('node_skeleton', []))}  "
                  f"binding={len(csealed.get('binding_pattern', []))}")
    return 0


def cmd_wiki_search(args):
    """Retrieve similar-intent experience BEFORE designing the next spec.

    Pure-Python keyword + tag + verdict filter (no LLM, no vector DB). This is
    the friction-elimination surface: the agent gets "how did a similar intent
    fare, what shape worked, what got denied" in hand BEFORE it produces the
    next spec -- format/shape arrive before output, not after.
    """
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    results = w.search(
        query=args.query or "",
        tags=args.tag or [],
        verdict=args.verdict,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("no matching experience")
        return 0
    for r in results:
        etype = r.get("entry_type", "experience")
        print(f"\n[{r['entry_id'][:18]}] ({etype}) {r['intent']}")
        if etype == "format_contract":
            print(f"  shape: {r.get('control_flow_shape')}  hits: {r['hits']}")
            if r.get("tags"):
                print(f"  tags: {', '.join(r['tags'])}")
            for ns in (r.get("node_skeleton") or []):
                req = ns.get("output_schema_required") or []
                vf = " verdict_field" if ns.get("verdict_field") else ""
                print(f"  node: {ns.get('id')} type={ns.get('type')} "
                      f"required={req}{vf} on_exhausted={ns.get('on_exhausted')}")
            if r.get("binding_pattern"):
                print(f"  binding: {r['binding_pattern']}")
            if r.get("learned"):
                print(f"  learned: {r['learned']}")
        else:
            print(f"  shape: {r['spec_shape']}  verdict: {r['verdict']}  hits: {r['hits']}")
            if r.get("tags"):
                print(f"  tags: {', '.join(r['tags'])}")
            for p in (r.get("patterns") or []):
                sig = p.get("cognitive_signature")
                sig_s = f"{sig['kind']}={sig['value']}" if sig else "-"
                print(f"  pattern: {p.get('failure_class')} "
                      f"sig={sig_s} recovery={p.get('recovery_action')}")
            if r.get("learned"):
                print(f"  learned: {r['learned']}")
        for imp in (r.get("impact") or []):
            print(f"  impact: [{imp['kind']}] {imp['reason']}")
    return 0


def cmd_wiki_show(args):
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    e = w.get(args.entry_id)
    if e is None:
        print(f"no entry {args.entry_id}", file=sys.stderr)
        return 1
    print(json.dumps(e, ensure_ascii=False, indent=2))
    return 0


def cmd_wiki_list(args):
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    entries = w.entries()
    if not entries:
        print("(empty wiki)")
        return 0
    amendments: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("entry_type") == "impact" and e.get("amends"):
            amendments.setdefault(e["amends"], []).append(e)
    for e in entries:
        if e.get("entry_type") != "experience":
            continue
        imps = amendments.get(e["entry_id"], [])
        print(f"{e['entry_id'][:18]}  {e.get('intent','')[:60]}  "
              f"verdict={e.get('verdict')}  impacts={len(imps)}")
    return 0


def cmd_wiki_impact(args):
    """Append a denied-replan / failed-gate / verify-failed lesson (append-only).

    Never rewrites the parent entry; appends an impact amendment so the chain
    stays intact and the lesson is hash-chained knowledge. `plan
    --wiki-suggest` surfaces these so the next plan doesn't re-propose a
    rejected path.
    """
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    try:
        sealed = w.add_impact(args.entry_id, args.kind, args.reason)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"appended impact [{args.kind}] onto {args.entry_id}  "
          f"amendment={sealed['entry_id']}")
    return 0


def cmd_wiki_verify(args):
    """Verify the wiki hash chain (tamper-evident under a trusted root)."""
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    errs = w.verify_chain()
    if errs:
        print(f"CHAIN WARN: {len(errs)} breakage(s):", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("CHAIN OK")
    return 0


def cmd_wiki_daydream(args):
    """Surface sediment gaps — missing lessons AND missing structured-output
    contracts (the Wiki Maintainer step, self-triggered).

    Scans --run-root for run dirs (dirs holding events.jsonl + verdict.json),
    reads each run's verdict.json (spec_version_id + verdict) + packet.md
    (intent), and lists the gaps against the wiki:

      - **contract gap** (highest priority): a VERIFIED run with no
        format_contract entry. The format_contract is axiom's prize -- the
        proven structure (node skeleton + output_schema required + binding
        pattern) the next spec copies so format arrives BEFORE output, not
        after. A VERIFIED run without one is structured-output knowledge left
        on the floor. (Non-VERIFIED runs produce no contract -- only VERIFIED
        structures are proven-correct, so the gap is only meaningful there.)
      - **experience gap**: no experience entry for this svid (no
        spec-shape/stagnation lesson sedimented at all).

    Read-only: surfaces candidates; the model/user runs `wiki extract` /
    `wiki extract --contract` to actually sediment (judgment, not reflex -- a
    run that taught nothing needs no entry). `--json` emits a machine-
    consumable list for agent consumption / chaining. Contract gaps sort
    first (structured output is the priority).
    """
    w = Wiki(_resolve_wiki_dir(args.wiki_dir))
    # P1-2 fix: dedup on (spec_version_id, run_dir), not just spec_version_id.
    # Two runs can share a spec_version_id (the plan template default is
    # "spec.v1"); keying only on svid made daydream mark the never-extracted
    # run as "has experience" because its sibling's entry shared the svid.
    # run_dir is part of the entry_id (_entry_id uses svid|run_dir|ts) so it is
    # a stable per-run discriminator. Normalize (resolve) so path-form
    # differences (relative / symlink / trailing slash) don't defeat the match.
    def _norm(rd):
        try:
            return str(Path(rd).resolve())
        except Exception:
            return str(rd)
    exp_keys = {
        (e.get("spec_version_id"), _norm(e.get("run_dir", "")))
        for e in w.entries()
        if e.get("entry_type") == "experience" and e.get("spec_version_id")
    }
    contract_keys = {
        (e.get("spec_version_id"), _norm(e.get("run_dir", "")))
        for e in w.entries()
        if e.get("entry_type") == "format_contract" and e.get("spec_version_id")
    }
    run_root = Path(args.run_root)
    # bounded scan: depth 1-3 (run_root itself, run_root/*, run_root/*/*).
    # Deeper run-dirs are unusual; nested run-dirs are refused at run time
    # anyway (_check_run_dir_nesting), so a bounded glob is enough.
    ev_paths = []
    for pat in ("events.jsonl", "*/events.jsonl", "*/*/events.jsonl"):
        ev_paths.extend(sorted(run_root.glob(pat)))
    rows = []
    for ev in ev_paths:
        run_dir = ev.parent
        vj = run_dir / "verdict.json"
        if not vj.is_file():
            continue  # not a materialized run (e.g. a sub-run without a verdict)
        try:
            v = json.loads(vj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        svid = v.get("spec_version_id")
        if not svid:
            continue
        verdict = v.get("verdict", "?")
        # P1-2: key on (svid, run_dir) so two runs sharing a spec_version_id
        # (the plan default is spec.v1) are distinct. Normalize the run_dir
        # (resolve symlinks/relative) so a wiki entry stored with one path form
        # matches the scanned path form.
        try:
            run_key = (svid, str(run_dir.resolve()))
        except Exception:
            run_key = (svid, str(run_dir))
        has_exp = run_key in exp_keys
        has_contract = run_key in contract_keys
        gaps = []
        if not has_exp:
            gaps.append("experience")
        if verdict == "VERIFIED" and not has_contract:
            gaps.append("contract")
        if not gaps:
            continue  # fully sedimented
        intent = ""
        pm = run_dir / "packet.md"
        if pm.is_file():
            try:
                for line in pm.read_text(encoding="utf-8").splitlines():
                    if line.startswith("**intent**"):
                        intent = line.split(":", 1)[-1].strip()
                        break
            except OSError:
                pass
        # sediment cmd: --contract first when the contract is missing (the
        # structured-output prize); plain experience extract otherwise.
        if "contract" in gaps:
            cmd = (f"axiom wiki extract <spec.json> --run-dir {run_dir} "
                   f"--wiki-dir {_resolve_wiki_dir(args.wiki_dir)} --contract")
        else:
            cmd = (f"axiom wiki extract <spec.json> --run-dir {run_dir} "
                   f"--wiki-dir {_resolve_wiki_dir(args.wiki_dir)}")
        rows.append({
            "run_dir": str(run_dir), "spec_version_id": svid,
            "verdict": verdict, "intent": intent,
            "has_experience": has_exp, "has_contract": has_contract,
            "gaps": gaps, "sediment_cmd": cmd,
        })
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"daydream: no sediment gaps found (scanned {run_root}; "
              f"wiki holds {len(exp_keys)} experience + "
              f"{len(contract_keys)} contract run-key).")
        return 0
    # contract gaps first (structured output is the priority)
    rows.sort(key=lambda r: (0 if "contract" in r["gaps"] else 1,
                             r["spec_version_id"]))
    n_contract = sum(1 for r in rows if "contract" in r["gaps"])
    print(f"daydream: {len(rows)} run(s) with sediment gaps under {run_root} "
          f"({n_contract} missing structured-output contract):")
    for r in rows:
        print(f"  {r['spec_version_id']}  verdict={r['verdict']}  "
              f"gaps={','.join(r['gaps'])}  {r['run_dir']}")
        if r["intent"]:
            print(f"    intent: {r['intent']}")
        print(f"    sediment: {r['sediment_cmd']}")
    return 0


def _skill_patterns_path():
    """The portable skill self-evolution store: <skill_root>/skill_patterns.jsonl.

    Resolved from cli.py's location (parent.parent = the axiom skill root,
    whether the source repo or a deployed copy), so the structured pattern
    store travels WITH axiom wherever it is installed -- sediment is for the
    agent, not humans, and it must be retrievable wherever axiom runs."""
    return Path(__file__).resolve().parent.parent / "skill_patterns.jsonl"


def _skill_wiki():
    return Wiki(_skill_patterns_path().parent, filename="skill_patterns.jsonl")


def _domain_contracts_path():
    """The portable domain-knowledge store: <skill_root>/domain_contracts.jsonl.

    domain_contract entries (surface/risk_floor/required_roles/binding_pattern/
    verdict_rule) are skill-level adjudication knowledge, not project run experience --
    they travel WITH axiom (same rationale as skill_patterns.jsonl) so a spec
    author in any project can recall "secret-auth → auth_gate → BLOCKED" via
    `plan --wiki-suggest` without reading SKILL.md/references."""
    return Path(__file__).resolve().parent.parent / "domain_contracts.jsonl"


def _domain_contracts_wiki():
    return Wiki(_domain_contracts_path().parent, filename="domain_contracts.jsonl")


def _pattern_wiki(args):
    """Resolve the skill-pattern Wiki store, honoring a --skill-wiki-dir
    override (for tests / non-standard installs); default = the axiom skill
    root, resolved from cli.py's location so it travels with axiom."""
    override = getattr(args, "skill_wiki_dir", None)
    if override:
        return Wiki(Path(override), filename="skill_patterns.jsonl")
    return _skill_wiki()


def cmd_wiki_pattern_add(args):
    """Append a STRUCTURED skill self-evolution pattern (entry_type=
    skill_pattern) to the skill's portable pattern store.

    Sediment is for the agent, not humans: structured fields (pattern_id /
    feature / friction / fix / commit / rejected / open_questions /
    sub_lessons), agent-consumable, never a markdown doc. Rejected approaches
    + open questions are structured so a future change reads 'tried X, rejected
    because Y' instead of re-walking the friction. The store travels with the
    skill (resolved from the axiom install root).
    """
    w = _pattern_wiki(args)
    rejected = []
    for r in (args.rejected or []):
        if "::" in r:
            a, _, b = r.partition("::")
            rejected.append({"approach": a.strip(), "reason": b.strip()})
        else:
            rejected.append({"approach": r.strip(), "reason": ""})
    sealed = w.append_skill_pattern(
        pattern_id=args.id, feature=args.feature, friction=args.friction,
        fix=args.fix, commit_sha=args.commit, rejected=rejected,
        open_questions=args.open_question or [],
        sub_lessons=args.sub_lesson or [], status=args.status,
    )
    print(f"appended skill_pattern {sealed['pattern_id']}  "
          f"{sealed['entry_id'][:18]}  status={sealed['status']}  "
          f"file={w.wiki_path}")
    return 0


def cmd_wiki_pattern_list(args):
    """List skill self-evolution patterns (structured, agent-consumable)."""
    w = _pattern_wiki(args)
    found = [e for e in w.entries()
             if e.get("entry_type") == "skill_pattern"]
    if not found:
        print("(no skill patterns)")
        return 0
    for e in found:
        print(f"{e.get('pattern_id')}  [{e.get('status', 'active')}]  "
              f"{e.get('feature', '')[:60]}  commit={e.get('commit_sha', '-')}")
    return 0


def cmd_wiki_pattern_show(args):
    """Show one skill pattern as full JSON (structured, agent-consumable)."""
    w = _pattern_wiki(args)
    e = w.get_by_pattern_id(args.pattern_id)
    if e is None:
        print(f"ERROR: no skill_pattern {args.pattern_id!r}", file=sys.stderr)
        return 1
    print(json.dumps(e, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="axiom",
        description="deterministic workflow harness -- design a bounded workflow "
                    "IR, hand it off, read the checkpoint back",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="emit a spec template")
    sp.add_argument("--intent")
    sp.add_argument("--out")
    sp.add_argument("--wiki-suggest", action="store_true",
                    help="retrieve similar-intent prior runs from the wiki "
                         "BEFORE emitting the template (format-in-hand: shape, "
                         "verdict, impact lessons arrive before output)")
    _add_wiki_dir(sp)
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("validate", help="validate a spec")
    sp.add_argument("spec")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("run", help="run a spec through the harness")
    sp.add_argument("spec")
    sp.add_argument("--fresh", action="store_true",
                    help="truncate the run-dir ledger before running (start clean)")
    sp.add_argument("--project-root", default=None,
                    help="project root for worker cwd + write_areas glob "
                         "(default: current working directory)")
    sp.add_argument("--runtime", choices=sorted(_BACKENDS), default=None,
                    help="execution backend: pi or cc (both routed through the "
                         "host_adapter file protocol). Default: cc. "
                         "If not set, prompts or reads AXIOM_RUNTIME env.")
    sp.add_argument("--auto-wiki", action="store_true",
                    help="after seal, distill this run's experience into the "
                         "wiki (append-only) so `plan --wiki-suggest` retrieves "
                         "it next time")
    _add_adopted_from(sp)
    _add_wiki_dir(sp)
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="re-walk and re-project a run")
    sp.add_argument("spec")
    _add_run_dir(sp)
    sp.add_argument("--runtime", choices=sorted(_BACKENDS), default=None,
                    help="override the run's recorded backend (a switch is "
                         "recorded in the ledger); default: resume with the "
                         "backend recorded at run start")
    # P1-1: resume must be able to sediment the (possibly changed) verdict, or
    # a BLOCKED->resume->VERIFIED sequence leaves the wiki stale at BLOCKED.
    sp.add_argument("--auto-wiki", action="store_true",
                    help="distill the resumed run's experience into the wiki "
                         "after seal (appends a revised entry; keeps the old)")
    _add_adopted_from(sp)
    _add_wiki_dir(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("state", help="project cognitive state")
    sp.add_argument("spec")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_state)

    sp = sub.add_parser("verdict", help="derive verdict")
    sp.add_argument("spec")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_verdict)

    sp = sub.add_parser("checkpoint", help="project checkpoint (only re-entry object)")
    sp.add_argument("spec")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_checkpoint)

    sp = sub.add_parser("continue",
                        help="resume the active run after /clear (no args: "
                             "reads the active-run pointer and prints the "
                             "checkpoint + next-step; --resume re-dispatches)")
    sp.add_argument("--spec", default=None,
                    help="override the pointer's spec path (if stale)")
    sp.add_argument("--run-dir", default=None,
                    help="override the pointer's run-dir (if stale)")
    sp.add_argument("--resume", action="store_true",
                    help="re-dispatch unfinished nodes (default: read-only "
                         "checkpoint, zero dispatch)")
    # P1-1: continue --resume must also be able to sediment (same as resume).
    sp.add_argument("--auto-wiki", action="store_true",
                    help="with --resume: distill the resumed run's experience "
                         "into the wiki after seal")
    _add_adopted_from(sp)
    _add_wiki_dir(sp)
    sp.set_defaults(func=cmd_continue)

    sp = sub.add_parser("gate", help="list / resolve gates")
    sp.add_argument("action", choices=["list", "resolve"])
    sp.add_argument("--gate-id")
    sp.add_argument("--decision", choices=["allow", "deny", "modify"])
    sp.add_argument("--modify-intent")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_gate)

    sp = sub.add_parser("claim",
                        help="list CONFLICTED claims / resolve via CLAIM_RESOLVED")
    sp.add_argument("action", choices=["list", "resolve"])
    sp.add_argument("--claim-id")
    sp.add_argument("--basis-ref")
    sp.add_argument("--spec-version-id", dest="spec_version_id")
    sp.add_argument("--note")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("journal", help="print ledger events + verify chain")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_journal)

    # M2: task-level delivery protocol (spec v1.1). The orchestrator records
    # quality-gap / review / decision events and reads the task projection
    # without hand-writing ledger JSON. All writes are schema-validated
    # before append; a bare-PASS review or an illegal gap disposition is
    # refused, not recorded.
    sp = sub.add_parser("task", help="task-level delivery protocol (M2)")
    sp.add_argument("task_action",
                    choices=["status", "record-gap", "record-disposition",
                             "record-review", "record-decision"],
                    help="status: project the delivery block; record-*: "
                         "append a validated quality-loop event")
    sp.add_argument("--task-id", required=True)
    sp.add_argument("--payload", default=None,
                    help="JSON payload for record-* actions")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("doctor", help="preflight: check pi / cc backend prerequisites")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("debug",
                        help="project a failure-forensic envelope for a "
                             "non-VERIFIED run (root-cause view: which node "
                             "stagnated/exhausted, why, recovery action)")
    sp.add_argument("spec", help="spec file (the run's spec)")
    sp.add_argument("--json", action="store_true",
                    help="emit the envelope as JSON")
    _add_run_dir(sp)
    sp.set_defaults(func=cmd_debug)

    # --- wiki: append-only spec-experience store + retrieval ---------------
    sp = sub.add_parser("wiki",
                        help="spec-experience wiki: distill run experience, "
                             "retrieve similar-intent lessons before planning")
    wsub = sp.add_subparsers(dest="wiki_action", required=True)

    sp_ex = wsub.add_parser("extract",
                           help="distill a finished run's spec+ledger into a "
                                "wiki experience entry (append-only)")
    sp_ex.add_argument("spec", help="spec file (the run's spec)")
    sp_ex.add_argument("--learned", default=None,
                       help="one-line lesson to carry into the wiki (optional)")
    sp_ex.add_argument("--contract", action="store_true",
                       help="also append a format_contract entry IF the run is "
                            "VERIFIED (the concrete proven structure: node "
                            "skeleton + output_schema required + binding pattern)")
    sp_ex.add_argument("--tag", action="append", default=None,
                       help="domain tag (repeatable): bugfix|ui|runtime-verify|...")
    _add_wiki_dir(sp_ex)
    _add_run_dir(sp_ex)
    sp_ex.set_defaults(func=cmd_wiki_extract)

    sp_se = wsub.add_parser("search",
                           help="retrieve similar-intent prior runs "
                                "(keyword + tag + verdict filter, pure python)")
    sp_se.add_argument("query", nargs="?", default="",
                       help="keyword query against intent/requirements/learned/shape")
    sp_se.add_argument("--tag", action="append", default=None,
                       help="filter by tag (repeatable)")
    sp_se.add_argument("--verdict", default=None,
                       choices=["VERIFIED", "PARTIAL", "BLOCKED", "UNVERIFIED"],
                       help="filter by verdict")
    sp_se.add_argument("--limit", type=int, default=10,
                       help="max results (default 10)")
    sp_se.add_argument("--json", action="store_true",
                      help="emit results as JSON")
    _add_wiki_dir(sp_se)
    sp_se.set_defaults(func=cmd_wiki_search)

    sp_sh = wsub.add_parser("show", help="show one entry (full JSON)")
    sp_sh.add_argument("entry_id")
    _add_wiki_dir(sp_sh)
    sp_sh.set_defaults(func=cmd_wiki_show)

    sp_li = wsub.add_parser("list", help="list all experience entries")
    _add_wiki_dir(sp_li)
    sp_li.set_defaults(func=cmd_wiki_list)

    sp_im = wsub.add_parser("impact",
                           help="append a denied-replan / failed-gate / "
                                "verify-failed lesson onto an entry (append-only)")
    sp_im.add_argument("entry_id")
    sp_im.add_argument("--kind", required=True,
                      help="replan_denied | gate_denied | verify_failed | "
                           "format_drift | other")
    sp_im.add_argument("--reason", required=True, help="why it was denied/failed")
    _add_wiki_dir(sp_im)
    sp_im.set_defaults(func=cmd_wiki_impact)

    sp_ve = wsub.add_parser("verify", help="verify the wiki hash chain")
    _add_wiki_dir(sp_ve)
    sp_ve.set_defaults(func=cmd_wiki_verify)

    sp_dd = wsub.add_parser(
        "daydream",
        help="surface sediment gaps -- missing lessons AND missing "
             "structured-output contracts (read-only; self-summarization)")
    sp_dd.add_argument("--run-root", default=".axiom",
                       help="root to scan for run dirs (default .axiom)")
    sp_dd.add_argument("--json", action="store_true",
                       help="emit the gap list as JSON (machine-consumable)")
    _add_wiki_dir(sp_dd)
    sp_dd.set_defaults(func=cmd_wiki_daydream)

    sp_pat = wsub.add_parser(
        "pattern",
        help="structured skill self-evolution patterns (portable, "
             "agent-consumable)")
    psub = sp_pat.add_subparsers(dest="pattern_action", required=True)

    sp_pa = psub.add_parser(
        "add",
        help="append a structured skill_pattern (agent sediment; never a "
             "markdown doc)")
    sp_pa.add_argument("--id", required=True, help="pattern id, e.g. P-001")
    sp_pa.add_argument("--feature", required=True,
                       help="the feature/behavior this pattern is about")
    sp_pa.add_argument("--friction", required=True,
                       help="the friction that motivated it")
    sp_pa.add_argument("--fix", required=True, help="the fix applied")
    sp_pa.add_argument("--commit", required=True, help="commit SHA")
    sp_pa.add_argument("--rejected", action="append", default=None,
                       help="rejected approach + why, 'approach::reason' "
                            "(repeatable)")
    sp_pa.add_argument("--open-question", action="append", default=None,
                       help="open question (repeatable)")
    sp_pa.add_argument("--sub-lesson", action="append", default=None,
                       help="sub-lesson (repeatable)")
    sp_pa.add_argument("--status", default="active",
                       help="active | superseded")
    sp_pa.add_argument("--skill-wiki-dir", default=None,
                       help="override the skill-pattern store dir (default: "
                            "axiom skill root, resolved from install)")
    sp_pa.set_defaults(func=cmd_wiki_pattern_add)

    sp_pl = psub.add_parser("list", help="list skill patterns")
    sp_pl.add_argument("--skill-wiki-dir", default=None,
                       help="override the skill-pattern store dir")
    sp_pl.set_defaults(func=cmd_wiki_pattern_list)

    sp_ps = psub.add_parser("show", help="show one skill pattern (full JSON)")
    sp_ps.add_argument("pattern_id")
    sp_ps.add_argument("--skill-wiki-dir", default=None,
                       help="override the skill-pattern store dir")
    sp_ps.set_defaults(func=cmd_wiki_pattern_show)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    # Guard so `python3 -m axiom.cli <cmd>` actually runs main() instead of
    # importing the module and silently exiting 0 (which masquerades as success).
    # The canonical entry is `python3 -m axiom` via __main__.py, but importing
    # cli.py as a module must not return a fake-zero. See change-assurance F1.
    sys.exit(main())
