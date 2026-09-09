"""per-surface evidence reconciliation (invariant #10: no borrowing evidence across surfaces).

Born from a real relay run (an admin-edit-user change): backend HTTP e2e (6 assertions)
is strong evidence, but it exercised the backend PATCH route + DB, NOT the frontend
UserListPanel component. Under the old global-pool rule (derive_assurance_verdict
filled one shared `filled` set), the backend e2e's `behavior` role silently satisfied
the frontend write_area's behavior slot -> holistic VERIFIED, masking the untested
frontend. This module forces each touched surface to independently satisfy its own
SURFACE_REQUIRED_ROLES; a pass evidence item only fills the surfaces it explicitly
lists in `addresses`.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))

from assurance import derive_assurance_verdict  # noqa: E402


def _ev(t, addrs, result="pass", **kw):
    e = {"type": t, "addresses": addrs, "result": result,
         "produced_by": "rg", "executed_by": "h", "adjudicated_by": "r"}
    e.update(kw)
    return e


CAP = {"runtime_mechanisms": ["http"]}


def test_backend_e2e_does_not_fill_frontend_behavior():
    # a real relay run's shape: runtime satisfied by e2e; frontend_interactive
    # gets static-scan only -> behavior missing -> PARTIAL.
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[
            _ev("static-scan", ["runtime", "frontend_interactive"]),
            _ev("e2e", ["runtime"], claim="PATCH backend -> 200 + persist"),
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime", "frontend_interactive"])
    assert v["status"] == "PARTIAL", v
    assert {"surface": "frontend_interactive", "role": "behavior"} \
        in v["missing_evidence"], v["missing_evidence"]
    # runtime must NOT be missing
    assert not any(m["surface"] == "runtime" for m in v["missing_evidence"])


def test_browser_e2e_addresses_frontend_makes_verified():
    # the adaptive-assurance path: spend a browser e2e on frontend_interactive
    # -> behavior filled -> VERIFIED (no cross-surface lie, explicit addresses).
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[
            _ev("static-scan", ["runtime", "frontend_interactive"]),
            _ev("e2e", ["runtime"]),
            _ev("e2e", ["frontend_interactive"], claim="browser: click Edit -> modal opens"),
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime", "frontend_interactive"])
    assert v["status"] == "VERIFIED", v


def test_evidence_without_addresses_fills_nothing_and_violates():
    # invariant #10: no addresses -> cannot fill ANY surface's slot + violation.
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[
            {"type": "e2e", "result": "pass",  # no addresses
             "produced_by": "h", "executed_by": "h", "adjudicated_by": "r"},
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime"])
    assert v["status"] != "VERIFIED", v
    assert any("addresses" in x for x in v["violations"]), v["violations"]
    # the unbound e2e did not fill runtime's behavior slot
    assert {"surface": "runtime", "role": "behavior"} in v["missing_evidence"]


def test_missing_evidence_carries_surface_field():
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[_ev("static-scan", ["runtime"])],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime", "frontend_interactive"])
    # missing entries are {surface, role} dicts, not bare role strings
    for m in v["missing_evidence"]:
        assert set(m.keys()) == {"surface", "role"}, m
    assert {"surface": "runtime", "role": "behavior"} in v["missing_evidence"]


def test_secret_auth_missing_auth_gate_is_blocked():
    # secret-auth surface requires auth_gate (human); missing -> BLOCKED,
    # not PARTIAL.
    v = derive_assurance_verdict(
        risk_floor="V3", declared_risk="V3",
        evidence=[
            _ev("static-scan", ["secret-auth"]),
            _ev("e2e", ["secret-auth"]),  # strong but not human -> no auth_gate
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["secret-auth"])
    assert v["status"] == "BLOCKED", v
    assert {"surface": "secret-auth", "role": "auth_gate"} in v["missing_evidence"]


def test_secret_auth_with_human_auth_gate_verified():
    v = derive_assurance_verdict(
        risk_floor="V3", declared_risk="V3",
        evidence=[
            _ev("static-scan", ["secret-auth"]),
            _ev("e2e", ["secret-auth"]),
            _ev("human", ["secret-auth"], claim="reviewer signed off"),
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["secret-auth"])
    assert v["status"] == "VERIFIED", v


def test_test_and_docs_surfaces_not_scored():
    # test/docs surfaces don't impose required roles; only runtime here.
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[
            _ev("static-scan", ["runtime"]),
            _ev("e2e", ["runtime"]),
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime", "test", "docs"])
    assert "test" not in v["surfaces_touched"]
    assert "docs" not in v["surfaces_touched"]
    assert v["status"] == "VERIFIED", v


def test_legacy_global_path_when_surfaces_none():
    # surfaces=None -> old ROLES_REQUIRED global pool (backward-compat for
    # fixtures not yet migrated to addresses).
    v = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[
            {"type": "static-scan", "result": "pass",
             "produced_by": "rg", "executed_by": "h", "adjudicated_by": "r"},
            {"type": "e2e", "result": "pass",
             "produced_by": "h", "executed_by": "h", "adjudicated_by": "r"},
        ],
        unresolved=[], capability_manifest=CAP,
        surfaces=None)
    # V1 requires reference_scan + behavior; static-scan gives ref, e2e gives behavior
    assert v["status"] == "VERIFIED", v
    assert "surfaces_touched" not in v  # legacy shape


def test_cross_surface_pooling_blocked_is_the_point():
    # THE real-relay-run regression: an e2e that addresses ONLY runtime must NOT fill
    # frontend_interactive's behavior, even though under the old global pool the same
    # e2e would have filled the shared `behavior` slot.
    v_per = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[_ev("e2e", ["runtime"])],
        unresolved=[], capability_manifest=CAP,
        surfaces=["runtime", "frontend_interactive"])
    v_global = derive_assurance_verdict(
        risk_floor="V1", declared_risk="V1",
        evidence=[{"type": "e2e", "result": "pass",
                   "produced_by": "h", "executed_by": "h", "adjudicated_by": "r"}],
        unresolved=[], capability_manifest=CAP,
        surfaces=None)
    # global (old) fills behavior -> only reference_scan missing -> PARTIAL but
    # the frontend behavior gap is invisible (missing_evidence is bare role names).
    # per-surface (new) -> runtime satisfied, frontend_interactive behavior
    # explicitly missing with a surface label.
    assert any(m == {"surface": "frontend_interactive", "role": "behavior"}
               for m in v_per["missing_evidence"]), v_per["missing_evidence"]
    # and the global path never surfaces the frontend gap by surface
    assert not any(isinstance(m, dict) and m.get("surface") == "frontend_interactive"
                   for m in v_global["missing_evidence"])
