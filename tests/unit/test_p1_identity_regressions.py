"""P1 identity-model regressions — attempt/result success is content-addressed.

Each test maps to a probe in axiom-engine-review3-evidence.json and proves
the fix for the false-VERIFIED / stale-verdict / failed-replay family. These
are NOT success-path tests; they are the failure paths the three rechecks
showed were still reproducible at 800cd7c.

  #6  failed_skeptic_false_verified     -> test_failed_skeptic_abstains_not_survives
  #6b zero_exit_error_envelope          -> test_envelope_is_error_not_clean
  #7  latest_failed_attempt_stale_verdict -> test_stale_verdict_after_failed_attempt
  #9  failed_dispatch_replayed          -> test_failed_dispatch_not_cached_as_success
"""
import json
import tempfile

from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.state import _agent_verdict_verified


def _env(result_str='{"x": 1}', cost=0.01, is_error=False):
    """Build a host dispatch stdout envelope. is_error=True injects the envelope
    error flag (exit 0 + is_error=true was the zero_exit_error_envelope probe)."""
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {}, "is_error": is_error,
    })


def _verify_node(sc=1):
    return {
        "type": "verify", "id": "v", "target": "{{findings}}",
        "skeptic_count": sc, "skeptic_prompt": "refute",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }


# ---------------------------------------------------------------------------
# #6: a skeptic whose process FAILED (exit_code=1) but whose stdout happens to
# parse as schema-conforming {refuted:false} must NOT survive -> must abstain.
# Before P1-6, dispatch_agent had no exit_code guard, so the failed skeptic's
# {refuted:false} flowed through -> survived -> VERIFIED -> sedimented as a
# "proven contract" (false_verdict_admitted_as_contract probe).
# ---------------------------------------------------------------------------
def test_failed_skeptic_abstains_not_survives(tmp_path):
    # skeptic returns exit_code=1 (process failed) but conforming {refuted:false}
    def runner(args, cwd=None):
        return (1, _env(json.dumps({"refuted": False, "evidence_ref": "e"})))
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h.run_verify(
        _verify_node(1),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    # the failed skeptic abstains (G4) -- NOT survived. survivors == [] means
    # the claim did not reach VERIFIED via a failed worker.
    assert out["survivors"] == [], (
        "a skeptic whose process failed (exit_code=1) must abstain, not survive "
        "-- surviving on a failed worker's conforming JSON was the false-VERIFIED hole"
    )
    # the verify_verdict event records did_survive=False
    vv = [e for e in h.ledger.events() if e.get("kind") == "verify_verdict"]
    assert vv and vv[-1].get("survived") is False


# ---------------------------------------------------------------------------
# #6b: a worker with exit_code=0 but envelope is_error=true + conforming
# verdict JSON must NOT be promoted to success evidence. Before P1-6,
# DispatchResult dropped is_error after classify_retry_class, so exit==0 passed
# the top-level success gate even though the envelope reported an error.
# ---------------------------------------------------------------------------
def test_envelope_is_error_not_clean(tmp_path):
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        # exit_code=0, but envelope is_error=true; body is conforming verdict
        return (0, _env(json.dumps({"verdict": "VERIFIED"}), is_error=True))

    h = Harness(tmp_path / "run", worker_runner=runner)
    # an agent node that owns a requirement's verdict via verdict_field
    node = {
        "type": "agent", "id": "n_ver", "prompt": "verify", "dispatch": "host",
        "output_schema": {"type": "object", "properties": {
            "verdict": {"type": "string"}}, "required": ["verdict"]},
        "verdict_field": "verdict",
        "allowed_tools": [], "write_areas": [], "acceptance": ["v"],
        "failure_policy": {"max_retries": 0, "retry_guard": "requires_new_evidence",
                            "on_exhausted": "degrade"},
    }
    result = h.dispatch_agent(node, {}, "spec.v1")
    # the envelope-is_error dispatch is NOT clean -> returns None (no agent_verdict)
    assert result is None, (
        "exit_code=0 + envelope is_error=true must NOT yield a validated result -- "
        "the prior hole accepted conforming verdict JSON from an error envelope"
    )
    # no agent_verdict event was emitted (so R stays unmet, not VERIFIED)
    assert not any(e.get("kind") == "agent_verdict" and e.get("node_id") == "n_ver"
                   for e in h.ledger.events())
    # the agent_result event records is_clean=False + is_error=True
    ar = [e for e in h.ledger.events()
          if e.get("kind") == "agent_result" and e.get("node_id") == "n_ver"]
    assert ar and ar[-1]["payload"]["is_clean"] is False
    assert ar[-1]["payload"]["is_error"] is True


# ---------------------------------------------------------------------------
# #7: a VERIFIED verdict is stale if a LATER attempt for the same node failed
# (cognitive_attempt). Before P1-7, _agent_verdict_verified used
# last-write-wins on agent_verdict only; an attempt-2 failure that degraded
# without emitting a new agent_verdict left attempt-1's VERIFIED standing.
# ---------------------------------------------------------------------------
def test_stale_verdict_after_failed_attempt(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    svid = "spec.v1"
    node = "n_ver"
    # attempt 1: clean success + VERIFIED
    h._record({"event_id": "e1", "kind": "agent_result", "spec_version_id": svid,
               "node_id": node, "seq": 1,
               "payload": {"validated_output": {"verdict": "VERIFIED"},
                           "is_clean": True, "exit_code": 0, "is_error": False},
               "claims": []})
    h._record({"event_id": "e2", "kind": "agent_verdict", "spec_version_id": svid,
               "node_id": node, "seq": 2, "verdict": "VERIFIED",
               "payload": {"verdict_field": "verdict"}, "claims": []})
    # at this point: no later failure -> VERIFIED stands
    assert _agent_verdict_verified(node, h.ledger, svid) is True
    # attempt 2: failed and degraded (cognitive_attempt, no new agent_verdict)
    h._record({"event_id": "e3", "kind": "cognitive_attempt", "spec_version_id": svid,
               "node_id": node, "seq": 3,
               "payload": {"attempt": 2, "denials": [], "conforms": False},
               "claims": []})
    # now: a later failed attempt invalidates the prior VERIFIED -> stale
    assert _agent_verdict_verified(node, h.ledger, svid) is False, (
        "a VERIFIED verdict followed by a failed attempt (cognitive_attempt) "
        "must be stale -- last-write-wins on agent_verdict alone inherited the "
        "prior VERIFIED (latest_failed_attempt_stale_verdict probe)"
    )


# ---------------------------------------------------------------------------
# #9: a dispatch that FAILED (exit_code!=0) but emitted conforming JSON must
# NOT be cached as success. Before P1-6, build_replay_cache admission used
# schema-conformance alone, so a failed worker's conforming output was
# replayed as success on resume (failed_dispatch_replayed probe: original=None,
# resumed returned {x:1} with fresh_calls=0).
# ---------------------------------------------------------------------------
def test_failed_dispatch_not_cached_as_success(tmp_path):
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        # exit_code=1 (failed) but conforming JSON body
        return (1, _env('{"x": 1}'))

    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 0, "retry_guard": "requires_new_evidence",
                            "on_exhausted": "degrade"},
    }
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=[],
                nodes={"n1": node},
                control_flow={"type": "sequence", "steps": ["n1"]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    # direct single-dispatch path (the path skeptics / parallel body use).
    # A failed worker (exit_code=1) whose stdout conforms is recorded as
    # agent_result(is_clean=False) and returns None.
    result = h.dispatch_agent(node, {}, "spec.v1")
    assert result is None, (
        "a failed dispatch (exit_code!=0) must return None, not a validated result"
    )
    after_first = calls["n"]
    # the failed dispatch produced an agent_result with is_clean=False
    ar = [e for e in h.ledger.events()
          if e.get("kind") == "agent_result" and e.get("node_id") == "n1"]
    assert ar and ar[-1]["payload"]["is_clean"] is False, (
        "the failed dispatch must record is_clean=False on its agent_result event "
        "so build_replay_cache can reject it"
    )
    # resume: build replay cache -- the is_clean=False entry must NOT be admitted,
    # so the node re-dispatches instead of replaying a failure as success.
    h2 = Harness(tmp_path / "run", worker_runner=runner)
    h2.build_replay_cache(spec)
    out = h2.dispatch_agent(node, {}, "spec.v1")
    assert calls["n"] == after_first + 1, (
        "a failed dispatch (exit_code!=0) must NOT be cached as success -- "
        "resume must re-dispatch, not replay the failed output "
        "(failed_dispatch_replayed: original=None, resumed={x:1}, fresh_calls=0)"
    )
    assert out is None  # still failing on re-dispatch, not replayed as success
    # no replay_hit event for n1 (it was not cached)
    assert not any(e.get("kind") == "replay_hit" and e.get("node_id") == "n1"
                   for e in h2.ledger.events())


# ---------------------------------------------------------------------------
# #10: loop resume must replay the loop's ACTUAL terminal output (recorded
# on loop_close at convergence), not the last agent_result of the body.
# Before P1-10, the replay branch grabbed the last agent_result and returned
# {x:1} (a mid-body agent) instead of the loop's terminal {transformed:2}
# (a script node whose script_result was never scanned). Also: if the loop's
# outer input changed since convergence, the cached terminal is stale ->
# resume re-enters the loop (changed_input A->B must not hit A).
# ---------------------------------------------------------------------------
def test_loop_replay_uses_terminal_output_not_last_agent(tmp_path):
    """loop_close{terminal_output=X} is what resume returns, not the last
    agent_result. Simulates a converged loop whose terminal body node was a
    script (script_result, not agent_result) -- the old replay grabbed the
    last agent_result and missed the script terminal."""
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    svid, node_id = "spec.v1", "loop1"
    # a prior converged loop: loop_open + an agent_result({x:1}, mid-body) +
    # loop_close{terminal_output={transformed:2}} (the script terminal).
    lid = "loop_loop1_aabbccdd"
    h.emit_loop_open(spec_version_id=svid, loop_id=lid, node_id=node_id,
                     max_iterations=1, until="dry<1")
    h._record({"event_id": "e2", "kind": "agent_result",
               "spec_version_id": svid, "node_id": "n_agent",
               "payload": {"loop_id": lid, "iteration": 1,
               "validated_output": {"x": 1}, "is_clean": True,
               "input_hash": None}})
    h.emit_loop_close(spec_version_id=svid, loop_id=lid, final_iteration=1,
                      exit_reason="converged",
                      terminal_output={"transformed": 2})
    # mark this harness as resume-mode (replay active)
    h._replay = {"_test_marker": True}  # resume-mode (truthy)
    # replay the loop node: should return terminal_output, NOT {x:1}
    loop_node = {"type": "repeat", "id": node_id, "body": ["n_agent"],
                 "max_iterations": 1, "until": "dry<1"}
    spec = Spec(spec_version_id=svid, parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=[],
                nodes={node_id: loop_node},
                control_flow={"type": "sequence", "steps": [node_id]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    out = h._run_repeat(loop_node, {}, svid, spec)
    assert out is not None
    assert out["validated_output"] == {"transformed": 2}, (
        "loop resume must replay the loop's ACTUAL terminal output "
        "(loop_close.terminal_output), not the last agent_result of the body "
        "(loop_replay_contract: original={transformed:2}, resumed was {x:1})"
    )


def test_loop_replay_skips_on_changed_input(tmp_path):
    """if the loop's outer input changed since convergence (input_hash
    mismatch), the cached terminal is stale -> resume re-enters the loop
    instead of replaying (changed_input A->B must not hit A)."""
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        return (0, _env(json.dumps({"v": "from-B"})))

    h = Harness(tmp_path / "run", worker_runner=runner)
    svid, node_id = "spec.v1", "loop1"
    # prior converged loop with input_hash=A and terminal {v:from-A}
    lid = "loop_loop1_aaaa1111"
    h.emit_loop_open(spec_version_id=svid, loop_id=lid, node_id=node_id,
                     max_iterations=1, until="dry<1")
    h.emit_loop_close(spec_version_id=svid, loop_id=lid, final_iteration=1,
                      exit_reason="converged",
                      terminal_output={"v": "from-A"},
                      input_hash=Harness._input_hash({"input": "A"}))
    h._replay = {"_test_marker": True}  # resume-mode (truthy)
    loop_node = {"type": "repeat", "id": node_id, "body": ["n_agent"],
                 "max_iterations": 1, "until": "dry<1"}
    n_agent = {"type": "agent", "id": "n_agent", "prompt": "p",
               "dispatch": "host", "output_schema": {"type": "object"},
               "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
               "failure_policy": {}}
    spec = Spec(spec_version_id=svid, parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=[],
                nodes={node_id: loop_node, "n_agent": n_agent},
                control_flow={"type": "sequence", "steps": [node_id]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    # current outer input is B (hash != A) -> NOT replayed -> re-enter loop
    out = h._run_repeat(loop_node, {"input": "B"}, svid, spec)
    # re-entered the loop -> worker dispatched (fresh call), output is B's
    assert calls["n"] >= 1, (
        "changed loop input must NOT be replayed from the stale cache -- "
        "resume re-enters the loop (loop_replay_contract.changed_input: "
        "initial {v:from-A}, resumed after input B still returned from-A)"
    )


# ---------------------------------------------------------------------------
# #11: gate authorization binds to the gated ACTION SCOPE (write_areas +
# risk + prompt), not just node_id. v1 allowing risk:high action A must NOT
# auto-authorize v2's action B under the same node_id (different write_areas
# / prompt) -> a scope mismatch opens a NEW gate.
# ---------------------------------------------------------------------------
def test_gate_reopens_on_changed_action_scope(tmp_path):
    """v1 risk:high on write_areas A is allowed; v2 same node_id but changed
    write_areas -> check_gate returns True (NEW gate), not False (inherited)."""
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, _env()))
    # v1: risk:high node n1, write_areas=["src/a.py"]
    n1_v1 = {"type": "agent", "id": "n1", "risk": "high",
             "write_areas": ["src/a.py"], "prompt": "edit a",
             "output_schema": {"type": "object"}}
    # v1 opens a gate (check_gate=True, no prior allow)
    assert h.check_gate(n1_v1) is True
    scope_v1 = Harness._action_scope_hash(n1_v1)
    # simulate v1 gate_open + human allow
    gid = h._gate("risk_high", "n1", "spec.v1", action_scope_hash=scope_v1)
    h._record({"event_id": "g1", "kind": "gate_resolve", "gate_id": gid,
               "decision": "allow", "spec_version_id": "spec.v1"})
    # after allow, v1's SAME scope is authorized (no new gate)
    assert h.check_gate(n1_v1) is False, "same action scope stays authorized"
    # v2: same node_id n1, but write_areas CHANGED (action B) -> new gate
    n1_v2 = {"type": "agent", "id": "n1", "risk": "high",
             "write_areas": ["src/b.py", "src/c.py"], "prompt": "edit b",
             "output_schema": {"type": "object"}}
    assert h.check_gate(n1_v2) is True, (
        "a changed action scope (different write_areas) under the same "
        "node_id must open a NEW gate, not inherit v1's allow "
        "(gate_authorization_scope: v1 allow A -> v2 action B unapproved)"
    )
    # scope hashes differ
    assert Harness._action_scope_hash(n1_v2) != scope_v1
