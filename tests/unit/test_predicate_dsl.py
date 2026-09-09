"""v2 predicate DSL: restricted-eval + built-in projections (spec §2).

Predicates are deterministic expressions evaluated by the harness over ledger
projections — NEVER an LLM call (invariant 1: orchestrator reasons only
between workflows, never between nodes). A predicate evaluation is a FACT
(invariant 3: claim != fact), not a claim.

Return shape: (value, claim_strength) where value is bool|'undefined' and
claim_strength is 'verified'|'proposed'|'deterministic' (cannot-branch-on-
PROPOSED rule, §2 Q2).
"""
import pytest
from axiom.harness import Harness


def _spec(budget=5.0):
    """Minimal spec stub for predicate eval."""
    return type("S", (), {
        "spec_version_id": "spec.v1",
        "budget_usd": budget,
        "max_stagnation": 1,
        "max_agents": 1000,
        "requirements": [],
        "success_evidence": [],
        "decision_trace": [],
    })()


# ---- basic comparison + boolean ----------------------------------------

def test_basic_comparison_and_boolean(tmp_path):
    """dry_count() < 2 -> True when 0 dry rounds (empty ledger, no loop)."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    val, strength = h._eval_predicate(
        "dry_count() < 2", {}, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "deterministic"
    # boolean composition
    val, _ = h._eval_predicate(
        "dry_count() < 2 and budget_used() < 10", {}, spec, h.ledger, "spec.v1")
    assert val is True


# ---- {{ref}} placeholder resolution -----------------------------------

def test_ref_placeholder_resolves_upstream(tmp_path):
    """{{n1.validated_output.x}} resolves validated_output['x'] of node n1."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    ctx = {"n1": {"validated_output": {"x": "hello"}}}
    val, strength = h._eval_predicate(
        "{{n1.validated_output.x}} == 'hello'", ctx, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "proposed"  # raw validated_output ref -> advisory


def test_ref_placeholder_numeric(tmp_path):
    """Numeric validated_output fields resolve and compare correctly."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    ctx = {"n1": {"validated_output": {"count": 3}}}
    val, _ = h._eval_predicate(
        "{{n1.validated_output.count}} > 2", ctx, spec, h.ledger, "spec.v1")
    assert val is True


# ---- claim_status reads ledger ----------------------------------------

def test_claim_status_reads_ledger(tmp_path):
    """claim_status('C1') == 'VERIFIED' reflects a verify_verdict survived."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    h.ledger.append({
        "event_id": "E1", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": True,
    })
    val, strength = h._eval_predicate(
        "claim_status('C1') == 'VERIFIED'", {}, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "verified"  # reads VERIFIED truth -> binding


def test_claim_status_unverified(tmp_path):
    """claim_status('C1') == 'VERIFIED' is False without a survived verify."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    val, strength = h._eval_predicate(
        "claim_status('C1') == 'VERIFIED'", {}, spec, h.ledger, "spec.v1")
    assert val is False
    assert strength == "verified"


# ---- budget built-ins -------------------------------------------------

def test_budget_builtins(tmp_path):
    """budget_used()/budget_remaining() from spec.budget_usd + recorded cost."""
    h = Harness(tmp_path / "run")
    spec = _spec(budget=5.0)
    h.ledger.append({
        "event_id": "E1", "kind": "agent_result",
        "spec_version_id": "spec.v1", "node_id": "n1",
        "payload": {"cost_usd": 0.5, "validated_output": {}},
        "claims": [],
    })
    val, _ = h._eval_predicate(
        "budget_used() == 0.5", {}, spec, h.ledger, "spec.v1")
    assert val is True
    val, _ = h._eval_predicate(
        "budget_remaining() == 4.5", {}, spec, h.ledger, "spec.v1")
    assert val is True
    # combined
    val, _ = h._eval_predicate(
        "budget_used() + budget_remaining() == 5.0", {}, spec, h.ledger, "spec.v1")
    assert val is True


# ---- no builtins (sandboxed eval) -------------------------------------

def test_no_builtins(tmp_path):
    """__import__('os') must raise; eval namespace has empty __builtins__."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    # __import__ not available -> NameError -> 'undefined'
    val, _ = h._eval_predicate(
        "__import__('os')", {}, spec, h.ledger, "spec.v1")
    assert val == "undefined"
    # open / eval / exec also unavailable
    val, _ = h._eval_predicate(
        "open('/etc/passwd')", {}, spec, h.ledger, "spec.v1")
    assert val == "undefined"
    # but True/False/None (keywords) still work
    val, _ = h._eval_predicate(
        "True and not False", {}, spec, h.ledger, "spec.v1")
    assert val is True


# ---- cannot-branch-on-PROPOSED rule (§2 Q2) ----------------------------

def test_cannot_branch_on_proposed(tmp_path):
    """{{ref}} (PROPOSED) -> advisory; claim_status(...) (VERIFIED) -> binding."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    # raw validated_output ref (PROPOSED) -> advisory
    ctx = {"n1": {"validated_output": {"field": "x"}}}
    val, strength = h._eval_predicate(
        "{{n1.validated_output.field}} == 'x'", ctx, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "proposed"
    # claim_status(...) == 'VERIFIED' (VERIFIED truth) -> binding
    h.ledger.append({
        "event_id": "E1", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": True,
    })
    val, strength = h._eval_predicate(
        "claim_status('C1') == 'VERIFIED'", {}, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "verified"
    # pure deterministic projection -> deterministic
    val, strength = h._eval_predicate(
        "dry_count() < 2", {}, spec, h.ledger, "spec.v1")
    assert val is True
    assert strength == "deterministic"


# ---- undefined ref -> 'undefined' ------------------------------------

def test_undefined_ref(tmp_path):
    """{{missing}} -> returns 'undefined' (safe default downstream)."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    val, strength = h._eval_predicate(
        "{{missing}}", {}, spec, h.ledger, "spec.v1")
    assert val == "undefined"
    assert strength == "proposed"  # has a {{}} ref -> proposed classification


