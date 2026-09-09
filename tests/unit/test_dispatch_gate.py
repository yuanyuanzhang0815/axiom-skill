"""Task 10: gate node dispatch (staged escalation) + run()-level GateHalt catch.

`_dispatch` routes a `type:"gate"` node to `_run_gate`, which (spec §1.4, §3, Q6):
  1. eval `node["trigger"]` via `_eval_predicate` -> (value, claim_strength).
  2. if value is True (triggered):
     - on_trigger='pause'  -> emit gate_open(reason='escalation') + raise
       GateHalt for approval; after gate_resolve(allow), execute body.
     - on_trigger='auto'   -> auto-escalate to next tier: gate_open(reason=
       'escalation') + gate_resolve(decision='escalate') + execute body with
       the next tier's config (model/allowed_tools/skeptic_count from
       node['escalation_tiers']).
     - on_trigger='replan' -> emit replan_requested (boundary); do NOT execute
       body.
  3. if value is False or 'undefined' (not triggered) -> execute body directly.
  4. REUSES the v1 gate_open/gate_resolve mechanism -- NO new event kinds.
     Distinguished from v1 risk-gates by gate_open.reason='escalation' (v1
     risk-gates use reason='risk'|'contract_drift'|'unretriable_failure').

`run()` catches GateHalt so gate nodes AND gate=True conditions pause+resume at
the run() level (Task 7 deferred this catch). The gate_open is already recorded
by `_gate()` before the raise; run() exits with a gate-halted state so the CLI
can surface it and `axiom gate resolve` + `axiom resume` can re-enter.
"""
import json
import pytest
from axiom.harness import Harness, GateHalt
from axiom.ir import Spec, Requirement
from axiom.state import derive_verdict


# ---- helpers ---------------------------------------------------------------

def _ok_runner(args, cwd=None):
    """Mock worker runner: always returns a conforming {answer:'42'}."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"answer": "42"}),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def _rec_runner():
    """Mock runner that captures the args it was called with (to verify tier
    config --model/--allowedTools reached the worker)."""
    captured = []

    def _r(args, cwd=None):
        captured.append(list(args))
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    _r.captured = captured
    return _r


def _agent(nid):
    return {
        "type": "agent", "id": nid, "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": [], "acceptance": ["a"], "failure_policy": {},
    }


def _gate(nid, trigger, body, on_trigger="pause", tiers=None):
    return {
        "type": "gate", "id": nid, "trigger": trigger,
        "body": list(body), "on_trigger": on_trigger,
        "escalation_tiers": tiers or [],
        "output_schema": {},
    }


def _spec(nodes, steps=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=[], nodes=nodes,
        control_flow={"type": "sequence", "steps": steps or []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _agent_ran(ledger, nid):
    return any(e.get("kind") == "agent_result" and e.get("node_id") == nid
               for e in ledger.events())


def _gate_opens(ledger, nid=None):
    return [e for e in ledger.events()
            if e.get("kind") == "gate_open"
            and (nid is None or e.get("node_id") == nid)]


def _gate_resolves(ledger, nid=None):
    return [e for e in ledger.events()
            if e.get("kind") == "gate_resolve"]


# ---- trigger not triggered -> body runs directly --------------------------

def test_trigger_false_no_gate_body_runs(tmp_path):
    """trigger False -> not triggered: body runs directly, NO gate_open."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "False", ["a1"]),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    assert _agent_ran(h.ledger, "a1")
    assert _gate_opens(h.ledger) == []
    assert out["validated_output"] == {"answer": "42"}


def test_trigger_undefined_no_gate_body_runs(tmp_path):
    """trigger 'undefined' (unresolvable ref) -> safe default: body runs,
    NO gate_open."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "{{missing}}", ["a1"]),
                  "a1": _agent("a1")})
    h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    assert _agent_ran(h.ledger, "a1")
    assert _gate_opens(h.ledger) == []


# ---- on_trigger='pause' -> gate_open(escalation) + GateHalt ----------------

def test_trigger_true_pause_emits_gate_open_escalation(tmp_path):
    """trigger True + on_trigger='pause' -> gate_open(reason='escalation') +
    GateHalt; body does NOT execute until resolved."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")})
    with pytest.raises(GateHalt) as ei:
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    gate_id = ei.value.gate_id
    gos = _gate_opens(h.ledger, "g1")
    assert len(gos) == 1
    assert gos[0]["gate_id"] == gate_id
    assert gos[0]["reason"] == "escalation"
    assert gos[0]["node_id"] == "g1"
    # body did NOT execute (gate halted before body)
    assert not _agent_ran(h.ledger, "a1")


