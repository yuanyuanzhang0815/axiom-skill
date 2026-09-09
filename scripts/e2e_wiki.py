"""End-to-end: drive the REAL Harness to produce a stagnating ledger, then
run the wiki pipeline against it:

  1. `axiom run` (stub runner -> stagnating)  ->  ledger with failure events
  2. `axiom wiki extract`                     ->  distill into wiki.jsonl
  3. `axiom wiki impact`                      ->  append a denied-replan lesson
  4. `axiom wiki search` (same intent)        ->  retrieve the lesson
  5. `axiom plan --wiki-suggest`              ->  prior shape/verdict/impact
                                                   in hand BEFORE next spec

Validates that the wiki puts prior-run experience in the agent's hand before
the next design -- format/shape arrive before output, eliminating format
friction at the source.
"""
import json, sys, tempfile, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.harness import Harness
from axiom.ir import Spec, Requirement

RUN = Path(tempfile.mkdtemp(prefix="e2e_wiki_"))
SPEC_FILE = RUN / "spec.json"
WIKI_DIR = RUN / "wiki"

# stub worker_runner: returns a result with permission_denials -> cognitive
# failure -> stagnation on retry (same shape as e2e_debug_envelope.py).
def stub_runner(args, cwd=None):
    res = {
        "type": "result",
        "result": json.dumps({"claim_id": "C1"}),
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "permission_denials": ["no_write:src/a.py"],
    }
    return 0, json.dumps(res)


spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e wiki probe: build the relay write path",
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
h.ledger.seal()
print("=== run() returned:", result)

print("\n=== REAL ledger events ===")
for e in h.ledger.events():
    print(f"  {e.get('kind'):24s} node={e.get('node_id','-')}")

def _axiom(*args):
    return subprocess.run(
        [sys.executable, "-m", "axiom", *args],
        capture_output=True, text=True)

# 1. extract
print("\n=== axiom wiki extract ===")
rc = _axiom("wiki", "extract", str(SPEC_FILE),
              "--run-dir", str(RUN / "run"),
              "--wiki-dir", str(WIKI_DIR),
              "--tag", "bugfix", "--tag", "relay",
              "--learned", "write-area glob must match repo root")
print("exit:", rc.returncode)
print(rc.stdout.strip())
if rc.stderr.strip():
    print("STDERR:", rc.stderr.strip())

# grab the entry_id from search output (more robust than parsing extract)
# 2. search
print("\n=== axiom wiki search (same intent) ===")
rc = _axiom("wiki", "search", "relay", "--wiki-dir", str(WIKI_DIR))
print("exit:", rc.returncode)
print(rc.stdout.strip())

# parse entry_id from --json search for the impact step
rc = _axiom("wiki", "search", "relay", "--wiki-dir", str(WIKI_DIR), "--json")
results = json.loads(rc.stdout)
entry_id = results[0]["entry_id"]

# 3. impact (a denied replan lesson)
print("\n=== axiom wiki impact ===")
rc = _axiom("wiki", "impact", entry_id,
              "--kind", "replan_denied",
              "--reason", "replanning without new evidence after stagnation",
              "--wiki-dir", str(WIKI_DIR))
print("exit:", rc.returncode)
print(rc.stdout.strip())

# 4. search again -- impact now aggregated
print("\n=== axiom wiki search (with impact) ===")
rc = _axiom("wiki", "search", "relay", "--wiki-dir", str(WIKI_DIR))
print("exit:", rc.returncode)
print(rc.stdout.strip())

# 5. plan --wiki-suggest (prior shape/verdict/impact in hand before next spec)
print("\n=== axiom plan --wiki-suggest ===")
rc = _axiom("plan", "--intent", "build the relay write path for the project",
              "--wiki-suggest", "--wiki-dir", str(WIKI_DIR),
              "--run-dir", str(RUN / "next-run"))
print("exit:", rc.returncode)
print("--- stderr (suggestions) ---")
print(rc.stderr.strip())
print("--- stdout (spec template, first 3 lines) ---")
print("\n".join(rc.stdout.splitlines()[:3]))

# 6. verify chain
print("\n=== axiom wiki verify ===")
rc = _axiom("wiki", "verify", "--wiki-dir", str(WIKI_DIR))
print("exit:", rc.returncode)
print(rc.stdout.strip())

print("\n=== DONE ===")
