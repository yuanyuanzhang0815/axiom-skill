"""GPT engine-audit probe reproductions (2026-09-07).

Each test reproduces one of the four P1 probes from the GPT engine review
(axiom-engine-review.md) and confirms the fix rejects or downgrades it.

P1-1: a required R with no success_evidence binding was silently skipped (unverified) -> VERIFIED.
P1-2: the first agent_verdict permanently masked a later one (VERIFIED->FAILED).
P1-3: a failed worker (exit_code=1) whose stdout carried schema-conforming
      {verdict:VERIFIED} was promoted to success evidence.
P1-7: the predicate DSL's __globals__ escape read arbitrary files (AST gate).
"""
import json
import os
import threading
import time
from axiom.ledger import Ledger
from axiom.ir import Spec, Requirement, validate_spec
from axiom.state import derive_verdict
from axiom.harness import Harness


def _spec(reqs, svid="spec.v1"):
    return Spec(
        spec_version_id=svid, parent_spec_id=None, revision=1, intent="i",
        requirements=reqs, boundaries=["b"], success_evidence=["S1:R1=s"],
        nodes={}, control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


# ---- P1-1: missing required binding -----------------------------------

def test_p1_1_missing_required_r_is_partial_not_verified(tmp_path):
    """R1/R2 required, only R1 bound to a survived claim -> PARTIAL, not VERIFIED.
    Before the fix, derive_verdict only walked success_evidence and silently
    dropped the unbound R2, yielding a false VERIFIED."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "verify_verdict", "claim_id": "C1",
               "refuted": False, "survived": True})
    spec = _spec([Requirement("R1", "r", "required"),
                  Requirement("R2", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]  # R2 silently unbound
    assert derive_verdict(spec, lg) == "PARTIAL"


def test_p1_1_validate_rejects_partial_required_binding(tmp_path):
    """validate_spec flags a required R that has no success_evidence binding
    when a required sibling IS bound (partial binding = silently skipped (unverified) trap)."""
    spec = _spec([Requirement("R1", "r", "required"),
                  Requirement("R2", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:C1"]  # R2 unbound
    errs = validate_spec(spec)
    assert any("R2" in e and "required" in e for e in errs), errs


def test_p1_1_no_required_bound_at_all_still_unverified(tmp_path):
    """A spec with NO required R bound is valid (verdict UNVERIFIED -- nothing
    to verify, not a silently skipped (unverified) case). The P1-1 check only fires when SOME required R is
    bound and a required sibling is not."""
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1"]  # no :Rn binding at all
    assert validate_spec(spec) == []
    assert derive_verdict(spec, Ledger(tmp_path / "run")) == "UNVERIFIED"


# ---- P1-2: last-write-wins agent_verdict -------------------------------

def test_p1_2_verified_then_failed_is_partial(tmp_path):
    """Same svid/node VERIFIED then FAILED -> PARTIAL (last-write-wins),
    not VERIFIED (first-match)."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "VERIFIED"})
    lg.append({"event_id": "E2", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "FAILED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "PARTIAL"


def test_p1_2_failed_then_verified_last_wins(tmp_path):
    """Symmetric: FAILED then VERIFIED -> VERIFIED (last-write-wins)."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "FAILED"})
    lg.append({"event_id": "E2", "kind": "agent_verdict",
               "spec_version_id": "spec.v1", "node_id": "n5", "verdict": "VERIFIED"})
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=node:n5"]
    assert derive_verdict(spec, lg) == "VERIFIED"


# ---- P1-3: failed worker stdout not promoted --------------------------

def _env_verified():
    """Worker stdout envelope carrying a schema-conforming {verdict:VERIFIED}."""
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"verdict": "VERIFIED"}),
        "session_id": "s", "total_cost_usd": 0.01,
        "permission_denials": [], "usage": {},
    })


def test_p1_3_failed_worker_not_promoted_to_verified(tmp_path):
    """Worker returns exit_code=1 with schema-conforming {verdict:VERIFIED}
    stdout. Before the fix the harness promoted it (cognitive == success);
    after, exit_code!=0 blocks promotion and no agent_verdict VERIFIED is
    emitted."""
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        return (1, _env_verified())
    h = Harness(tmp_path / "run", worker_runner=runner)
    node_n5 = {
        "type": "agent", "id": "n5", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object",
                          "properties": {"verdict": {"type": "string"}},
                          "required": ["verdict"]},
        "acceptance": ["a"], "failure_policy": {},
        "write_areas": [], "verdict_field": "verdict",
    }
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=["S1:R1=node:n5"],
                nodes={"n5": node_n5},
                control_flow={"type": "sequence", "steps": ["n5"]},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    h.run(spec)
    # no agent_verdict VERIFIED was emitted for n5 (failed worker not promoted)
    verdicts = [e for e in h.ledger.events()
                if e.get("kind") == "agent_verdict"
                and e.get("node_id") == "n5"]
    assert not any(e.get("verdict") == "VERIFIED" for e in verdicts), verdicts
    assert derive_verdict(spec, h.ledger) != "VERIFIED"
    # the worker WAS dispatched (not skipped) -- the fix is in the promotion
    # gate, not a no-op
    assert calls["n"] >= 1


# ---- P1-7: predicate __globals__ escape (AST gate) ---------------------

def _stub_spec():
    return type("S", (), {
        "spec_version_id": "spec.v1", "budget_usd": 5.0,
        "max_stagnation": 1, "max_agents": 1000, "requirements": [],
        "success_evidence": [], "decision_trace": [],
    })()


def test_p1_7_globals_escape_via_attribute_is_undefined(tmp_path):
    """Accessing __globals__ on a built-in function object must be rejected
    by the AST gate (Attribute node) -> 'undefined', not a real escape.
    Before the fix, empty __builtins__ left function __globals__ reachable,
    which exposed module globals (Path) -> arbitrary file read."""
    h = Harness(tmp_path / "run")
    val, _ = h._eval_predicate(
        "dry_count.__globals__", {}, _stub_spec(), h.ledger, "spec.v1")
    assert val == "undefined"


def test_p1_7_subscript_escape_is_undefined(tmp_path):
    """dry_count.__globals__['Path'] uses Subscript + Attribute -> rejected."""
    h = Harness(tmp_path / "run")
    val, _ = h._eval_predicate(
        "dry_count.__globals__['Path']", {}, _stub_spec(), h.ledger, "spec.v1")
    assert val == "undefined"


# ---- helpers for P2-8/P2-9 (loop + gate) --------------------------------

def _ok_runner(args, cwd=None):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _agent(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _repeat(nid, body, max_iterations, until="dry<2"):
    return {
        "type": "repeat", "id": nid, "body": body,
        "max_iterations": max_iterations, "until": until,
        "failure_policy": {"on_exhausted": "block"},
    }


def _spec_loop(nodes, steps=("r1",)):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"], nodes=nodes,
        control_flow={"type": "sequence", "steps": list(steps)},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


# ---- P2-8 bug 1: failed dispatch never replays (P1-3 blocks it) --------

def test_p2_8_bug1_failed_dispatch_not_replayed(tmp_path):
    """P2-8 bug 1 probe: a repeat body whose worker FAILED (exit_code!=0)
    must NOT be cached for replay. build_replay_cache only caches conforming
    agent_result events, and (after P1-3) a failed worker emits no
    agent_result at all -- so a resume re-dispatches the failed iteration
    rather than replaying a non-existent success. Confirms P1-3's fix
    indirectly blocks the P2-8 bug-1 feedback path."""
    calls = {"n": 0}

    def fail_runner(args, cwd=None):
        calls["n"] += 1
        return (1, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"verdict": "VERIFIED"}),  # conforming stdout
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=fail_runner)
    spec = _spec_loop({
        "r1": _repeat("r1", ["a1"], max_iterations=1, until="dry<2"),
        "a1": {**_agent("a1"), "verdict_field": "verdict",
               "output_schema": {"type": "object",
                                 "properties": {"verdict": {"type": "string"}},
                                 "required": ["verdict"]}},
    })
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    # no agent_result was emitted (failed worker, exit_code=1, not promoted)
    agent_results = [e for e in h.ledger.events()
                     if e.get("kind") == "agent_result"
                     and e.get("node_id") == "a1"]
    assert agent_results == [], agent_results
    # build_replay_cache finds nothing replayable for a1. R6-1 changed the
    # cache structure: failures leave TOMBSTONE entries (vo=None) so the
    # newest batch's state decides replayability -- the meaningful assertion
    # is "no slot holds a replayable success", not "the cache is empty".
    h.build_replay_cache(spec)
    replayable = [
        (key, occ) for key, occs in getattr(h, "_replay", {}).items()
        for occ, entries in occs.items()
        if any(vo is not None for _run, _h, vo in entries)
    ]
    assert replayable == [], h._replay
    # resume re-dispatches (cache miss), not replays
    first_calls = calls["n"]
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    replays = [e for e in h.ledger.events() if e.get("kind") == "replay_hit"]
    assert replays == [], "failed dispatch must not be replayed"
    assert calls["n"] > first_calls, "resume must re-dispatch the failed iteration"


# ---- P2-8 bug 2: resume of converged repeat replays, not re-enters ------

def test_p2_8_bug2_resume_replays_converged_repeat(tmp_path):
    """P2-8 bug 2: a resume that re-enters an already-converged repeat must
    replay (replay_hit) the prior converged body output, NOT generate a fresh
    loop_id and re-dispatch the worker. Before the fix, the fresh uuid4
    loop_id missed the replay cache key (which carried the OLD loop_id) ->
    zero replay_hit + a redundant worker dispatch on every resume."""
    calls = {"n": 0}

    def runner(args, cwd=None):
        calls["n"] += 1
        return _ok_runner(args, cwd)

    h = Harness(tmp_path / "run", worker_runner=runner)
    spec = _spec_loop({
        "r1": _repeat("r1", ["a1"], max_iterations=3, until="dry<2"),
        "a1": _agent("a1"),
    })
    # first dispatch: converges (dry_count reaches 2 within 3 iterations)
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    closes = [e for e in h.ledger.events() if e.get("kind") == "loop_close"]
    assert closes and closes[-1].get("payload", {}).get("exit_reason") == "converged"
    first_dispatch_calls = calls["n"]
    first_loop_opens = [e for e in h.ledger.events()
                        if e.get("kind") == "loop_open"]

    # simulate resume: rebuild replay cache (this is what _run_resume does)
    h.build_replay_cache(spec)

    # second dispatch (resume context, _replay is set) -> prior-converged replay
    h._dispatch(spec.nodes["r1"], {}, "spec.v1", spec)
    opens = [e for e in h.ledger.events() if e.get("kind") == "loop_open"]
    replays = [e for e in h.ledger.events() if e.get("kind") == "replay_hit"]
    assert len(opens) == len(first_loop_opens), \
        "resume of converged repeat must NOT open a new loop"
    assert len(replays) == 1, "resume of converged repeat must emit replay_hit"
    # worker was NOT re-dispatched on resume (call count unchanged)
    assert calls["n"] == first_dispatch_calls, \
        "resume of converged repeat must not re-dispatch the body worker"


# ---- P2-9: gate already authorized is not re-gated -----------------------

def test_p2_9_gate_already_authorized_not_re_gated(tmp_path):
    """P2-9: a high-risk node whose gate was already resolved(allow) must
    NOT be re-gated on resume/re-dispatch. Before the fix, check_gate ignored
    prior gate_resolve events -> every resume re-blocked an already-authorized
    node, forcing a human to re-approve the same gate.

    P-024 update: the fixture now records the gate_open the way _gate()
    actually writes it (reason=risk_high + action_scope_hash) -- the old
    fixture's reason-less, scope-less event encoded the pre-P-011 wildcard
    semantics that P-024 deliberately removed (a scope-less gate is NOT an
    authorization for risk actions anymore)."""
    h = Harness(tmp_path / "run")
    node = {"type": "agent", "id": "n1", "risk": "high", "prompt": "p",
            "dispatch": "host", "output_schema": {"type": "object"},
            "write_areas": [], "acceptance": ["a"], "failure_policy": {}}
    # first check: gated (no prior authorization)
    assert h.check_gate(node) is True
    # human approved the gate (event shape matches _gate()'s real writes)
    h._record({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
               "node_id": "n1", "spec_version_id": "spec.v1",
               "reason": "risk_high",
               "action_scope_hash": Harness._action_scope_hash(node),
               "payload": {}})
    h._record({"event_id": "E2", "kind": "gate_resolve", "gate_id": "G1",
               "decision": "allow", "spec_version_id": "spec.v1",
               "payload": {}})
    # resume / re-dispatch: already authorized -> NOT re-gated
    assert h.check_gate(node) is False


def test_p2_9_gate_unresolved_still_gated(tmp_path):
    """P2-9 negative: a high-risk node with a gate_open but NO gate_resolve
    (gate still pending) must remain gated."""
    h = Harness(tmp_path / "run")
    node = {"type": "agent", "id": "n1", "risk": "high", "prompt": "p",
            "dispatch": "host", "output_schema": {"type": "object"},
            "write_areas": [], "acceptance": ["a"], "failure_policy": {}}
    h._record({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
               "node_id": "n1", "spec_version_id": "spec.v1", "payload": {}})
    # gate opened but not resolved -> still gated
    assert h.check_gate(node) is True


def test_p2_9_gate_deny_still_gated(tmp_path):
    """P2-9 negative: a gate resolved(deny) must NOT count as authorized."""
    h = Harness(tmp_path / "run")
    node = {"type": "agent", "id": "n1", "risk": "high", "prompt": "p",
            "dispatch": "host", "output_schema": {"type": "object"},
            "write_areas": [], "acceptance": ["a"], "failure_policy": {}}
    h._record({"event_id": "E1", "kind": "gate_open", "gate_id": "G1",
               "node_id": "n1", "spec_version_id": "spec.v1", "payload": {}})
    h._record({"event_id": "E2", "kind": "gate_resolve", "gate_id": "G1",
               "decision": "deny", "spec_version_id": "spec.v1",
               "payload": {}})
    # deny != allow -> not authorized -> still gated
    assert h.check_gate(node) is True


# ---- P1-4: file-protocol concurrency (request_id partitioning) --------

def test_p1_4_concurrent_dispatches_no_mismatch(tmp_path, monkeypatch):
    """P1-4: two concurrent dispatch_via_host calls (parallel node fan-out)
    sharing one run-dir must NOT cross-poll results. Before the fix, both
    wrote the single dispatch_req.json -> B overwrote A -> host answered B
    -> A polled first and got B's result (data mismatch) while B timed out
    (its answer was stolen by A). After the fix, each gets a per-rid
    dispatch_req_{rid}.json / dispatch_res_{rid}.json pair -> no overwrite."""
    from axiom.dispatch import dispatch_via_host
    run_dir = tmp_path / "dispatch"
    run_dir.mkdir()
    monkeypatch.setenv("AXIOM_RUN_DIR", str(run_dir))
    monkeypatch.setenv("AXIOM_CLI_DISPATCH_TIMEOUT", "15")
    monkeypatch.setenv("AXIOM_CLI_POLL_INTERVAL", "0.02")
    results = {}

    def host():
        """Mock host: glob dispatch_req_*.json, echo prompt as result_text,
        write dispatch_res_{rid}.json."""
        import glob
        from pathlib import Path
        for _ in range(500):
            new_reqs = sorted(glob.glob(str(run_dir / "dispatch_req_*.json")))
            for rp_str in new_reqs:
                rp = Path(rp_str)
                rid = rp.stem.replace("dispatch_req_", "")
                res_p = run_dir / f"dispatch_res_{rid}.json"
                if res_p.exists():
                    continue  # already served
                try:
                    with open(rp) as f:
                        req = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                try:
                    os.remove(rp)  # signal "processing"
                except OSError:
                    pass
                res = {
                    "session_id": "host", "result_text": req["prompt"],
                    "num_turns": 1, "cost_usd": 0.0,
                    "permission_denials": [], "exit_code": 0,
                    "retry_class": "cognitive",
                }
                with open(res_p, "w") as f:
                    json.dump(res, f)
            time.sleep(0.01)

    host_t = threading.Thread(target=host, daemon=True)
    host_t.start()

    def dispatch(prompt):
        r = dispatch_via_host(prompt=prompt, schema={}, allowed_tools=[],
                             model=None, cwd=None)
        results[prompt] = r.result_text

    tA = threading.Thread(target=dispatch, args=("task-A",))
    tB = threading.Thread(target=dispatch, args=("task-B",))
    tA.start()
    tB.start()
    tA.join(timeout=20)
    tB.join(timeout=20)

    # the fix: each dispatch gets its own result, no cross-poll
    assert results.get("task-A") == "task-A", \
        f"A got {results.get('task-A')!r} (cross-poll mismatch)"
    assert results.get("task-B") == "task-B", \
        f"B got {results.get('task-B')!r} (cross-poll mismatch)"


# ---- P2-10: sealed ledger tail truncation ------------------------------

def test_p2_10_sealed_tail_truncation_detected(tmp_path):
    """P2-10: a sealed ledger whose tail was truncated (last event removed)
    must fail verify_chain. Before the fix, the surviving prefix was
    internally consistent (seq/prev_hash/event_hash all matched within it),
    so verify_chain returned [] and masked the truncation; the seal manifest
    still pointed at the now-missing tail event."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "spec_version_id": "spec.v1", "node_id": "n1", "payload": {}})
    lg.append({"event_id": "E2", "kind": "agent_result",
               "spec_version_id": "spec.v1", "node_id": "n2", "payload": {}})
    lg.seal()  # manifest final_event_hash = E2's hash
    # truncate the tail: drop the last line of events.jsonl
    p = lg.events_path
    lines = p.read_text(encoding="utf-8").rstrip("\n").split("\n")
    p.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    # manifest still points at E2, current tail is E1 -> mismatch
    errs = lg.verify_chain()
    assert any("seal manifest mismatch" in e for e in errs), errs


def test_p2_10_sealed_intact_chain_still_valid(tmp_path):
    """P2-10 negative: a sealed ledger whose tail was NOT tampered must still
    pass verify_chain (manifest matches current tail)."""
    lg = Ledger(tmp_path / "run")
    lg.append({"event_id": "E1", "kind": "agent_result",
               "spec_version_id": "spec.v1", "node_id": "n1", "payload": {}})
    lg.append({"event_id": "E2", "kind": "agent_result",
               "spec_version_id": "spec.v1", "node_id": "n2", "payload": {}})
    lg.seal()
    assert lg.verify_chain() == []


# ---- P2-11: default template verifier self-closes --------------------

def test_p2_11_default_template_validates_and_omits_hook(tmp_path):
    """P2-11: the default plan template (code-change: n1_edit touches src/**)
    must validate WITHOUT assurance_hook on n_verify, via the ir.py
    assurance_opt_out escape hatch. Before the fix, ir.py forced
    assurance_hook on the completion-verdict node of any code-change spec
    (no opt_out), but the hook was uncloseable (adjudicate.py missing +
    verify-only n_verify -> no_worktree/receipt_missing -> UNVERIFIED),
    so the default template could never self-close."""
    import argparse
    from axiom.cli import cmd_plan, _load_spec
    out_path = tmp_path / "plan.json"
    cmd_plan(argparse.Namespace(intent=None, out=str(out_path)))
    spec = _load_spec(str(out_path))
    assert validate_spec(spec) == [], "default template must validate"
    n_verify = spec.nodes["n_verify"]
    assert "assurance_hook" not in n_verify, \
        "default template must omit assurance_hook on verify-only n_verify (P2-11)"
    assert spec.assurance_opt_out, \
        "default template must declare assurance_opt_out (P2-11 escape hatch)"


def test_p2_11_verifier_self_closes_without_hook(tmp_path):
    """P2-11: without assurance_hook, the verifier's {verdict:VERIFIED} is
    recorded as agent_verdict VERIFIED (trusted via verdict_field) and
    derive_verdict reaches VERIFIED (self-close). With the hook, the same
    conforming output is overridden to UNVERIFIED (adjudicate.py missing).
    The opt_out + omitted hook path lets the default template self-close."""
    node_no_hook = {
        "type": "agent", "id": "n_verify", "verdict_field": "verdict",
        "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"]},
        "allowed_tools": ["Read"], "write_areas": [], "acceptance": ["v"],
        "failure_policy": {"max_retries": 1, "retry_guard": "requires_new_evidence", "on_exhausted": "degrade"},
    }
    node_with_hook = {**node_no_hook,
                      "assurance_hook": {"receipt_path": "receipt.json"}}

    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"verdict": "VERIFIED", "issues": []}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    def _spec(node):
        return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                    intent="i", requirements=[Requirement("R1", "r", "required")],
                    boundaries=["b"], success_evidence=["S1:R1=node:n_verify"],
                    nodes={"n_verify": node},
                    control_flow={"type": "sequence", "steps": ["n_verify"]},
                    decision_trace=[], budget_usd=5.0, max_concurrent=16,
                    max_agents=1000, max_stagnation=1)

    # without hook -> VERIFIED (self-close)
    h = Harness(tmp_path / "no_hook", worker_runner=runner)
    h.run(_spec(node_no_hook))
    v_no = [e for e in h.ledger.events()
            if e.get("kind") == "agent_verdict"
            and e.get("node_id") == "n_verify"]
    assert any(e.get("verdict") == "VERIFIED" for e in v_no), v_no

    # with hook -> UNVERIFIED (adjudicate.py missing overrides agent VERIFIED)
    h2 = Harness(tmp_path / "with_hook", worker_runner=runner)
    h2.run(_spec(node_with_hook))
    v_hook = [e for e in h2.ledger.events()
              if e.get("kind") == "agent_verdict"
              and e.get("node_id") == "n_verify"]
    assert v_hook and all(e.get("verdict") == "UNVERIFIED" for e in v_hook), v_hook


