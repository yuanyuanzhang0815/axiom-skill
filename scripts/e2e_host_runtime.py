"""End-to-end: prove axiom's control layer runs against ANY agent runtime
via the file-protocol dispatch path (AXIOM_RUNTIME=host), with no external
agent CLI coupled in.

axiom (control layer) writes dispatch_req.json and polls for
dispatch_res.json. This script acts as the HOST AGENT (the role pi /
Claude Code would play): it watches for the request file, reads the prompt +
schema, produces a conforming result, writes the response file. The
DispatchResult envelope is honored by construction.

Spec: agent(what is 6*7) -> script(double the answer via template flow).
Agent dispatch goes through the file protocol; script node runs directly
(subprocess, zero agent runtime). Proves control/execution separation.
"""
import json, os, sys, tempfile, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec

RUN = Path(tempfile.mkdtemp(prefix="e2e_cli_"))
DISPATCH_DIR = RUN / "dispatch"
DISPATCH_DIR.mkdir()

# a deterministic script that doubles the agent's answer (proves template flow
# in cli-runtime mode). {{n_agent.validated_output.answer}} renders to the
# scalar "42" (a non-scalar would be JSON-serialised; a scalar is passed as-is).
DBL = RUN / "double.py"
DBL.write_text(
    'import json,sys\n'
    'a=int(sys.argv[1])\n'
    'print(json.dumps({"doubled": a*2}))\n')

spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e cli-runtime: control layer decoupled from agent runtime",
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

# --- axiom control layer runs with AXIOM_RUNTIME=host --------------
#    it writes dispatch_req.json and BLOCKS polling for dispatch_res.json.
env = dict(os.environ)
env["AXIOM_RUNTIME"] = "host"
env["AXIOM_RUN_DIR"] = str(DISPATCH_DIR)
env["AXIOM_CLI_DISPATCH_TIMEOUT"] = "30"   # short; host responds in <5s

# --- HOST AGENT (the role pi/cc plays) --------------------------
# watches for dispatch_req.json, reads it, produces a result conforming to
# the request's schema, writes dispatch_res.json with the DispatchResult fields.
HOST_LOG = []

def host_agent():
    """Act as an arbitrary agent runtime honoring the file protocol."""
    # P1-4: per-rid request files (dispatch_req_{rid}.json). Discover by glob;
    # legacy single-writer dispatch_req.json kept as fallback.
    from pathlib import Path
    import glob
    req_path = None
    for _ in range(50):
        new_reqs = sorted(glob.glob(str(DISPATCH_DIR / "dispatch_req_*.json")))
        if new_reqs:
            req_path = Path(new_reqs[0])
            break
        legacy = DISPATCH_DIR / "dispatch_req.json"
        if legacy.exists():
            req_path = legacy
            break
        time.sleep(0.1)
    else:
        HOST_LOG.append("HOST: no request appeared")
        return
    with open(req_path) as f:
        req = json.load(f)
    HOST_LOG.append(f"HOST: read {req_path.name}, prompt={req['prompt']!r}, "
                    f"schema.required={req['schema'].get('required')}")
    # ANY agent runtime would do this: run the prompt, produce JSON matching
    # the schema. Here we deterministically produce the answer.
    result_text = json.dumps({"answer": 42})
    res = {
        "session_id": "host-session-1",
        "result_text": result_text,
        "num_turns": 1,
        "cost_usd": 0.003,
        "permission_denials": [],
        "exit_code": 0,
        "retry_class": "cognitive",
    }
    # P1-4: write res to the per-rid path matching the discovered req
    if req_path.name == "dispatch_req.json":
        res_path = DISPATCH_DIR / "dispatch_res.json"
    else:
        rid = req_path.stem.replace("dispatch_req_", "")
        res_path = DISPATCH_DIR / f"dispatch_res_{rid}.json"
    with open(res_path, "w") as f:
        json.dump(res, f)
    HOST_LOG.append(f"HOST: wrote {res_path.name} (DispatchResult fields)")

# run host in a thread, axiom control layer in the main thread
host_t = threading.Thread(target=host_agent, daemon=True)
host_t.start()

# swap env so the Harness subprocess-dispatch path uses the file protocol
old_env = dict(os.environ)
os.environ.update(env)
try:
    h = Harness(RUN / "run", worker_runner=None)  # None -> real runner, but
    # dispatch_via_host ignores the runner entirely (it uses the file
    # protocol), so the runner param is moot under AXIOM_RUNTIME=host.
    h.run(spec)
finally:
    os.environ.clear()
    os.environ.update(old_env)
host_t.join(timeout=5)

events = list(h.ledger.events())
agent_evs = [e for e in events if e.get("kind") == "agent_result"]
script_evs = [e for e in events if e.get("kind") == "script_result"]

print(f"run_dir: {RUN}")
print(f"dispatch_dir: {DISPATCH_DIR}")
for line in HOST_LOG:
    print(f"  {line}")
print(f"events: {len(events)} | {len(agent_evs)} agent_result | {len(script_evs)} script_result")

# 1. the host agent received the request and its result flowed back
assert agent_evs, "no agent_result — file protocol dispatch failed"
agent_out = agent_evs[0]["payload"]["validated_output"]
assert agent_out.get("answer") == 42, f"host result didn't flow: {agent_out}"
print(f"  n_agent: answer={agent_out['answer']} ✅ (file protocol: req→host→res→ledger)")

# 2. the host's cost was recorded (budget tracking works without an external agent CLI)
assert agent_evs[0]["payload"].get("cost_usd") == 0.003, "cost not recorded"
print(f"  n_agent: cost_usd={agent_evs[0]['payload']['cost_usd']} ✅ (budget tracking via host-reported field)")

# 3. script node ran (zero agent runtime) and received the agent's answer
#    via template flow
assert script_evs, "no script_result"
dbl_out = script_evs[0]["payload"]["parsed_output"]
assert dbl_out.get("doubled") == 84, f"template flow broke in host mode: {dbl_out}"
print(f"  n_double: doubled={dbl_out['doubled']} ✅ (script ran, got 42 via template, 42*2=84)")

# 4. dispatch went through the file protocol (no direct agent subprocess)
assert agent_evs[0]["payload"].get("num_turns") == 1
print(f"  no direct subprocess: AXIOM_RUNTIME=host used file protocol ✅")

print("\nE2E PASS: control layer runs against a file-protocol host agent.")
print("  axiom wrote dispatch_req.json → host (any runtime) read it, "
      "produced a schema-conforming result, wrote dispatch_res.json →")
print("  axiom parsed the DispatchResult envelope into the ledger. Zero coupled agent CLI.")
print("  script node ran directly (subprocess, no agent runtime).")
print("  This is the seam pi / codex / CC would plug into.")
