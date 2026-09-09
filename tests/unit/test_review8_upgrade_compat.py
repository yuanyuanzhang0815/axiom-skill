"""R8-1 (review8 P2): upgrade compat -- a NEW engine resuming a ledger
written by a PRE-R7-1 engine must reuse the legacy fan-out results.

Pre-R7-1 fan-out branch attempts were tagged exec_occurrence=0 (shared
slot 0, multiplexed by input_hash); R7-1+ tags "<container>:<idx>". The
new engine's resume must fall back to the legacy slot instead of
re-dispatching succeeded branches (or exhausting the agent cap -> None).

The "old" ledger is produced by the CURRENT engine with the exec_pos
plumbing patched out (dispatch_agent drops the kwarg), which is byte-
identical to what the pre-R7-1 engine wrote: payload.exec_occurrence=0.
"""
import json
import threading

import pytest

from axiom.harness import Harness
from axiom.ir import Spec, Requirement


def _env(value, cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": json.dumps(value), "session_id": "s",
        "total_cost_usd": cost, "permission_denials": [], "usage": {},
    })


def _node(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p {{item}}",
        "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _spec(max_agents=1000):
    source = _node("source")
    source["prompt"] = "p"
    parallel = {"type": "parallel", "id": "p",
                "over": "{{source.validated_output.items}}",
                "body": _node("body"), "concurrency": 2}
    nodes = {"source": source, "p": parallel}
    return Spec(spec_version_id="upgrade.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=claim:C1"],
                nodes=nodes,
                control_flow={"type": "sequence", "steps": ["source", "p"]},
                decision_trace=[], budget_usd=50.0, max_concurrent=2,
                max_agents=max_agents, max_stagnation=1)


def _legacy_run(h, spec):
    """Run with the R7-1 exec_pos plumbing removed == pre-R7-1 wire format."""
    orig = Harness.dispatch_agent

    def legacy_dispatch(self, node, upstream_values, spec_version_id,
                        spec=None, localized=False, exec_pos=None):
        assert exec_pos is None or True  # swallowed below regardless
        return orig(self, node, upstream_values, spec_version_id, spec=spec,
                    localized=localized)  # pre-R7-1: no exec_pos forwarded

    Harness.dispatch_agent = legacy_dispatch
    try:
        return h.run(spec)
    finally:
        Harness.dispatch_agent = orig


def _fanout_spec_with_legacy_ledger(tmp_path, max_agents):
    """First walk in legacy format; returns (run_dir, spec, legacy_events)."""
    spec = _spec(max_agents=max_agents)

    def worker(args, cwd=None):
        prompt = args[0]  # [prompt, "--output-format", ...]
        if "one" in prompt:
            return (0, _env({"x": "A"}))
        if "two" in prompt:
            return (0, _env({"x": "B"}))
        return (0, _env({"items": ["one", "two"]}))

    h = Harness(tmp_path / "run", worker_runner=worker)
    first = _legacy_run(h, spec)
    values = [item["validated_output"] for item in first["validated_output"]]
    assert values == [{"x": "A"}, {"x": "B"}], values
    body_events = [e for e in h.ledger.events()
                   if e.get("node_id") == "body" and e["kind"] == "agent_result"]
    # wire-format assertion: this ledger is what pre-R7-1 wrote (int slot 0,
    # no string position tags anywhere).
    assert [e["payload"].get("exec_occurrence") for e in body_events] == [0, 0]
    return tmp_path / "run", spec, len(h.ledger.events())


@pytest.mark.parametrize("max_agents", [1000, 3])
def test_upgrade_replays_legacy_fanout_slot0(tmp_path, max_agents):
    """New engine + old ledger: zero new dispatches, [A, B] preserved.

    max_agents=3 exactly covers source+2 branches of the first walk; if the
    resume wrongly re-dispatched both branches the cap would exhaust and
    produce [None, None] (the review8 P2 failure shape)."""
    run_dir, spec, n_legacy = _fanout_spec_with_legacy_ledger(tmp_path,
                                                              max_agents)
    calls = []

    def surprised(args, cwd=None):
        calls.append(1)
        return (0, _env({"x": "repeated work"}))

    h2 = Harness(run_dir, worker_runner=surprised)
    h2.build_replay_cache(spec)
    resumed = h2.run(spec)
    values = [item["validated_output"] if item else None
              for item in resumed["validated_output"]]
    assert calls == [], f"resume re-dispatched {len(calls)} branch(es)"
    assert values == [{"x": "A"}, {"x": "B"}], values
    hits = [e for e in h2.ledger.events()[n_legacy:]
            if e.get("kind") == "replay_hit" and e.get("node_id") == "body"]
    assert len(hits) == 2
    # position identity of the NEW walk + audit marker of the LEGACY slot.
    assert {h["payload"]["exec_occurrence"] for h in hits} == {"p:0", "p:1"}
    assert all(h["payload"].get("legacy_slot") == 0 for h in hits)


