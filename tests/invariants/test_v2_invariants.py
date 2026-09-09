"""Task 16: v2 adversarial invariant suite (spec §9 table + §10 boundaries).

Each test ATTEMPTS TO BYPASS a v2 invariant. Tasks 1-14 must PREVENT the
bypass — so every test PASSES (the invariant is upheld). A test that FAILS
reveals a gap in Tasks 1-14 and is routed to the relevant task's fix loop.

Guards (one per §9 Q + structural):
  Q1  test_cannot_loop_forever          — max_iterations hard bound; no unbounded loop
  Q2  test_cannot_branch_on_PROPOSED    — PROPOSED predicate = advisory; can't satisfy required success_evidence
  Q3  test_worktree_no_cross_node_leak  — worktree output pinned to artifacts/, no write to main checkout
  Q4  test_until_honest_budget_exit     — mid-iteration budget → iteration_close+loop_close exit_reason='budget_exhausted' → PARTIAL
  Q5  test_until_vs_repeat_semantics    — until distinct node (cond+budget_aware); repeat keeps dry metric
  Q6  test_gate_escalation_vs_risk_auditable — escalation vs risk gates distinguishable by reason
  Q7  test_loop_does_not_starve_global_cap — global concurrency cap shared (documented limitation)
  Q8  test_iteration_2_not_pop_iteration_1  — resume cache keyed (svid,node_id,loop_id,iteration)
  Q9  test_loop_refine_last_verify_wins — latest verify_verdict is truth (not stuck at first)
  -   test_branch_provenance_closed     — every condition dispatch records a branch_decision event
"""
import dataclasses
import hashlib
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from axiom.harness import Harness, GateHalt
from axiom.ir import Spec, Requirement, validate_spec, RepeatNode, UntilNode
from axiom.ledger import Ledger
from axiom.state import (
    derive_verdict,
    derive_claim_status,
    derive_dry_count,
    project_cognitive_state,
)


# ---- shared helpers (mirrors prior test files; not re-implementing harness) -

def _ok_runner(result_str='{"answer": "42"}', cost=0.0):
    def _r(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": result_str, "session_id": "s",
            "total_cost_usd": cost, "permission_denials": [], "usage": {},
        }))
    return _r


def _agent(nid, write_areas=None):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": write_areas or [], "acceptance": ["a"],
        "failure_policy": {},
    }


def _verify_node(vid, sc=1):
    return {
        "type": "verify", "id": vid, "target": "{{findings}}",
        "skeptic_count": sc, "skeptic_prompt": "refute",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }


def _repeat(nid, body, max_iterations, until="dry<2", on_exhausted="block"):
    return {
        "type": "repeat", "id": nid, "body": body,
        "max_iterations": max_iterations, "until": until,
        "failure_policy": {"on_exhausted": on_exhausted},
    }


def _until(nid, body, max_iterations, cond="claim_status('C1') == 'VERIFIED'",
           budget_aware=True, on_exhausted="degrade"):
    return {
        "type": "until", "id": nid, "body": body,
        "max_iterations": max_iterations, "cond": cond,
        "budget_aware": budget_aware,
        "failure_policy": {"on_exhausted": on_exhausted},
    }


def _condition(nid, predicate, then, else_=(), gate=False):
    return {
        "type": "condition", "id": nid, "predicate": predicate,
        "then_branch": then, "else_branch": list(else_), "gate": gate,
        "output_schema": {},
    }


def _gate(nid, trigger, body, on_trigger="pause", tiers=None):
    return {
        "type": "gate", "id": nid, "trigger": trigger,
        "body": list(body), "on_trigger": on_trigger,
        "escalation_tiers": tiers or [], "output_schema": {},
    }


def _spec(nodes, steps, *, budget_usd=5.0, max_concurrent=16,
          max_stagnation=1, success_evidence=("S1:R1=claim:C1",)):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=list(success_evidence), nodes=nodes,
        control_flow={"type": "sequence", "steps": list(steps)},
        decision_trace=[], budget_usd=budget_usd,
        max_concurrent=max_concurrent, max_agents=1000,
        max_stagnation=max_stagnation,
    )


def _loop_open(ledger):
    return next(e for e in ledger.events() if e.get("kind") == "loop_open")


