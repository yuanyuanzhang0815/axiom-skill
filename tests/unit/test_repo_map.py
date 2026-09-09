"""Tests for scripts/generate_repo_map.py — derived Repo Map generator.

Covers: ast extraction correctness, commit stamp, --check stale detection.
This is the P-002 mechanism test (`axiom wiki pattern show P-002`)."""
import re
import sys
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_repo_map.py"
PKG = "axiom"


def _load_gen():
    spec = importlib.util.spec_from_file_location("generate_repo_map", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gen():
    return _load_gen()


def test_analyze_extracts_top_level_defs(gen):
    """ast extraction: top-level class Wiki + extract_entry etc. in wiki.py."""
    wiki = gen.PKG_DIR / "wiki.py"
    info = gen.analyze(wiki)
    assert info["file"] == "wiki.py"
    assert ("Wiki", 62) in info["classes"]
    assert any(name == "extract_entry" for name, _ in info["funcs"])
    # role extracted from the first docstring line
    assert "Spec-experience wiki" in info["role"]


def test_intra_imports_captures_function_level_import(gen):
    """A function-body `from axiom.state import ...` in wiki.py is also an intra-import edge."""
    wiki = gen.PKG_DIR / "wiki.py"
    tree = gen.ast.parse((wiki).read_text(encoding="utf-8"))
    edges = gen._intra_imports(tree)
    assert "state" in edges, "wiki.py imports axiom.state (function-level) — must be captured"


def test_compute_layers_leaves_and_aggregator(gen):
    """Leaf layer (no intra-import) = L0; cli.py is the sole aggregation point (L2, imports both wiki and harness)."""
    files = sorted(gen.PKG_DIR.glob("*.py"))
    modules = [gen.analyze(f) for f in files]
    layers = gen.compute_layers(modules)
    by_name = {m["modname"]: m for m in modules}
    # ir/ledger/dispatch/state are leaves
    for leaf in ("ir", "ledger", "dispatch"):
        assert layers[leaf] == 0, f"{leaf} should be leaf L0"
    # cli imports wiki + harness (aggregation point)
    assert "wiki" in by_name["cli"]["imports"]
    assert "harness" in by_name["cli"]["imports"]
    assert layers["cli"] > layers["wiki"], "cli must be above wiki"
    assert layers["cli"] > layers["harness"], "cli must be above harness"


def test_render_contains_commit_stamp_and_discipline(gen):
    """render output includes commit stamp + stale warning + discipline reminder."""
    files = sorted(gen.PKG_DIR.glob("*.py"))
    modules = [gen.analyze(f) for f in files]
    stamp = gen.git_stamp()
    out = gen.render(modules, stamp)
    assert f"commit `{stamp['commit']}`" in out
    assert "stale" in out.lower()
    assert "wiki.py" in out
    assert "Coverage before conclusion" in out  # discipline reminder pointing to SKILL.md
    assert "Retrieval surface, NOT a source of truth" in out


def test_main_writes_file_and_check_fresh(gen, tmp_path):
    """main --out writes a file; --check returns 0 when fresh."""
    out = tmp_path / "REPO_MAP.md"
    rc = gen.main(["--out", str(out)])
    assert rc == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "axiom Repo Map" in text
    # fresh check: stamp == HEAD
    rc2 = gen.main(["--check", "--out", str(out)])
    assert rc2 == 0, "fresh (stamp==HEAD) should return 0"


def test_check_stale_when_missing(gen, tmp_path):
    """--check returns 1 when the file does not exist (stale: does not exist)."""
    out = tmp_path / "nope.md"
    rc = gen.main(["--check", "--out", str(out)])
    assert rc == 1
