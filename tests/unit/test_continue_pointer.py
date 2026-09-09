"""Active-run pointer + `axiom continue` cross-session resume.

These tests pin the contract documented in SKILL.md's "Cross-session resume
(after /clear)" section:

- `axiom run` writes an active-run pointer (project-scoped + global mirror),
  sealed:false on start, refreshed sealed:true+verdict on end.
- `axiom continue` (no args) reads the pointer and lands on the checkpoint;
  no pointer / stale pointer -> exit 2.
- `--resume` re-dispatches unfinished nodes (only when the pointer is
  unsealed, i.e. a crashed/interrupted run); on a sealed run it prints guidance.
- `--spec`/`--run-dir` bypass the pointer.

Isolation: every test chdir's into tmp_path before `continue`, so the
cwd_local pointer lookup (Path.cwd()/.axiom/active.json) can't be satisfied
by a stray worktree-level pointer written by some other test in the suite
that calls `axiom run` without --project-root. The global mirror is
monkeypatched to tmp_path so the suite never touches ~/.axiom.
"""
import json
from pathlib import Path

from axiom.cli import main
from axiom.ir import Spec, Requirement, spec_to_json


def _spec_file(path, steps=None, se=None):
    s = Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=se or ["S1:R1=claim:C1"],
        nodes={
            "n1": {
                "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
                "output_schema": {"type": "object"}, "allowed_tools": [],
                "write_areas": [], "acceptance": ["a"], "failure_policy": {},
            }
        },
        control_flow={"type": "sequence", "steps": steps or []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )
    Path(path).write_text(spec_to_json(s), encoding="utf-8")
    return str(path)


def _no_global(monkeypatch, tmp_path):
    """Redirect the global active pointer to a path inside tmp_path."""
    monkeypatch.setattr("axiom.cli._active_global_path",
                        lambda: tmp_path / "global_active.json")


def _fake_runner(monkeypatch):
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    monkeypatch.setattr("axiom.harness._default_runner", runner)


def _unseal_both(tmp_path):
    """Flip project + global pointers to sealed:false (interrupted run)."""
    for p in (tmp_path / ".axiom" / "active.json",
              tmp_path / "global_active.json"):
        entry = json.loads(p.read_text())
        entry["sealed"] = False
        p.write_text(json.dumps(entry), encoding="utf-8")


# --- run writes the pointer ---------------------------------------------------

def test_run_writes_sealed_pointer(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])  # no dispatch -> PARTIAL
    rc = main(["run", spec, "--run-dir", str(tmp_path / "run"),
               "--project-root", str(tmp_path)])
    assert rc == 4  # PARTIAL
    capsys.readouterr()

    proj = json.loads((tmp_path / ".axiom" / "active.json").read_text())
    glob = json.loads((tmp_path / "global_active.json").read_text())
    for entry in (proj, glob):
        assert entry["sealed"] is True
        assert entry["verdict"] == "PARTIAL"
        assert entry["spec_version_id"] == "spec.v1"
        assert Path(entry["spec_path"]).is_absolute()
        assert Path(entry["run_dir"]).is_absolute()
        assert entry["started_at"]
    assert proj["project_root"] == str(tmp_path.resolve())


def test_run_pointer_preserves_started_at_across_seal(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])
    main(["run", spec, "--run-dir", str(tmp_path / "run"),
          "--project-root", str(tmp_path)])
    capsys.readouterr()
    entry = json.loads((tmp_path / "global_active.json").read_text())
    assert entry["sealed"] is True
    assert entry["started_at"]  # written at start, preserved on seal


# --- continue (no args) ------------------------------------------------------

def test_continue_no_args_lands_on_checkpoint(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])
    main(["run", spec, "--run-dir", str(tmp_path / "run"),
          "--project-root", str(tmp_path)])
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)  # cwd_local -> tmp_path/.axiom/active.json

    rc = main(["continue"])
    assert rc == 4  # PARTIAL, matches verdict
    cap = capsys.readouterr()
    assert "PARTIAL" in cap.err
    assert '"verdict"' in cap.out or '"checkpoint"' in cap.out


def test_continue_no_pointer_exits_2(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)  # no cwd_local, no global -> None
    rc = main(["continue"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "No active axiom run" in err


def test_continue_stale_pointer_exits_2(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])
    main(["run", spec, "--run-dir", str(tmp_path / "run"),
          "--project-root", str(tmp_path)])
    capsys.readouterr()
    # point spec_path at a nonexistent file (stale pointer)
    entry = json.loads((tmp_path / ".axiom" / "active.json").read_text())
    entry["spec_path"] = str(tmp_path / "vanished.json")
    (tmp_path / ".axiom" / "active.json").write_text(
        json.dumps(entry), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # cwd_local reads the stale pointer

    rc = main(["continue"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stale" in err.lower()


def test_continue_explicit_args_bypass_pointer(tmp_path, capsys, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])
    main(["run", spec, "--run-dir", str(tmp_path / "run"),
          "--project-root", str(tmp_path)])
    capsys.readouterr()
    # both --spec and --run-dir given -> _read_active is skipped
    rc = main(["continue", "--spec", spec, "--run-dir", str(tmp_path / "run")])
    assert rc == 4  # PARTIAL


# --- continue --resume -------------------------------------------------------

def test_continue_resume_on_unsealed_redispatches_and_reseals(
        tmp_path, capsys, monkeypatch):
    # An unsealed pointer means the run never reached its end write (crash /
    # interrupt). --resume must re-walk the run, re-seal, and refresh the pointer.
    _no_global(monkeypatch, tmp_path)
    _fake_runner(monkeypatch)
    spec = _spec_file(tmp_path / "spec.json", steps=["n1"])
    rc = main(["run", spec, "--run-dir", str(tmp_path / "run"),
               "--project-root", str(tmp_path)])
    assert rc == 4  # PARTIAL (C1 never verified)
    capsys.readouterr()
    _unseal_both(tmp_path)  # simulate the run being interrupted
    monkeypatch.chdir(tmp_path)  # cwd_local reads the unsealed pointer

    rc = main(["continue", "--resume"])
    assert rc == 4  # still PARTIAL -- re-walked, no new evidence
    cap = capsys.readouterr()
    assert "resumed" in cap.err.lower()
    assert '"resumed":true' in cap.out.lower().replace(" ", "")
    # pointer re-sealed
    entry = json.loads((tmp_path / ".axiom" / "active.json").read_text())
    assert entry["sealed"] is True


def test_continue_resume_on_sealed_run_is_guidance_not_redispatch(
        tmp_path, capsys, monkeypatch):
    # A sealed run has already dispatched everything; --resume must NOT claim
    # to re-dispatch -- it prints sealed guidance instead.
    _no_global(monkeypatch, tmp_path)
    spec = _spec_file(tmp_path / "spec.json", steps=[])
    main(["run", spec, "--run-dir", str(tmp_path / "run"),
          "--project-root", str(tmp_path)])
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)  # cwd_local reads the sealed pointer

    rc = main(["continue", "--resume"])
    assert rc == 4  # PARTIAL guidance
    err = capsys.readouterr().err
    assert "resumed" not in err.lower()
    assert "sealed" in err.lower() or "new run" in err.lower()
