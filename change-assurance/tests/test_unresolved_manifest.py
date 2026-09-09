"""C: unresolved is computed, not imagined by the Agent — 4 acceptance cases.

Tests two pure functions + the adjudicate end-to-end:
  derive_unresolved(manifest, analysis)        — machine set-difference
  validate_proposed_unresolved(proposed, manifest) — checks whether the LLM fabricated

Three-state iron rule: present / absent_confirmed / unknown.
Only absent_confirmed can suppress a capability's unresolved.
unknown expresses epistemic uncertainty, not "there is a bug".
"""
import json
import sys
from pathlib import Path

# add the skill scripts directory to the import path
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from unresolved_from_manifest import derive_unresolved, validate_proposed_unresolved  # noqa: E402
import adjudicate  # noqa: E402


def _ids(items):
    return [u["id"] for u in items]


# ---- Case 1: a real-time app has no WebSocket; LLM proposes websocket_refresh -> violation ----

def test_case1_absent_confirmed_blocks_invented_unresolved():
    """websocket=absent_confirmed: the project does not have this mechanism; the LLM
    proposing websocket_refresh is fabricating (this is the regression case for the
    real-time-app websocket hallucination)."""
    manifest = {"websocket": "absent_confirmed", "event_bus": "absent_confirmed",
                "background_jobs": "present", "database": "present",
                "openapi": "unknown", "shared_state": "present"}
    proposed = [{"id": "websocket_refresh not inspected", "impact": "high"}]
    violations = validate_proposed_unresolved(proposed, manifest)
    assert len(violations) == 1
    assert "absent_confirmed" in violations[0]
    assert "inventing" in violations[0]
    # and an absent_confirmed capability itself produces no unresolved (no gap to record)
    derived = derive_unresolved(manifest, {"websocket_scan": False})
    assert not any("websocket" in u["id"] for u in derived), (
        "absent_confirmed capability must NOT spawn an unresolved")


# ---- Case 2: event_bus present + unscanned -> unresolved auto-appears ----

def test_case2_present_unscanned_spawns_coverage_gap():
    manifest = {"event_bus": "present"}
    derived = derive_unresolved(manifest, {"event_bus_scan": False})
    assert len(derived) == 1
    assert derived[0]["kind"] == "coverage_gap"
    assert derived[0]["capability"] == "event_bus"
    assert "event_bus_scan not performed" in derived[0]["id"]


# ---- Case 3: event_bus present + scanned -> none ----

def test_case3_present_scanned_no_unresolved():
    manifest = {"event_bus": "present"}
    derived = derive_unresolved(manifest, {"event_bus_scan": True})
    assert len(derived) == 0


# ---- Case 4: capability unknown -> epistemic unresolved, must not be told as a concrete bug ----

def test_case4_unknown_is_epistemic_not_bug_story():
    """websocket=unknown: expresses "not inspected clearly", not "there is a websocket bug".
    If the LLM writes 'websocket_refresh affected' -> violation (treating uncertainty as a conclusion)."""
    manifest = {"websocket": "unknown"}
    derived = derive_unresolved(manifest, {"websocket_scan": False})
    assert len(derived) == 1
    assert derived[0]["kind"] == "capability_unknown"
    assert "unknown" in derived[0]["id"].lower()
    assert "refresh" not in derived[0]["id"].lower(), (
        "unknown must read as epistemic 'capability unknown', not a concrete "
        "effect like 'websocket_refresh affected'")

    # the LLM telling an unknown as a concrete effect -> violation
    proposed = [{"id": "websocket_refresh affected", "impact": "high"}]
    violations = validate_proposed_unresolved(proposed, manifest)
    assert len(violations) == 1
    assert "unknown" in violations[0]
    assert "epistemically" in violations[0] or "epistemic" in violations[0]


# ---- end-to-end: adjudicate wires manifest violations into the verdict, blocking false VERIFIED ----

def test_adjudicate_manifest_violation_blocks_verified(tmp_path):
    """A receipt with a manifest: the LLM proposes websocket_refresh (absent_confirmed)
    -> manifest_violation -> a would-be VERIFIED is downgraded to PARTIAL."""
    receipt = {
        "write_areas": ["docs/readme.md"], "declared_risk": "V0",
        "evidence": [{"claim": "docs ok", "type": "static-scan",
                      "produced_by": "grep", "executed_by": "h",
                      "adjudicated_by": "r", "result": "pass"}],
        "unresolved": [{"id": "websocket_refresh not inspected", "impact": "high"}],
        "capability_manifest": {
            "websocket": "absent_confirmed", "event_bus": "absent_confirmed",
            "background_jobs": "present", "database": "present",
            "openapi": "unknown", "shared_state": "present"},
        "analysis_performed": {"websocket_scan": False, "event_bus_scan": False,
                              "background_job_scan": False, "db_schema_check": True,
                              "openapi_diff_check": True, "shared_state_scan": True},
    }
    v = adjudicate.adjudicate(receipt)
    assert v["status"] == "PARTIAL", "inventing websocket_refresh must block VERIFIED"
    assert v.get("manifest_violations"), "manifest_violations recorded"
    assert "websocket_refresh" in v["manifest_violations"][0]
    # the derived unresolved uses the machine set-difference, no fabricated websocket_refresh
    assert not any("websocket_refresh" in str(u) for u in v.get("unresolved_high_impact", []))


def test_adjudicate_no_manifest_records_violation(tmp_path):
    """manifest missing -> does not default to "no runtime mechanisms"; records a violation (may not be V0)."""
    receipt = {
        "write_areas": ["docs/readme.md"], "declared_risk": "V0",
        "evidence": [{"claim": "docs ok", "type": "static-scan",
                      "produced_by": "grep", "executed_by": "h",
                      "adjudicated_by": "r", "result": "pass"}],
        "unresolved": [], "capability_manifest": None,
    }
    v = adjudicate.adjudicate(receipt)
    assert any("capability_manifest" in s or "manifest" in s.lower()
               for s in v.get("violations", [])), \
        "manifest absent must be a violation, not silent V0"
