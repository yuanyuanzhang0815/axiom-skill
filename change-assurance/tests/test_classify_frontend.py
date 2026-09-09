"""classify_change: frontend_interactive vs frontend_presentational.

Risk-adaptive: a .tsx with event handlers/state/forms/Modal/mutation needs a
behavior oracle; pure CSS/presentational JSX needs only static_check. Avoids
the "run a NASA-grade pipeline for a one-line copy change" tax while still
gating interactive changes.

Bare .tsx -> frontend_presentational (V0); content scan upgrades to
frontend_interactive (V1) when interaction patterns match. CSS/presentational
stays V0. Heavier path signals (shared-state, public-contract, secret-auth)
win first and are NOT downgraded by the frontend rule.
"""
import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CLASSIFY = SKILL / "scripts" / "classify_change.py"


def _classify(tmp_path: Path, rel: str, content: str) -> dict:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(CLASSIFY), str(p)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _file_surf(d: dict) -> tuple[str, str]:
    f = d["files"][0]
    return f["surface"], f["floor"]


def test_tsx_with_event_handler_is_interactive(tmp_path):
    d = _classify(tmp_path, "src/Panel.tsx", (
        "import {useState} from 'react';\n"
        "export const Panel = () => {\n"
        "  const [open, setOpen] = useState(false);\n"
        "  return <button onClick={() => setOpen(true)}>Edit</button>;\n"
        "};\n"))
    surf, floor = _file_surf(d)
    assert surf == "frontend_interactive" and floor == "V1", (surf, floor)
    assert any(s.startswith("frontend:") for s in d["files"][0]["signals"])


def test_tsx_with_form_is_interactive(tmp_path):
    d = _classify(tmp_path, "src/Form.tsx",
                  "export const F = () => <form><input name='x'/></form>;\n")
    assert _file_surf(d) == ("frontend_interactive", "V1")


def test_tsx_with_modal_is_interactive(tmp_path):
    d = _classify(tmp_path, "src/Dialog.tsx",
                  "export const D = () => <Modal open>hi</Modal>;\n")
    assert _file_surf(d) == ("frontend_interactive", "V1")


def test_tsx_with_mutation_is_interactive(tmp_path):
    d = _classify(tmp_path, "src/Mut.tsx",
                  "export const M = () => { const m = useMutation(); return null; };\n")
    assert _file_surf(d) == ("frontend_interactive", "V1")


def test_tsx_pure_presentational_is_presentational(tmp_path):
    d = _classify(tmp_path, "src/Label.tsx", (
        "export const Label = ({text}: {text: string}) => (\n"
        "  <span className='label'>{text}</span>\n"
        ");\n"))
    surf, floor = _file_surf(d)
    assert surf == "frontend_presentational" and floor == "V0", (surf, floor)


def test_css_is_presentational(tmp_path):
    # CSS class names like .submit-form must NOT trip the interaction patterns
    # (submit/onClick are word-boundary matches but .css is not a component ext).
    d = _classify(tmp_path, "style.css",
                  ".btn { color: red; } .submit-form { width: 100px; } .onClick-hover {}\n")
    assert _file_surf(d) == ("frontend_presentational", "V0")


def test_backend_py_is_runtime(tmp_path):
    d = _classify(tmp_path, "backend/app/db.py",
                  "def update_display_name(uid, name): return None\n")
    assert _file_surf(d) == ("runtime", "V1")


def test_store_tsx_path_signal_wins(tmp_path):
    # store.tsx -> shared-state (V2) via PATH_SIGNALS; the frontend rule must
    # NOT downgrade it to frontend_interactive (V1 < V2).
    d = _classify(tmp_path, "src/store.tsx",
                  "export const useStore = create(() => ({}));\n")
    assert _file_surf(d) == ("shared-state", "V2")


def test_api_path_tsx_is_public_contract(tmp_path):
    # frontend/src/api/client.ts -> public-contract (V2) via /api/ path signal;
    # NOT downgraded to frontend_presentational.
    d = _classify(tmp_path, "frontend/src/api/client.ts",
                  "export const adminUpdateUser = (id, body) => fetch(`/admin/${id}`, {method:'PATCH', body});\n")
    assert _file_surf(d) == ("public-contract", "V2")


def test_aggregate_mixed_surfaces(tmp_path):
    # a spec touching backend runtime + frontend interactive + presentational:
    # risk_floor = max(V1, V1, V0) = V1; surfaces_touched lists all three.
    import json as _j
    paths = []
    for rel, content in [
        ("backend/app/main.py", "def f(): pass\n"),
        ("src/Panel.tsx", "export const P = () => <button onClick={()=>{}}>x</button>;\n"),
        ("src/Label.tsx", "export const L = () => <span>hi</span>;\n"),
        ("src/style.css", ".x { color: red; }\n"),
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        paths.append(str(p))
    r = subprocess.run(
        [sys.executable, str(CLASSIFY)] + paths,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    agg = json.loads(r.stdout)
    assert agg["risk_floor"] == "V1"
    touched = set(agg["surfaces_touched"])
    assert "runtime" in touched and "frontend_interactive" in touched \
        and "frontend_presentational" in touched, touched
