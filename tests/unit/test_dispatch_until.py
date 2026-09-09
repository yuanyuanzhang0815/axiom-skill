"""Task 9: until node dispatch (general loop-until-cond, spec §1.3/§2/§3/§8).

`_dispatch` routes a `type:"until"` node to `_run_until`, which:
  1. generates a unique loop_id; emits loop_open{loop_id, node_id,
     max_iterations, cond} (UntilNode uses cond, not until).
  2. for k in 1..max_iterations:
       a. budget_aware PRE-iteration check: if node.budget_aware (default
          True) and _budget_already_exceeded(spec) -> exit immediately:
          loop_close{final_iteration:k-1, exit_reason='budget_exhausted'}
          WITHOUT entering iteration k (no iteration_open, no body dispatch).
          PARTIAL via the downstream degrade routing. (Mid-iteration
          _check_caps exit is Task 11, NOT this task.)
       b. emit iteration_open{loop_id, iteration:k}.
       d. run node["body"] via _run_steps — BODY EVENTS (agent_result /
          verify_verdict / stagnating / artifact_write) carry loop_id+
          iteration:k IN THEIR PAYLOAD via the Task 8 _loop_ctx thread-local
          (reused, not reinvented). DO-UNTIL: body runs FIRST.
       c. eval node["cond"] via _eval_predicate -> (value, claim_strength).
          value is True|False|'undefined'; the loop exits when value is True.
          cond on claim_status(...)=='VERIFIED' -> claim_strength='verified'
          (binding); cond on a PROPOSED {{ref}} -> 'proposed' (advisory, Q2).
          Evaluated AFTER the body (do-until) so the exit uses the POST-body
          ledger state (no wasted body iteration).
       e. emit iteration_close{loop_id, iteration:k, condition_eval, dry_count,
          claim_strength} (dry_count via state.derive_dry_count for audit;
          until uses cond, not the dry metric).
       f. if value is True (cond met) -> converged; break.
  3. emit loop_close{loop_id, final_iteration:k, exit_reason} — 'converged'
     if cond met, 'exhausted' if max_iterations hit without cond. (Task 11
     ADDS 'budget_exhausted' mid-iteration exit + 'stagnation_cap'.)
  4. if not converged -> _exhaust with failure_policy.on_exhausted: default
     'degrade' -> soft None -> PARTIAL; 'block' -> BLOCKED; 'replan' ->
     replan_requested.

Invariants: (1) loop exit DETERMINISTIC (cond eval is a pure ledger read;
max_iterations hard bound — NEVER an LLM call decides continuation);
(2) exits via cond, NOT mid-loop replan; (3) cond on VERIFIED=binding,
cond on PROPOSED=advisory (Q2).
"""
import json
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.state import derive_verdict, derive_dry_count


# ---- helpers ---------------------------------------------------------------

def _ok_runner(args, cwd=None):
    """Mock worker runner: always returns a conforming {answer:'42'}.
    A body of only this agent emits agent_result and NO verify_verdict /
    stagnating -> every iteration is DRY (dry_count increments, but until
    uses cond, not the dry metric)."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _skeptic_survive_runner(args, cwd=None):
    """Mock runner: always returns {refuted: false}. A verify body with this
    skeptic emits verify_verdict with survived=True -> C1 becomes VERIFIED
    after the first body run (so cond claim_status('C1')=='VERIFIED' is
    True starting from iteration 2)."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"refuted": False, "evidence_ref": "e"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _agent(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _verify_node(vid, sc=1):
    return {
        "type": "verify", "id": vid, "target": "{{findings}}",
        "skeptic_count": sc, "skeptic_prompt": "refute",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }


def _until(nid, body, max_iterations, cond="True", budget_aware=True,
           on_exhausted="degrade"):
    return {
        "type": "until", "id": nid, "body": body,
        "max_iterations": max_iterations, "cond": cond,
        "budget_aware": budget_aware,
        "failure_policy": {"on_exhausted": on_exhausted},
    }


def _spec(nodes, steps=("u1",), success_evidence=("S1:R1=claim:C1",)):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=list(success_evidence), nodes=nodes,
        control_flow={"type": "sequence", "steps": list(steps)},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
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


# ---- convergence (cond met -> converged) -----------------------------------

