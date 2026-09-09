"""Task 11: loop budget/stagnation caps (spec §8, Q4 mid-iteration exit).

Wires `_check_caps`/`_budget_already_exceeded` + a stagnation cap into BOTH
`_run_repeat` and `_run_until`. Per iteration, AFTER the body runs:

  1. Mid-iteration budget exit (Q4): if `_budget_already_exceeded(spec)` (the
     budget tripped DURING the body — the `budget_exhausted` event was already
     emitted inside `_tally` when the body dispatch crossed budget_usd) -> emit
     `iteration_close{exit_reason="budget_exhausted"}` -> break ->
     `loop_close{exit_reason="budget_exhausted"}` -> PARTIAL. The partial
     iteration's work + cost are auditable via the body's `agent_result` events
     + the `iteration_close`. Completing the iteration would risk exceeding
     budget, so the loop exits immediately (Q4).
  2. Stagnation cap: track consecutive stagnating body iterations (an iteration
     is stagnating iff its body emitted a `stagnating` event tagged with the
     loop's loop_id+iteration). If consecutive stagnating count exceeds
     `spec.max_stagnation` -> break -> `loop_close{exit_reason="stagnation_cap"}`
     -> PARTIAL. Honest: stops infinite-retry of a body that isn't progressing.

Both checks apply to repeat AND until. until additionally keeps its Task 9
`budget_aware` PRE-iteration check (refuse to ENTER iteration k when budget
already exceeded); repeat has no budget_aware flag, so it enters the iteration
and the per-body-dispatch `_budget_already_exceeded` guard (in dispatch_agent /
_run_agent_with_retry) skips the body dispatch, then the mid-iteration check
catches it.
"""
import json
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.state import derive_verdict


# ---- helpers ---------------------------------------------------------------

def _ok_runner_cost(cost):
    """Mock runner: conforming {answer:'42'} with the given cost_usd."""
    def _r(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": cost,
            "permission_denials": [], "usage": {},
        }))
    return _r


def _nonconforming_runner(cost):
    """Mock runner: returns {} (non-conforming for a schema requiring
    `claim_id`). Same cognitive signature every dispatch -> stagnates on
    retry (no cognitive delta)."""
    def _r(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({}),
            "session_id": "s", "total_cost_usd": cost,
            "permission_denials": [], "usage": {},
        }))
    return _r


def _agent_loose(nid):
    """Agent with a loose {type:object} schema -> {answer:'42'} conforms."""
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _agent_strict(nid, max_retries=2, on_exhausted="degrade"):
    """Agent whose schema REQUIRES `claim_id` -> {} is non-conforming
    (cognitive failure with a stable signature -> stagnates on retry)."""
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries,
            "retry_guard": "requires_new_evidence",
            "on_exhausted": on_exhausted,
        },
    }


def _repeat(nid, body, max_iterations, until="dry<2", on_exhausted="degrade"):
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


def _spec(nodes, steps, *, budget_usd=5.0, max_stagnation=1,
          success_evidence=("S1:R1=claim:C1",)):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=list(success_evidence), nodes=nodes,
        control_flow={"type": "sequence", "steps": list(steps)},
        decision_trace=[], budget_usd=budget_usd, max_concurrent=16,
        max_agents=1000, max_stagnation=max_stagnation,
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


def _stagnating(ledger):
    return [e for e in ledger.events() if e.get("kind") == "stagnating"]


def _budget_exhausted(ledger):
    return [e for e in ledger.events() if e.get("kind") == "budget_exhausted"]


# ---- mid-iteration budget exit (Q4) ----------------------------------------

