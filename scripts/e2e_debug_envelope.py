"""End-to-end: drive the REAL Harness to produce a stagnating ledger, then
run `axiom debug` against it. Validates that derive_debug_envelope reads
harness-produced events (not just synthetic test fixtures)."""
import json, sys, tempfile, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement

RUN = Path(tempfile.mkdtemp(prefix="e2e_debug_"))
SPEC_FILE = RUN / "spec.json"

# stub worker_runner: returns (exit_code, stdout). stdout carries a JSON result
# line with permission_denials -> cognitive failure -> stagnation on retry.
def stub_runner(args, cwd=None):
    res = {
        "type": "result",
        "result": json.dumps({"claim_id": "C1"}),  # conforms to schema
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "permission_denials": ["no_write:src/a.py"],
    }
    return 0, json.dumps(res)


spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e debug probe",
    requirements=[Requirement("R1", "produce a claim", "required")],
    boundaries=["b"],
    success_evidence=["S1:R1=claim:C1"],
    nodes={
        "n1": {
            "id": "n1", "type": "agent",
            "prompt": "produce claim C1",
            "acceptance": "claim C1 present",
            "failure_policy": {"on_exhausted": "block"},
            "write_areas": [],
            "output_schema": {"type": "object", "required": ["claim_id"]},
        }
    },
    control_flow={"type": "sequence", "steps": ["n1"]},
    decision_trace=[], budget_usd=5.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)

# persist spec for the CLI to read back
SPEC_FILE.write_text(json.dumps({
    "spec_version_id": spec.spec_version_id, "parent_spec_id": None,
    "revision": 1, "intent": spec.intent,
    "requirements": [{"id": "R1", "text": "produce a claim",
                      "criticality": "required"}],
    "boundaries": list(spec.boundaries),
    "success_evidence": list(spec.success_evidence),
    "nodes": spec.nodes,
    "control_flow": spec.control_flow,
    "decision_trace": [], "budget_usd": 5.0, "max_concurrent": 16,
    "max_agents": 1000, "max_stagnation": 1,
}), encoding="utf-8")

h = Harness(RUN / "run", worker_runner=stub_runner)
result = h.run(spec)
print("=== run() returned:", result)
h.ledger.seal()

print("\n=== REAL ledger events (harness-produced) ===")
for e in h.ledger.events():
    print(f"  {e.get('kind'):24s} node={e.get('node_id','-'):6} "
          f"gate={e.get('gate_id','-'):6} "
          f"payload_keys={list((e.get('payload') or {}).keys())}")

print("\n=== axiom debug (text) ===")
rc = subprocess.run(
    [sys.executable, "-m", "axiom", "debug", str(SPEC_FILE),
     "--run-dir", str(RUN / "run")],
    capture_output=True, text=True)
print("exit:", rc.returncode)
print(rc.stdout)
if rc.stderr:
    print("STDERR:", rc.stderr)

print("\n=== axiom debug --json ===")
rc = subprocess.run(
    [sys.executable, "-m", "axiom", "debug", str(SPEC_FILE),
     "--run-dir", str(RUN / "run"), "--json"],
    capture_output=True, text=True)
print("exit:", rc.returncode)
try:
    env = json.loads(rc.stdout)
    print(json.dumps(env, indent=2, ensure_ascii=False))
except Exception:
    print(rc.stdout)