def test_gate_pause_after_resolve_executes_body(tmp_path):
    """After gate_resolve(allow), re-dispatch skips re-gating and runs body
    (resume flow)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")})
    # first dispatch: gate_open + GateHalt
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    go = _gate_opens(h.ledger, "g1")[0]
    h.resolve_gate(go["gate_id"], "allow")
    # second dispatch: gate already allowed -> body executes
    out = h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    assert _agent_ran(h.ledger, "a1")
    assert out["validated_output"] == {"answer": "42"}
    # no second gate_open (resume skips re-gating)
    assert len(_gate_opens(h.ledger, "g1")) == 1


def test_gate_pause_deny_does_not_execute_body(tmp_path):
    """gate_resolve(deny) is terminal: body does NOT execute; returns empty
    output (deny blocks the escalation, not 'try the next tier')."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")})
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    go = _gate_opens(h.ledger, "g1")[0]
    h.resolve_gate(go["gate_id"], "deny")
    out = h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    assert out == {"validated_output": {}}
    assert not _agent_ran(h.ledger, "a1")
    assert len(_gate_opens(h.ledger, "g1")) == 1


# ---- on_trigger='auto' -> auto-escalate to next tier -----------------------

def test_on_trigger_auto_escalates_to_next_tier(tmp_path):
    """on_trigger='auto' -> gate_open(escalation) + gate_resolve(escalate)
    + body runs with the next tier's config (model/allowed_tools applied)."""
    rec = _rec_runner()
    h = Harness(tmp_path / "run", worker_runner=rec)
    tiers = [{"model": "opus", "allowed_tools": ["shell", "web"],
              "skeptic_count": 5}]
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="auto",
                              tiers=tiers),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    # gate_open(escalation) emitted
    gos = _gate_opens(h.ledger, "g1")
    assert len(gos) == 1 and gos[0]["reason"] == "escalation"
    # gate_resolve(escalate) emitted (auto-escalation is binding)
    resolves = _gate_resolves(h.ledger)
    assert len(resolves) == 1 and resolves[0]["decision"] == "escalate"
    # body executed
    assert _agent_ran(h.ledger, "a1")
    assert out["validated_output"] == {"answer": "42"}
    # tier config reached the worker: --model opus + --allowedTools shell web
    assert rec.captured, "runner was never called"
    args = rec.captured[0]
    assert "--model" in args
    assert args[args.index("--model") + 1] == "opus"
    assert "--allowedTools" in args
    ai = args.index("--allowedTools")
    assert args[ai + 1] == "shell" and args[ai + 2] == "web"


# ---- on_trigger='replan' -> replan_requested (boundary) --------------------

def test_on_trigger_replan_emits_replan_requested(tmp_path):
    """on_trigger='replan' -> emit replan_requested (boundary); body does NOT
    execute. Invariant (2): replan only at workflow boundary."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="replan"),
                  "a1": _agent("a1")})
    out = h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    # replan_requested emitted at the boundary
    rp = [e for e in h.ledger.events() if e.get("kind") == "replan_requested"]
    assert len(rp) == 1
    assert rp[0]["node_id"] == "g1"
    # body did NOT execute
    assert not _agent_ran(h.ledger, "a1")
    # no gate_open (replan path does not gate)
    assert _gate_opens(h.ledger) == []
    assert out == {"validated_output": {}}


# ---- unresolved gate_open -> derive_verdict BLOCKED ------------------------

def test_unresolved_gate_open_verdict_blocked(tmp_path):
    """An unresolved gate_open(reason='escalation') makes derive_verdict return
    BLOCKED (fires for free via v1's state machine -- no new event kind)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")})
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    # gate_open unresolved -> derive_verdict BLOCKED
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


# ---- Q6: escalation vs risk coexist auditably ------------------------------

def test_escalation_vs_risk_coexist_auditably(tmp_path):
    """Q6: a v1 reason='risk_high' gate and a v2 reason='escalation' gate in
    one ledger are both distinguishable by reason (no naming collision)."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    # v1 risk-gate (emitted by _gate directly, as v1 does for risk:high nodes)
    h._gate("risk_high", "risky_node", "spec.v1")
    # v2 escalation gate (emitted by _run_gate's pause path)
    spec = _spec({"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
                  "a1": _agent("a1")})
    with pytest.raises(GateHalt):
        h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    gos = _gate_opens(h.ledger)
    reasons = {e["reason"] for e in gos}
    # both reasons present and distinguishable
    assert "risk_high" in reasons
    assert "escalation" in reasons
    # both unresolved -> derive_verdict BLOCKED (single blocked state, both gates)
    assert derive_verdict(spec, h.ledger) == "BLOCKED"
    # project_checkpoint's blocked_items carries both, each with its reason
    cp = h.project_checkpoint(spec)
    blocked_reasons = {b["reason"] for b in cp.get("blocked_items", [])}
    assert "risk_high" in blocked_reasons
    assert "escalation" in blocked_reasons


# ---- run()-level GateHalt catch (Task 7's deferred work) -------------------

def test_run_catches_gate_halt_pause_node(tmp_path):
    """A top-level gate node with on_trigger='pause' raises GateHalt inside
    _run_sequence; run() catches it and returns a gate-halted state instead
    of crashing. The gate_open(escalation) is in the ledger; derive_verdict
    sees it unresolved -> BLOCKED."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec(
        {"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
         "a1": _agent("a1")},
        steps=["g1"],
    )
    out = h.run(spec)
    # run() caught GateHalt -> returns a gate-halted marker (not a crash)
    assert isinstance(out, dict)
    assert "gate_halted" in out
    # gate_open(escalation) recorded
    gos = _gate_opens(h.ledger, "g1")
    assert len(gos) == 1 and gos[0]["reason"] == "escalation"
    # body did NOT run
    assert not _agent_ran(h.ledger, "a1")
    # verdict BLOCKED (unresolved gate)
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


