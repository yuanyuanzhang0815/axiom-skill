"""v2 Task 12: worktree isolation + runtime_assets auto-symlink.

The sample_app-symlink gate: a worktree-isolated worker, from inside the
worktree, imports a gitignored namespace package + opens data/.env WITHOUT
any manual symlinks -- the harness auto-symlinks `runtime_assets` globs
from the main checkout into the fresh git worktree before dispatch. This
is exactly what sample_app SaaS manually patched (symlink sample_app + .env +
data) in v1; v2 automates it.

No-leak (the #1 v1 friction root cause): worker writes stay in the
worktree, not the main checkout.

Invariant 6: worktree creation/cleanup are runtime mechanics -- they emit
NO ledger events (only artifact_write captures worktree output, per §5
hash-chain interaction).
"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from axiom.harness import Harness


# --- shared helpers -----------------------------------------------------------

def _env(result_str='{"ok": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _agent(**overrides):
    base = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
        "isolation": "worktree",
        "runtime_assets": ["data/**", "backend/.env", "sample_app/**"],
    }
    base.update(overrides)
    return base


def _git_init_project(project_root: Path):
    """A git repo with one tracked file (README.md) and UNTRACKED runtime
    assets (data/db.sqlite, backend/.env, sample_app/__init__.py). `git worktree
    add` checks out only tracked files -> the new worktree lacks the runtime
    assets -> the harness must symlink them in (the sample_app-symlink gate)."""
    project_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(project_root), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"],
                   cwd=str(project_root), check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=str(project_root), check=True)
    (project_root / "README.md").write_text("# project\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(project_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=str(project_root), check=True)
    # untracked runtime assets -- NOT present in a fresh git worktree
    (project_root / "data").mkdir(exist_ok=True)
    (project_root / "data" / "db.sqlite").write_bytes(b"SQLITE-DATA")
    (project_root / "backend").mkdir(exist_ok=True)
    (project_root / "backend" / ".env").write_text("KEY=val\n", encoding="utf-8")
    (project_root / "sample_app").mkdir(exist_ok=True)
    (project_root / "sample_app" / "__init__.py").write_text(
        "# sample_app ns\n", encoding="utf-8")


def _snippet_runner(snippet: str):
    """Fake runner that runs a python snippet from cwd (the worker cwd =
    the worktree path) and returns a clean dispatch result envelope. The snippet
    IS the gate: if import/open fails (no symlink), subprocess raises and
    the dispatch (and the test) fails."""
    def runner(args, cwd=None):
        subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=cwd, check=True, capture_output=True, text=True,
        )
        return (0, _env())
    return runner


# --- the sample_app-symlink gate (MUST pass) --------------------------------------

def test_worktree_worker_imports_app_db_config(tmp_path):
    """THE GATE: worker imports sample_app + opens data/db.sqlite + reads
    backend/.env from inside the worktree, with NO manual symlinks. Without
    the auto-symlink, ModuleNotFoundError / FileNotFoundError kill the
    snippet. (sample_app SaaS manually symlinked these in v1; v2 automates it.)"""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    snippet = textwrap.dedent("""
        import os
        # cwd MUST be the worktree, not the main checkout -- without the
        # intercept the worker runs from project_root and "passes" trivially
        cwd = os.getcwd()
        assert "wt_n1_" in cwd and "run" in cwd, \
            f"cwd is not the worktree: {cwd}"
        # the symlinks must exist in the worktree, pointing at the main checkout
        for rel in ("data", "backend/.env", "sample_app"):
            assert os.path.lexists(rel), f"{rel} missing in worktree"
            assert os.path.islink(rel), \
                f"{rel} is not a symlink (auto-symlink failed): {rel}"
        # the sample_app namespace package imports from inside the worktree
        import sample_app
        # the data DB is reachable through the symlink
        assert open("data/db.sqlite", "rb").read() == b"SQLITE-DATA"
        # the env file is readable
        env = open("backend/.env").read()
        assert "KEY=val" in env, f"env content wrong: {env!r}"
    """)
    h = Harness(run_dir, worker_runner=_snippet_runner(snippet),
                project_root=project)
    out = h.dispatch_agent(_agent(write_areas=["src/**/*"]), {}, "spec.v1")
    assert out is not None, "dispatch must succeed when runtime_assets are symlinked"


# --- no-leak (the #1 v1 friction root cause) ---------------------------------

def test_worktree_no_leak_to_main_checkout(tmp_path):
    """Worker writes src/x.py inside the worktree -> the file is created in
    the WORKTREE, NOT in the main checkout. (v1 bug: worker cwd=run_dir ->
    the host resolved project to main checkout -> changes leaked to main.)"""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    snippet = textwrap.dedent("""
        import os
        os.makedirs("src", exist_ok=True)
        open("src/x.py", "w").write("x = 1")
    """)
    h = Harness(run_dir, worker_runner=_snippet_runner(snippet),
                project_root=project)
    out = h.dispatch_agent(_agent(write_areas=["src/**/*"]), {}, "spec.v1")
    assert out is not None
    # main checkout untouched -- the leak that plagued v1 SaaS does not happen
    assert not (project / "src" / "x.py").exists(), \
        "LEAK: worker write reached the main checkout"
    # the artifact IS pinned (from the worktree, content-addressed)
    expected = "sha256:" + hashlib.sha256(b"x = 1").hexdigest()
    refs = [e["payload"]["artifact_refs"] for e in h.ledger.events()
            if e.get("kind") == "artifact_write"]
    assert refs and expected in refs[0], "worker output must be pinned"
    assert (run_dir / "artifacts" / expected).read_bytes() == b"x = 1"


# --- the rest of the 7-step intercept ----------------------------------------

def test_worktree_dir_created(tmp_path):
    """isolation='worktree' -> run_dir/wt_{node_id}_{hash} exists during
    dispatch and the worker cwd is the worktree path."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    seen = {}

    def runner(args, cwd=None):
        seen["cwd"] = cwd
        seen["exists"] = Path(cwd).exists() if cwd else False
        return (0, _env())

    h = Harness(run_dir, worker_runner=runner, project_root=project)
    h.dispatch_agent(_agent(), {}, "spec.v1")
    assert seen.get("exists"), "worktree must exist during dispatch"
    cwd = seen.get("cwd") or ""
    assert "wt_n1_" in cwd and str(run_dir) in cwd, \
        f"cwd must be run_dir/wt_n1_<hash>, got {cwd!r}"


