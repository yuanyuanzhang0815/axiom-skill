"""v2 Task 14: resume cache key extension (spec §7, Q8).

Cache key changes from (svid, node_id) to (svid, node_id, loop_id, iteration)
for loop-body nodes; (svid, node_id, None, 0) for non-loop (preserves v1 key
space). input_hash = sha256(json.dumps(upstream_values, sort_keys=True)) is a
secondary validation on cache hit: mismatch -> miss + re-dispatch.

Adversarial guard (Q8): iteration-2 must NOT pop iteration-1's cached result
(different key -> miss -> re-dispatch).
"""
import hashlib
import json

from axiom.harness import Harness
from axiom.ir import Spec, Requirement


# ---- helpers ---------------------------------------------------------------

def _env(result_str='{"x": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _agent(nid="n1"):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _spec(steps, svid="spec.v1"):
    return Spec(spec_version_id=svid, parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes={s["id"]: s for s in steps if isinstance(s, dict)},
                control_flow={"type": "sequence",
                              "steps": [s["id"] if isinstance(s, dict) else s
                                        for s in steps]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)


def _input_hash(values):
    """Mirror of Harness._input_hash for test-side expectations."""
    return hashlib.sha256(
        json.dumps(values, sort_keys=True).encode()).hexdigest()


def _agent_result_event(svid, nid, vo, *, loop_id=None, iteration=None,
                        input_hash=None, cost=0.01):
    """Build a ledger agent_result event as dispatch_agent /
    _run_agent_with_retry would record it. Body events carry loop_id +
    iteration in their payload when emitted inside a loop (Task 8 _loop_ctx
    injection). input_hash is recorded at dispatch time (Task 14)."""
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


# --- Q8 adversarial guard -----------------------------------------------

def test_iteration_2_not_pop_iteration_1(tmp_path):
    """Q8: build cache with iteration-1 result; pop at iteration-2 -> MISS
    (different key) -> re-dispatch. The whole point of the key extension.

    Without the key extension, all iterations of a body node (fixed
    node_id) collide into one deque; FIFO pop would return iteration-1's
    result at iteration-2 (stale hit)."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.ledger.append(_agent_result_event(
        svid, "n_body", {"x": 1},
        loop_id="L1", iteration=1,
        input_hash=_input_hash({"a": 1})))
    spec = _spec([_agent("n_body")])
    h.build_replay_cache(spec)
    # pop at iteration 2 (same loop_id L1, different iteration)
    h._loop_ctx.ctx = {"loop_id": "L1", "iteration": 2}
    hit = h._replay_pop(svid, "n_body", {"a": 1})
    assert hit is None  # MISS: iteration-2 != iteration-1 -> re-dispatch


# --- v1 key space preserved ---------------------------------------------

def test_nonloop_node_v1_keyspace_preserved(tmp_path):
    """Non-loop nodes use key (svid, node_id, None, 0) -- v1 keyspace
    preserved. A resume re-walk pops the cached entry correctly (v1 resume
    still works). _loop_ctx.ctx is None outside a loop."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.ledger.append(_agent_result_event(
        svid, "n1", {"x": 1},
        input_hash=_input_hash({"a": 1})))
    spec = _spec([_agent("n1")])
    h.build_replay_cache(spec)
    # non-loop: _loop_ctx.ctx is None -> key (svid, n1, None, 0)
    assert getattr(h._loop_ctx, "ctx", None) is None
    hit = h._replay_pop(svid, "n1", {"a": 1})
    assert hit is not None
    assert hit["validated_output"] == {"x": 1}
    # replay_hit event emitted
    assert any(e.get("kind") == "replay_hit" for e in h.ledger.events())