def test_cond_met_converges(tmp_path):
    """cond='claim_status('C1')=='VERIFIED'' met after 1 iter (do-until) ->
    exit_reason='converged'. Body is a verify with a surviving skeptic ->
    C1 becomes VERIFIED during iter 1's body. Do-until: body runs first,
    then cond is evaluated (now True) -> break at the END of iter 1 (no
    wasted second body run)."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive_runner)
    spec = _spec({"u1": _until("u1", ["v1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)

    lo = _loop_open(h.ledger)
    assert lo["payload"]["cond"] == "claim_status('C1') == 'VERIFIED'"
    assert "until" not in lo["payload"]
    assert lo["payload"]["max_iterations"] == 5
    assert lo["node_id"] == "u1"

    ics = _iter_closes(h.ledger)
    assert len(ics) == 1  # do-until: body makes cond True -> exit at end of iter 1
    assert ics[0]["payload"]["iteration"] == 1
    assert ics[0]["payload"]["condition_eval"] == "claim_status('C1') == 'VERIFIED'"

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "converged"
    assert lc["payload"]["final_iteration"] == 1


# ---- exhaustion (cond never met -> exhausted -> degrade -> PARTIAL) --------

def test_cond_never_met_exhausted_degrade_partial(tmp_path):
    """cond never met (body is a plain agent, no verify -> C1 stays PROPOSED)
    -> all iterations False -> exit_reason='exhausted'. on_exhausted='degrade'
    (UntilNode default) -> soft None -> C1 never VERIFIED -> PARTIAL."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    assert lc["payload"]["final_iteration"] == 3
    assert len(_iter_closes(h.ledger)) == 3
    # degrade: no gate_open for the until node
    assert not any(e.get("kind") == "gate_open" and e.get("node_id") == "u1"
                   for e in h.ledger.events())
    assert out is None  # degrade returns soft None
    # C1 never verified -> R1 unmet -> PARTIAL
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


# ---- budget_aware PRE-iteration check (Q4 pre-iteration; mid-iteration = Task 11)

def test_budget_aware_pre_iteration_skips_dispatch(tmp_path):
    """budget_aware=True (default) + _budget_already_exceeded before iter k ->
    loop_close exit_reason='budget_exhausted' WITHOUT entering iteration k
    (no iteration_open, no body dispatch). final_iteration=k-1 (0 if budget
    exceeded before iter 1). PARTIAL via the downstream degrade routing."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # pre-seed cost beyond budget_usd=5.0 (simulates budget already crossed
    # by prior dispatches; _budget_already_exceeded reads _cost_total).
    h._cost_total = 10.0
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 0  # no iteration entered
    # no iteration_open / iteration_close events (loop exited pre-iteration)
    assert not any(e.get("kind") == "iteration_open"
                   for e in h.ledger.events())
    assert not any(e.get("kind") == "iteration_close"
                   for e in h.ledger.events())
    # body NOT dispatched (no agent_result for the body agent)
    assert not _agent_results(h.ledger, "a1")
    # degrade -> soft None -> PARTIAL
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_budget_aware_false_skips_pre_iteration_check(tmp_path):
    """budget_aware=False: the pre-iteration budget check is SKIPPED. The
    loop runs normally (cond never met -> exhausted), even though budget_aware
    is explicitly False. No 'budget_exhausted' exit_reason."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=2,
                               cond="claim_status('C1') == 'VERIFIED'",
                               budget_aware=False),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    assert lc["payload"]["final_iteration"] == 2


def test_budget_aware_mid_iteration_exit(tmp_path):
    """Q4 mid-iteration budget exit: budget hit DURING iteration-2's body
    dispatch -> the conforming agent_result IS recorded (partial work
    auditable), then iteration_close exit_reason='budget_exhausted' then
    loop_close exit_reason='budget_exhausted' -> PARTIAL. The budget_aware
    PRE-iteration check passed for iter2 (budget not yet exceeded before the
    body); the body dispatch itself crossed budget_usd -> mid-iteration exit."""
    def cost_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 2.0,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=cost_runner)
    # pre-seed so iter1 body -> cost_total 5.0 (not >5), iter2 body -> 7.0 (>5).
    h._cost_total = 3.0
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    # partial work auditable: 2 agent_results (iter1 + the iter2 dispatch that
    # tripped budget, recorded before the capped bail).
    ars = _agent_results(h.ledger, "a1")
    assert len(ars) == 2
    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    assert ars[1]["payload"]["loop_id"] == loop_id
    assert ars[1]["payload"]["iteration"] == 2

    ics = _iter_closes(h.ledger)
    assert len(ics) == 2
    assert ics[0]["payload"].get("exit_reason") in (None,)  # iter1 normal
    assert ics[1]["payload"]["exit_reason"] == "budget_exhausted"
    assert ics[1]["payload"]["iteration"] == 2

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 2
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


# ---- cond on PROPOSED is advisory (§2 Q2) -----------------------------------

def test_cond_on_proposed_is_advisory(tmp_path):
    """cond referencing a raw {{ref}} (PROPOSED validated_output) yields
    claim_strength='proposed' (advisory, Q2). The loop exits when cond is
    True, but the exit is advisory — recorded in iteration_close, not
    binding (unlike cond on claim_status(...)=='VERIFIED')."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)

    ics = _iter_closes(h.ledger)
    assert len(ics) == 1  # cond True at iter 1 -> converged after 1 iteration
    assert ics[0]["payload"]["claim_strength"] == "proposed"
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "converged"
    assert lc["payload"]["final_iteration"] == 1


def test_cond_on_verified_is_binding(tmp_path):
    """cond on claim_status(...)=='VERIFIED' yields claim_strength='verified'
    (binding). Pre-seed a survived verify_verdict so cond is True at iter 1 ->
    converged after 1 iteration."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # pre-seed a survived verify_verdict so claim_status('C1') == 'VERIFIED'
    h.ledger.append({
        "event_id": "E0", "kind": "verify_verdict",
        "spec_version_id": "spec.v1", "claim_id": "C1", "survived": True,
    })
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    ics = _iter_closes(h.ledger)
    assert len(ics) == 1
    assert ics[0]["payload"]["claim_strength"] == "verified"
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "converged"


