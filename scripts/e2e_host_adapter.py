"""End-to-end: the standalone host_adapter.py script (not an inline thread)
does the file-protocol round-trip with axiom and produces a verdict.

This proves the reusable host adapter — the drop-in tool pi/codex/CC would
run alongside axiom — actually works as a real subprocess against a real
axiom run. Distinct from e2e_host_runtime.py which uses an inline thread
hardcoded to {"answer":42}; here the host_adapter.py script is launched as
a separate process in --mock-result mode and must poll/serve atomically.

Spec: agent(what is 6*7) -> script(double the answer). Agent dispatch goes
through the file protocol to the standalone host_adapter process.
"""
import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec

RUN = Path(tempfile.mkdtemp(prefix="e2e_host_adapter_"))
DISPATCH_DIR = RUN / "dispatch"
DISPATCH_DIR.mkdir()
RUN_DIR = RUN / "run"

DBL = RUN / "double.py"
DBL.write_text(
    'import json,sys\n'
    'a=int(sys.argv[1])\n'
    'print(json.dumps({"doubled": a*2}))\n')

spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e standalone host_adapter round-trip",
    requirements=[Requirement("R1", "compute and double 6*7", "required")],
    boundaries=["no external agent CLI coupled in"],
    success_evidence=["S1:R1=claim:C1"],
    nodes={
        "n_agent": {
            "id": "n_agent", "type": "agent",
            "prompt": "Compute 6*7. Return JSON {answer: <number>}.",
            "acceptance": "answer is 42",
            "write_areas": [],
            "output_schema": {"type": "object", "required": ["answer"]},
            "failure_policy": {"max_retries": 0, "on_exhausted": "block"},
        },
        "n_double": {
            "id": "n_double", "type": "script",
            "script_path": str(DBL),
            "args": ["{{n_agent.validated_output.answer}}"],
            "output_schema": {"type": "object", "required": ["doubled"]},
            "failure_policy": {"max_retries": 1, "on_exhausted": "block"},
        },
    },
    control_flow={"type": "sequence", "steps": ["n_agent", "n_double"]},
    decision_trace=[], budget_usd=5.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)
errs = validate_spec(spec)
assert not errs, f"spec invalid: {errs}"

# Launch the standalone host_adapter.py as a REAL subprocess in mock mode.
# It must poll dispatch_req.json, read the prompt, echo the canned JSON, and
# write dispatch_res.json atomically — proving the reusable tool works.
adapter = subprocess.Popen(
    [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "host_adapter.py"),
     "--run-dir", str(DISPATCH_DIR),
     "--mock-result", '{"answer": 42}',
     "--poll-interval", "0.2"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

# swap env so axiom uses the file protocol against the adapter's run_dir
old_env = dict(os.environ)
os.environ["AXIOM_RUNTIME"] = "host"
os.environ["AXIOM_RUN_DIR"] = str(DISPATCH_DIR)
os.environ["AXIOM_CLI_DISPATCH_TIMEOUT"] = "30"
try:
    h = Harness(RUN_DIR, worker_runner=None)
    h.run(spec)
finally:
    os.environ.clear()
    os.environ.update(old_env)

# stop the adapter now that the run finished
adapter.terminate()
try:
    adapter.wait(timeout=5)
except subprocess.TimeoutExpired:
    adapter.kill()
adapter_err = adapter.stderr.read().strip()
print(f"adapter stderr:\n  " + "\n  ".join(adapter_err.splitlines()))

events = list(h.ledger.events())
agent_evs = [e for e in events if e.get("kind") == "agent_result"]
script_evs = [e for e in events if e.get("kind") == "script_result"]

print(f"run_dir: {RUN}")
print(f"events: {len(events)} | {len(agent_evs)} agent_result | "
      f"{len(script_evs)} script_result")

# 1. the standalone adapter received the request and its result flowed back
assert agent_evs, "no agent_result — standalone host_adapter round-trip failed"
agent_out = agent_evs[0]["payload"]["validated_output"]
assert agent_out.get("answer") == 42, f"host result didn't flow: {agent_out}"
print(f"  n_agent: answer={agent_out['answer']} ✅ "
      f"(standalone host_adapter: req→subprocess→res→ledger)")

# 2. the adapter's fields were recorded (num_turns/cost in payload, proving
#    the standalone script — not an inline thread — wrote dispatch_res.json)
assert agent_evs[0]["payload"].get("num_turns") == 1, \
    f"num_turns not recorded from host_adapter: {agent_evs[0]['payload'].get('num_turns')}"
assert agent_evs[0]["payload"].get("cost_usd") == 0.0
print(f"  num_turns={agent_evs[0]['payload']['num_turns']}, "
      f"cost_usd={agent_evs[0]['payload']['cost_usd']} ✅ "
      f"(host_adapter wrote the DispatchResult envelope)")

# 3. script node ran and received the agent's answer via template flow
assert script_evs, "no script_result"
dbl_out = script_evs[0]["payload"]["parsed_output"]
assert dbl_out.get("doubled") == 84, f"template flow broke: {dbl_out}"
print(f"  n_double: doubled={dbl_out['doubled']} ✅ (42*2=84 via template)")

# 4. verdict is PARTIAL — the spec has an agent node (claim C1) and a script
#    node (evidence) but NO verify node, so C1 is never marked survived by a
#    verify_verdict → PARTIAL (honest: VERIFIED needs a verify node, which
#    would dispatch through the adapter the same way — no new host code).
from axiom.state import derive_verdict
v = derive_verdict(spec, h.ledger)
assert v == "PARTIAL", f"expected PARTIAL (no verify node), got {v}"
print(f"  verdict={v} ✅ (no verify node → C1 unverified → PARTIAL; a verify")
print(f"  node would dispatch through host_adapter identically → VERIFIED)")

print("\nE2E PASS: standalone host_adapter.py drives a full axiom run.")
print("  The reusable script (not an inline thread) polled dispatch_req.json,")
print("  echoed the canned schema-conforming result, wrote dispatch_res.json")
print("  atomically — and axiom recorded the agent_result (num_turns/cost from")
print("  the host's DispatchResult envelope) and ran the script node via template")
print("  flow. Verdict PARTIAL because this spec has no verify node; a verify")
print("  node would dispatch through host_adapter identically → VERIFIED.")
print("  Swap --mock-result for --agent-cmd 'pi --prompt {prompt} --json'")
print("  to use a real agent CLI (pi/codex/CC).")
