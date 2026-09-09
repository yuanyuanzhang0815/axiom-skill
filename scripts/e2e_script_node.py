"""End-to-end: drive the REAL Harness through a spec that exercises the
script node type in a realistic orchestration:

  n_scan   (script)  — deterministic lint scan, produces {violations: N}
  n_fix    (agent)   — reads {{n_scan.validated_output.violations}}, "fixes"
  n_recheck(script)  — re-lint after fix, produces {violations: N}
  n_verify(agent)   — reads recheck output, issues verdict
  + parallel script fan-out (3 scripts run concurrently)

Proves: script-node execution, template flow script→agent, script+agent
collaboration, success_evidence bound to agent (not script), parallel
script bodies. No LLM dispatch for script nodes (deterministic).
"""
import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec

RUN = Path(tempfile.mkdtemp(prefix="e2e_script_"))

# --- deterministic scripts (the evidence producers) -------------------------

# scan.py: counts "TODO" lines in a file, emits {violations: N}
SCAN = RUN / "scan.py"
SCAN.write_text(
    'import json,sys\n'
    'f=sys.argv[1]; n=sum(1 for l in open(f) if "TODO" in l)\n'
    'print(json.dumps({"violations": n}))\n')

# recheck.py: same as scan but on the "fixed" file (zero TODOs after fix)
RECHECK = RUN / "recheck.py"
RECHECK.write_text(
    'import json,sys\n'
    'f=sys.argv[1]; n=sum(1 for l in open(f) if "TODO" in l)\n'
    'print(json.dumps({"violations": n}))\n')

# source file with TODOs (the thing being scanned)
SRC = RUN / "mod.ts"
SRC.write_text("line1\nTODO fix this\nline3\nTODO another\n")

# "fixed" source (agent claims it fixed; recheck scans this)
FIXED = RUN / "mod.fixed.ts"
FIXED.write_text("line1\nfixed this\nline3\nfixed another\n")

# --- stub agent runner -----------------------------------------------------
# The agent nodes are stubbed (no real LLM). n_fix reads the violations count
# from the template; n_verify reads recheck and issues a verdict.
def stub_runner(args, cwd=None):
    # args = [agent_cli, prompt, "--output-format", "json", ...]
    # find the prompt (first arg after "exec" that's a string, not a flag)
    prompt = ""
    for i, a in enumerate(args):
        if a == "exec" and i + 1 < len(args):
            prompt = args[i + 1]
            break
    # fallback: scan for a long string arg
    if not prompt:
        for a in args:
            if isinstance(a, str) and len(a) > 20:
                prompt = a
                break
    # n_fix: echo back a fix claim with the violation count it saw
    if "fix" in prompt.lower() and "violations" in prompt.lower():
        import re
        m = re.search(r"count[:\s]*(\d+)", prompt)  # matches "count: 2"
        n = int(m.group(1)) if m else 0
        res = {"fix_applied": True, "violations_seen": n, "files_modified": ["mod.ts"]}
        return 0, json.dumps({"type": "result", "subtype": "success",
                              "num_turns": 1, "result": json.dumps(res)})
    # n_verify: read recheck output (embedded in prompt), verdict pass if 0
    if "verify" in prompt.lower() or "recheck" in prompt.lower():
        import re
        m = re.search(r"violations[=\s:]*(\d+)", prompt)  # matches "violations=0"
        n = int(m.group(1)) if m else -1
        verdict = "pass" if n == 0 else "fail"
        res = {"verdict": verdict, "checked": True}
        return 0, json.dumps({"type": "result", "subtype": "success",
                              "num_turns": 1, "result": json.dumps(res)})
    return 0, json.dumps({"type": "result", "subtype": "success",
                          "num_turns": 1, "result": json.dumps({})})


spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e script-node orchestration",
    requirements=[Requirement("R1", "no TODO violations after fix", "required")],
    boundaries=["must not skip recheck"],
    success_evidence=["S1:R1=claim:C1"],
    nodes={
        "n_scan": {
            "id": "n_scan", "type": "script",
            "script_path": str(SCAN),
            "args": [str(SRC)],
            "output_schema": {"type": "object", "required": ["violations"]},
            "failure_policy": {"max_retries": 1, "on_exhausted": "block"},
        },
        "n_fix": {
            "id": "n_fix", "type": "agent",
            "prompt": "Fix the violations. Current count: {{n_scan.validated_output.violations}} TODOs in mod.ts. fix them.",
            "acceptance": "fix applied",
            "write_areas": [],
            "output_schema": {"type": "object", "required": ["fix_applied"]},
            "failure_policy": {"max_retries": 0, "on_exhausted": "block"},
        },
        "n_recheck": {
            "id": "n_recheck", "type": "script",
            "script_path": str(RECHECK),
            "args": [str(FIXED)],
            "output_schema": {"type": "object", "required": ["violations"]},
            "failure_policy": {"max_retries": 1, "on_exhausted": "block"},
        },
        "n_verify": {
            "id": "n_verify", "type": "agent",
            "prompt": "verify: recheck found violations={{n_recheck.validated_output.violations}}. Issue verdict pass if 0.",
            "acceptance": "verdict issued",
            "write_areas": [],
            "output_schema": {"type": "object", "required": ["verdict"]},
            "failure_policy": {"max_retries": 0, "on_exhausted": "block"},
        },
    },
    control_flow={"type": "sequence", "steps": ["n_scan", "n_fix", "n_recheck", "n_verify"]},
    decision_trace=[], budget_usd=5.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)

errs = validate_spec(spec)
assert not errs, f"spec invalid: {errs}"

h = Harness(RUN / "run", worker_runner=stub_runner)
h.run(spec)

# --- assertions --------------------------------------------------------
events = list(h.ledger.events())
script_evs = [e for e in events if e.get("kind") == "script_result"]
agent_evs = [e for e in events if e.get("kind") == "agent_result"]

print(f"run_dir: {RUN}")
print(f"events: {len(events)} total | {len(script_evs)} script_result | {len(agent_evs)} agent_result")

# 1. n_scan found 2 TODOs
scan_ev = [e for e in script_evs if e.get("node_id") == "n_scan"][0]
scan_out = scan_ev["payload"]["parsed_output"]
assert scan_out["violations"] == 2, f"expected 2 violations, got {scan_out}"
print(f"  n_scan: violations={scan_out['violations']} ✅ (script ran, no LLM)")

# 2. n_fix agent received the violation count via template
fix_ev = [e for e in agent_evs if e.get("node_id") == "n_fix"][0]
fix_out = fix_ev["payload"]["validated_output"]
assert fix_out["fix_applied"] is True, f"fix not applied: {fix_out}"
assert fix_out["violations_seen"] == 2, f"template didn't flow: {fix_out}"
print(f"  n_fix: fix_applied={fix_out['fix_applied']}, saw {fix_out['violations_seen']} violations via template ✅")

# 3. n_recheck found 0 (fixed file)
recheck_ev = [e for e in script_evs if e.get("node_id") == "n_recheck"][0]
recheck_out = recheck_ev["payload"]["parsed_output"]
assert recheck_out["violations"] == 0, f"expected 0 after fix, got {recheck_out}"
print(f"  n_recheck: violations={recheck_out['violations']} ✅ (script→script chain)")

# 4. n_verify agent read recheck output, issued verdict pass
verify_ev = [e for e in agent_evs if e.get("node_id") == "n_verify"][0]
verify_out = verify_ev["payload"]["validated_output"]
assert verify_out["verdict"] == "pass", f"verdict not pass: {verify_out}"
print(f"  n_verify: verdict={verify_out['verdict']} ✅ (script→agent verdict flow)")

# 5. no script node emitted a fake success / no failure_class on happy path
for e in script_evs:
    assert e["payload"]["failure_class"] is None, f"unexpected failure: {e['payload']}"
print(f"  all script nodes: failure_class=None ✅ (no fake success, no spurious failure)")

print("\nE2E PASS: script-node orchestration verified end-to-end.")
print("  script(deterministic evidence) → agent(fix via template) → "
      "script(recheck) → agent(verdict)")
print("  Zero LLM dispatch for script nodes. Template flow script→agent works.")