def _loop_close(ledger):
    return next(e for e in ledger.events() if e.get("kind") == "loop_close")


def _iter_closes(ledger):
    return [e for e in ledger.events() if e.get("kind") == "iteration_close"]


def _agent_results(ledger, nid):
    return [e for e in ledger.events()
            if e.get("kind") == "agent_result" and e.get("node_id") == nid]


def _branch_decisions(ledger):
    return [e for e in ledger.events() if e.get("kind") == "branch_decision"]


def _input_hash(values):
    return hashlib.sha256(
        json.dumps(values, sort_keys=True).encode()).hexdigest()


# ---- worktree helpers (from test_worktree.py) ------------------------------

def _git_init_project(project_root: Path):
    project_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(project_root), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"],
                   cwd=str(project_root), check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=str(project_root), check=True)
    (project_root / "README.md").write_text("# project\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(project_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=str(project_root), check=True)


def _snippet_runner(snippet: str, result_str='{"ok": 1}', cost=0.01):
    def runner(args, cwd=None):
        subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=cwd, check=True, capture_output=True, text=True,
        )
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": result_str, "session_id": "s",
            "total_cost_usd": cost, "permission_denials": [], "usage": {},
        }))
    return runner


def _agent_result_event(svid, nid, vo, *, loop_id=None, iteration=None,
                        input_hash=None, cost=0.01):
    payload = {"validated_output": vo, "cost_usd": cost,
               "num_turns": 1, "permission_denials": [],
               "result_text_preview": ""}
    if loop_id is not None:
        payload["loop_id"] = loop_id
    if iteration is not None:
        payload["iteration"] = iteration
    if input_hash is not None:
        payload["input_hash"] = input_hash
    return {"event_id": f"e_{nid}", "kind": "agent_result",
            "spec_version_id": svid, "node_id": nid,
            "payload": payload, "claims": []}


# =====================================================================
# Q1: test_cannot_loop_forever
# =====================================================================

def test_cannot_loop_forever(tmp_path):
    """Q1 guard: a repeat/until node MUST have max_iterations (hard bound) +
    a convergence predicate (until/cond). No spec can loop unboundedly.

    BYPASS ATTEMPT: construct a loop node WITHOUT max_iterations (unbounded
    loop that never terminates). If validate_spec accepted it, the harness
    would loop forever at runtime.

    ASSERTION: validate_spec REJECTS it (non-zero max_iterations is REQUIRED).
    AND a loop with max_iterations=N always terminates at exactly N iterations
    (the hard bound is enforced at runtime).
    """
    # --- attempt 1: repeat node WITHOUT max_iterations ---
    bad_repeat = _repeat("r1", ["a1"], max_iterations=5)
    bad_repeat.pop("max_iterations")
    spec_bad_r = _spec({"r1": bad_repeat, "a1": _agent("a1")}, ["r1"])
    errs_r = validate_spec(spec_bad_r)
    assert any("max_iterations" in e and "r1" in e for e in errs_r), (
        "validate_spec must reject a repeat node without max_iterations "
        f"(unbounded loop). Errors: {errs_r}")

    # --- attempt 2: until node WITHOUT max_iterations ---
    bad_until = _until("u1", ["a1"], max_iterations=3)
    bad_until.pop("max_iterations")
    spec_bad_u = _spec({"u1": bad_until, "a1": _agent("a1")}, ["u1"])
    errs_u = validate_spec(spec_bad_u)
    assert any("max_iterations" in e and "u1" in e for e in errs_u), (
        "validate_spec must reject an until node without max_iterations "
        f"(unbounded loop). Errors: {errs_u}")

    # --- attempt 3: repeat without convergence predicate (until) ---
    bad_repeat_no_until = _repeat("r1", ["a1"], max_iterations=3)
    bad_repeat_no_until.pop("until")
    spec_bad_no_until = _spec(
        {"r1": bad_repeat_no_until, "a1": _agent("a1")}, ["r1"])
    errs_no_until = validate_spec(spec_bad_no_until)
    assert any("until" in e and "r1" in e for e in errs_no_until), (
        "validate_spec must reject a repeat without a convergence predicate. "
        f"Errors: {errs_no_until}")

    # --- runtime hard bound: a loop always terminates within max_iterations ---
    # cond never met (body is a plain agent, C1 never VERIFIED) -> exits at
    # max_iterations. If max_iterations were not a hard bound, this loop
    # would never terminate.
    h = Harness(tmp_path / "run", worker_runner=_ok_runner())
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=4,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")}, ["u1"])
    h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    ics = _iter_closes(h.ledger)
    assert len(ics) == 4, (
        f"loop must terminate at exactly max_iterations=4 (hard bound); "
        f"got {len(ics)} iterations")
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    assert lc["payload"]["final_iteration"] == 4


