"""v2 Task 15: CLI + journal v2 event handling + loop status render.

Confirms (spec §3, §4):
  - `journal --verify` on a ledger with loop/iteration/branch events → CHAIN OK
    (the hash chain is KIND-AGNOSTIC — ledger._event_hash hashes
    event-minus-event_hash with sort_keys; invariant 5).
  - `state` cmd on an active loop → output shows PROGRESSING + loop_id +
    current iteration (from project_cognitive_state's `loops` field augmented
    by the CLI with the latest iteration_open; invariant 6).
  - `checkpoint` cmd shows `open_questions` on replan exit
    (replan_requested → open_questions) and `blocked_items` on gate exit
    (unresolved gate_open); also renders loop progress (`loops` field).

No new CLI commands. Control-flow is in-spec, orchestrated via existing
run/resume/checkpoint/gate. Invariants guarded: (5) hash-chain kind-agnostic;
(6) honest projection.
"""
import json
from pathlib import Path
from axiom.cli import main
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, spec_to_json


def _spec_path(tmp_path, svid="spec.v1"):
    """Write a minimal valid spec (empty control_flow) to tmp_path/spec.json."""
    s = Spec(
        spec_version_id=svid, parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=claim:C1"], nodes={
            "n1": {
                "type": "agent", "id": "n1", "prompt": "p",
                "dispatch": "host", "output_schema": {"type": "object"},
                "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
                "failure_policy": {},
            }
        },
        control_flow={"type": "sequence", "steps": []}, decision_trace=[],
        budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1,
    )
    p = tmp_path / "spec.json"
    p.write_text(spec_to_json(s), encoding="utf-8")
    return str(p)


def _emit_full_loop(h, svid, loop_id="L1", node_id="repeat1",
                    final_iter=1, exit_reason="converged"):
    """Emit a full loop lifecycle: loop_open → iteration_open → body
    agent_result (payload carries loop_id+iteration) → iteration_close →
    loop_close. Kind-agnostic chain must stay intact across all 5 kinds."""
    h.emit_loop_open(spec_version_id=svid, loop_id=loop_id, node_id=node_id,
                     max_iterations=3, until="dry<2")
    h.emit_iteration_open(spec_version_id=svid, loop_id=loop_id,
                          iteration=final_iter)
    h._record({
        "event_id": h._ev_id(), "kind": "agent_result",
        "spec_version_id": svid, "node_id": "worker1",
        "payload": {"validated_output": {"answer": 42}, "cost_usd": 0.01,
                     "loop_id": loop_id, "iteration": final_iter},
        "claims": [],
    })
    h.emit_iteration_close(spec_version_id=svid, loop_id=loop_id,
                           iteration=final_iter, condition_eval="dry<2",
                           dry_count=0)
    h.emit_loop_close(spec_version_id=svid, loop_id=loop_id,
                      final_iteration=final_iter, exit_reason=exit_reason)


# --- journal --verify on v2 events: kind-agnostic chain (invariant 5) --------