def test_nonloop_node_v1_keyspace_preserved_no_input_hash(tmp_path):
    """v1 events (no input_hash in payload) still replay correctly -- the
    input_hash check is skipped when the stored hash is None. This is the
    v1 backward-compat guarantee: existing ledgers resume without change."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    # no input_hash field (v1 event)
    h.ledger.append(_agent_result_event(svid, "n1", {"x": 1}))
    spec = _spec([_agent("n1")])
    h.build_replay_cache(spec)
    hit = h._replay_pop(svid, "n1", {"a": 1})
    assert hit is not None
    assert hit["validated_output"] == {"x": 1}


# --- input_hash secondary validation -----------------------------------

def test_input_hash_mismatch_on_hit_is_miss(tmp_path):
    """Same key (svid, node_id, None, 0), but different upstream values ->
    input_hash mismatch -> cache miss + re-dispatch. Guards over-list-change:
    if the upstream values changed, the cached result is stale."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.ledger.append(_agent_result_event(
        svid, "n1", {"x": 1},
        input_hash=_input_hash({"a": 1})))
    spec = _spec([_agent("n1")])
    h.build_replay_cache(spec)
    # same key, but different upstream values -> input_hash mismatch
    hit = h._replay_pop(svid, "n1", {"a": 999})
    assert hit is None  # input_hash mismatch -> miss


def test_input_hash_match_on_hit_is_hit(tmp_path):
    """Same key AND same upstream values (order-independent via sort_keys) ->
    input_hash match -> cache hit. Sanity check that the guard doesn't
    reject valid hits."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.ledger.append(_agent_result_event(
        svid, "n1", {"x": 1},
        input_hash=_input_hash({"a": 1, "b": 2})))
    spec = _spec([_agent("n1")])
    h.build_replay_cache(spec)
    # same key, same upstream values (dict order differs) -> hit
    hit = h._replay_pop(svid, "n1", {"b": 2, "a": 1})
    assert hit is not None
    assert hit["validated_output"] == {"x": 1}


# --- replay does not recreate worktree ---------------------------------

def test_replay_does_not_recreate_worktree(tmp_path):
    """Cached validated_output is replayed WITHOUT worktree recreation
    (artifact_ref already pinned). _worktree_setup must NOT be called on a
    replay hit (spec §7: replay returns the cached JSON directly).

    The replay check (_replay_pop) is BEFORE _worktree_setup in dispatch_agent
    and _run_agent_with_retry -- a hit returns before the worktree path."""
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner,
                project_root=tmp_path)
    a = _agent("n1")
    spec = _spec([a])
    # first run: dispatches n1 (replay miss -> _worktree_setup called, returns
    # None for non-worktree node -> real dispatch).
    h.run(spec)
    n_after_first = len(calls)
    assert n_after_first == 1
    # resume: build cache, re-walk
    h2 = Harness(tmp_path / "run", worker_runner=runner,
                 project_root=tmp_path)
    wt_calls = []
    orig = h2._worktree_setup
    def tracking_setup(*a, **kw):
        wt_calls.append(1)
        return orig(*a, **kw)
    h2._worktree_setup = tracking_setup
    h2.build_replay_cache(spec)
    h2._run_sequence(spec)
    assert len(calls) == n_after_first  # zero new dispatches
    assert len(wt_calls) == 0  # no worktree recreation on replay


# --- loop iteration-1 DOES pop iteration-1 (positive control) ---------

def test_iteration_1_pops_iteration_1(tmp_path):
    """Positive control for Q8: the SAME iteration DOES pop its own cached
    result. build cache with iteration-1; pop at iteration-1 (same loop_id)
    -> HIT. This verifies the key extension doesn't break same-iteration
    resume."""
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.ledger.append(_agent_result_event(
        svid, "n_body", {"x": 1},
        loop_id="L1", iteration=1,
        input_hash=_input_hash({"a": 1})))
    spec = _spec([_agent("n_body")])
    h.build_replay_cache(spec)
    h._loop_ctx.ctx = {"loop_id": "L1", "iteration": 1}
    hit = h._replay_pop(svid, "n_body", {"a": 1})
    assert hit is not None
    assert hit["validated_output"] == {"x": 1}