def test_empty_runtime_assets_auto_detects(tmp_path):
    """Empty runtime_assets -> auto-detect still symlinks data/, .env, and
    untracked importable dirs (the namespace package sample_app/)."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    snippet = textwrap.dedent("""
        import os
        assert "wt_n1_" in os.getcwd(), "not running in worktree"
        import sample_app
        assert open("data/db.sqlite", "rb").read() == b"SQLITE-DATA"
        assert os.path.lexists("data") and os.path.islink("data")
    """)
    h = Harness(run_dir, worker_runner=_snippet_runner(snippet),
                project_root=project)
    out = h.dispatch_agent(_agent(runtime_assets=[]), {}, "spec.v1")
    assert out is not None


def test_artifact_pinned_to_run_dir_artifacts_sha256(tmp_path):
    """Worktree output is content-addressed (sha256:<hash>) in the
    centralized run_dir/artifacts, stable across iterations (same content ->
    same hash, independent of worktree path)."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    snippet = ('import os; assert "wt_n1_" in os.getcwd(); '
               'os.makedirs("src", exist_ok=True); '
               'open("src/out.txt","w").write("stable")')
    h = Harness(run_dir, worker_runner=_snippet_runner(snippet),
                project_root=project)
    h.dispatch_agent(_agent(write_areas=["src/**/*"]), {}, "spec.v1")
    expected = "sha256:" + hashlib.sha256(b"stable").hexdigest()
    assert (run_dir / "artifacts" / expected).exists(), \
        "artifact must be pinned content-addressed in run_dir/artifacts"


def test_worktree_cleaned_up_in_finally(tmp_path):
    """After dispatch, the worktree dir is gone (finally cleanup)."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    seen = {}

    def runner(args, cwd=None):
        seen["cwd"] = cwd
        return (0, _env())

    h = Harness(run_dir, worker_runner=runner, project_root=project)
    h.dispatch_agent(_agent(), {}, "spec.v1")
    assert seen.get("cwd") and "wt_n1_" in seen["cwd"], \
        "worktree must have been used during dispatch"
    leftover = list(run_dir.glob("wt_n1_*"))
    assert not leftover, f"worktree not cleaned up: {leftover}"


def test_worktree_kept_when_keep_worktrees(tmp_path):
    """keep_worktrees=True leaves the worktree for debugging."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    seen = {}

    def runner(args, cwd=None):
        seen["cwd"] = cwd
        return (0, _env())

    h = Harness(run_dir, worker_runner=runner, project_root=project,
                keep_worktrees=True)
    h.dispatch_agent(_agent(), {}, "spec.v1")
    assert seen.get("cwd") and "wt_n1_" in seen["cwd"]
    assert list(run_dir.glob("wt_n1_*")), "worktree should be kept"


def test_worktree_mechanics_emit_no_ledger_events(tmp_path):
    """Worktree creation/cleanup are runtime mechanics -- they emit NO
    ledger events (only artifact_write captures worktree output; §5
    hash-chain interaction, invariant 6)."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    seen = {}

    def runner(args, cwd=None):
        seen["cwd"] = cwd
        return (0, _env())

    h = Harness(run_dir, worker_runner=runner, project_root=project)
    h.dispatch_agent(_agent(), {}, "spec.v1")
    assert seen.get("cwd") and "wt_n1_" in seen["cwd"], \
        "test only meaningful if a worktree was actually used"
    kinds = {e.get("kind") for e in h.ledger.events()}
    forbidden = {"worktree_create", "worktree_cleanup", "worktree_setup",
                 "worktree_remove"}
    assert not (kinds & forbidden), \
        f"worktree mechanics emitted events: {kinds & forbidden}"
    assert "agent_result" in kinds, "agent_result should still be emitted"


def test_non_worktree_node_unaffected(tmp_path):
    """isolation='none' (default) -> no worktree dir created, cwd is
    project_root (v1 Fix A unchanged). Regression guard for the 422."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    project.mkdir(parents=True, exist_ok=True)
    seen = {}

    def runner(args, cwd=None):
        seen["cwd"] = cwd
        return (0, _env())

    h = Harness(run_dir, worker_runner=runner, project_root=project)
    node = _agent()
    node.pop("isolation")  # default 'none'
    node.pop("runtime_assets")
    h.dispatch_agent(node, {}, "spec.v1")
    assert seen.get("cwd") == str(project), \
        f"non-worktree cwd must be project_root, got {seen.get('cwd')!r}"
    assert not list(run_dir.glob("wt_n1_*")), "no worktree for isolation=none"