def test_journal_verify_on_v2_events_chain_ok(tmp_path, capsys):
    """A ledger with all 5 new v2 event kinds (loop_open/iteration_open/
    iteration_close/loop_close/branch_decision) + a body agent_result verifies
    cleanly. The hash chain is KIND-AGNOSTIC (ledger._event_hash hashes
    event-minus-event_hash with sort_keys), so journal's verify_chain() handles
    the new kinds without any ledger schema change. invariant 5."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    _emit_full_loop(h, svid)
    h.emit_branch_decision(
        spec_version_id=svid, node_id="cond1",
        predicate="claim_status('C1')=='VERIFIED'",
        eval_result=True, branch_taken="then",
        degraded=False, claim_strength="deterministic",
    )
    h.ledger.seal()

    rc = main(["journal", "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    out = capsys.readouterr()
    # explicit chain-intact confirmation rendered on stdout
    assert "CHAIN OK" in out.out
    # no tamper indicators on stderr
    assert "CHAIN WARN" not in out.err
    # all 5 new kinds + body agent_result present in the journal dump
    kinds_seen = set()
    for line in out.out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            kinds_seen.add(json.loads(line).get("kind"))
    assert {"loop_open", "iteration_open", "iteration_close", "loop_close",
            "branch_decision", "agent_result"} <= kinds_seen


# --- state cmd: active loop → PROGRESSING + loop_id + iteration --------------

def test_state_shows_active_loop_progressing(tmp_path, capsys):
    """An active loop (loop_open WITHOUT loop_close) → cognitive state
    progress=PROGRESSING (spec §4). The loops field shows loop_id +
    state=active. The CLI augments each active loop with the current
    iteration (latest iteration_open for that loop_id) so the orchestrator
    sees loop progress without diving the journal. invariant 6."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.emit_loop_open(spec_version_id=svid, loop_id="L1", node_id="repeat1",
                     max_iterations=3, until="dry<2")
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=1)
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=2)
    # loop_close NOT emitted → loop is ACTIVE, latest iteration = 2

    rc = main(["state", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    st = json.loads(capsys.readouterr().out)
    assert st["progress"] == "PROGRESSING"
    loops = st["loops"]
    assert len(loops) == 1
    assert loops[0]["loop_id"] == "L1"
    assert loops[0]["state"] == "active"
    assert loops[0]["exit_reason"] is None
    assert loops[0]["iteration"] == 2  # latest iteration_open


def test_state_closed_loop_shows_exit_reason(tmp_path, capsys):
    """A closed loop (loop_open + loop_close) → loops field shows state=closed
    + exit_reason; iteration is None (no active iteration)."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    _emit_full_loop(h, svid, final_iter=2, exit_reason="converged")

    rc = main(["state", spec_path, "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    st = json.loads(capsys.readouterr().out)
    loops = st["loops"]
    assert len(loops) == 1
    assert loops[0]["state"] == "closed"
    assert loops[0]["exit_reason"] == "converged"


# --- checkpoint: open_questions on replan exit -------------------------------

def test_checkpoint_shows_open_questions_on_replan(tmp_path, capsys):
    """replan_requested event (on_exhausted=replan, spec §4) → checkpoint
    open_questions non-empty with status=replan_requested + ref pointing at
    the replan_requested event. The orchestrator reads the boundary signal
    from the checkpoint alone (no journal dive). invariant 6."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h._record({
        "event_id": h._ev_id(), "kind": "replan_requested",
        "spec_version_id": svid, "node_id": "n_find",
        "payload": {"reason": "retries_exhausted"}, "claims": [],
    })

    rc = main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "run")])
    cp = json.loads(capsys.readouterr().out)
    oq = cp["open_questions"]
    assert any(q.get("status") == "replan_requested" for q in oq)
    assert any("replan_requested" in q.get("ref", "") for q in oq)


# --- checkpoint: blocked_items on gate exit ---------------------------------

def test_checkpoint_shows_blocked_items_on_gate(tmp_path, capsys):
    """An unresolved gate_open (on_trigger=pause, §1.4 / v1 risk-gate) →
    checkpoint blocked_items non-empty with gate_id + reason + node_id, and
    verdict=BLOCKED. The orchestrator sees the blocked gate from the
    checkpoint alone. invariant 6."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h._gate("escalation", "gate1", svid)  # unresolved gate_open

    rc = main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "run")])
    cp = json.loads(capsys.readouterr().out)
    assert cp["verdict"] == "BLOCKED"
    blocked = cp["blocked_items"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "escalation"
    assert blocked[0]["node_id"] == "gate1"


# --- checkpoint renders loop progress (invariant 6) -------------------------

def test_checkpoint_renders_loop_progress(tmp_path, capsys):
    """checkpoint renders loop progress (loops field, spec §4) so the
    orchestrator sees active loops from the checkpoint alone, matching
    state's loops field. An active loop shows loop_id + state=active +
    current iteration. invariant 6."""
    spec_path = _spec_path(tmp_path)
    h = Harness(tmp_path / "run")
    svid = "spec.v1"
    h.emit_loop_open(spec_version_id=svid, loop_id="L1", node_id="repeat1",
                     max_iterations=3, until="dry<2")
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=1)
    # loop_close NOT emitted → loop is ACTIVE

    rc = main(["checkpoint", spec_path, "--run-dir", str(tmp_path / "run")])
    cp = json.loads(capsys.readouterr().out)
    assert "loops" in cp
    assert len(cp["loops"]) == 1
    assert cp["loops"][0]["loop_id"] == "L1"
    assert cp["loops"][0]["state"] == "active"
    assert cp["loops"][0]["iteration"] == 1  # latest iteration_open