def test_legacy_fallback_keeps_newest_failure_tombstone(tmp_path):
    """A newer FAILED legacy attempt must block the older legacy success.

    Legacy walk: branch 'p:0' (input 'one') succeeds A, then a second legacy
    walk re-dispatches it (cache cleared => miss) and FAILS -> tombstone at
    slot 0 newest batch. Resume must NOT serve the stale A for input 'one'.
    """
    spec = _spec()
    run_dir = tmp_path / "run"
    state = {"mode": "success"}

    def worker(args, cwd=None):
        prompt = args[0]
        if "one" in prompt and state["mode"] == "fail":
            return (1, "boom")  # operational failure -> tombstone
        if "one" in prompt:
            return (0, _env({"x": "A"}))
        if "two" in prompt:
            return (0, _env({"x": "B"}))
        return (0, _env({"items": ["one", "two"]}))

    h = Harness(run_dir, worker_runner=worker)
    _legacy_run(h, spec)
    # second legacy walk over a FRESH harness whose empty replay cache forces
    # re-dispatch; branch 'one' now fails (slot-0 tombstone at a newer run).
    state["mode"] = "fail"
    h_mid = Harness(run_dir, worker_runner=worker)
    _legacy_run(h_mid, spec)
    # upgrade: resume with the new engine. 'two' replays B; 'one' must miss
    # (tombstone) and re-dispatch -- NOT fall back to the stale A.
    state["mode"] = "success"
    redispatched = []

    def worker2(args, cwd=None):
        prompt = args[0]
        if "one" in prompt:
            redispatched.append("one")
            return (0, _env({"x": "A2"}))
        return (0, _env({"x": "unexpected"}))

    h2 = Harness(run_dir, worker_runner=worker2)
    h2.build_replay_cache(spec)
    resumed = h2.run(spec)
    values = [item["validated_output"] if item else None
              for item in resumed["validated_output"]]
    assert redispatched == ["one"]
    assert values == [{"x": "A2"}, {"x": "B"}], values


def test_new_position_slot_wins_over_legacy_slot0(tmp_path):
    """Mixed ledger: once a NEW-format attempt exists at 'p:0', the legacy
    slot-0 entry for the same input is NOT served for that position -- new
    data owns the position. (Legacy walk, then a failed new-format walk
    re-dispatching only branch 0, then resume.)"""
    spec = _spec()
    run_dir = tmp_path / "run"
    state = {"fail_one": False}

    def worker(args, cwd=None):
        prompt = args[0]
        if "one" in prompt:
            if state["fail_one"]:
                return (1, "boom")
            return (0, _env({"x": "A"}))
        if "two" in prompt:
            return (0, _env({"x": "B"}))
        return (0, _env({"items": ["one", "two"]}))

    _legacy_run(Harness(run_dir, worker_runner=worker), spec)
    # new-format walk with branch 'one' failing -> tombstone at slot "p:0",
    # while legacy slot 0 still holds the older success A for the same hash.
    state["fail_one"] = True
    Harness(run_dir, worker_runner=worker).run(spec)
    state["fail_one"] = False
    redispatched = []

    def worker2(args, cwd=None):
        prompt = args[0]
        if "one" in prompt:
            redispatched.append("one")
            return (0, _env({"x": "A3"}))
        return (0, _env({"x": "unexpected"}))

    h2 = Harness(run_dir, worker_runner=worker2)
    h2.build_replay_cache(spec)
    resumed = h2.run(spec)
    values = [item["validated_output"] if item else None
              for item in resumed["validated_output"]]
    # 'p:0' has its own (failed) slot -> no legacy fallback -> re-dispatch.
    # 'p:1' has no new-format entries -> legacy fallback serves B.
    assert redispatched == ["one"]
    assert values == [{"x": "A3"}, {"x": "B"}], values
