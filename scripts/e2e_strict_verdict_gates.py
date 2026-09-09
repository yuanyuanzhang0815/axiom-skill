"""End-to-end: CONFLICTED claim -> CLAIM_RESOLVED -> SUPPORTED (axiom M3).

Constructs a ledger with two verify nodes contradicting on claim C1
(verify_a survived, verify_b refuted) -> CONFLICTED -> derive_verdict PARTIAL.
Then the CLI `axiom claim resolve` writes a CLAIM_RESOLVED event (grounded on
an evidence basis_ref) -> derive_claim_status SUPPORTED (still PARTIAL until
verify). Proves the full absorbed-axiom claim lifecycle:

  verify contradiction -> CONFLICTED -> human grounded resolution -> SUPPORTED

A human CANNOT self-certify VERIFIED -- the claim still needs verify to reach
a terminal. This is the anti-self-deception gate (absorbed from the original
axiom's stricter verdict model, NOT the cognitive loop the user REDUCED).
"""
import json, sys, tempfile, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom.ledger import Ledger
from axiom.state import derive_claim_status, derive_verdict
from axiom.ir import Spec, Requirement

RUN = Path(tempfile.mkdtemp(prefix="e2e_svg_"))
RUN_DIR = RUN / "run"

lg = Ledger(RUN_DIR)
# two DISTINCT verify nodes contradict on C1 (a real conflict, NOT same-node
# iterative refinement which would be last-write-wins).
lg.append({"event_id": "E1", "kind": "verify_verdict",
           "spec_version_id": "spec.v1", "node_id": "verify_a",
           "claim_id": "C1", "survived": True, "refuted": False})
lg.append({"event_id": "E2", "kind": "verify_verdict",
           "spec_version_id": "spec.v1", "node_id": "verify_b",
           "claim_id": "C1", "survived": False, "refuted": True})

spec = Spec(
    spec_version_id="spec.v1", parent_spec_id=None, revision=1,
    intent="e2e strict-verdict-gates",
    requirements=[Requirement("R1", "criterion", "required")],
    boundaries=[], success_evidence=["S1:R1=claim:C1"],
    nodes={}, control_flow={"type": "sequence", "steps": []},
    decision_trace=[], budget_usd=5.0, max_concurrent=16,
    max_agents=1000, max_stagnation=1,
)

# 1. CONFLICTED: two verify nodes contradict -> CONFLICTED (not last-write-wins)
assert derive_claim_status("C1", lg, svid="spec.v1") == "CONFLICTED", \
    "two verify nodes contradicting should be CONFLICTED"
assert derive_verdict(spec, lg) == "PARTIAL", "CONFLICTED -> PARTIAL (not BLOCKED)"
print(f"1. CONFLICTED: claim={derive_claim_status('C1', lg, 'spec.v1')}, "
      f"verdict={derive_verdict(spec, lg)} ✅ (two nodes contradicted, PARTIAL not BLOCKED)")

# 2. CLI `axiom claim list` surfaces the CONFLICTED claim
r = subprocess.run(
    [sys.executable, "-m", "axiom", "claim", "list", "--run-dir", str(RUN_DIR)],
    capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert "CONFLICTED" in r.stdout, f"claim list should show CONFLICTED: {r.stdout}"
print(f"2. `axiom claim list` shows CONFLICTED ✅")

# 3. CLI `axiom claim resolve` grounds a resolution (basis_ref -> evidence)
r = subprocess.run(
    [sys.executable, "-m", "axiom", "claim", "resolve",
     "--claim-id", "C1", "--basis-ref", "evidence:E1",
     "--note", "manual review: verify_a's evidence is canonical",
     "--run-dir", str(RUN_DIR)],
    capture_output=True, text=True)
assert r.returncode == 0, f"resolve failed: {r.stderr}"
print(f"3. `axiom claim resolve --basis-ref evidence:E1` ✅")

# 4. claim is now SUPPORTED (NOT VERIFIED); verdict still PARTIAL -- a human
#    cannot self-certify VERIFIED; the claim still needs verify to terminal.
lg2 = Ledger(RUN_DIR)  # re-read ledger after the CLI wrote the event
status = derive_claim_status("C1", lg2, svid="spec.v1")
assert status == "SUPPORTED", f"after resolve should be SUPPORTED, got {status}"
assert derive_verdict(spec, lg2) == "PARTIAL", "SUPPORTED != VERIFIED -> PARTIAL"
print(f"4. after resolve: claim={status}, verdict={derive_verdict(spec, lg2)} ✅ "
      f"(SUPPORTED, not VERIFIED -- human cannot self-certify)")

# 5. CLAIM_RESOLVED event is in the ledger (svid-scoped, basis_ref recorded)
crs = [e for e in lg2.events() if e.get("kind") == "claim_resolved"]
assert len(crs) == 1, f"expected 1 claim_resolved, got {len(crs)}"
assert crs[0]["basis_ref"] == "evidence:E1"
assert crs[0]["spec_version_id"] == "spec.v1"
print(f"5. CLAIM_RESOLVED event in ledger (basis_ref={crs[0]['basis_ref']}, "
      f"svid={crs[0]['spec_version_id']}) ✅")

# 6. ledger hash chain intact after the CLI append
errs = lg2.verify_chain()
assert not errs, f"chain broken after claim_resolved: {errs}"
print(f"6. ledger hash chain intact ✅")

print("\nE2E PASS: CONFLICTED -> CLAIM_RESOLVED -> SUPPORTED (axiom M3).")
print("  two verify nodes contradicted on C1 -> CONFLICTED (signal last-write-wins")
print("  used to erase) -> verdict PARTIAL (not auto-BLOCKED);")
print("  human grounded resolution (basis_ref -> evidence) -> SUPPORTED (NOT VERIFIED);")
print("  a human cannot self-certify VERIFIED -- the claim still needs verify.")
print("  Same-node cross-iteration survived+refuted stays last-write-wins (Q9 refinement).")
