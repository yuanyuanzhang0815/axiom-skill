"""Real-pi CODING end-to-end: pi uses TOOLS to write a file, then a verify
skeptic (also pi, tools) reads the file and adjudicates the claim → VERIFIED.

This exercises the full claim lifecycle through real LLM dispatches, not a
mock: agent node produces claim C1 (with evidence — the written file), verify
node dispatches a skeptic through host_adapter --pi --tools that READS the
file and votes {refuted: false}. C1 survives → VERIFIED.

Friction being probed (the point of this run):
  - does pi --tools actually edit the cwd's files when dispatched?
  - does the verify skeptic path work through host_adapter (2nd dispatch)?
  - does glm reliably produce the schema-conforming findings JSON with tools?
  - cost/stall behavior of 2 sequential tool-bearing dispatches.

Cost: 2 real glm calls (coder + skeptic), each ~10-30s with tools. Coding-plan
metered at 0. Skip if pi/auth unavailable (reports friction, exits non-zero).
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec
from axiom.state import derive_verdict, derive_claim_status

RUN = Path(tempfile.mkdtemp(prefix="e2e_picode_"))
DISPATCH_DIR = RUN / "dispatch"
DISPATCH_DIR.mkdir()
RUN_DIR = RUN / "run"
PROJECT_DIR = RUN / "project"   # pi writes calc.py HERE (isolated, not the repo)
PROJECT_DIR.mkdir()

spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e real-pi coding: agent writes calc.py, skeptic verifies it",
    requirements=[Requirement("R1", "calc.py defines double(n) returning n*2",
                               "required")],
    boundaries=["no external agent CLI", "tools: Write/Read only"],
    success_evidence=["S1:R1=claim:C1"],
    nodes={
        "n_coder": {
            "id": "n_coder", "type": "agent",
            "prompt": (
                "Create a file named `calc.py` in the current directory. "
                "It must define a Python function `double(n)` that returns n*2. "
                "Use the Write tool to create it. "
                "After creating the file, respond with ONLY this JSON object "
                "and no other text:\n"
                "{\"findings\": [{\"claim_id\": \"C1\", "
                "\"summary\": \"calc.py defines double(n) returning n*2\"}]}"),
            "acceptance": "calc.py exists with double(n) returning n*2",
            "write_areas": [],
            "output_schema": {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim_id": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                            "required": ["claim_id"],
                        },
                    },
                },
                "required": ["findings"],
            },
            "failure_policy": {"max_retries": 1, "on_exhausted": "block"},
        },
        "n_verify": {
            "id": "n_verify", "type": "verify",
            "target": "{{n_coder.validated_output.findings}}",
            "skeptic_count": 1,
            "skeptic_prompt": (
                "Read the file `calc.py` in the current directory using the Read "
                "tool. Check whether it defines a function `double(n)` that "
                "returns n*2. "
                "Respond with ONLY this JSON object and no other text:\n"
                "{\"refuted\": false, \"evidence_ref\": \"calc.py\"} if the "
                "function is correct, or "
                "{\"refuted\": true, \"evidence_ref\": \"calc.py\"} if it is "
                "missing or wrong."),
            "output_schema": {"type": "object"},
            "allowed_tools": ["Read"],
            "read_areas": [],
            "failure_policy": {"max_retries": 1, "on_exhausted": "block"},
        },
    },
    control_flow={"type": "sequence", "steps": ["n_coder", "n_verify"]},
    decision_trace=[], budget_usd=2.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)
errs = validate_spec(spec)
assert not errs, f"spec invalid: {errs}"

# host_adapter --pi --tools: tools enabled so pi can Write (coder) + Read (skeptic).
adapter = subprocess.Popen(
    [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "host_adapter.py"),
     "--pi", "--tools", "--run-dir", str(DISPATCH_DIR), "--poll-interval", "0.3"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

old_env = dict(os.environ)
os.environ["AXIOM_RUNTIME"] = "host"
os.environ["AXIOM_RUN_DIR"] = str(DISPATCH_DIR)
os.environ["AXIOM_CLI_DISPATCH_TIMEOUT"] = "180"  # 2 tool-bearing dispatches
try:
    h = Harness(RUN_DIR, project_root=PROJECT_DIR, worker_runner=None)
    h.run(spec)
finally:
    os.environ.clear()
    os.environ.update(old_env)

adapter.terminate()
try:
    adapter.wait(timeout=5)
except subprocess.TimeoutExpired:
    adapter.kill()
adapter_err = "\n  ".join(adapter.stderr.read().strip().splitlines())
print(f"adapter stderr:\n  {adapter_err}\n")

events = list(h.ledger.events())
agent_evs = [e for e in events if e.get("kind") == "agent_result"]
verify_evs = [e for e in events if e.get("kind") == "verify_verdict"]
print(f"events: {len(events)} | {len(agent_evs)} agent_result | "
      f"{len(verify_evs)} verify_verdict")

# friction surface: did the 2 dispatches actually flow back?
if not agent_evs:
    print("FRICTION: coder dispatch did not return an agent_result.")
    print("  Check adapter stderr above (auth/tools/glm reliability).")
    sys.exit(1)

# 1. coder produced claim C1 with the findings schema
coder = agent_evs[0]
vo = coder["payload"]["validated_output"]
print(f"  n_coder.validated_output: {json.dumps(vo, ensure_ascii=False)}")
assert vo.get("findings"), f"coder didn't produce findings: {vo}"
c1 = vo["findings"][0]
assert c1.get("claim_id") == "C1", f"wrong claim_id: {c1}"
print(f"  ✅ coder produced claim C1 (strength via agent_result)")

# 2. pi actually wrote calc.py (real tool use, not just text)
calc = PROJECT_DIR / "calc.py"
if not calc.exists():
    print(f"FRICTION: pi did not write {calc} — tools not working via host_adapter?")
    print(f"  (project_dir contents: {list(PROJECT_DIR.iterdir())})")
    sys.exit(1)
content = calc.read_text()
print(f"  calc.py contents:\n    " + "\n    ".join(content.splitlines()))
assert "double" in content and "n*2" in content.replace(" ", ""), \
    f"calc.py doesn't define double(n) returning n*2: {content!r}"
print(f"  ✅ pi --tools wrote calc.py (real tool use through host_adapter)")

# 3. verify skeptic dispatched + adjudicated C1
if not verify_evs:
    print("FRICTION: no verify_verdict — skeptic dispatch did not flow back.")
    sys.exit(1)
vv = verify_evs[0]
print(f"  n_verify.verify_verdict: claim={vv['claim_id']} "
      f"survived={vv['survived']} refuted={vv['refuted']}")
assert vv["claim_id"] == "C1"
assert vv["survived"] is True, f"claim C1 did not survive verify: {vv}"
print(f"  ✅ skeptic read calc.py → refuted=false → C1 survived")

# 4. full lifecycle closed: C1 VERIFIED → verdict VERIFIED
status = derive_claim_status("C1", h.ledger, svid="spec.v1")
v = derive_verdict(spec, h.ledger)
print(f"  claim C1 status: {status} | verdict: {v}")
assert status == "VERIFIED", f"C1 not VERIFIED: {status}"
assert v == "VERIFIED", f"verdict not VERIFIED: {v}"
print(f"  ✅ full lifecycle: agent claim C1 → skeptic survived → VERIFIED")

print("\nE2E PASS: real-pi CODING round-trip → VERIFIED.")
print(f"  2 real glm dispatches through host_adapter --pi --tools:")
print(f"    1. coder (pi) wrote calc.py with double(n) → claim C1")
print(f"    2. skeptic (pi) read calc.py → refuted=false → C1 survived")
print(f"  claim C1 → VERIFIED → requirement R1 met → verdict VERIFIED.")
print(f"  The verify-node path works through host_adapter (2nd dispatch),")
print(f"  and pi --tools edits the cwd's files. Real LLM, zero coupled agent CLI.")
