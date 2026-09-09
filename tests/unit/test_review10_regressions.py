"""R10 (review10) regression tests -- the three acceptance gaps found by
reproducing against a real run:

  R10-1  `axiom verdict` skipped the evidence freshness probe, so a code
         change flipped `axiom checkpoint` to PARTIAL while `axiom verdict`
         kept printing VERIFIED + exit 0. All verdict-reporting entries
         (verdict/state/checkpoint/debug + the in-loop predicate builtin)
         now share the probe.
  R10-2  A reviewer declaring evidence_from whose source never ran still
         produced VERIFIED + evidence_grade=none -- honest labeling, but no
         gate. Declared machine evidence missing now blocks VERIFIED.
  R10-3  A --runtime switch at resume was recorded in the ledger but never
         persisted to run_context.json, so the next no-arg resume silently
         reverted to the original backend.
"""
import json
import os
from pathlib import Path

import axiom.cli as cli
from axiom.cli import main
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, spec_to_json


def _env(value, cost=0.0):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": json.dumps(value), "session_id": "s",
        "total_cost_usd": cost, "permission_denials": [], "usage": {},
    })


def _project(tmp_path):
    """A tiny project: code.py under scope + a checker script."""
    target = tmp_path / "code.py"
    target.write_text("VALUE = 1\n")
    script = tmp_path / "check.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "v = pathlib.Path(sys.argv[1]).read_text()\n"
        "print(json.dumps({'ok': 'VALUE = 1' in v}))\n")
    return target, script


def _evidence_spec(tmp_path, run_reviewer_only=False):
    target, script = _project(tmp_path)
    nodes = {
        "n_check": {
            "type": "script", "id": "n_check",
            "script_path": str(script), "args": [str(target)],
            "output_schema": {"type": "object", "required": ["ok"]},
            "evidence_scope": ["code.py"],
        },
        "n_review": {
            "type": "agent", "id": "n_review", "prompt": "review",
            "dispatch": "host",
            "output_schema": {"type": "object", "required": ["verdict"]},
            "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
            "failure_policy": {}, "verdict_field": "verdict",
            "evidence_from": "n_check",
        },
    }
    steps = ["n_review"] if run_reviewer_only else ["n_check", "n_review"]
    s = Spec(spec_version_id="r10.v1", parent_spec_id=None, revision=1,
             intent="i", requirements=[Requirement("R1", "r", "required")],
             boundaries=["b"], success_evidence=["S1:R1=node:n_review"],
             nodes=nodes,
             control_flow={"type": "sequence", "steps": steps},
             decision_trace=[], budget_usd=50.0, max_concurrent=2,
             max_agents=1000, max_stagnation=1)
    p = tmp_path / "spec.json"
    p.write_text(spec_to_json(s), encoding="utf-8")
    return s, str(p), target


def _fake_runner(args, cwd=None):
    return (0, _env({"verdict": "VERIFIED"}))


# ---- R10-1: verdict shares the freshness probe ----------------------------