# ---- P2-12: claim_id namespace partitioning (SP/SEC accepted) --------

def test_p2_12_claim_id_accepts_domain_prefix(tmp_path):
    """P2-12 #1: SKILL.md recommends SP1/SEC1 to partition claim namespaces
    across verify targets (so two verifiers never collide on the same id),
    but ir.py's claim_id format only accepted C\\d+|dec_\\w+ -> SP1/SEC1
    specs failed validation and the doc/engine drifted. The format now
    accepts [A-Z]+\\d+ (C1, SP1, SEC1, ...) so domain-prefixed claim ids
    validate, bind, and resolve through state._evidence_to_claim."""
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:SP1"]
    assert validate_spec(spec) == [], "SP1 claim id should validate"
    spec.success_evidence = ["S1:R1=claim:SEC1"]
    assert validate_spec(spec) == [], "SEC1 claim id should validate"
    # state._evidence_to_claim resolves the domain-prefixed binding
    from axiom.state import _evidence_to_claim
    assert _evidence_to_claim("S1:R1=claim:SP1") == "SP1"
    assert _evidence_to_claim("S1:R1=claim:SEC1") == "SEC1"
    # classic C1 still works (regression guard)
    assert _evidence_to_claim("S1:R1=claim:C1") == "C1"


def test_p2_12_claim_id_rejects_lowercase_node_id(tmp_path):
    """P2-12 #1 negative: a lowercase node id (n5) is NOT a valid claim id --
    claim ids are uppercase-prefixed to distinguish them from node ids, so a
    success_evidence that binds R to claim:n5 is flagged (it would silently
    never match any verify_verdict claim_id)."""
    spec = _spec([Requirement("R1", "r", "required")])
    spec.success_evidence = ["S1:R1=claim:n5"]
    errs = validate_spec(spec)
    assert any("not a valid claim id" in e for e in errs), errs