# =====================================================================
# Q2: test_cannot_branch_on_PROPOSED
# =====================================================================

def test_cannot_branch_on_PROPOSED(tmp_path):
    """Q2 guard: a predicate referencing a PROPOSED {{ref}} validated_output
    field is ADVISORY (claim_strength='proposed', degraded=True). A binding
    branch requires gate=True OR a predicate on claim_status(...)=='VERIFIED'.
    An advisory branch CANNOT satisfy required success_evidence (the advisory
    exit doesn't make a claim VERIFIED).

    BYPASS ATTEMPT: a condition whose predicate reads a PROPOSED {{ref}}
    (not claim_status(...)=='VERIFIED') evaluates True and runs the then-branch.
    The then-branch's agent claims C1. If the advisory branch were treated as
    BINDING, C1 would be VERIFIED and the required success_evidence satisfied.

    ASSERTION: (1) the branch_decision has claim_strength='proposed' +
    degraded=True (advisory); (2) derive_claim_status('C1') is NOT VERIFIED
    (the advisory branch is a FACT, not a verify_verdict — it doesn't elevate
    C1); (3) derive_verdict is PARTIAL (required success_evidence unmet).
    """
    h = Harness(tmp_path / "run", worker_runner=_ok_runner())
    spec = _spec({"c1": _condition("c1",
                                   "{{n1.validated_output.x}} == 'y'",
                                   ["a_then"]),
                  "a_then": _agent("a_then")}, ["c1"])
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["c1"], values, "spec.v1", spec)

    # (1) advisory: claim_strength='proposed', degraded=True
    bds = _branch_decisions(h.ledger)
    assert len(bds) == 1
    p = bds[0]["payload"]
    assert p["claim_strength"] == "proposed", (
        f"PROPOSED predicate must be advisory (claim_strength='proposed'); "
        f"got {p['claim_strength']!r}")
    assert p["degraded"] is True, (
        "PROPOSED predicate must set degraded=True (advisory, not binding)")
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"

    # (2) the advisory branch does NOT elevate C1 to VERIFIED
    # (branch_decision is a FACT, not a verify_verdict — derive_claim_status
    # doesn't read it; the then-branch agent claims C1 supported, not VERIFIED)
    status = derive_claim_status("C1", h.ledger, svid="spec.v1")
    assert status != "VERIFIED", (
        f"advisory branch must NOT make C1 VERIFIED (only verify_verdict "
        f"can); got {status!r}")

    # (3) required success_evidence unmet -> PARTIAL (not VERIFIED)
    assert derive_verdict(spec, h.ledger) == "PARTIAL", (
        "advisory branch can't satisfy required success_evidence; "
        "verdict must be PARTIAL, not VERIFIED")


# =====================================================================
# test_branch_provenance_closed
# =====================================================================

def test_branch_provenance_closed(tmp_path):
    """Every condition dispatch records a branch_decision event (no branch
    taken without a recorded fact). Provenance is closed: the ledger proves
    WHICH branch was taken and WHY.

    BYPASS ATTEMPT: a condition dispatch that takes a branch but records NO
    branch_decision event (a 'silent branch' — the orchestrator can't audit
    why the branch was taken).

    ASSERTION: exactly one branch_decision event per condition dispatch,
    carrying predicate, eval_result, branch_taken, degraded, claim_strength.
    The branch_taken field matches which body agent actually ran.
    """
    h = Harness(tmp_path / "run", worker_runner=_ok_runner())
    spec = _spec({"c1": _condition("c1", "True", ["a_then"], ["a_else"]),
                  "a_then": _agent("a_then"), "a_else": _agent("a_else")},
                 ["c1"])
    h._dispatch(spec.nodes["c1"], {}, "spec.v1", spec)

    bds = _branch_decisions(h.ledger)
    # exactly one branch_decision (no silent branch)
    assert len(bds) == 1, (
        f"exactly one branch_decision per condition dispatch; got {len(bds)}")
    bd = bds[0]
    assert bd["kind"] == "branch_decision"
    assert bd["node_id"] == "c1"
    p = bd["payload"]
    # all required provenance fields present
    assert "predicate" in p and p["predicate"] == "True"
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"
    assert "degraded" in p
    assert "claim_strength" in p
    # provenance matches reality: then-branch ran, else-branch didn't
    assert _agent_results(h.ledger, "a_then"), "then-branch must have run"
    assert not _agent_results(h.ledger, "a_else"), "else-branch must NOT have run"


