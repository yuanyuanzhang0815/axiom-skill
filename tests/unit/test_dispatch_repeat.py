"""Task 8: repeat node dispatch (loop-until-dry convergence, spec §1.2/§3/§4/§8).

`_dispatch` routes a `type:"repeat"` node to `_run_repeat`, which:
  1. parses `until` (default "dry<2" -> N=2); generates a unique loop_id.
  2. emits loop_open{loop_id, node_id, max_iterations, until}.
  3. for k in 1..max_iterations:
       emit iteration_open{loop_id, iteration:k};
       run node["body"] via _run_steps — BODY EVENTS (agent_result /
       verify_verdict / stagnating / artifact_write) carry loop_id+iteration:k
       IN THEIR PAYLOAD (spec §3 rule, invariant 6);
       compute dry_count via state.derive_dry_count (re-derivation, §4);
       emit iteration_close{loop_id, iteration:k, condition_eval, dry_count};
       if until="dry<N" and dry_count>=N -> converged; break.
  4. emit loop_close{loop_id, final_iteration:k, exit_reason} — 'converged'
     or 'exhausted'. (Task 11 adds 'budget_exhausted'/'stagnation_cap'.)
  5. if not converged -> _exhaust(node, svid, reason, localized=False) with
     failure_policy.on_exhausted: block->BLOCKED; degrade->soft None->PARTIAL;
     replan->replan_requested. Default block -> BLOCKED (required-convergence).

Invariants (1) deterministic loop exit (dry_count re-derivation + max_iterations
hard bound — NEVER an LLM call); (6) dry_count is a re-derivation, not cached.
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
    stagnating -> every iteration is DRY."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _skeptic_refute_runner(args, cwd=None):
    """Mock runner: always returns {refuted: true}. A verify body with this
    skeptic emits verify_verdict with survived=false every iteration -> every
    iteration is NON-DRY (dry_count never reaches N)."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"refuted": True, "evidence_ref": "e"}),
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


def _repeat(nid, body, max_iterations, until="dry<2", on_exhausted="block"):
    return {
        "type": "repeat", "id": nid, "body": body,
        "max_iterations": max_iterations, "until": until,
        "failure_policy": {"on_exhausted": on_exhausted},
    }


def _spec(nodes, steps=("r1",), success_evidence=("S1:R1=claim:C1",)):
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


# ---- convergence -----------------------------------------------------------

