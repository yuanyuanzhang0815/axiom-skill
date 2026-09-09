"""classify_change + assurance: type-declaration surface (.d.ts decoupling).

Born from a calibration run (an edit-user relay's baseURL->env change):
`vite-env.d.ts` is a TypeScript ambient declaration file. The old EXT_DEFAULT
lumped `.ts`->runtime (V1) which demands `behavior` — structurally unsatisfiable
for a declaration file (tsc fills static_check, not behavior). The fix: `.d.ts`
-> type-declaration surface (contract=[static_check], no behavior required).

Calibration point (user-pinned): surface decides "what evidence is meaningful",
risk decides "how strong is needed", the two are decoupled. A high-impact .d.ts
!= needs a runtime behavior oracle — a public API .d.ts is high-risk V2, but its
meaningful evidence is tsc/contract-diff/reference-scan, not browser/runtime
behavior. So .d.ts's surface is fixed to type-declaration; path signals only
raise the floor (risk) without changing surface.
"""
import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CLASSIFY = SKILL / "scripts" / "classify_change.py"

HERE = SKILL / "scripts"
sys.path.insert(0, str(HERE))

from assurance import derive_assurance_verdict  # noqa: E402

CAP = {"runtime_mechanisms": ["http"]}


def _classify(paths: list[str]) -> dict:
    r = subprocess.run(
        [sys.executable, str(CLASSIFY), *paths],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- classify: .d.ts -> type-declaration ----

def test_plain_dts_is_type_declaration_v0():
    d = _classify(["src/vite-env.d.ts"])
    f = d["files"][0]
    assert f["surface"] == "type-declaration", f["surface"]
    assert f["floor"] == "V0", f["floor"]
    assert "type-declaration" in d["surfaces_touched"]


def test_dts_does_not_become_runtime_via_ext():
    """Old bug: .ts->runtime->demanding behavior. .d.ts must land on type-declaration, not runtime."""
    d = _classify(["src/vite-env.d.ts"])
    assert d["files"][0]["surface"] != "runtime"
    assert d["risk_floor"] == "V0"


def test_dts_under_api_path_raises_floor_keeps_surface():
    """surface=type-declaration (contract=static_check, no behavior);
    path /api/ raises the floor to V2 (risk), but surface is unchanged — decoupled."""
    d = _classify(["src/api/types/index.d.ts"])
    f = d["files"][0]
    assert f["surface"] == "type-declaration", f["surface"]
    assert f["floor"] == "V2", f["floor"]   # path raises risk to V2
    assert "type-declaration" in d["surfaces_touched"]
    assert "public-contract" not in d["surfaces_touched"]  # surface unchanged


def test_dts_content_ignored_no_behavior_upgrade():
    """Even if a .d.ts file's content contains useState/onClick (declaring React
    types), the surface stays type-declaration and does NOT upgrade to
    frontend_interactive — declaration files have no runtime behavior."""
    d = _classify(["src/shim.d.ts"])  # path/ext classification works even if the file is absent
    assert d["files"][0]["surface"] == "type-declaration"


def test_dcts_dmts_recognized():
    d = _classify(["a.d.cts", "b.d.mts"])
    assert all(f["surface"] == "type-declaration" for f in d["files"])


def test_calib1_scenario_surfaces_and_floor():
    """A calibration run's real 5 write_areas: 3 api .ts(public-contract V2) + .env(config V0)
    + vite-env.d.ts(type-declaration V0). risk_floor=V2 (public-contract),
    surfaces contains type-declaration."""
    d = _classify([
        "frontend-product-ui/src/api/client.ts",
        "frontend-product-ui/src/api/analytics.ts",
        "frontend-product-ui/src/api/templates.ts",
        "frontend-product-ui/.env",
        "frontend-product-ui/src/vite-env.d.ts",
    ])
    assert set(d["surfaces_touched"]) == {"public-contract", "config",
                                          "type-declaration"}, d["surfaces_touched"]
    assert d["risk_floor"] == "V2"


# ---- assurance: type-declaration contract = [static_check], no behavior needed ----

def _ev(t, addrs, result="pass", **kw):
    e = {"type": t, "addresses": addrs, "result": result,
         "produced_by": "rg", "executed_by": "h", "adjudicated_by": "r"}
    e.update(kw)
    return e


def test_type_declaration_tsc_satisfies_no_behavior_needed():
    """.d.ts hits the type-declaration surface; tsc(compiler) fills static_check -> VERIFIED.
    Key: behavior is NOT required. This is the core assertion of the calibration run's replay after the fix."""
    v = derive_assurance_verdict(
        risk_floor="V0", declared_risk="V0",
        evidence=[_ev("compiler", ["type-declaration"],
                      claim="tsc --noEmit clean")],
        unresolved=[], capability_manifest=CAP,
        surfaces=["type-declaration"],
    )
    assert v["status"] == "VERIFIED", v
    assert v["missing_evidence"] == [], v["missing_evidence"]


def test_high_risk_dts_still_no_behavior_required():
    """Decoupling iron rule: a high-risk (V2) .d.ts still does NOT require behavior.
    surface(type-declaration) decides the evidence form [tsc], risk(V2) only decides
    strength. risk_floor=V2 but the contract is still =[static_check]."""
    v = derive_assurance_verdict(
        risk_floor="V2", declared_risk="V2",
        evidence=[_ev("compiler", ["type-declaration"],
                      claim="tsc clean; public API types unchanged")],
        unresolved=[], capability_manifest=CAP,
        surfaces=["type-declaration"],
    )
    assert v["status"] == "VERIFIED", v
    # behavior is not in the type-declaration contract — this is the essence of the calibration fix
    assert not any(m.get("role") == "behavior"
                   for m in v["missing_evidence"]), v["missing_evidence"]


def test_type_declaration_missing_static_check_is_unverified():
    """Ran no tsc/static check and no evidence filled a slot -> UNVERIFIED (not PARTIAL;
    PARTIAL requires some evidence filled). The contract still applies; the missing
    static_check is recorded in missing_evidence."""
    v = derive_assurance_verdict(
        risk_floor="V0", declared_risk="V0",
        evidence=[_ev("unit", ["type-declaration"], ai_authored=True,
                      claim="AI-freshly-written test")],  # weak, fills no slot
        unresolved=[], capability_manifest=CAP,
        surfaces=["type-declaration"],
    )
    assert v["status"] == "UNVERIFIED", v
    assert {"surface": "type-declaration", "role": "static_check"} in v["missing_evidence"], v["missing_evidence"]


def test_static_scan_also_satisfies_type_declaration():
    """static-scan also fills static_check (not only compiler) -> satisfied."""
    v = derive_assurance_verdict(
        risk_floor="V0", declared_risk="V0",
        evidence=[_ev("static-scan", ["type-declaration"],
                      claim="rg confirms the declaration exists")],
        unresolved=[], capability_manifest=CAP,
        surfaces=["type-declaration"],
    )
    assert v["status"] == "VERIFIED", v
