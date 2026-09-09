"""R9-2 (review9 P1): unified backend entry tests.

The backend is a persisted, three-axis choice -- WHO executes (pi / cc),
WHERE (location), WHAT IT CAN DO (capabilities) -- recorded in run_context.json
at run start and honored across resume. Previously resume silently re-resolved
the runtime from the caller's environment with NO host side spawned (a pi/cc
run resumed into a timeout).

Covered:
  - run --runtime pi records runtime_backend and spawns the adapter
  - resume inherits the RECORDED backend (no env re-resolution)
  - an explicit different --runtime on resume is a ledger-recorded switch
  - a backend whose binary is missing fails BEFORE the run starts
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import axiom.cli as cli
from axiom.cli import _resolve_backend, _read_run_context


def test_resolve_backend_rejects_unknown():
    try:
        _resolve_backend("does-not-exist")
    except ValueError as e:
        assert "unknown runtime backend" in str(e)
    else:
        raise AssertionError("unknown backend must be rejected")


def test_resolve_backend_pi_requires_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: None)
    try:
        _resolve_backend("pi")
    except RuntimeError as e:
        assert "requires 'pi' on PATH" in str(e)
        assert "NOT started" in str(e)
    else:
        raise AssertionError("missing pi binary must fail before the run")


def test_spawn_host_pi_invokes_adapter_preset(monkeypatch):
    """_spawn_host('pi') must launch host_adapter.py --pi."""
    spawned = {}

    class _P:
        pid = 4242

    def _fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return _P()
    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    cli._spawn_host("pi", "/tmp/rd", 1234)
    cmd = spawned["cmd"]
    assert cmd[1].endswith("host_adapter.py") and "--pi" in cmd, cmd


def test_spawn_host_runner_is_none():
    """The runner path (test/direct-subprocess mode) spawns no host adapter."""
    assert cli._spawn_host("runner", "/tmp/rd", 1234) is None


def _ctx_with_backend(tmp_path, backend="pi"):
    rd = tmp_path / "run"
    rd.mkdir()
    cli._write_run_context(str(rd), project_root=str(tmp_path),
                           runtime_backend={"backend": backend,
                                            "protocol": "host",
                                            "location": "local",
                                            "capabilities": ["cost"]})
    return rd


def test_resume_inherits_recorded_backend(monkeypatch, tmp_path):
    """Resume must use the RECORDED backend, not the caller's env/default."""
    rd = _ctx_with_backend(tmp_path, backend="pi")
    spawned = []
    monkeypatch.setattr(cli, "_spawn_host",
                        lambda name, run_dir, pid, enable_tools=False: spawned.append(name))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/pi")
    monkeypatch.delenv("AXIOM_RUNTIME", raising=False)
    rec = cli._resume_backend(str(rd), spec=None)
    assert rec["name"] == "pi" and spawned == ["pi"]
    assert os.environ.get("AXIOM_RUNTIME") == "host"  # file protocol


def test_resume_switch_is_ledger_recorded(monkeypatch, tmp_path):
    """--runtime cc over a recorded pi backend -> backend_switch event."""
    rd = _ctx_with_backend(tmp_path, backend="pi")
    monkeypatch.setattr(cli, "_spawn_host", lambda *a, **k: None)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/x")
    class _S:
        spec_version_id = "s.v1"
    rec = cli._resume_backend(str(rd), spec=_S(),
                              requested_runtime="cc")
    assert rec["name"] == "cc"
    events = [json.loads(x)
              for x in (rd / "events.jsonl").read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert "backend_switch" in kinds
    ev = [e for e in events if e["kind"] == "backend_switch"][0]
    assert ev["payload"]["from"] == "pi" and ev["payload"]["to"] == "cc"


def test_resume_same_runtime_is_not_a_switch(monkeypatch, tmp_path):
    rd = _ctx_with_backend(tmp_path, backend="cc")
    monkeypatch.setattr(cli, "_spawn_host", lambda *a, **k: None)
    rec = cli._resume_backend(str(rd), spec=None,
                              requested_runtime="cc")
    assert rec["name"] == "cc"
    assert not (rd / "events.jsonl").exists(), \
        "same backend must not record a switch"


def test_run_context_backend_survives_merge_prior(tmp_path):
    """A resume-time context rewrite (merge_prior=True, no new backend) must
    keep the run's recorded backend, not wipe it."""
    rd = _ctx_with_backend(tmp_path, backend="cc")
    cli._write_run_context(str(rd), project_root=str(tmp_path),
                           merge_prior=True)
    ctx = _read_run_context(str(rd))
    assert ctx["runtime_backend"]["backend"] == "cc"