def test_verdict_cli_stales_after_code_change(tmp_path, monkeypatch, capsys):
    """checkpoint and verdict must agree: after the scoped code changes,
    BOTH report non-VERIFIED and verdict exits non-zero (was: checkpoint
    PARTIAL, verdict VERIFIED + exit 0)."""
    monkeypatch.setattr("axiom.harness._default_runner", _fake_runner)
    monkeypatch.setenv("AXIOM_RUNTIME", "runner")
    spec, spec_path, target = _evidence_spec(tmp_path)
    run_dir = str(tmp_path / "run")
    rc = main(["run", spec_path, "--run-dir", run_dir,
               "--project-root", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()

    rc = main(["verdict", spec_path, "--run-dir", run_dir])
    assert capsys.readouterr().out.strip() == "VERIFIED"
    assert rc == 0

    target.write_text("VALUE = 2\n")  # code changes after the verdict

    rc = main(["verdict", spec_path, "--run-dir", run_dir])
    out = capsys.readouterr().out.strip()
    assert out != "VERIFIED", \
        "verdict must not keep projecting a stale VERIFIED"
    assert rc != 0, "stale evidence must exit non-zero (was exit 0)"

    rc = main(["checkpoint", spec_path, "--run-dir", run_dir])
    cp = json.loads(capsys.readouterr().out)
    assert cp["verdict"] == out, \
        "verdict and checkpoint must be the SAME projection"


def test_verdict_cli_missing_evidence_blocks(tmp_path, monkeypatch, capsys):
    """R10-2 via CLI: reviewer VERIFIED with declared evidence missing ->
    `axiom verdict` is non-VERIFIED with non-zero exit (was VERIFIED+0)."""
    monkeypatch.setattr("axiom.harness._default_runner", _fake_runner)
    monkeypatch.setenv("AXIOM_RUNTIME", "runner")
    spec, spec_path, _ = _evidence_spec(tmp_path, run_reviewer_only=True)
    run_dir = str(tmp_path / "run")
    rc = main(["run", spec_path, "--run-dir", run_dir,
               "--project-root", str(tmp_path)])
    assert rc != 0, "run must not exit VERIFIED without the declared evidence"
    capsys.readouterr()

    rc = main(["verdict", spec_path, "--run-dir", run_dir])
    out = capsys.readouterr().out.strip()
    assert out == "PARTIAL"
    assert rc == 4

    rc = main(["checkpoint", spec_path, "--run-dir", run_dir])
    cp = json.loads(capsys.readouterr().out)
    assert cp["verdict"] == "PARTIAL"
    assert cp["evidence_grade"] == "none"  # honestly graded, still gated
    statuses = [q.get("status") for q in cp["open_questions"]]
    assert "agent_verdict_evidence_missing" in statuses, statuses


# ---- R10-2: the projection gate itself ------------------------------------

def test_declared_evidence_missing_never_projects_verified(tmp_path):
    """Harness-level: evidence_from declared, source never ran, reviewer
    returns VERIFIED -> checkpoint PARTIAL, not VERIFIED."""
    spec, _, _ = _evidence_spec(tmp_path, run_reviewer_only=True)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    av = [e for e in h.ledger.events() if e["kind"] == "agent_verdict"]
    assert av[0]["payload"]["evidence_grade"] == "none"
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] != "VERIFIED", \
        "declared machine evidence missing must block VERIFIED"
    assert cp["evidence_grade"] == "none"


def test_declared_evidence_missing_blocks_without_probe(tmp_path):
    """The missing-evidence gate is ledger-pure: it holds even when the
    caller passes no freshness probe (unlike the staleness check)."""
    from axiom.state import derive_verdict
    spec, _, _ = _evidence_spec(tmp_path, run_reviewer_only=True)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    assert derive_verdict(spec, h.ledger) != "VERIFIED"


def test_stale_evidence_reports_stale_status(tmp_path):
    """Diagnostic: a code change after the verdict is named
    agent_verdict_evidence_stale (distinct from never-having-had-evidence)."""
    spec, _, target = _evidence_spec(tmp_path)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    assert h.project_checkpoint(spec)["verdict"] == "VERIFIED"
    target.write_text("VALUE = 2\n")
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "PARTIAL"
    statuses = [q.get("status") for q in cp["open_questions"]]
    assert "agent_verdict_evidence_stale" in statuses, statuses


# ---- R10-3: backend switch persists ---------------------------------------

def _ctx_with_backend(tmp_path, backend="pi"):
    rd = tmp_path / "run"
    rd.mkdir()
    cli._write_run_context(str(rd), project_root=str(tmp_path),
                           runtime_backend={"backend": backend,
                                            "protocol": "host",
                                            "location": "local",
                                            "capabilities": ["cost"]})
    return rd


def test_backend_switch_persists_to_run_context(monkeypatch, tmp_path):
    """--runtime cc over recorded pi: the NEXT no-arg resume must
    inherit cc (the switch is durable), not silently revert to pi."""
    rd = _ctx_with_backend(tmp_path, backend="pi")
    monkeypatch.setattr(cli, "_spawn_host", lambda *a, **k: None)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/x")
    monkeypatch.delenv("AXIOM_RUNTIME", raising=False)

    class _S:
        spec_version_id = "s.v1"

    rec = cli._resume_backend(str(rd), spec=_S(),
                              requested_runtime="cc")
    assert rec["name"] == "cc"
    ctx = cli._read_run_context(str(rd))
    assert ctx["runtime_backend"]["backend"] == "cc", \
        "the switch must persist into run_context.json"

    # next no-arg resume: recorded backend wins, no new switch event
    before = (rd / "events.jsonl").read_text()
    rec2 = cli._resume_backend(str(rd), spec=_S())
    assert rec2["name"] == "cc"
    after = (rd / "events.jsonl").read_text()
    assert before == after, "no-arg resume after a switch must not re-switch"


def test_backend_switch_preserves_other_context_fields(tmp_path):
    """The targeted backend update must not clobber adoption/wiki fields
    (why _write_run_context could not be reused for this)."""
    rd = tmp_path / "run"
    rd.mkdir()
    cli._write_run_context(
        str(rd), project_root=str(tmp_path), auto_wiki=True,
        wiki_dir=str(tmp_path / "wiki"),
        adopted_from=["w:abc"], adopted_from_svid="s.v1",
        runtime_backend={"backend": "pi", "protocol": "host",
                         "location": "local", "capabilities": ["cost"]})
    cli._persist_run_context_backend(
        str(rd), {"backend": "cc", "protocol": "host",
                  "location": "local", "capabilities": ["cost", "tools"]})
    ctx = cli._read_run_context(str(rd))
    assert ctx["runtime_backend"]["backend"] == "cc"
    assert ctx["auto_wiki"] is True
    assert ctx["wiki_dir"] == str(tmp_path / "wiki")
    assert ctx["adopted_from"] == ["w:abc"]
    assert ctx["adopted_from_svid"] == "s.v1"
    assert ctx["project_root"] == str(Path(tmp_path).resolve())


def test_explicit_runtime_on_unrecorded_run_is_adopted(monkeypatch, tmp_path):
    """Pre-R9-2 run-dir (no recorded backend) + explicit --runtime: the
    choice becomes the run's persisted fact, so the next no-arg resume does
    not re-resolve from the environment."""
    rd = tmp_path / "run"
    rd.mkdir()
    cli._write_run_context(str(rd), project_root=str(tmp_path))
    monkeypatch.setattr(cli, "_spawn_host", lambda *a, **k: None)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/x")

    class _S:
        spec_version_id = "s.v1"

    rec = cli._resume_backend(str(rd), spec=_S(), requested_runtime="pi")
    assert rec["name"] == "pi"
    ctx = cli._read_run_context(str(rd))
    assert ctx["runtime_backend"]["backend"] == "pi"
    # no recorded 'from' -> no switch event (nothing to switch FROM)
    assert not (rd / "events.jsonl").exists()