def test_dry_converges_after_two_dry_rounds(tmp_path):
    """until='dry<2', body is a successful agent (every iteration dry).
    Iteration 1: dry_count=1 (1<2, continue). Iteration 2: dry_count=2
    (2>=2 -> converged, break). loop_close exit_reason='converged',
    final_iteration=2."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    lo = _loop_open(h.ledger)
    loop_id = lo["payload"]["loop_id"]
    assert lo["payload"]["max_iterations"] == 5
    assert lo["payload"]["until"] == "dry<2"
    assert lo["node_id"] == "r1"

    ics = _iter_closes(h.ledger)
    assert len(ics) == 2  # converged after 2 iterations
    # dry_count is a re-derivation (invariant 6): k=1 -> 1, k=2 -> 2
    assert ics[0]["payload"]["dry_count"] == 1
    assert ics[0]["payload"]["iteration"] == 1
    assert ics[0]["payload"]["condition_eval"] == "dry<2"
    assert ics[1]["payload"]["dry_count"] == 2
    assert ics[1]["payload"]["iteration"] == 2

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "converged"
    assert lc["payload"]["final_iteration"] == 2
    assert lc["payload"]["loop_id"] == loop_id


def test_dry_converges_default_until_is_dry_lt_2(tmp_path):
    """Default until='dry<2' (omitted on the node) converges after 2 dry
    rounds. RepeatNode's dataclass default is 'dry<2'."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # until omitted -> default "dry<2" (ir.py RepeatNode default)
    rep = _repeat("r1", ["a1"], max_iterations=5)
    rep.pop("until")  # simulate the default
    spec = _spec({"r1": rep, "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "converged"
    assert _loop_close(h.ledger)["payload"]["final_iteration"] == 2
    assert _loop_open(h.ledger)["payload"]["until"] == "dry<2"


def test_dry_lt_1_converges_after_one_dry_round(tmp_path):
    """until='dry<1' converges after a single dry round (N=1)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=3, until="dry<1"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "converged"
    assert lc["payload"]["final_iteration"] == 1
    assert _iter_closes(h.ledger)[0]["payload"]["dry_count"] == 1


# ---- exhaustion -----------------------------------------------------------

def test_max_iterations_exhausted_block(tmp_path):
    """Body is a verify that always refutes -> every iteration non-dry
    (dry_count=0, never reaches 2). max_iterations=3 -> loop_close
    exit_reason='exhausted', final_iteration=3. on_exhausted='block' (default)
    -> _exhaust -> _gate -> gate_open unresolved -> derive_verdict BLOCKED."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=3, until="dry<2"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    out = h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    assert lc["payload"]["final_iteration"] == 3
    # all 3 iterations non-dry (dry_count=0)
    ics = _iter_closes(h.ledger)
    assert len(ics) == 3
    assert all(ic["payload"]["dry_count"] == 0 for ic in ics)
    # block: an unresolved gate_open for the repeat node
    gate_opens = [e for e in h.ledger.events()
                  if e.get("kind") == "gate_open" and e.get("node_id") == "r1"]
    assert len(gate_opens) == 1
    # dispatch returns None (block routes through _gate, no output)
    assert out is None
    # derive_verdict: unresolved gate -> BLOCKED
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


def test_on_exhausted_degrade_partial(tmp_path):
    """on_exhausted='degrade' -> no gate; _exhaust returns None. The verify
    body refutes C1 (survived=false) -> latest verify_verdict for C1 is
    REFUTED -> R1:claim:C1 unmet -> derive_verdict PARTIAL."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=2,
                                 until="dry<2", on_exhausted="degrade"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    out = h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    lc = _loop_close(h.ledger)
    assert lc["payload"]["exit_reason"] == "exhausted"
    # degrade: no gate_open for the repeat node
    assert not any(e.get("kind") == "gate_open" and e.get("node_id") == "r1"
                   for e in h.ledger.events())
    assert out is None  # degrade returns soft None
    # C1 refuted -> R1 unmet -> PARTIAL
    assert derive_verdict(spec, h.ledger) == "PARTIAL"


def test_on_exhausted_replan_emits_replan_requested(tmp_path):
    """on_exhausted='replan' -> _exhaust emits replan_requested (boundary
    signal, no gate). Returns None."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=2,
                                 until="dry<2", on_exhausted="replan"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    out = h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)
    assert out is None
    assert any(e.get("kind") == "replan_requested"
               for e in h.ledger.events())
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "exhausted"


# ---- body tagging (spec §3 rule: loop_id+iteration in payload) -----------

def test_body_events_carry_loop_id_iteration_payload(tmp_path):
    """Body agent_result events MUST carry loop_id+iteration:k IN THEIR
    PAYLOAD (spec §3, invariant 6). A kind suffix would silently break
    kind-filtered projections; payload-preserves them (Task 5 rule)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    a_results = _agent_results(h.ledger, "a1")
    # one agent_result per iteration (2 iterations -> converge)
    assert len(a_results) == 2
    for i, e in enumerate(a_results, start=1):
        p = e["payload"]
        assert p["loop_id"] == loop_id, f"iter {i}: missing loop_id in payload"
        assert p["iteration"] == i, f"iter {i}: missing iteration in payload"
    # the body agent_result's kind stays 'agent_result' (no suffix) so the
    # kind-filtered projection derive_claim_status still finds it
    assert all(e.get("kind") == "agent_result" for e in a_results)


def test_body_verify_verdict_carries_loop_id_iteration(tmp_path):
    """A verify body's verify_verdict events carry loop_id+iteration in
    payload. derive_dry_count scans these (survived=false tagged
    loop_id+iteration) -> the re-derivation is the truth (invariant 6)."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=2, until="dry<2"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    vv = [e for e in h.ledger.events() if e.get("kind") == "verify_verdict"]
    assert len(vv) == 2  # one per iteration
    for i, e in enumerate(vv, start=1):
        p = e["payload"]
        assert p["loop_id"] == loop_id
        assert p["iteration"] == i
        assert e.get("survived") is False  # refuted -> non-dry
    # the re-derivation from these tagged events == 0 every iteration
    for k in (1, 2):
        assert derive_dry_count(h.ledger, "spec.v1", loop_id, k) == 0


# ---- loop_close once + hard bound -----------------------------------------

def test_loop_close_emitted_once(tmp_path):
    """Exactly one loop_close event per loop instance, regardless of
    convergence vs exhaustion."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    assert len([e for e in h.ledger.events() if e.get("kind") == "loop_close"]) == 1


def test_max_iterations_hard_bound_cannot_loop_forever(tmp_path):
    """max_iterations is a HARD bound: even if the body keeps producing non-dry
    rounds, the loop exits at max_iterations (cannot-loop-forever guard)."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=4, until="dry<2"),
                  "v1": _verify_node("v1")})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)
    # exactly max_iterations iterations ran (no more)
    assert len(_iter_closes(h.ledger)) == 4
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "exhausted"
    assert _loop_close(h.ledger)["payload"]["final_iteration"] == 4


