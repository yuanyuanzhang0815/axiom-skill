"""R9-3 (review9 P1): machine-generated acceptance evidence.

A script node declaring evidence_scope gets its execution recorded as a
machine evidence pack (command, exit code, output, scoped-file fingerprint,
git HEAD). A reviewer declaring evidence_from receives that pack in its
prompt and binds its verdict to the evidence fingerprint. Code changes after
the verdict STALE the evidence -- the verdict no longer projects VERIFIED.
No hand-written receipt.json; the machine records, the agent judges.

Covered:
  - script run emits an evidence_pack with the fingerprint + command + exit
  - reviewer prompt receives the pack; its verdict records evidence_id
  - verdict projects VERIFIED while the code is unchanged
  - a code change STALES the verdict -> PARTIAL, no silent inheritance
  - evidence_from with no pack -> evidence_grade=none (honest degradation,
    never a bare VERIFIED that silently means machine-checked)
"""
import json
from pathlib import Path

from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(value, cost=0.0):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": json.dumps(value), "session_id": "s",
        "total_cost_usd": cost, "permission_denials": [], "usage": {},
    })


def _reviewer(nid, evidence_from=None):
    n = {
        "type": "agent", "id": nid, "prompt": "review the evidence",
        "dispatch": "host",
        "output_schema": {"type": "object", "required": ["verdict"]},
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {}, "verdict_field": "verdict",
    }
    if evidence_from:
        n["evidence_from"] = evidence_from
    return n


def _spec(script_node, reviewer, tmp_path):
    return Spec(spec_version_id="ev.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=node:n_review"],
                nodes={script_node["id"]: script_node, reviewer["id"]: reviewer},
                control_flow={"type": "sequence",
                              "steps": [script_node["id"], reviewer["id"]]},
                decision_trace=[], budget_usd=50.0, max_concurrent=2,
                max_agents=1000, max_stagnation=1)


def _script_node(tmp_path, scope):
    target = tmp_path / "code.py"
    target.write_text("VALUE = 1\n")
    script = tmp_path / "check.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "v = pathlib.Path(sys.argv[1]).read_text()\n"
        "print(json.dumps({'ok': 'VALUE = 1' in v}))\n")
    return {
        "type": "script", "id": "n_check",
        "script_path": str(script),
        "args": [str(target)],
        "output_schema": {"type": "object", "required": ["ok"]},
        "evidence_scope": scope,
    }, target


def test_script_emits_evidence_pack(tmp_path):
    script_node, _ = _script_node(tmp_path, ["code.py"])
    reviewer = _reviewer("n_review", evidence_from="n_check")
    spec = _spec(script_node, reviewer, tmp_path)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    packs = [e for e in h.ledger.events() if e["kind"] == "evidence_pack"]
    assert len(packs) == 1, "script with evidence_scope must emit one pack"
    p = packs[0]["payload"]
    assert p["exit_code"] == 0 and p["failure_class"] is None
    assert p["evidence_id"] and "code.py" in p["files"]
    assert "check.py" in p["command"]


def test_verdict_binds_evidence_and_projects_verified(tmp_path):
    script_node, _ = _script_node(tmp_path, ["code.py"])
    reviewer = _reviewer("n_review", evidence_from="n_check")
    spec = _spec(script_node, reviewer, tmp_path)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    av = [e for e in h.ledger.events() if e["kind"] == "agent_verdict"]
    assert len(av) == 1
    assert av[0]["payload"]["evidence_grade"] == "machine"
    assert av[0]["payload"]["evidence_id"]
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "VERIFIED", cp.get("open_questions")
    assert cp["evidence_grade"] == "machine"


def test_code_change_stales_the_verdict(tmp_path):
    script_node, target = _script_node(tmp_path, ["code.py"])
    reviewer = _reviewer("n_review", evidence_from="n_check")
    spec = _spec(script_node, reviewer, tmp_path)
    h = Harness(tmp_path / "run", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    assert h.project_checkpoint(spec)["verdict"] == "VERIFIED"
    # code changes AFTER the verdict -> the evidence fingerprint no longer
    # matches -> the verdict must NOT keep projecting VERIFIED.
    target.write_text("VALUE = 2\n")
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] != "VERIFIED", \
        "a verdict bound to stale evidence must not survive a code change"


def test_reviewer_receives_evidence_in_prompt(tmp_path):
    script_node, _ = _script_node(tmp_path, ["code.py"])
    reviewer = _reviewer("n_review", evidence_from="n_check")
    spec = _spec(script_node, reviewer, tmp_path)
    seen_prompts = []

    def runner(args, cwd=None):
        seen_prompts.append(args[0] if len(args) > 0 else "")
        return (0, _env({"verdict": "VERIFIED"}))

    h = Harness(tmp_path / "run", project_root=tmp_path, worker_runner=runner)
    h.run(spec)
    assert any("MACHINE EVIDENCE" in p and "exit_code" in p
               for p in seen_prompts), \
        "reviewer prompt must carry the machine evidence block"


def test_evidence_from_without_pack_is_grade_none(tmp_path):
    """A reviewer declaring evidence_from whose source exists in the spec
    but never RAN gets an honestly-graded judgment (none) -- never a bare
    VERIFIED silently meaning machine-checked."""
    script_node, _ = _script_node(tmp_path, ["code.py"])
    reviewer = _reviewer("n_review", evidence_from="n_check")
    # The script node EXISTS in the spec (validate passes) but the sequence
    # never executes it -> no evidence pack is produced at run time.
    spec = Spec(spec_version_id="ev.v2", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=node:n_review"],
                nodes={script_node["id"]: script_node, reviewer["id"]: reviewer},
                control_flow={"type": "sequence", "steps": [reviewer["id"]]},
                decision_trace=[], budget_usd=50.0, max_concurrent=2,
                max_agents=1000, max_stagnation=1)
    h = Harness(tmp_path / "run2", project_root=tmp_path,
                worker_runner=lambda *a, **k: (0, _env({"verdict": "VERIFIED"})))
    h.run(spec)
    av = [e for e in h.ledger.events() if e["kind"] == "agent_verdict"]
    assert av[0]["payload"]["evidence_grade"] == "none"
    assert av[0]["payload"]["evidence_id"] is None
    cp = h.project_checkpoint(spec)
    # The verdict value CAN still be VERIFIED (the reviewer judged), but the
    # checkpoint must honestly show it rests on no machine evidence.
    assert cp["evidence_grade"] == "none"