def test_run_catches_gate_halt_condition_binding_branch(tmp_path):
    """Task 7's gate=True condition raises GateHalt at run() level too. Before
    Task 10, run() did not catch it -> a real spec with gate=True at top-level
    control_flow.steps crashed with uncaught GateHalt. Now run() catches it."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    cond = {
        "type": "condition", "id": "c1", "predicate": "True",
        "then_branch": ["a1"], "else_branch": [], "gate": True,
        "output_schema": {},
    }
    spec = _spec({"c1": cond, "a1": _agent("a1")}, steps=["c1"])
    out = h.run(spec)
    assert isinstance(out, dict)
    assert "gate_halted" in out
    gos = [e for e in h.ledger.events()
           if e.get("kind") == "gate_open" and e.get("node_id") == "c1"]
    assert len(gos) == 1 and gos[0]["reason"] == "binding_branch"
    assert not _agent_ran(h.ledger, "a1")
    assert derive_verdict(spec, h.ledger) == "BLOCKED"


def test_run_after_gate_resolve_resumes_gate_node(tmp_path):
    """End-to-end: run() halts on the gate; resolve(allow); run() again ->
    body executes. The resume guard in _run_gate skips re-gating."""
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    spec = _spec(
        {"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
         "a1": _agent("a1")},
        steps=["g1"],
    )
    # first run: halts
    out1 = h.run(spec)
    assert "gate_halted" in out1
    assert not _agent_ran(h.ledger, "a1")
    # human resolves the gate (allow)
    go = _gate_opens(h.ledger, "g1")[0]
    h.resolve_gate(go["gate_id"], "allow")
    # second run: resume guard sees the allowed gate -> body executes
    out2 = h.run(spec)
    assert _agent_ran(h.ledger, "a1")
    assert out2["validated_output"] == {"answer": "42"}
    # only one gate_open (resume did not re-gate)
    assert len(_gate_opens(h.ledger, "g1")) == 1


# ---- Fix 1: auto-path immutable node-override + parallel-sublist coverage ---

def test_auto_path_applies_tier_to_parallel_sublist_agents(tmp_path):
    """on_trigger='auto' with body=['parallel', 'a1', 'a2'] -- BOTH a1 and a2
    receive the escalated tier config (model/allowed_tools), not just the first
    agent. _run_steps reads spec.nodes[nid] for each parallel-sublist id, so the
    immutable override must cover every referenced agent id (recursive walk)."""
    rec = _rec_runner()
    h = Harness(tmp_path / "run", worker_runner=rec)
    tiers = [{"model": "opus", "allowed_tools": ["shell", "web"]}]
    spec = _spec({"g1": _gate("g1", "True", [["parallel", "a1", "a2"]],
                              on_trigger="auto", tiers=tiers),
                  "a1": _agent("a1"), "a2": _agent("a2")})
    h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    # both agents dispatched (2 worker dispatch calls)
    assert len(rec.captured) == 2
    for args in rec.captured:
        assert "--model" in args
        assert args[args.index("--model") + 1] == "opus"
        assert "--allowedTools" in args
        ai = args.index("--allowedTools")
        assert args[ai + 1] == "shell" and args[ai + 2] == "web"


def test_auto_path_does_not_mutate_spec_nodes(tmp_path):
    """The auto-path immutable node-override must NOT mutate the original
    spec.nodes (spec §0 / invariant 6: Spec History is append-only via
    revisions; spec.nodes is never modified at runtime). The override builds
    a shallow spec copy with an overridden nodes dict; originals are untouched."""
    rec = _rec_runner()
    h = Harness(tmp_path / "run", worker_runner=rec)
    tiers = [{"model": "opus", "allowed_tools": ["shell"]}]
    spec = _spec({"g1": _gate("g1", "True", ["a1"],
                              on_trigger="auto", tiers=tiers),
                  "a1": _agent("a1")})
    # snapshot the original a1 node before dispatch
    a1_before = dict(spec.nodes["a1"])
    nodes_before = {k: dict(v) for k, v in spec.nodes.items()}
    h._dispatch(spec.nodes["g1"], {}, "spec.v1", spec)
    # original spec.nodes unchanged: a1 has no model/allowed_tools override
    assert spec.nodes["a1"] == a1_before
    assert spec.nodes == nodes_before
    assert "model" not in spec.nodes["a1"]
    # the body did run (the override applied only to the copy)
    assert _agent_ran(h.ledger, "a1")


# ---- Fix 2: cmd_resume catches GateHalt (no CLI crash on unresolved gate) ---

def test_cmd_resume_unresolved_gate_returns_blocked_not_crash(tmp_path, capsys, monkeypatch):
    """`axiom resume` without first resolving a gate must NOT crash with an
    uncaught GateHalt traceback. cmd_resume wraps _run_sequence in try/except
    GateHalt (mirroring run()'s catch) -> seal + materialize -> BLOCKED verdict
    + exit 3 (same as cmd_run). The user can then `axiom gate resolve`."""
    from axiom.cli import main
    from axiom.ir import spec_to_json

    def fake_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    monkeypatch.setattr("axiom.harness._default_runner", fake_runner)
    spec = _spec(
        {"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
         "a1": _agent("a1")},
        steps=["g1"],
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec_to_json(spec), encoding="utf-8")
    run_dir = str(tmp_path / "run")
    # first: axiom run -> halts on the gate (exit 3, BLOCKED)
    rc1 = main(["run", str(spec_path), "--run-dir", run_dir])
    assert rc1 == 3  # BLOCKED
    # second: axiom resume WITHOUT resolving the gate -> must NOT crash
    # (no traceback); returns exit 3, BLOCKED, gate still unresolved.
    rc2 = main(["resume", str(spec_path), "--run-dir", run_dir])
    assert rc2 == 3  # BLOCKED, not a crash
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "BLOCKED" in out
    # gate still unresolved; body did not run in either invocation
    # (read the ledger directly to confirm no agent_result for a1)
    from axiom.ledger import Ledger
    lg = Ledger(run_dir)
    assert not any(e.get("kind") == "agent_result" and e.get("node_id") == "a1"
                   for e in lg.events())


def test_cmd_resume_after_gate_resolve_executes_body(tmp_path, capsys, monkeypatch):
    """`axiom resume` AFTER `axiom gate resolve <gid> allow` -> the resume
    guard in _run_gate sees the allowed gate and executes the body (no halt)."""
    from axiom.cli import main
    from axiom.ir import spec_to_json

    def fake_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"answer": "42"}),
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))

    monkeypatch.setattr("axiom.harness._default_runner", fake_runner)
    spec = _spec(
        {"g1": _gate("g1", "True", ["a1"], on_trigger="pause"),
         "a1": _agent("a1")},
        steps=["g1"],
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec_to_json(spec), encoding="utf-8")
    run_dir = str(tmp_path / "run")
    # run -> halts (exit 3)
    assert main(["run", str(spec_path), "--run-dir", run_dir]) == 3
    # find the unresolved gate_id from the ledger (robust to seq drift)
    from axiom.ledger import Ledger
    lg = Ledger(run_dir)
    gate_id = next(e["gate_id"] for e in lg.events()
                   if e.get("kind") == "gate_open" and e.get("node_id") == "g1")
    # resolve the gate (allow)
    assert main(["gate", "resolve", "--run-dir", run_dir,
                 "--gate-id", gate_id, "--decision", "allow"]) == 0
    capsys.readouterr()  # drain
    # resume -> body executes (no halt)
    rc = main(["resume", str(spec_path), "--run-dir", run_dir])
    # the body ran; verdict is PARTIAL (C1 never verified -- no verify node),
    # not BLOCKED (gate resolved). Either way, NOT a crash, NOT 3.
    assert rc != 3  # not BLOCKED (gate resolved)
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert any(e.get("kind") == "agent_result" and e.get("node_id") == "a1"
               for e in lg.events())