def test_repeat_budget_hit_mid_iteration_emits_budget_exhausted_partial(tmp_path):
    """Q4 (repeat): budget trips DURING iteration-2's body dispatch -> the
    conforming agent_result IS recorded (partial work auditable), then
    iteration_close{exit_reason='budget_exhausted'} -> loop_close
    exit_reason='budget_exhausted', final_iteration=2 -> PARTIAL. The loop
    exits immediately rather than completing an iteration that risked exceeding
    budget."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner_cost(2.0))
    # pre-seed cost so iter-2's body dispatch crosses budget_usd=5.0:
    # iter1 -> cost_total 5.0 (not >5), iter2 -> 7.0 (>5 -> trips).
    h._cost_total = 3.0
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<3"),
                  "a1": _agent_loose("a1")}, steps=("r1",))
    out = h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    # partial work auditable: 2 agent_results (iter1 + the iter2 dispatch that
    # tripped budget, recorded BEFORE the capped bail per the v1 _tally design).
    ars = _agent_results(h.ledger, "a1")
    assert len(ars) == 2, f"expected 2 agent_results (partial work), got {len(ars)}"
    # the partial iteration's body events carry loop_id+iteration (§3)
    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    assert ars[1]["payload"]["loop_id"] == loop_id
    assert ars[1]["payload"]["iteration"] == 2

    ics = _iter_closes(h.ledger)
    assert len(ics) == 2
    assert ics[0]["payload"].get("exit_reason") in (None,)  # iter1 normal
    assert ics[1]["payload"]["exit_reason"] == "budget_exhausted"  # iter2 partial
    assert ics[1]["payload"]["iteration"] == 2

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 2

    # exactly one budget_exhausted event (emitted inside _tally on the trip;
    # the mid-iteration re-check is silent via _budget_already_exceeded).
    assert len(_budget_exhausted(h.ledger)) == 1
    # degrade -> soft None -> C1 never VERIFIED -> PARTIAL
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_until_budget_hit_mid_iteration_emits_budget_exhausted_partial(tmp_path):
    """Q4 (until): same mid-iteration budget exit for _run_until. budget_aware
    PRE-iteration check passes (budget not yet exceeded before iter2), body
    dispatch trips budget -> iteration_close{exit_reason='budget_exhausted'}
    -> loop_close{exit_reason='budget_exhausted'} -> PARTIAL."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner_cost(2.0))
    h._cost_total = 3.0  # iter1 -> 5.0 (not >5), iter2 -> 7.0 (>5, trips)
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent_loose("a1")}, steps=("u1",))
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    ars = _agent_results(h.ledger, "a1")
    assert len(ars) == 2  # partial work auditable
    ics = _iter_closes(h.ledger)
    assert len(ics) == 2
    assert ics[1]["payload"]["exit_reason"] == "budget_exhausted"
    assert ics[1]["payload"]["iteration"] == 2
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 2
    assert len(_budget_exhausted(h.ledger)) == 1
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


# ---- budget already exceeded before a body dispatch -> no dispatch ---------

def test_repeat_budget_already_exceeded_no_body_dispatch(tmp_path):
    """repeat has no budget_aware PRE-iteration check (only until does), so it
    ENTERS iteration 1 (iteration_open emitted). The per-body-dispatch
    `_budget_already_exceeded` guard in _run_agent_with_retry refuses to
    dispatch the body agent (no agent_result, no cost). The mid-iteration
    check then catches it: iteration_close{exit_reason='budget_exhausted'} ->
    loop_close{exit_reason='budget_exhausted'}, final_iteration=1 -> PARTIAL."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner_cost(2.0))
    h._cost_total = 10.0  # already over budget_usd=5.0 before the loop
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=3, until="dry<2"),
                  "a1": _agent_loose("a1")}, steps=("r1",))
    out = h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    # body NOT dispatched (no agent_result for a1 — budget guard refused it)
    assert not _agent_results(h.ledger, "a1")
    # iteration WAS entered (repeat has no pre-iteration check)
    assert len(_iter_closes(h.ledger)) == 1
    assert _iter_closes(h.ledger)[0]["payload"]["exit_reason"] == "budget_exhausted"
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 1
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_until_budget_already_exceeded_mid_loop_skips_next_body(tmp_path):
    """until: budget trips in iteration 1's body dispatch. Iteration 1's
    iteration_close carries exit_reason='budget_exhausted' (mid-iteration Q4
    exit). The loop breaks immediately — iteration 2 is never entered (no
    iteration_open for iter 2, no body dispatch). final_iteration=1.
    Distinct from the pre-iteration case (which would have final_iteration=0):
    here the body DID run and trip budget mid-dispatch."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner_cost(3.0))
    h._cost_total = 3.0  # iter1 body dispatch -> 6.0 > 5.0 -> trips mid-iter1
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=3,
                               cond="claim_status('C1') == 'VERIFIED'",
                               budget_aware=True),
                  "a1": _agent_loose("a1")}, steps=("u1",))
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    # iter1 entered + body dispatched (partial work recorded, budget tripped)
    assert len(_agent_results(h.ledger, "a1")) == 1
    ics = _iter_closes(h.ledger)
    assert len(ics) == 1
    assert ics[0]["payload"]["exit_reason"] == "budget_exhausted"
    assert ics[0]["payload"]["iteration"] == 1
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "budget_exhausted"
    assert lc["payload"]["final_iteration"] == 1
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


# ---- stagnation cap --------------------------------------------------------