def test_undefined_on_nameerror(tmp_path):
    """Bare undefined name -> 'undefined'."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    val, _ = h._eval_predicate(
        "undefined_name == 5", {}, spec, h.ledger, "spec.v1")
    assert val == "undefined"


# ---- iteration + stagnation built-ins ----------------------------------

def test_iteration_builtin(tmp_path):
    """iteration() returns the current loop iteration index."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    val, _ = h._eval_predicate(
        "iteration() == 3", {}, spec, h.ledger, "spec.v1", iteration=3)
    assert val is True


def test_stagnation_count(tmp_path):
    """stagnation_count() counts stagnating events for this node (svid-scoped)."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    h.ledger.append({
        "event_id": "E1", "kind": "stagnating",
        "spec_version_id": "spec.v1", "node_id": "n1",
    })
    h.ledger.append({
        "event_id": "E2", "kind": "stagnating",
        "spec_version_id": "spec.v1", "node_id": "n1",
    })
    h.ledger.append({
        "event_id": "E3", "kind": "stagnating",
        "spec_version_id": "spec.v1", "node_id": "n2",
    })
    val, _ = h._eval_predicate(
        "stagnation_count() == 2", {}, spec, h.ledger, "spec.v1", node_id="n1")
    assert val is True


# ---- dry_count in a loop context --------------------------------------

def test_dry_count_in_loop(tmp_path):
    """dry_count() counts consecutive dry rounds ending at current iteration."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    # iteration 1: dry (no verify_verdict survived=false, no stagnating)
    # iteration 2: dry
    # at iteration=2, dry_count() == 2 -> dry<2 is False
    val, _ = h._eval_predicate(
        "dry_count() == 2", {}, spec, h.ledger, "spec.v1",
        loop_id="L1", iteration=2)
    assert val is True
    # dry_count() < 2 is False at iteration=2 with 2 dry rounds
    val, _ = h._eval_predicate(
        "dry_count() < 2", {}, spec, h.ledger, "spec.v1",
        loop_id="L1", iteration=2)
    assert val is False


def test_dry_count_reset_by_stagnating(tmp_path):
    """A stagnating event makes that iteration non-dry, resetting the streak."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    # iteration 1: dry
    # iteration 2: stagnating (non-dry) -> streak resets to 0
    # iteration 3: dry -> streak = 1
    h.ledger.append({
        "event_id": "E1", "kind": "stagnating",
        "spec_version_id": "spec.v1", "node_id": "n1",
        "payload": {"loop_id": "L1", "iteration": 2},
    })
    val, _ = h._eval_predicate(
        "dry_count() == 1", {}, spec, h.ledger, "spec.v1",
        loop_id="L1", iteration=3)
    assert val is True


# ---- verdict built-in -------------------------------------------------

def test_verdict_builtin(tmp_path):
    """verdict() projects task completion from spec + ledger."""
    h = Harness(tmp_path / "run")
    spec = _spec()
    # no requirements -> UNVERIFIED
    val, _ = h._eval_predicate(
        "verdict() == 'UNVERIFIED'", {}, spec, h.ledger, "spec.v1")
    assert val is True