def test_loop_close_hash_chain_intact(tmp_path):
    """The full loop lifecycle preserves the hash chain (invariant 5)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=3, until="dry<2"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    assert h.ledger.verify_chain() == []


def test_converged_returns_last_body_output(tmp_path):
    """On convergence, _run_repeat returns the last iteration's body output
    ({validated_output: ...})."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=5, until="dry<2"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    assert out is not None
    assert out["validated_output"] == {"answer": "42"}


def test_loop_id_unique_per_dispatch(tmp_path):
    """Two dispatches of the same repeat node yield different loop_ids (unique
    per loop instance)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"r1": _repeat("r1", ["a1"], max_iterations=3, until="dry<2"),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    opens = [e for e in h.ledger.events() if e.get("kind") == "loop_open"]
    assert len(opens) == 2
    assert opens[0]["payload"]["loop_id"] != opens[1]["payload"]["loop_id"]


def test_parallel_branch_in_body_inherits_loop_ctx(tmp_path):
    """A parallel fan-out inside a repeat body: each branch's agent_result
    carries the parent loop's loop_id+iteration. ThreadPoolExecutor workers do
    NOT inherit threading.local, so run_parallel must capture the parent ctx
    and re-set it in each worker (spec §3 body-tagging rule)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    parallel_node = {
        "type": "parallel", "id": "p1", "over": "{{items}}",
        "body": _agent("pa"), "concurrency": 3,
    }
    spec = _spec({"r1": _repeat("r1", ["p1"], max_iterations=3, until="dry<2"),
                  "p1": parallel_node, "pa": _agent("pa")})
    values = {"items": [{"v": 1}, {"v": 2}, {"v": 3}]}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    pa_results = _agent_results(h.ledger, "pa")
    # 3 items per iteration; converges after 2 dry rounds -> 6 agent_results
    assert len(pa_results) == 6
    for e in pa_results:
        assert e["payload"]["loop_id"] == loop_id, "parallel branch missing loop_id"
        assert e["payload"]["iteration"] in (1, 2), "parallel branch missing iteration"
    iter1 = [e for e in pa_results if e["payload"]["iteration"] == 1]
    iter2 = [e for e in pa_results if e["payload"]["iteration"] == 2]
    assert len(iter1) == 3 and len(iter2) == 3
    assert _loop_close(h.ledger)["payload"]["exit_reason"] == "converged"


def test_pipeline_branch_in_body_inherits_loop_ctx(tmp_path):
    """A pipeline fan-out inside a repeat body: each stage's agent_result
    carries the parent loop's loop_id+iteration (same capture+set pattern)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    pipeline_node = {
        "type": "pipeline", "id": "p1", "items": "{{items}}",
        "stages": [_agent("ps")], "concurrency": 2,
    }
    spec = _spec({"r1": _repeat("r1", ["p1"], max_iterations=3, until="dry<2"),
                  "p1": pipeline_node, "ps": _agent("ps")})
    values = {"items": [{"v": 1}, {"v": 2}]}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    ps_results = _agent_results(h.ledger, "ps")
    # 2 items * 1 stage * 2 iterations (converge) = 4 agent_results
    assert len(ps_results) == 4
    for e in ps_results:
        assert e["payload"]["loop_id"] == loop_id
        assert e["payload"]["iteration"] in (1, 2)


def test_verify_skeptic_in_body_inherits_loop_ctx(tmp_path):
    """Skeptics of a verify body run via ThreadPoolExecutor; their agent_result
    events carry the parent loop's loop_id+iteration (run_verify captures+
    re-sets the parent ctx in _run_one_skeptic)."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner)
    # 3 skeptics so they actually run concurrently (sc=1 runs in main thread)
    spec = _spec({"r1": _repeat("r1", ["v1"], max_iterations=2, until="dry<2"),
                  "v1": _verify_node("v1", sc=3)})
    values = {"findings": [{"claim_id": "C1", "text": "t"}]}
    h._dispatch(spec.nodes["r1"], values, "spec.v1", spec)

    loop_id = _loop_open(h.ledger)["payload"]["loop_id"]
    # skeptic agent_result events (node_id starts with _skeptic_)
    sk_results = [e for e in h.ledger.events()
                  if e.get("kind") == "agent_result"
                  and e.get("node_id", "").startswith("_skeptic_")]
    # 3 skeptics * 2 iterations = 6
    assert len(sk_results) == 6
    for e in sk_results:
        assert e["payload"]["loop_id"] == loop_id
        assert e["payload"]["iteration"] in (1, 2)