def test_repeat_stagnation_cap_breaks_loop(tmp_path):
    """stagnation cap (repeat): body agent fails with the same cognitive
    signature every retry (non-conforming {} for a schema requiring claim_id).
    Each iteration's body emits stagnating events (the agent's own retry loop
    stagnates). With max_stagnation=2: iter1 stag (count 1, 1>2 no), iter2 stag
    (count 2, 2>2 no), iter3 stag (count 3, 3>2 YES) -> loop_close
    exit_reason='stagnation_cap', final_iteration=3 -> PARTIAL. The loop stops
    honestly instead of infinite-retrying a body that isn't progressing."""
    h = Harness(tmp_path / "run", worker_runner=_nonconforming_runner(0.01))
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent_strict("a1", max_retries=2, on_exhausted="degrade")},
                 steps=("r1",), max_stagnation=2)
    out = h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    # 3 iterations (cap trips after the 3rd consecutive stagnating iteration)
    ics = _iter_closes(h.ledger)
    assert len(ics) == 3, f"expected 3 iterations before cap, got {len(ics)}"
    # the stagnating iterations' iteration_close are NORMAL (no exit_reason;
    # the cap is a loop-level decision, recorded on loop_close)
    assert all(ic["payload"].get("exit_reason") in (None,) for ic in ics)
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "stagnation_cap"
    assert lc["payload"]["final_iteration"] == 3
    # each iteration's body stagnated (>=1 stagnating event tagged loop_id+iter)
    stag = _stagnating(h.ledger)
    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    per_iter = {k: [e for e in stag
                    if e.get("payload", {}).get("loop_id") == loop_id
                    and e.get("payload", {}).get("iteration") == k]
                for k in (1, 2, 3)}
    assert all(len(v) >= 1 for v in per_iter.values()), (
        f"each stagnating iteration must have >=1 stagnating event: {per_iter}")
    # no budget trip (stagnation, not budget)
    assert not _budget_exhausted(h.ledger)
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_until_stagnation_cap_breaks_loop(tmp_path):
    """stagnation cap (until): same body (non-conforming agent), cond never met
    (claim_status('C1') == 'VERIFIED' stays False). 3 consecutive stagnating
    iterations with max_stagnation=2 -> loop_close exit_reason='stagnation_cap'
    -> PARTIAL."""
    h = Harness(tmp_path / "run", worker_runner=_nonconforming_runner(0.01))
    spec = _spec({"u1": _until("u1", ["a1"], max_iterations=5,
                               cond="claim_status('C1') == 'VERIFIED'"),
                  "a1": _agent_strict("a1", max_retries=2, on_exhausted="degrade")},
                 steps=("u1",), max_stagnation=2)
    out = h._dispatch(spec.nodes["u1"], {}, "spec.v1", spec)

    ics = _iter_closes(h.ledger)
    assert len(ics) == 3
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "stagnation_cap"
    assert lc["payload"]["final_iteration"] == 3
    assert not _budget_exhausted(h.ledger)
    assert out is None
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_stagnation_cap_resets_on_non_stagnating_iteration(tmp_path):
    """A non-stagnating iteration in the middle resets the consecutive streak
    (the cap counts CONSECUTIVE stagnating iterations). With max_stagnation=2:
    stag, ok(reset), stag, stag, stag -> cap trips on the 5th iteration (3
    consecutive after the reset), not earlier.

    A failing iteration (max_retries=2, same cognitive sig) takes 3 attempts
    and emits 2 stagnating events; a conforming iteration takes 1 attempt and
    emits none. The runner conforms only on the 4th call (the single attempt of
    iteration 2) and fails elsewhere, yielding the sequence above."""
    calls = {"n": 0}

    def mixed_runner(args, cwd=None):
        calls["n"] += 1
        # conform on call 4 only (iteration 2's single successful attempt) with
        # a payload that satisfies the strict schema (requires claim_id); fail
        # (non-conforming {}) everywhere else.
        result = {"claim_id": "C1"} if calls["n"] == 4 else {}
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps(result),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=mixed_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=8, until="dry<2"),
                  "a1": _agent_strict("a1", max_retries=2, on_exhausted="degrade")},
                 steps=("r1",), max_stagnation=2)
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    lc = _loop_close(h.ledger)
    # iter1 stag (count1), iter2 ok (reset to 0), iter3 stag (1), iter4 stag (2),
    # iter5 stag (3 >2 -> cap). final_iteration=5.
    assert lc["payload"]["exit_reason"] == "stagnation_cap", (
        f"reset must prevent an early cap; got {lc['payload']['exit_reason']}")
    assert lc["payload"]["final_iteration"] == 5, (
        f"expected cap at iter 5 (3 consecutive after reset), "
        f"got {lc['payload']['final_iteration']}")


# ---- convergence beats stagnation cap --------------------------------------

def test_repeat_convergence_beats_stagnation_cap(tmp_path):
    """If an iteration stagnates BUT the convergence predicate is also met
    (dry_count >= N), the loop exits 'converged' — stagnation cap does not
    override a successful convergence. (For repeat this is hard to construct
    since stagnating -> non-dry -> dry_count resets; this test documents that
    the convergence check runs BEFORE the stagnation cap check, so a converged
    loop never trips the stagnation cap.) A clean-converging repeat (no
    stagnation) exits 'converged' with no stagnation_cap."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner_cost(0.0))
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent_loose("a1")}, steps=("r1",), max_stagnation=2)
    out = h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "converged"
    assert _loop_close(h.ledger)["payload"]["final_iteration"] == 2
    assert out is not None
    assert out["validated_output"] == {"answer": "42"}
