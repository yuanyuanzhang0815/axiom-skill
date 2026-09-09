import json
from pathlib import Path
from axiom.cli import main
from axiom.ir import Spec, Requirement, spec_to_json


def _write_spec(path, nodes=None, steps=None, se=None):
    s = Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=se or ["S1:R1=claim:C1"],
        nodes=nodes or {
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


def test_plan_emits_template(tmp_path, capsys):
    rc = main(["plan", "--out", str(tmp_path / "spec.json"), "--intent", "demo"])
    assert rc == 0
    spec = json.loads((tmp_path / "spec.json").read_text())
    assert spec["intent"] == "demo"
    assert "nodes" in spec and "n1_edit" in spec["nodes"] and "n_verify" in spec["nodes"]
    # the emitted template must itself validate
    rc = main(["validate", str(tmp_path / "spec.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VALID" in out and "contract_hash=" in out


def test_validate_rejects_invalid_spec(tmp_path, capsys):
    bad = {"spec_version_id": "spec.v1", "parent_spec_id": None, "revision": 1,
           "intent": "i", "requirements": [], "boundaries": ["b"],
           "success_evidence": [], "contract_drift": False, "contract_hash_value": "",
           "nodes": {"n1": {"type": "agent", "id": "n1", "prompt": "p"}},
           "control_flow": {"type": "sequence", "steps": ["n1"]}, "decision_trace": [],
           "budget_usd": 5.0, "max_concurrent": 16, "max_agents": 1000, "max_stagnation": 1}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    rc = main(["validate", str(p)])
    assert rc == 1
    assert "INVALID" in capsys.readouterr().err


def test_run_empty_steps_outputs_checkpoint(tmp_path, capsys):
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])  # no dispatch
    rc = main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc == 4  # PARTIAL (C1 never produced)
    out = capsys.readouterr().out
    assert "checkpoint" in out
    assert "verdict" in out


def test_run_executes_agent_with_fake_runner(tmp_path, capsys, monkeypatch):
    def fake_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    monkeypatch.setattr("axiom.harness._default_runner", fake_runner)
    spec_path = _write_spec(tmp_path / "spec.json", steps=["n1"])
    rc = main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    # PARTIAL: agent ran but C1 never verified (no verify node)
    assert rc == 4  # PARTIAL
    out = capsys.readouterr().out
    assert "answer" in out  # validated_output threaded into CLI output


def test_state_shows_progress(tmp_path, capsys):
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    capsys.readouterr()  # drain
    rc = main(["state", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    out = capsys.readouterr().out
    st = json.loads(out)
    assert "progress" in st and "completion" in st


def test_verdict_command(tmp_path, capsys):
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    capsys.readouterr()
    rc = main(["verdict", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc != 0  # not VERIFIED (PARTIAL/UNVERIFIED/BLOCKED)
    assert capsys.readouterr().out.strip() in ("PARTIAL", "UNVERIFIED", "BLOCKED")


def test_checkpoint_command(tmp_path, capsys):
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    capsys.readouterr()
    rc = main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc != 0  # not VERIFIED
    cp = json.loads(capsys.readouterr().out)
    for k in ("verdict", "synthesized_output", "evidence_summary",
              "blocked_items", "drift", "open_questions"):
        assert k in cp


def test_journal_command_prints_events(tmp_path, capsys):
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    capsys.readouterr()
    rc = main(["journal", "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    out = capsys.readouterr().out
    # v2 Task 15: journal prints "CHAIN OK" on success (kind-agnostic chain
    # intact). Empty ledger -> just "CHAIN OK"; non-empty -> events + "CHAIN OK".
    assert "CHAIN OK" in out or "agent_result" in out or "event" in out


def test_gate_list_and_resolve(tmp_path, capsys):
    from axiom.harness import Harness
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v1")
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    capsys.readouterr()
    rc = main(["gate", "list", "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    assert "gate_open" in capsys.readouterr().out
    rc = main(["gate", "resolve", "--gate-id", gid, "--decision", "allow",
               "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    assert "resolved" in capsys.readouterr().out
    # after resolve, gate list shows NO unresolved gates (the resolved one is gone)
    main(["gate", "list", "--run-dir", str(tmp_path / "run")])
    assert gid not in capsys.readouterr().out, \
        "a resolved gate must not appear in `gate list`"


def test_gate_modify_creates_revision(tmp_path, capsys):
    from axiom.harness import Harness
    h = Harness(tmp_path / "run")
    gid = h._gate("contract_drift", "n1", "spec.v1")
    rc = main(["gate", "resolve", "--gate-id", gid, "--decision", "modify",
               "--modify-intent", "revised", "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    assert (tmp_path / "run" / "spec.v2.json").exists()


def test_cli_module_entry_runs_main(tmp_path):
    # Regression (change-assurance F1): `python3 -m axiom.cli` used to import
    # the module and silently exit 0 without calling main() -- a fake success
    # that collided with the assurance philosophy ("command ran" != "verification
    # actually ran"). The __main__ guard must route through main().
    import subprocess
    import sys

    spec_path = _write_spec(tmp_path / "spec.json", steps=[])

    # valid command: must enter main(), print VALID, exit 0
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.cli", "validate", str(spec_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "VALID" in proc.stdout and "contract_hash=" in proc.stdout, proc.stdout

    # invalid: no subcommand -> argparse required=True -> non-zero (NOT silent 0)
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.cli"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, "silent exit 0 would be the F1 regression"


# ---- P-004: _warn_empty_ledger in cmd_checkpoint/verdict/state ----

def test_checkpoint_warns_empty_ledger(tmp_path, capsys):
    # e) run_dir with no events.jsonl -> checkpoint stderr warns
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "empty_run")])
    err = capsys.readouterr().err
    assert "no ledger events" in err, err


def test_checkpoint_no_warn_when_ledger_has_events(tmp_path, capsys, monkeypatch):
    # f) run_dir with non-empty events.jsonl -> no warn. Use a fake runner so
    # `run` actually dispatches n1 and writes an agent_result event (steps=[]
    # writes no events, so the warn would still fire — that's case e, not f).
    def fake_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    monkeypatch.setattr("axiom.harness._default_runner", fake_runner)
    spec_path = _write_spec(tmp_path / "spec.json", steps=["n1"])
    main(["run", spec_path, "--run-dir", str(tmp_path / "run")])
    capsys.readouterr()  # drain run output
    main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "run")])
    err = capsys.readouterr().err
    assert "no ledger events" not in err, err


def test_empty_ledger_warn_pure_stderr_no_exit_change(tmp_path, capsys):
    # g) warn is pure stderr — exit code + JSON stdout unchanged
    spec_path = _write_spec(tmp_path / "spec.json", steps=[])
    rc = main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "empty_run")])
    assert rc != 0  # not VERIFIED (unchanged — warn doesn't alter exit)
    out = capsys.readouterr().out
    cp = json.loads(out)  # stdout JSON still parseable (warn on stderr only)
    assert "verdict" in cp