# =====================================================================
# Q3: test_worktree_no_cross_node_leak
# =====================================================================

def test_worktree_no_cross_node_leak(tmp_path):
    """Q3 guard: a worktree-isolated worker's output is pinned to artifacts/
    (content-addressed); NO write leaks to the main checkout.

    BYPASS ATTEMPT: a worktree worker writes src/x.py. If the worktree
    isolation is broken (v1 bug: cwd=run_dir, the worker resolves project to main
    checkout), the write leaks to the main checkout.

    ASSERTION: (1) the file is NOT in the main checkout (no leak);
    (2) the output IS pinned to run_dir/artifacts/sha256:<hash> (the
    content-addressed merge unit — no auto-merge, downstream reconciles).
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _git_init_project(project)
    snippet = textwrap.dedent("""
        import os
        assert "wt_" in os.getcwd(), "must run in the worktree"
        os.makedirs("src", exist_ok=True)
        open("src/x.py", "w").write("x = 1")
    """)
    h = Harness(run_dir, worker_runner=_snippet_runner(snippet),
                project_root=project)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": ["src/**/*"], "acceptance": ["a"], "failure_policy": {},
        "isolation": "worktree",
    }
    out = h.dispatch_agent(node, {}, "spec.v1")
    assert out is not None

    # (1) NO leak to main checkout (the v1 friction root cause)
    assert not (project / "src" / "x.py").exists(), (
        "LEAK: worker write reached the main checkout (worktree isolation "
        "broken)")

    # (2) output pinned to artifacts/ (content-addressed merge unit)
    expected = "sha256:" + hashlib.sha256(b"x = 1").hexdigest()
    refs = [e["payload"]["artifact_refs"] for e in h.ledger.events()
            if e.get("kind") == "artifact_write"]
    assert refs and expected in refs[0], (
        "worker output must be pinned (content-addressed artifact_refs)")
    assert (run_dir / "artifacts" / expected).read_bytes() == b"x = 1", (
        "artifact must be in run_dir/artifacts/<sha256>")


# =====================================================================
# Q4: test_until_honest_budget_exit
# =====================================================================

def test_until_honest_budget_exit(tmp_path):
    """Q4 guard: mid-iteration budget exhaustion emits
    iteration_close{exit_reason='budget_exhausted'} + loop_close
    {exit_reason='budget_exhausted'} → PARTIAL. The partial iteration's work
    is auditable (not silently dropped).

    BYPASS ATTEMPT: budget trips DURING iteration-2's body dispatch. If the
    loop silently exited without recording the partial work, the partial
    iteration's cost + agent_result would be lost (dishonest exit).

    ASSERTION: (1) the partial body agent_result IS recorded (auditable);
    (2) iteration_close carries exit_reason='budget_exhausted'; (3) loop_close
    carries exit_reason='budget_exhausted'; (4) verdict is PARTIAL.
    """
    def cost_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 2.0,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=cost_runner)
    # pre-seed so iter1 body -> cost 5.0 (not >5), iter2 body -> 7.0 (>5, trips)
    h._cost_total = 3.0
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")}, ["u1"])
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    # (1) partial work IS recorded (2 agent_results: iter1 + the iter2 trip)
    ars = _agent_results(h.ledger, "a1")
    assert len(ars) == 2, (
        f"partial work must be auditable (2 agent_results); got {len(ars)}")
    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    assert ars[1]["payload"]["loop_id"] == loop_id
    assert ars[1]["payload"]["iteration"] == 2

    # (2) iteration_close carries exit_reason='budget_exhausted'
    ics = _iter_closes(h.ledger)
    assert len(ics) == 2
    assert ics[1]["payload"]["exit_reason"] == "budget_exhausted"
    assert ics[1]["payload"]["iteration"] == 2

    # (3) loop_close carries exit_reason='budget_exhausted'
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 2

    # (4) PARTIAL (partial work auditable, not silently dropped)
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


# =====================================================================
# Q5: test_until_vs_repeat_semantics
# =====================================================================

def test_until_vs_repeat_semantics():
    """Q5 guard: until is a DISTINCT node (arbitrary cond + budget_aware);
    repeat keeps the dry metric (until field). They are NOT conflatable.

    BYPASS ATTEMPT: treat until and repeat as the same node kind (conflate
    cond with until, drop budget_aware). If they were the same dataclass /
    same required fields, the distinction would be lost.

    ASSERTION: (1) RepeatNode and UntilNode are different dataclasses with
    different field sets — RepeatNode has `until` (not `cond`/`budget_aware`);
    UntilNode has `cond`+`budget_aware` (not `until`); (2) validate_spec
    enforces different required fields (repeat requires `until`, until
    requires `cond`); (3) a repeat with `cond` is rejected, an until with
    `until` is rejected (can't swap their fields).
    """
    # (1) different dataclasses with different field sets
    repeat_fields = {f.name for f in dataclasses.fields(RepeatNode)}
    until_fields = {f.name for f in dataclasses.fields(UntilNode)}
    assert "until" in repeat_fields and "cond" not in repeat_fields, (
        f"RepeatNode must have `until` (dry metric), NOT `cond`; "
        f"fields: {repeat_fields}")
    assert "budget_aware" not in repeat_fields, (
        "RepeatNode must NOT have `budget_aware` (that's UntilNode's field)")
    assert "cond" in until_fields and "budget_aware" in until_fields, (
        f"UntilNode must have `cond`+`budget_aware`; fields: {until_fields}")
    assert "until" not in until_fields, (
        "UntilNode must NOT have `until` (that's RepeatNode's dry-metric field)")
    assert repeat_fields != until_fields, "repeat and until must be distinct"

    # (2) validate_spec enforces different required fields
    # repeat missing `until` -> rejected
    bad_repeat = _repeat("r1", ["a1"], max_iterations=3)
    bad_repeat.pop("until")
    errs_r = validate_spec(_spec({"r1": bad_repeat, "a1": _agent("a1")}, ["r1"]))
    assert any("until" in e and "r1" in e for e in errs_r), (
        f"repeat must require `until`; errors: {errs_r}")

    # until missing `cond` -> rejected
    bad_until = _until("u1", ["a1"], max_iterations=3)
    bad_until.pop("cond")
    errs_u = validate_spec(_spec({"u1": bad_until, "a1": _agent("a1")}, ["u1"]))
    assert any("cond" in e and "u1" in e for e in errs_u), (
        f"until must require `cond`; errors: {errs_u}")

    # (3) can't swap fields: repeat with `cond` (but no `until`) -> rejected
    swap_repeat = _repeat("r1", ["a1"], max_iterations=3)
    swap_repeat.pop("until")
    swap_repeat["cond"] = "True"
    errs_swap_r = validate_spec(
        _spec({"r1": swap_repeat, "a1": _agent("a1")}, ["r1"]))
    assert any("until" in e and "r1" in e for e in errs_swap_r), (
        "repeat with `cond` instead of `until` must be rejected (not "
        "conflatable)")

    # until with `until` (but no `cond`) -> rejected
    swap_until = _until("u1", ["a1"], max_iterations=3)
    swap_until.pop("cond")
    swap_until["until"] = "dry<2"
    errs_swap_u = validate_spec(
        _spec({"u1": swap_until, "a1": _agent("a1")}, ["u1"]))
    assert any("cond" in e and "u1" in e for e in errs_swap_u), (
        "until with `until` instead of `cond` must be rejected (not "
        "conflatable)")


# =====================================================================
# Q6: test_gate_escalation_vs_risk_auditable
# =====================================================================

def test_gate_escalation_vs_risk_auditable(tmp_path):
    """Q6 guard: a v2 escalation gate (gate_open.reason='escalation') and a v1
    risk-gate (reason='risk_high') in one ledger are DISTINGUISHABLE
    (derive_verdict / blocked_items can tell them apart).

    BYPASS ATTEMPT: both gate kinds share the `gate` event name. If the reason
    field were absent or ignored, an escalation gate could masquerade as a risk
    gate (or vice versa) — the auditor can't tell WHY the gate opened.

    ASSERTION: (1) both gate_open events carry distinguishable `reason`
    values; (2) project_checkpoint's blocked_items carries each with its own
    reason; (3) derive_verdict is BLOCKED (both unresolved).
    """
    h = Harness(tmp_path / "run", worker_runner=_ok_runner())
    # v1 risk-gate (emitted by _gate directly, as v1 does for risk:high nodes)
    h._gate("risk_high", "risky_node", "spec.v1")
    # v2 escalation gate (emitted by _run_gate's pause path)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")}, ["g1"])
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)

    # (1) both gate_open events present with distinguishable reasons
    gos = [e for e in h.ledger.events() if e.get("kind") == "gate_open"]
    reasons = {e["reason"] for e in gos}
    assert "risk_high" in reasons, "v1 risk-gate must be present"
    assert "escalation" in reasons, "v2 escalation gate must be present"
    assert "risk_high" != "escalation", (
        "risk and escalation reasons must be distinguishable")

    # (2) blocked_items carries each with its own reason
    cp = h.project_checkpoint(spec)
    blocked_reasons = {b["reason"] for b in cp.get("blocked_items", [])}
    assert "risk_high" in blocked_reasons, (
        "blocked_items must carry the risk gate's reason")
    assert "escalation" in blocked_reasons, (
        "blocked_items must carry the escalation gate's reason")

    # (3) both unresolved -> BLOCKED
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


# =====================================================================
# Q7: test_loop_does_not_starve_global_cap (documented limitation)
# =====================================================================

def test_loop_does_not_starve_global_cap(tmp_path):
    """Q7 (documented limitation): spec-global concurrency is preserved — a
    loop does NOT monopolize the global cap. Per-branch concurrency is v2+
    (§10). The documented v2 behavior: a loop's body agents count against
    spec.max_concurrent like any other dispatch (the loop gets NO private
    concurrency pool).

    BYPASS ATTEMPT: a parallel node inside a loop body declares concurrency=32.
    If the loop privatized the concurrency cap (ignoring spec.max_concurrent),
    32 threads would run simultaneously, starving other spec-level work.

    ASSERTION: the parallel node inside the loop body is capped by
    spec.max_concurrent=3 (at most 3 concurrent worker threads). The loop
    does not get its own concurrency pool.
    """
    seen_threads = set()

    def runner(args, cwd=None):
        seen_threads.add(threading.get_ident())
        time.sleep(0.02)
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=runner)
    parallel_node = {
        "type": "parallel", "id": "p1", "over": "{{items}}",
        "body": _agent("pa"), "concurrency": 32,
    }
    # until="dry<1" -> converges after 1 iteration (1 dry round >= 1)
    spec = _spec(
        {"r1": _repeat("r1", ["p1"], max_iterations=3, until="dry<1"),
         "p1": parallel_node, "pa": _agent("pa")},
        ["r1"], max_concurrent=3)
    values = {"items": list(range(8))}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    # the parallel node asked for 32, but spec.max_concurrent=3 caps it
    # (the loop body's parallel shares the global cap — no private pool)
    assert len(seen_threads) <= 3, (
        f"loop body parallel must be capped by spec.max_concurrent=3; "
        f"got {len(seen_threads)} concurrent threads (loop privatized the "
        "cap — Q7 violation)")
    assert len(seen_threads) > 1, (
        "the parallel must actually run concurrently (>1 thread)")
    # loop converged after 1 dry round
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "converged"


# =====================================================================
# Q8: test_iteration_2_not_pop_iteration_1
# =====================================================================

def test_iteration_2_not_pop_iteration_1(tmp_path):
    """Q8 guard: resume cache is keyed by (svid, node_id, loop_id, iteration).
    Iteration-2's pop does NOT return iteration-1's cached result (different
    key → miss → re-dispatch).

    BYPASS ATTEMPT: without the key extension (v1 key = (svid, node_id)), all
    iterations of a body node (fixed node_id) collide into one deque. FIFO pop
    would return iteration-1's result at iteration-2 (stale hit — the wrong
    iteration's work is replayed).

    ASSERTION: iteration-2's pop returns None (MISS → re-dispatch), while
    iteration-1's pop returns the cached hit (positive control).
    """
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    # seed cache with iteration-1's result
    h.ledger.append(_agent_result_event(
        svid, "n_body", {"x": 1},
        loop_id="L1", iteration=1,
        input_hash=_input_hash({"a": 1})))
    spec = _spec({"n_body": _agent("n_body")}, ["n_body"])
    h.build_replay_cache(spec)

    # iteration-2 pop: DIFFERENT key (L1, iteration=2) → MISS → re-dispatch
    h._loop_ctx.ctx = {"loop_id": "L1", "iteration": 2}
    hit2 = h._replay_pop(svid, "n_body", {"a": 1})
    assert hit2 is None, (
        "iteration-2 must NOT pop iteration-1's cached result (different "
        "cache key → miss → re-dispatch); got a stale hit")

    # positive control: iteration-1 pop DOES return its own cached result
    h._loop_ctx.ctx = {"loop_id": "L1", "iteration": 1}
    hit1 = h._replay_pop(svid, "n_body", {"a": 1})
    assert hit1 is not None, (
        "iteration-1 must pop its own cached result (same key → hit)")
    assert hit1["validated_output"] == {"x": 1}


# =====================================================================
# Q9: test_loop_refine_last_verify_wins
# =====================================================================

def test_loop_refine_last_verify_wins(tmp_path):
    """Q9 guard: a claim re-verified across loop iterations (survived at k=1,
    refuted at k=2) → last-write-wins → the LATEST verify_verdict is the truth
    (REFUTED, not stuck at survived).

    BYPASS ATTEMPT: if the state used first-write-wins (stuck at the k=1
    survived verdict), derive_claim_status would return VERIFIED even after
    k=2 refuted it. (The v1 refuted-dominates bug is the opposite failure mode
    — refuted at k=1, survived at k=2 would wrongly stay REFUTED.)

    ASSERTION: (1) survived at k=1, refuted at k=2 → REFUTED (latest wins, not
    stuck at survived); (2) the reverse (refuted at k=1, survived at k=2) →
    VERIFIED (latest wins, NOT refuted-dominates). Both directions together
    prove last-write-wins unambiguously.
    """
    # --- direction 1: survived at k=1, refuted at k=2 → REFUTED ---
    lg1 = Ledger(tmp_path / "run1")
    lg1.append({
        "event_id": "vv_k1", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": True, "refuted": False,
        "payload": {"loop_id": "L1", "iteration": 1},
    })
    lg1.append({
        "event_id": "vv_k2", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": False, "refuted": True,
        "payload": {"loop_id": "L1", "iteration": 2},
    })
    assert derive_claim_status("C1", lg1, svid="spec.v1") == "REFUTED", (
        "survived at k=1, refuted at k=2 → latest wins → REFUTED "
        "(not stuck at survived)")

    # --- direction 2: refuted at k=1, survived at k=2 → VERIFIED ---
    lg2 = Ledger(tmp_path / "run2")
    lg2.append({
        "event_id": "vv_k1", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": False, "refuted": True,
        "payload": {"loop_id": "L1", "iteration": 1},
    })
    lg2.append({
        "event_id": "vv_k2", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1",
        "survived": True, "refuted": False,
        "payload": {"loop_id": "L1", "iteration": 2},
    })
    assert derive_claim_status("C1", lg2, svid="spec.v1") == "VERIFIED", (
        "refuted at k=1, survived at k=2 → latest wins → VERIFIED "
        "(NOT refuted-dominates)")

    # --- three iterations: survived → refuted → survived → VERIFIED ---
    lg3 = Ledger(tmp_path / "run3")
    for k, (surv, refu) in enumerate(
            [(True, False), (False, True), (True, False)], start=1):
        lg3.append({
            "event_id": f"vv_k{k}", "kind": "verify_verdict",
            "spec_version_id": "spec.v1", "claim_id": "C1",
            "survived": surv, "refuted": refu,
            "payload": {"loop_id": "L1", "iteration": k},
        })
    assert derive_claim_status("C1", lg3, svid="spec.v1") == "VERIFIED", (
        "survived→refuted→survived → latest (k=3 survived) wins → VERIFIED")
