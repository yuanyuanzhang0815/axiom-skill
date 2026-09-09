"""v2 Task 5: loop/iteration/branch ledger events + hash-chain transparency.

5 new event kinds (spec §3), all flowing through Harness._record -> ledger.append,
acquiring seq/prev_hash/ts/event_hash. The hash chain is KIND-AGNOSTIC
(_event_hash hashes event-minus-event_hash with sort_keys) so NO ledger.py schema
change is needed — the chain structure is unchanged.

Body events (agent_result/verify_verdict/stagnating/artifact_write) carry
loop_id + iteration:k IN THEIR PAYLOAD — NOT as a kind suffix. This preserves
kind-filtered projections like derive_claim_status (filters kind=='agent_result').

Invariants guarded:
  (5) hash-chain tamper-evidence — kind-agnostic, unchanged structure.
  (6) honest projection — iteration:k in payload, not kind-suffix.
"""
import pytest

from axiom.harness import Harness
from axiom.ledger import Ledger, _GENESIS_HASH, _event_hash
from axiom.state import derive_claim_status


def _harness(tmp_path) -> Harness:
    return Harness(tmp_path / "run")


# ---------------------------------------------------------------------------
# Step 2.1 — full loop lifecycle, chain intact
# ---------------------------------------------------------------------------

def test_loop_lifecycle_chain_intact(tmp_path):
    """Emit loop_open -> iteration_open -> agent_result (body, payload has
    loop_id+iteration) -> iteration_close -> loop_close; verify_chain() empty."""
    h = _harness(tmp_path)
    svid = "sv1"

    h.emit_loop_open(
        spec_version_id=svid, loop_id="L1", node_id="repeat1",
        max_iterations=3, until="dry<2",
    )
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=1)
    # body agent_result: loop_id + iteration live IN THE PAYLOAD (not a suffix)
    h._record({
        "event_id": h._ev_id(), "kind": "agent_result",
        "spec_version_id": svid, "node_id": "worker1",
        "payload": {
            "validated_output": {"answer": 42}, "cost_usd": 0.01,
            "loop_id": "L1", "iteration": 1,
        },
        "claims": [],
    })
    h.emit_iteration_close(
        spec_version_id=svid, loop_id="L1", iteration=1,
        condition_eval="dry<2", dry_count=0,
    )
    h.emit_loop_close(
        spec_version_id=svid, loop_id="L1",
        final_iteration=1, exit_reason="converged",
    )

    events = h.ledger.events()
    kinds = [e["kind"] for e in events]
    assert kinds == [
        "loop_open", "iteration_open", "agent_result",
        "iteration_close", "loop_close",
    ]
    # invariant (5): chain intact after a full loop lifecycle
    assert h.ledger.verify_chain() == []


# ---------------------------------------------------------------------------
# Step 2.2 — kind-filtered projection unaffected by iteration in payload
# ---------------------------------------------------------------------------

def test_kind_filtered_projection_unaffected_by_iteration_payload(tmp_path):
    """derive_claim_status filters kind=='agent_result'. An agent_result that
    carries loop_id+iteration IN ITS PAYLOAD must still be found by that
    projection. A kind SUFFIX (agent_result_iter1) would have silently broken
    it — this test guards the 'payload not suffix' rule (invariant 6)."""
    lg = Ledger(tmp_path / "run")
    # body agent_result with loop_id+iteration in payload + a real claim
    lg.append({
        "event_id": "E1", "kind": "agent_result", "spec_version_id": "sv1",
        "node_id": "worker1",
        "payload": {
            "validated_output": {"answer": 42}, "cost_usd": 0.01,
            "loop_id": "L1", "iteration": 2,
        },
        "claims": [{"claim_id": "C1", "text": "t", "strength": "supported"}],
    })
    # kind-filtered projection still sees the claim -> SUPPORTED (not PROPOSED)
    assert derive_claim_status("C1", lg, svid="sv1") == "SUPPORTED"

    # and verify_verdict with loop_id+iteration in payload still works too
    lg.append({
        "event_id": "E2", "kind": "verify_verdict", "spec_version_id": "sv1",
        "node_id": "verify1", "claim_id": "C1", "refuted": False,
        "survived": True,
        "payload": {"loop_id": "L1", "iteration": 2},
    })
    assert derive_claim_status("C1", lg, svid="sv1") == "VERIFIED"


# ---------------------------------------------------------------------------
# Step 2.3 — seq monotonic across the lifecycle
# ---------------------------------------------------------------------------

def test_seq_monotonic(tmp_path):
    """seq increases 1..N across the lifecycle; prev_hash links each to the
    prior event's event_hash."""
    h = _harness(tmp_path)
    svid = "sv1"
    h.emit_loop_open(spec_version_id=svid, loop_id="L1", node_id="r",
                    max_iterations=2, until="dry<2")
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=1)
    h.emit_iteration_close(spec_version_id=svid, loop_id="L1", iteration=1,
                           condition_eval="dry<2", dry_count=1)
    h.emit_iteration_open(spec_version_id=svid, loop_id="L1", iteration=2)
    h.emit_iteration_close(spec_version_id=svid, loop_id="L1", iteration=2,
                           condition_eval="dry<2", dry_count=2)
    h.emit_loop_close(spec_version_id=svid, loop_id="L1",
                      final_iteration=2, exit_reason="converged")

    events = h.ledger.events()
    seqs = [e["seq"] for e in events]
    assert seqs == [1, 2, 3, 4, 5, 6]
    # prev_hash chain links
    assert events[0]["prev_event_hash"] == _GENESIS_HASH
    for i in range(1, len(events)):
        assert events[i]["prev_event_hash"] == events[i - 1]["event_hash"]


