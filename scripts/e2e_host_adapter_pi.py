"""Real-pi end-to-end: axiom dispatches through host_adapter.py --pi to a
LIVE provider model (the configured pi model), not a mock.

This is the friction-eliminated real verification: host_adapter sources
~/.zshrc so DASHSCOPE_API_KEY loads, parses pi's --mode json NDJSON into
result_text/cost/turns, and axiom records the agent_result + verifies.

Skip condition: if DASHSCOPE_API_KEY isn't loadable or pi can't reach the
provider, the test reports the friction and exits non-zero rather than
fabricating a pass.

Cost: one tiny glm call (~free, coding-plan metered at 0). No tools.
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec
from axiom.state import derive_verdict

RUN = Path(tempfile.mkdtemp(prefix="e2e_pi_"))
DISPATCH_DIR = RUN / "dispatch"
DISPATCH_DIR.mkdir()
RUN_DIR = RUN / "run"

# Spec: an agent node that must return {answer: <6*7>}. pi answers it.
# No script node — keep the real-LLM run to ONE dispatch (cheapest).
spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e real-pi: axiom dispatches to a live glm via host_adapter --pi",
    requirements=[Requirement("R1", "compute 6*7 via the agent", "required")],
    boundaries=["no external agent CLI", "no tools (pure text answer)"],
    success_evidence=["S1:R1=claim:C1"],
    nodes={
        "n_agent": {
            "id": "n_agent", "type": "agent",
            "prompt": "Compute 6*7. Reply with ONLY the JSON object "
                      "{\"answer\": <number>} and nothing else.",
            "acceptance": "answer is 42",
            "write_areas": [],
            "output_schema": {"type": "object", "required": ["answer"]},
            "failure_policy": {"max_retries": 0, "on_exhausted": "block"},
        },
    },
    control_flow={"type": "sequence", "steps": ["n_agent"]},
    decision_trace=[], budget_usd=2.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)
errs = validate_spec(spec)
assert not errs, f"spec invalid: {errs}"

# Launch host_adapter --pi as a real subprocess. It sources ~/.zshrc (auth),
# parses pi-json NDJSON, invokes glm via alibaba-cloud.
adapter = subprocess.Popen(
    [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "host_adapter.py"),
     "--pi", "--run-dir", str(DISPATCH_DIR), "--poll-interval", "0.3"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

old_env = dict(os.environ)
os.environ["AXIOM_RUNTIME"] = "host"
os.environ["AXIOM_RUN_DIR"] = str(DISPATCH_DIR)
os.environ["AXIOM_CLI_DISPATCH_TIMEOUT"] = "120"  # glm can take 10-20s
try:
    h = Harness(RUN_DIR, worker_runner=None)
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
print(f"adapter stderr:\n  {adapter_err}")

events = list(h.ledger.events())
agent_evs = [e for e in events if e.get("kind") == "agent_result"]

if not agent_evs:
    print("\nFRICTION: no agent_result — pi dispatch did not flow back.")
    print("  Check adapter stderr above for auth/connection errors.")
    print(f"  (DASHSCOPE_API_KEY must be in ~/.zshrc; glm must be reachable)")
    sys.exit(1)

agent_out = agent_evs[0]["payload"]["validated_output"]
print(f"\nevents: {len(events)} | agent_result=1")
print(f"  n_agent.validated_output: {agent_out}")
print(f"  num_turns={agent_evs[0]['payload'].get('num_turns')}, "
      f"cost_usd={agent_evs[0]['payload'].get('cost_usd')}")

# 1. the live glm answered 42 (real model output, not canned)
assert agent_out.get("answer") == 42, \
    f"pi/glm did not answer 42: {agent_out}"
print(f"  ✅ LIVE glm returned answer=42 (real model output, not a mock)")

# 2. host_adapter parsed pi's NDJSON (num_turns from turn_start events)
assert agent_evs[0]["payload"].get("num_turns", 0) >= 1, \
    f"num_turns not parsed from pi NDJSON: {agent_evs[0]['payload'].get('num_turns')}"
print(f"  ✅ host_adapter parsed pi --mode json NDJSON "
      f"(num_turns={agent_evs[0]['payload']['num_turns']})")

# 3. result_text_preview is the real glm JSON (not canned)
preview = agent_evs[0]["payload"].get("result_text_preview", "")
assert "42" in preview and "answer" in preview, \
    f"result_text_preview not the glm answer: {preview}"
print(f"  ✅ result_text from live glm: {preview!r}")

# 4. verdict PARTIAL (no verify node; C1 unverified) — honest, VERIFIED needs
#    a verify node which would dispatch through host_adapter --pi identically
v = derive_verdict(spec, h.ledger)
assert v == "PARTIAL", f"expected PARTIAL, got {v}"
print(f"  ✅ verdict={v} (no verify node → C1 unverified; a verify node would "
      f"dispatch through host_adapter --pi → VERIFIED)")

print("\nE2E PASS: real pi round-trip through host_adapter --pi.")
print("  axiom wrote dispatch_req.json → host_adapter sourced ~/.zshrc, ran")
print("  pi -p --mode json (the configured provider model, thinking off),")
print("  parsed the NDJSON event stream → result_text/cost/turns →")
print("  dispatch_res.json → axiom recorded agent_result with the LIVE model")
print("  answer (42). Zero coupled agent CLI. The friction (auth env + NDJSON) is")
print("  eliminated by the --pi preset. Add --tools for coding dispatches.")