# ---- body tagging (spec §3 rule: loop_id+iteration in payload) -------------

def test_body_events_carry_loop_id_iteration_payload(tmp_path):
    """Body agent_result events carry loop_id+iteration:k IN THEIR PAYLOAD
    (spec §3, invariant 6 — reuses Task 8's _loop_ctx thread-local mechanism,
    NOT reinvented). A kind suffix would break kind-filtered projections."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    a_results = _agent_results(h.ledger, "a1")
    assert len(a_results) == 1  # one iteration (cond True at iter 1)
    for e in a_results:
        assert e["payload"]["loop_id"] == loop_id, "missing loop_id in payload"
        assert e["payload"]["iteration"] == 1, "missing iteration in payload"
        assert e.get("kind") == "agent_result"  # no kind suffix


def test_body_verify_verdict_carries_loop_id_iteration(tmp_path):
    """A verify body's verify_verdict events carry loop_id+iteration in
    payload (Task 8's _loop_ctx tags body events — reused in _run_until).
    Do-until: body makes C1 VERIFIED -> cond True -> exit after 1 iteration."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive_runner)
    spec = _spec({"u1": _until("u1", ["v1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    vv = [e for e in h.ledger.events() if e.get("kind") == "verify_verdict"]
    assert len(vv) == 1  # do-until: 1 body run -> 1 verify_verdict -> cond True -> exit
    for e in vv:
        assert e["payload"]["loop_id"] == loop_id
        assert e["payload"]["iteration"] == 1
        assert e.get("survived") is True


# ---- structural invariants -------------------------------------------------

def test_loop_close_emitted_once(tmp_path):
    """Exactly one loop_close event per loop instance, regardless of
    convergence vs exhaustion vs budget_exhausted."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)
    assert len([e for e in h.ledger.events() if e.get("kind") == "loop_close"]) == 1


def test_max_iterations_hard_bound_cannot_loop_forever(tmp_path):
    """max_iterations is a HARD bound: cond never met -> exits at
    max_iterations (cannot-loop-forever guard)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=4,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    assert len(_iter_closes(h.ledger)) == 4
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "exhausted"
    assert _loop_close(h.ledger)["payload"]["final_iteration"] == 4


def test_loop_close_hash_chain_intact(tmp_path):
    """The full loop lifecycle preserves the hash chain (invariant 5)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)
    assert h.ledger.verify_chain() == []


def test_converged_returns_last_body_output(tmp_path):
    """On convergence, _run_until returns the last iteration's body output
    ({validated_output: ...})."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    out = h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)
    assert out is not None
    assert out["validated_output"] == {"answer": "42"}


def test_loop_id_unique_per_dispatch(tmp_path):
    """Two dispatches of the same until node yield different loop_ids
    (unique per loop instance)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="{{n1.validated_output.x}} == 'y'"),
                  "a1": _agent("a1")})
    values = {"n1": {"validated_output": {"x": "y"}}}
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)
    h._dispatch(spec.nodes["u1"], values, "spec.v1", spec)
    opens = [e for e in h.ledger.events() if e.get("kind") == "loop_open"]
    assert len(opens) == 2
    assert opens[0]["payload"]["loop_id"] != opens[1]["payload"]["loop_id"]


# ---- exhaustion routing (failure_policy.on_exhausted) ----------------------

def test_on_exhausted_block_routes_through_gate(tmp_path):
    """on_exhausted='block' -> _exhaust -> _gate -> gate_open unresolved ->
    derive_verdict BLOCKED. UntilNode default is 'degrade'; 'block' is
    opt-in (required-convergence semantics for until)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=2,
                               cond="claim_status('C1') == 'VERIFIED'",
                               on_exhausted="block"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    gate_opens = [e for e in h.ledger.events()
                  if e.get("kind") == "gate_open" and e.get("node_id") == "u1"]
    assert len(gate_opens) == 1
    assert out is None
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


def test_on_exhausted_replan_emits_replan_requested(tmp_path):
    """on_exhausted='replan' -> _exhaust emits replan_requested (boundary
    signal, no gate). Returns None."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=2,
                               cond="claim_status('C1') == 'VERIFIED'",
                               on_exhausted="replan"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)
    assert out is None
    assert any(e.get("kind") == "replan_requested"
               for e in h.ledger.events())
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "exhausted"