# ---------------------------------------------------------------------------
# Step 2.4 — event_hash stable (deterministic, kind-agnostic)
# ---------------------------------------------------------------------------

def test_event_hash_stable(tmp_path):
    """Re-append the SAME payload (with a pinned ts) to two FRESH ledgers ->
    identical event_hash. Proves _event_hash is deterministic and
    kind-agnostic: the new kinds hash through the same path as v1 events."""
    payload = {"loop_id": "L1", "node_id": "r", "max_iterations": 3,
               "until": "dry<2"}
    ev = {
        "event_id": "E1", "kind": "loop_open", "spec_version_id": "sv1",
        "node_id": "r", "payload": payload, "ts": "2026-08-27T00:00:00+00:00",
    }
    lg1 = Ledger(tmp_path / "r1")
    lg2 = Ledger(tmp_path / "r2")
    sealed1 = lg1.append(dict(ev))
    sealed2 = lg2.append(dict(ev))
    assert sealed1["event_hash"] == sealed2["event_hash"]

    # also confirm a different kind with the SAME payload shape hashes
    # DIFFERENTLY (kind is part of the hashed content — kind-agnostic STRUCTURE,
    # not kind-blind content).
    ev_branch = dict(ev)
    ev_branch["kind"] = "branch_decision"
    sealed_b = lg1.append(ev_branch)
    assert sealed_b["event_hash"] != sealed1["event_hash"]

    # and the pure function is stable too
    assert _event_hash(dict(ev, event_hash="x")) == _event_hash(
        dict(ev, event_hash="y")
    )


# ---------------------------------------------------------------------------
# Step 2.5 — branch_decision recorded with all fields
# ---------------------------------------------------------------------------

def test_branch_decision_recorded(tmp_path):
    """branch_decision carries node_id, predicate, eval_result (bool|'undefined'),
    branch_taken ('then'|'else'), degraded (bool), claim_strength
    (verified|proposed|deterministic) — all in the payload per spec §3."""
    h = _harness(tmp_path)
    svid = "sv1"
    h.emit_branch_decision(
        spec_version_id=svid, node_id="cond1",
        predicate="claim_status('C1')=='VERIFIED'",
        eval_result=True, branch_taken="then",
        degraded=False, claim_strength="deterministic",
    )
    h.emit_branch_decision(
        spec_version_id=svid, node_id="cond2",
        predicate="agent.x > 0",
        eval_result="undefined", branch_taken="else",
        degraded=True, claim_strength="proposed",
    )

    events = [e for e in h.ledger.events() if e["kind"] == "branch_decision"]
    assert len(events) == 2

    e1 = events[0]
    assert e1["node_id"] == "cond1"
    p = e1["payload"]
    assert p["node_id"] == "cond1"
    assert p["predicate"] == "claim_status('C1')=='VERIFIED'"
    assert p["eval_result"] is True
    assert p["branch_taken"] == "then"
    assert p["degraded"] is False
    assert p["claim_strength"] == "deterministic"

    e2 = events[1]
    p2 = e2["payload"]
    assert p2["eval_result"] == "undefined"
    assert p2["branch_taken"] == "else"
    assert p2["degraded"] is True
    assert p2["claim_strength"] == "proposed"

    # chain still intact with branch events
    assert h.ledger.verify_chain() == []


# ---------------------------------------------------------------------------
# Extra: exit_reason closed-set + iteration payload in iteration_open/close
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "converged", "exhausted", "budget_exhausted", "stagnation_cap",
])
def test_loop_close_exit_reasons(tmp_path, reason):
    h = _harness(tmp_path)
    h.emit_loop_close(spec_version_id="sv1", loop_id="L1",
                      final_iteration=3, exit_reason=reason)
    e = h.ledger.events()[-1]
    assert e["kind"] == "loop_close"
    assert e["payload"]["exit_reason"] == reason
    assert e["payload"]["final_iteration"] == 3
    assert e["payload"]["loop_id"] == "L1"
    assert h.ledger.verify_chain() == []


def test_iteration_open_close_payload_shape(tmp_path):
    """iteration_open carries {loop_id, iteration:k}; iteration_close carries
    {loop_id, iteration:k, condition_eval, dry_count} — k is a payload field,
    not a kind suffix."""
    h = _harness(tmp_path)
    h.emit_iteration_open(spec_version_id="sv1", loop_id="L1", iteration=3)
    h.emit_iteration_close(spec_version_id="sv1", loop_id="L1", iteration=3,
                           condition_eval="dry<2", dry_count=1)
    open_ev = h.ledger.events()[0]
    close_ev = h.ledger.events()[1]
    assert open_ev["kind"] == "iteration_open"
    assert open_ev["payload"] == {"loop_id": "L1", "iteration": 3}
    assert close_ev["kind"] == "iteration_close"
    assert close_ev["payload"] == {
        "loop_id": "L1", "iteration": 3,
        "condition_eval": "dry<2", "dry_count": 1,
    }


def test_loop_open_until_vs_cond(tmp_path):
    """loop_open carries until (RepeatNode) OR cond (UntilNode) — not both."""
    h = _harness(tmp_path)
    h.emit_loop_open(spec_version_id="sv1", loop_id="L1", node_id="rep1",
                    max_iterations=3, until="dry<2")
    h.emit_loop_open(spec_version_id="sv1", loop_id="L2", node_id="unt1",
                    max_iterations=5, cond="claim_status('C1')=='VERIFIED'")
    e1, e2 = h.ledger.events()
    assert e1["payload"]["until"] == "dry<2"
    assert "cond" not in e1["payload"]
    assert e2["payload"]["cond"] == "claim_status('C1')=='VERIFIED'"
    assert "until" not in e2["payload"]
