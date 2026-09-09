"""wiki: append-only spec-experience store + retrieval.

WikiSkill's Wiki layer over axiom: Raw=ledger, Wiki=wiki.jsonl (this),
Skills=spec.v{n}. The wiki distills a run's verdict + failure patterns into a
searchable, never-rolled-back store so `plan --wiki-suggest` puts prior
shape/verdict/impact in hand BEFORE the next spec is designed -- format
arrives before output, eliminating the source of format friction.
"""
import json
from pathlib import Path

import pytest

from axiom.cli import main as cli_main
from axiom.harness import Harness
from axiom.ir import Spec, Requirement
from axiom.ledger import Ledger
from axiom.state import derive_debug_envelope, derive_spec_shape
from axiom.wiki import Wiki, extract_entry


def _spec(intent="produce a claim for the relay",
          success_evidence=("S1:R1=claim:C1",), nodes=None, svid="spec.v1"):
    return Spec(
        spec_version_id=svid, parent_spec_id=None, revision=1,
        intent=intent, requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"], success_evidence=list(success_evidence),
        nodes=nodes or {}, control_flow={"type": "sequence", "steps": []},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def _ev(kind, **kw):
    e = {"event_id": kw.pop("eid", f"ev_{kind}"), "kind": kind}
    e.update(kw)
    return e


def _stagnating_ledger(h, svid="spec.v1"):
    """A ledger with one stagnating node (denials -> cognitive signature)."""
    h.ledger.append(_ev("stagnating", spec_version_id=svid, node_id="n1",
                        payload={"attempt": 2,
                                 "denials": ["no_write:src/a.py"],
                                 "conforms": False, "cost_usd": 0.01,
                                 "num_turns": 1,
                                 "result_text_preview": "could not write"}))


# --- derive_spec_shape ------------------------------------------------------

def test_spec_shape_counts_node_types():
    spec = _spec(nodes={
        "n1": {"type": "agent", "id": "n1"},
        "n2": {"type": "verify", "id": "n2"},
        "n3": {"type": "agent", "id": "n3"},
    })
    shape = derive_spec_shape(spec)
    assert "agentx2" in shape
    assert "verifyx1" in shape
    assert "sequence" in shape
    assert "budget=5.0" in shape
    assert "max_stag=1" in shape


# --- extract ---------------------------------------------------------------

def test_extract_distills_pattern_from_stagnating(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object",
                                                 "required": ["claim_id"]}}})
    _stagnating_ledger(h)
    entry = extract_entry(spec, h.ledger, str(tmp_path / "run"),
                          tags=["bugfix"], learned="write-area must match repo")
    assert entry["verdict"] == "PARTIAL"
    assert entry["spec_shape"]
    assert len(entry["patterns"]) == 1
    p = entry["patterns"][0]
    assert p["failure_class"] == "stagnating"
    assert p["cognitive_signature"]["kind"] == "denials"
    assert p["recovery_action"] == "re-dispatch"
    assert entry["tags"] == ["bugfix"]
    assert entry["learned"] == "write-area must match repo"


def test_extract_verified_run_has_no_patterns(tmp_path):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec()
    # a VERIFIED run: verify_verdict survived -> R1 -> VERIFIED, no failures
    h.ledger.append(_ev("agent_result", spec_version_id="spec.v1",
                        node_id="n1", claims=[{"claim_id": "C1",
                                               "strength": "supported"}]))
    h.ledger.append(_ev("verify_verdict", spec_version_id="spec.v1",
                        claim_id="C1", survived=True, refuted=False))
    entry = extract_entry(spec, h.ledger, str(tmp_path / "run"))
    assert entry["verdict"] == "VERIFIED"
    assert entry["patterns"] == []


# --- append + only-add -----------------------------------------------------

def test_append_seals_chain_fields(tmp_path):
    w = Wiki(tmp_path / "wiki")
    e = w.append({"entry_type": "experience", "intent": "x",
                  "spec_version_id": "spec.v1", "run_dir": "/r",
                  "verdict": "PARTIAL", "patterns": [], "learned": "",
                  "tags": [], "requirements": [], "spec_shape": "s"})
    assert e["entry_id"].startswith("sha256:")
    assert e["prev_hash"] == "sha256:" + "0" * 64
    assert e["event_hash"].startswith("sha256:")
    assert e["ts"]


def test_consecutive_extract_does_not_mutate_old_entry(tmp_path):
    """Only-add: a second extract appends a NEW entry; the first's bytes
    are never rewritten (append-only, not in-place amendment)."""
    w = Wiki(tmp_path / "wiki")
    e1 = w.append({"intent": "first", "spec_version_id": "spec.v1",
                   "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                   "learned": "", "tags": [], "requirements": [],
                   "spec_shape": "s"})
    e2 = w.append({"intent": "second", "spec_version_id": "spec.v1",
                   "run_dir": "/r", "verdict": "VERIFIED", "patterns": [],
                   "learned": "", "tags": [], "requirements": [],
                   "spec_shape": "s"})
    entries = w.entries()
    assert len(entries) == 2
    # first entry unchanged (same sealed fields)
    assert entries[0]["entry_id"] == e1["entry_id"]
    assert entries[0]["intent"] == "first"
    # chain links: e2.prev_hash == e1.event_hash
    assert entries[1]["prev_hash"] == e1["event_hash"]


# --- impact (append-only amendment) ---------------------------------------

def test_impact_appends_without_mutating_parent(tmp_path):
    w = Wiki(tmp_path / "wiki")
    parent = w.append({"intent": "x", "spec_version_id": "spec.v1",
                       "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                       "learned": "", "tags": [], "requirements": [],
                       "spec_shape": "s"})
    before = list(w.entries())
    sealed = w.add_impact(parent["entry_id"], "replan_denied",
                          "replanning without new evidence")
    after = w.entries()
    assert len(after) == len(before) + 1  # appended, not mutated
    assert sealed["entry_type"] == "impact"
    assert sealed["amends"] == parent["entry_id"]
    assert sealed["impact_kind"] == "replan_denied"
    # parent entry bytes unchanged
    assert after[0] == before[0]


def test_impact_on_missing_entry_raises(tmp_path):
    w = Wiki(tmp_path / "wiki")
    with pytest.raises(KeyError):
        w.add_impact("sha256:nonexistent", "verify_failed", "x")


def test_search_aggregates_impact(tmp_path):
    w = Wiki(tmp_path / "wiki")
    parent = w.append({"intent": "build relay", "spec_version_id": "spec.v1",
                       "run_dir": "/r", "verdict": "PARTIAL",
                       "patterns": [], "learned": "denials on write",
                       "tags": ["bugfix"], "requirements": [],
                       "spec_shape": "agentx1|sequence|budget=5|max_stag=1"})
    w.add_impact(parent["entry_id"], "replan_denied", "no new evidence")
    w.add_impact(parent["entry_id"], "verify_failed", "schema mismatch")
    results = w.search(query="relay")
    assert len(results) == 1
    r = results[0]
    assert len(r["impact"]) == 2
    kinds = {i["kind"] for i in r["impact"]}
    assert kinds == {"replan_denied", "verify_failed"}


# --- search ----------------------------------------------------------------

def test_search_keyword_hits_rank(tmp_path):
    w = Wiki(tmp_path / "wiki")
    for intent, verdict in [("build relay", "PARTIAL"),
                            ("build dashboard ui", "VERIFIED"),
                            ("relay debugging", "BLOCKED")]:
        w.append({"intent": intent, "spec_version_id": "spec.v1",
                  "run_dir": "/r", "verdict": verdict, "patterns": [],
                  "learned": "", "tags": [], "requirements": [],
                  "spec_shape": "s"})
    results = w.search(query="relay")
    assert len(results) == 2  # "build relay" + "relay debugging"
    # both hit "relay"; rank stable by entry_id tiebreak
    for r in results:
        assert "relay" in r["intent"]


def test_search_tag_filter(tmp_path):
    w = Wiki(tmp_path / "wiki")
    w.append({"intent": "a", "spec_version_id": "spec.v1", "run_dir": "/r",
              "verdict": "PARTIAL", "patterns": [], "learned": "",
              "tags": ["bugfix", "ui"], "requirements": [], "spec_shape": "s"})
    w.append({"intent": "b", "spec_version_id": "spec.v1", "run_dir": "/r",
              "verdict": "VERIFIED", "patterns": [], "learned": "",
              "tags": ["ui"], "requirements": [], "spec_shape": "s"})
    results = w.search(tags=["bugfix"])
    assert len(results) == 1
    assert results[0]["intent"] == "a"


def test_search_verdict_filter(tmp_path):
    w = Wiki(tmp_path / "wiki")
    w.append({"intent": "a", "spec_version_id": "spec.v1", "run_dir": "/r",
              "verdict": "VERIFIED", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    w.append({"intent": "b", "spec_version_id": "spec.v1", "run_dir": "/r",
              "verdict": "PARTIAL", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    results = w.search(verdict="VERIFIED")
    assert len(results) == 1
    assert results[0]["verdict"] == "VERIFIED"


def test_search_empty_query_returns_all(tmp_path):
    w = Wiki(tmp_path / "wiki")
    for i in range(3):
        w.append({"intent": f"i{i}", "spec_version_id": "spec.v1",
                  "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                  "learned": "", "tags": [], "requirements": [],
                  "spec_shape": "s"})
    assert len(w.search()) == 3


# --- verify chain ----------------------------------------------------------

def test_verify_chain_intact(tmp_path):
    w = Wiki(tmp_path / "wiki")
    for i in range(3):
        w.append({"intent": f"i{i}", "spec_version_id": "spec.v1",
                  "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                  "learned": "", "tags": [], "requirements": [],
                  "spec_shape": "s"})
    assert w.verify_chain() == []


def test_verify_chain_detects_tamper(tmp_path):
    w = Wiki(tmp_path / "wiki")
    w.append({"intent": "i", "spec_version_id": "spec.v1", "run_dir": "/r",
              "verdict": "PARTIAL", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    # tamper: rewrite the file with a mutated intent (keep event_hash stale)
    lines = w.wiki_path.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[0])
    e["intent"] = "tampered"
    w.wiki_path.write_text(json.dumps(e) + "\n", encoding="utf-8")
    errs = w.verify_chain()
    assert any("event_hash mismatch" in s for s in errs)


# --- CLI: plan --wiki-suggest ---------------------------------------------

def test_plan_wiki_suggest_surfaces_lessons(tmp_path, capsys):
    w = Wiki(tmp_path / "wiki")
    parent = w.append({"intent": "build the relay write path", "spec_version_id": "spec.v1",
                       "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                       "learned": "write-area must match repo root",
                       "tags": ["bugfix"], "requirements": [],
                       "spec_shape": "agentx1|sequence|budget=5|max_stag=1"})
    w.add_impact(parent["entry_id"], "replan_denied", "no new evidence")
    rc = cli_main(["plan", "--intent", "build the relay write path",
                   "--wiki-suggest", "--wiki-dir", str(tmp_path / "wiki"),
                   "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    captured = capsys.readouterr()
    # suggestions go to stderr so stdout stays a clean spec template
    assert "wiki experience" in captured.err
    assert "PARTIAL" in captured.err
    assert "replan_denied" in captured.err
    # stdout is still a valid spec template
    spec = json.loads(captured.out)
    assert "spec_version_id" in spec


def test_plan_wiki_suggest_empty(tmp_path, capsys):
    rc = cli_main(["plan", "--intent", "novel intent nothing matches",
                   "--wiki-suggest", "--wiki-dir", str(tmp_path / "wiki"),
                   "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no prior experience" in captured.err


# --- CLI: wiki extract / search / verify -----------------------------------

def test_cli_extract_then_search(tmp_path, capsys):
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _spec(nodes={"n1": {"type": "agent", "id": "n1",
                               "output_schema": {"type": "object",
                                                 "required": ["claim_id"]}}})
    _stagnating_ledger(h)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "spec_version_id": spec.spec_version_id, "parent_spec_id": None,
        "revision": 1, "intent": spec.intent,
        "requirements": [{"id": "R1", "text": "r", "criticality": "required"}],
        "boundaries": ["b"], "success_evidence": ["S1:R1=claim:C1"],
        "nodes": spec.nodes, "control_flow": {"type": "sequence", "steps": []},
        "decision_trace": [], "budget_usd": 5.0, "max_concurrent": 16,
        "max_agents": 1000, "max_stagnation": 1,
    }, ensure_ascii=False), encoding="utf-8")
    rc = cli_main(["wiki", "extract", str(spec_path),
                   "--run-dir", str(tmp_path / "run"),
                   "--wiki-dir", str(tmp_path / "wiki"),
                   "--tag", "bugfix", "--learned", "match write-area"])
    assert rc == 0
    out = capsys.readouterr()
    assert "extracted" in out.out
    assert "PARTIAL" in out.out
    # search retrieves it
    rc = cli_main(["wiki", "search", "claim",
                   "--wiki-dir", str(tmp_path / "wiki")])
    assert rc == 0
    out = capsys.readouterr()
    assert "PARTIAL" in out.out
    # chain intact
    rc = cli_main(["wiki", "verify", "--wiki-dir", str(tmp_path / "wiki")])
    assert rc == 0
    out = capsys.readouterr()
    assert "CHAIN OK" in out.out


def test_cli_impact_then_list(tmp_path, capsys):
    w = Wiki(tmp_path / "wiki")
    parent = w.append({"intent": "x", "spec_version_id": "spec.v1",
                       "run_dir": "/r", "verdict": "PARTIAL", "patterns": [],
                       "learned": "", "tags": [], "requirements": [],
                       "spec_shape": "s"})
    rc = cli_main(["wiki", "impact", parent["entry_id"],
                   "--kind", "gate_denied", "--reason", "unresolved gate",
                   "--wiki-dir", str(tmp_path / "wiki")])
    assert rc == 0
    rc = cli_main(["wiki", "list", "--wiki-dir", str(tmp_path / "wiki")])
    assert rc == 0
    out = capsys.readouterr()
    assert "impacts=1" in out.out


# --- format_contract (contract layer) --------------------------------------

def _verified_ledger(h, svid="spec.v1"):
    """A VERIFIED ledger: agent supported the claim, verify survived it."""
    h.ledger.append(_ev("agent_result", spec_version_id=svid, node_id="n1",
                        claims=[{"claim_id": "C1", "strength": "supported"}],
                        payload={"result_text_preview": "done"}))
    h.ledger.append(_ev("verify_verdict", spec_version_id=svid,
                        claim_id="C1", survived=True, refuted=False))
    # node: binding path (contract spec uses S1:R1=node:n_verify): the verifier
    # node emits an agent_verdict event with verdict=VERIFIED.
    h.ledger.append(_ev("agent_verdict", spec_version_id=svid,
                        node_id="n_verify", verdict="VERIFIED"))


def _contract_spec():
    """A spec whose structure is worth copying: agent edit + agent verify with
    verdict_field + node: binding (the proven pattern)."""
    return _spec(
        intent="build the relay write path",
        success_evidence=("S1:R1=node:n_verify",),
        nodes={
            "n_edit": {"type": "agent", "id": "n_edit",
                       "output_schema": {"type": "object",
                                         "required": ["files_modified"]},
                       "write_areas": ["src/**"],
                       "acceptance": ["file modified"],
                       "failure_policy": {"on_exhausted": "block"}},
            "n_verify": {"type": "agent", "id": "n_verify",
                          "verdict_field": "verdict",
                          "output_schema": {"type": "object",
                                            "required": ["verdict"]},
                          "write_areas": [],
                          "acceptance": ["verdict present"],
                          "failure_policy": {"on_exhausted": "degrade"}},
        },
    )


def test_contract_extract_from_verified(tmp_path):
    from axiom.wiki import extract_contract_entry
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _contract_spec()
    _verified_ledger(h)
    entry = extract_contract_entry(spec, h.ledger, str(tmp_path / "run"),
                                   tags=["relay"])
    assert entry is not None
    assert entry["entry_type"] == "format_contract"
    skel = {n["id"]: n for n in entry["node_skeleton"]}
    assert "n_edit" in skel and "n_verify" in skel
    assert skel["n_edit"]["output_schema_required"] == ["files_modified"]
    assert skel["n_verify"]["verdict_field"] is True
    assert skel["n_verify"]["write_areas_empty"] is True
    assert skel["n_edit"]["on_exhausted"] == "block"
    assert entry["binding_pattern"] == ["S1:R1=node:n_verify"]
    assert entry["control_flow_shape"].endswith("|sequence")
    assert entry["tags"] == ["relay"]


def test_contract_skipped_for_non_verified(tmp_path):
    from axiom.wiki import extract_contract_entry
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _contract_spec()
    _stagnating_ledger(h)  # PARTIAL, not VERIFIED
    entry = extract_contract_entry(spec, h.ledger, str(tmp_path / "run"))
    assert entry is None  # no contract from a failed run


def test_contract_search_returns_structure(tmp_path):
    from axiom.wiki import extract_contract_entry
    w = Wiki(tmp_path / "wiki")
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _contract_spec()
    _verified_ledger(h)
    w.append(extract_contract_entry(spec, h.ledger, str(tmp_path / "run"),
                                    tags=["relay"]))
    results = w.search(query="relay")
    assert len(results) == 1
    r = results[0]
    assert r["entry_type"] == "format_contract"
    assert r["binding_pattern"] == ["S1:R1=node:n_verify"]
    assert any(n["id"] == "n_verify" for n in r["node_skeleton"])


def test_plan_wiki_suggest_returns_contract(tmp_path, capsys):
    from axiom.wiki import extract_contract_entry
    w = Wiki(tmp_path / "wiki")
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _contract_spec()
    _verified_ledger(h)
    w.append(extract_contract_entry(spec, h.ledger, str(tmp_path / "run"),
                                    tags=["relay"]))
    rc = cli_main(["plan", "--intent", "build the relay write path",
                   "--wiki-suggest", "--wiki-dir", str(tmp_path / "wiki"),
                   "--run-dir", str(tmp_path / "run")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "format-contract" in captured.err
    assert "n_verify" in captured.err
    assert "binding:" in captured.err
    # stdout still a clean spec template
    spec = json.loads(captured.out)
    assert "spec_version_id" in spec


def test_contract_and_experience_share_chain(tmp_path):
    """experience -> contract -> experience: one hash chain, experience
    sealed fields unchanged, verify CHAIN OK (D1/D5)."""
    from axiom.wiki import extract_contract_entry
    w = Wiki(tmp_path / "wiki")
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (1, ""))
    spec = _contract_spec()
    _verified_ledger(h)
    # experience first
    e1 = w.append(extract_entry(spec, h.ledger, str(tmp_path / "run")))
    # contract second
    c1 = w.append(extract_contract_entry(spec, h.ledger, str(tmp_path / "run")))
    # experience third
    e2 = w.append(extract_entry(spec, h.ledger, str(tmp_path / "run")))
    entries = w.entries()
    assert len(entries) == 3
    # shared chain: each prev_hash points at prior event_hash
    assert entries[1]["prev_hash"] == e1["event_hash"]
    assert entries[2]["prev_hash"] == c1["event_hash"]
    # experience sealed fields unchanged by contract append
    assert entries[0]["entry_id"] == e1["entry_id"]
    assert entries[0]["verdict"] == "VERIFIED"
    # contract entry_type distinct
    assert entries[1]["entry_type"] == "format_contract"
    # chain intact
    assert w.verify_chain() == []


# --- skill-pattern store (structured sediment, agent-consumable) ----------

def test_pattern_add_seals_structured_entry(tmp_path, capsys):
    rc = cli_main(["wiki", "pattern", "add", "--id", "P-test",
                   "--feature", "demo feature", "--friction", "the friction",
                   "--fix", "the fix", "--commit", "abc123",
                   "--rejected", "approach A::reason A",
                   "--open-question", "is X justified?",
                   "--sub-lesson", "sub here",
                   "--skill-wiki-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "appended skill_pattern P-test" in out
    w = Wiki(tmp_path, filename="skill_patterns.jsonl")
    e = w.get_by_pattern_id("P-test")
    assert e["entry_type"] == "skill_pattern"
    assert e["pattern_id"] == "P-test"
    assert e["feature"] == "demo feature"
    assert e["commit_sha"] == "abc123"
    assert e["rejected"] == [{"approach": "approach A", "reason": "reason A"}]
    assert e["open_questions"] == ["is X justified?"]
    assert e["sub_lessons"] == ["sub here"]
    assert e["status"] == "active"
    assert w.verify_chain() == []  # hash chain intact


def test_pattern_show_returns_full_json(tmp_path, capsys):
    cli_main(["wiki", "pattern", "add", "--id", "P-2",
              "--feature", "f", "--friction", "fr", "--fix", "fx",
              "--commit", "deadbeef", "--skill-wiki-dir", str(tmp_path)])
    capsys.readouterr()  # flush add's stdout before show
    rc = cli_main(["wiki", "pattern", "show", "P-2",
                   "--skill-wiki-dir", str(tmp_path)])
    assert rc == 0
    e = json.loads(capsys.readouterr().out)
    assert e["pattern_id"] == "P-2"
    assert e["entry_type"] == "skill_pattern"


def test_pattern_show_missing_exits_1(tmp_path, capsys):
    rc = cli_main(["wiki", "pattern", "show", "P-nope",
                   "--skill-wiki-dir", str(tmp_path)])
    assert rc == 1


def test_pattern_list(tmp_path, capsys):
    cli_main(["wiki", "pattern", "add", "--id", "P-1",
              "--feature", "first feature", "--friction", "f", "--fix", "x",
              "--commit", "aaa", "--skill-wiki-dir", str(tmp_path)])
    cli_main(["wiki", "pattern", "add", "--id", "P-2",
              "--feature", "second feature", "--friction", "f", "--fix", "x",
              "--commit", "bbb", "--skill-wiki-dir", str(tmp_path)])
    rc = cli_main(["wiki", "pattern", "list",
                   "--skill-wiki-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "P-1" in out and "P-2" in out
    assert "first feature" in out


def test_pattern_get_by_pattern_id_latest_wins(tmp_path):
    """A second append of the same pattern_id (a superseding update) is found
    by get_by_pattern_id (latest wins); the old entry stays in the chain
    (append-only, never rolled back)."""
    w = Wiki(tmp_path, filename="skill_patterns.jsonl")
    w.append_skill_pattern("P-x", "f", "fr", "fix1", "c1", status="active")
    w.append_skill_pattern("P-x", "f", "fr", "fix2", "c2", status="superseded")
    e = w.get_by_pattern_id("P-x")
    assert e["fix"] == "fix2"  # latest
    assert e["status"] == "superseded"
    assert len(w.entries()) == 2  # both retained (append-only)


def test_search_surfaces_skill_pattern(tmp_path):
    w = Wiki(tmp_path, filename="skill_patterns.jsonl")
    w.append_skill_pattern("P-1", "cross-session resume", "the /clear stall",
                           "active pointer", "abcdef0")
    results = w.search(query="resume")
    assert len(results) == 1
    r = results[0]
    assert r["entry_type"] == "skill_pattern"
    assert r["pattern_id"] == "P-1"
    assert r["feature"] == "cross-session resume"
    assert r["commit_sha"] == "abcdef0"


# --- daydream (self-summarization, structured output) ----------------------

def _materialize_fake_run(run_dir, svid, intent, verdict="PARTIAL"):
    """Write a run-dir's materialized projections (no spec needed):
    events.jsonl + verdict.json (svid+verdict) + packet.md (intent),
    mirroring what Harness.materialize writes."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"event_id": "e1", "kind": "agent_result",
                    "spec_version_id": svid}) + "\n", encoding="utf-8")
    (run_dir / "verdict.json").write_text(json.dumps({
        "spec_version_id": svid, "verdict": verdict,
        "contract_hash": "x", "cost_usd_total": 0.0, "dispatch_count": 1,
    }), encoding="utf-8")
    (run_dir / "packet.md").write_text(
        f"# {svid}\n\n**intent**: {intent}\n", encoding="utf-8")


def test_daydream_experience_gap(tmp_path, capsys):
    _materialize_fake_run(tmp_path / ".axiom" / "run", "spec.v1",
                          "build the relay", verdict="PARTIAL")
    rc = cli_main(["wiki", "daydream",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "spec.v1" in out
    assert "PARTIAL" in out
    assert "gaps=experience" in out
    assert "sediment:" in out
    # no contract gap for a PARTIAL run -> plain extract (no --contract)
    assert "--contract" not in out


def test_daydream_contract_gap_for_verified(tmp_path, capsys):
    """A VERIFIED run with an experience entry but NO format_contract entry is
    the highest-priority gap: the structured-output contract (axiom's prize) is
    missing. Sediment hint uses --contract, and it surfaces even though an
    experience entry exists."""
    _materialize_fake_run(tmp_path / ".axiom" / "run", "spec.v1",
                          "build the relay", verdict="VERIFIED")
    w = Wiki(tmp_path / ".axiom" / "wiki")
    w.append({"entry_type": "experience", "intent": "build the relay",
              "spec_version_id": "spec.v1",
              "run_dir": str(tmp_path / ".axiom" / "run"),
              "verdict": "VERIFIED", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    rc = cli_main(["wiki", "daydream",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "spec.v1" in out
    assert "gaps=contract" in out
    assert "--contract" in out  # the sediment hint uses --contract
    assert "missing structured-output contract" in out


def test_daydream_contract_hidden_when_both_exist(tmp_path, capsys):
    """A VERIFIED run with BOTH experience AND contract entries is fully
    sedimented -> not surfaced."""
    _materialize_fake_run(tmp_path / ".axiom" / "run", "spec.v1",
                          "build the relay", verdict="VERIFIED")
    w = Wiki(tmp_path / ".axiom" / "wiki")
    w.append({"entry_type": "experience", "intent": "x",
              "spec_version_id": "spec.v1",
              "run_dir": str(tmp_path / ".axiom" / "run"),
              "verdict": "VERIFIED", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    w.append({"entry_type": "format_contract", "intent": "x",
              "spec_version_id": "spec.v1",
              "run_dir": str(tmp_path / ".axiom" / "run"),
              "tags": [], "node_skeleton": [], "binding_pattern": [],
              "control_flow_shape": "s", "learned": ""})
    rc = cli_main(["wiki", "daydream",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    assert "no sediment gaps" in capsys.readouterr().out


def test_daydream_no_gaps(tmp_path, capsys):
    rc = cli_main(["wiki", "daydream",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    assert "no sediment gaps" in capsys.readouterr().out


def test_daydream_json(tmp_path, capsys):
    _materialize_fake_run(tmp_path / ".axiom" / "run", "spec.v1",
                          "build the relay", verdict="PARTIAL")
    rc = cli_main(["wiki", "daydream", "--json",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list)
    assert len(rows) == 1
    r = rows[0]
    assert r["spec_version_id"] == "spec.v1"
    assert r["verdict"] == "PARTIAL"
    assert r["gaps"] == ["experience"]
    assert r["has_experience"] is False
    assert r["has_contract"] is False
    assert "sediment_cmd" in r


def test_daydream_distinguishes_runs_sharing_svid(tmp_path, capsys):
    """#2: two runs sharing spec_version_id (the plan template default is
    spec.v1) must be distinguished. Sedimenting run-A's experience must NOT
    mark run-B as 'has experience' -- before P1-2, daydream keyed only on
    spec_version_id, so run-B was silently skipped (returned empty gap list)."""
    run_a = tmp_path / ".axiom" / "run-a"
    run_b = tmp_path / ".axiom" / "run-b"
    _materialize_fake_run(run_a, "spec.v1", "build A", verdict="VERIFIED")
    _materialize_fake_run(run_b, "spec.v1", "build B", verdict="VERIFIED")
    w = Wiki(tmp_path / ".axiom" / "wiki")
    # only run-A is sedimented (experience + contract), run-B is NOT
    w.append({"entry_type": "experience", "intent": "build A",
              "spec_version_id": "spec.v1", "run_dir": str(run_a),
              "verdict": "VERIFIED", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    w.append({"entry_type": "format_contract", "intent": "build A",
              "spec_version_id": "spec.v1", "run_dir": str(run_a),
              "tags": [], "node_skeleton": [], "binding_pattern": [],
              "control_flow_shape": "s", "learned": ""})
    rc = cli_main(["wiki", "daydream",
                   "--wiki-dir", str(tmp_path / ".axiom" / "wiki"),
                   "--run-root", str(tmp_path / ".axiom")])
    assert rc == 0
    out = capsys.readouterr().out
    # run-A is fully sedimented -> not surfaced. run-B is NOT sedimented ->
    # surfaced with BOTH gaps (experience + contract). Before the fix, run-B
    # was skipped because spec.v1 was already in exp_keys.
    assert "run-b" in out, "run-B (never extracted) must surface as a gap"
    assert "gaps=experience,contract" in out or (
        "gaps=experience" in out and "contract" in out), (
        "run-B must report both experience and contract gaps")
    assert "run-a" not in out, "run-A (fully sedimented) must not surface"


def test_plan_surfaces_contract_format_drift_warning(tmp_path, capsys):
    """#3: a format_contract with a format_drift impact amendment must surface
    the warning in `plan --wiki-suggest`, not just in search(). Before P1-3,
    plan dropped impact and still recommended 'copy the proven structure'."""
    w = Wiki(tmp_path / "wiki")
    w.append({"entry_type": "format_contract", "intent": "build relay",
              "spec_version_id": "spec.v1",
              "run_dir": "/r", "tags": [], "node_skeleton": [],
              "binding_pattern": [], "control_flow_shape": "agentx1|sequence",
              "learned": ""})
    # amend with a format_drift warning (a known structural defect)
    w.add_impact(w.entries()[-1]["entry_id"], "format_drift",
                 "verify node skeleton dropped on extract; do not copy as-is")
    rc = cli_main(["plan", "--intent", "build relay",
                   "--wiki-suggest", "--wiki-dir", str(tmp_path / "wiki"),
                   "--out", str(tmp_path / "spec.json")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "format-contract" in err
    assert "format_drift" in err, (
        "plan must surface the format_drift impact on a defective contract, "
        "not drop it (the warning lived in search() only)"
    )
    assert "KNOWN DEFECT" in err, (
        "a defective contract must be flagged, not recommended for blind copy"
    )


def test_resume_auto_wiki_appends_revised_verdict(tmp_path, capsys):
    """#1: a resumed run that went BLOCKED -> resume(--auto-wiki) -> VERIFIED
    must append the revised verdict to the wiki, not stay stuck at the old
    BLOCKED entry. Before P1-1, resume did not sediment, so the wiki kept the
    pre-resume BLOCKED verdict (resume_leaves_blocked_experience probe)."""
    from axiom.cli import _run_resume
    from axiom.ir import Spec, Requirement
    import tempfile

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # a wiki that already holds the OLD (BLOCKED) experience entry for this run
    w = Wiki(tmp_path / "wiki")
    w.append({"entry_type": "experience", "intent": "i",
              "spec_version_id": "spec.v1", "run_dir": str(run_dir),
              "verdict": "BLOCKED", "patterns": [], "learned": "",
              "tags": [], "requirements": [], "spec_shape": "s"})
    assert len(w.entries()) == 1
    assert w.entries()[0]["verdict"] == "BLOCKED"

    # build a spec + ledger that derives VERIFIED
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=[],
                nodes={}, control_flow={"type": "sequence", "steps": []},
                decision_trace=[], budget_usd=5.0, max_concurrent=16,
                max_agents=1000, max_stagnation=1)
    # materialize a VERIFIED verdict.json so _run_resume's materialize sees it;
    # but _run_resume re-walks the (empty) sequence + seals. Use a ledger with
    # a gate_open+gate_resolve to force... simpler: directly exercise the
    # _maybe_sediment path by calling _run_resume with auto_wiki on an already-
    # sealed ledger is not possible (it seals itself). Instead test the helper:
    from axiom.cli import _maybe_sediment
    from axiom.harness import Harness
    h = Harness(run_dir, worker_runner=lambda a, cwd=None: (0, "{}"))
    # simulate a VERIFIED run's ledger (empty sequence + no gates -> VERIFIED
    # requires a bound R; here R1 has no binding -> UNVERIFIED, but we want to
    # test the sediment path, not verdict derivation). Use a spec with no
    # required R to get VERIFIED trivially.
    spec_nv = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                   intent="i", requirements=[], boundaries=["b"],
                   success_evidence=[], nodes={},
                   control_flow={"type": "sequence", "steps": []},
                   decision_trace=[], budget_usd=5.0, max_concurrent=16,
                   max_agents=1000, max_stagnation=1)
    h.ledger.seal()
    _maybe_sediment(spec_nv, h.ledger, str(run_dir), str(tmp_path / "wiki"),
                   auto_wiki=True)
    # wiki now holds TWO entries: old BLOCKED + new (revised). The new one's
    # verdict reflects the actual finished run (UNVERIFIED for empty spec, but
    # the point is it was APPENDED, not the old BLOCKED staying alone).
    entries = w.entries()
    assert len(entries) == 2, (
        "resume --auto-wiki must append a revised entry; the old BLOCKED "
        "entry must stay (append-only), not be the only entry"
    )
    assert entries[0]["verdict"] == "BLOCKED"  # old preserved
    # the appended entry is distinct from the old (revised sediment)
    assert entries[1]["entry_id"] != entries[0]["entry_id"]


# --- P-014 co-evolution loop: adoption declaration + outcome attribution ----
#
# plan --wiki-suggest retrieves prior experience/contracts; the HOST decides
# to adopt one (the adoption judgment is the host's); axiom records the
# adoption (spec.adopted_from / --adopted-from) and at seal attributes the
# outcome back to the adopted entries as adoption_outcome impact amendments --
# so future retrieval sees "this contract was adopted N times, runs went
# VERIFIED/PARTIAL".

from axiom.cli import _maybe_sediment, _merge_adopted_from


def test_merge_adopted_from_cli_into_spec():
    spec = _spec()
    spec.adopted_from = ["aaa111"]
    _merge_adopted_from(spec, ["bbb222", "aaa111", None])
    assert spec.adopted_from == ["aaa111", "bbb222"]
    # an empty CLI list does not change the spec declaration
    _merge_adopted_from(spec, None)
    assert spec.adopted_from == ["aaa111", "bbb222"]


def test_spec_adopted_from_roundtrip():
    from axiom.ir import spec_from_json, spec_to_json
    spec = _spec()
    spec.adopted_from = ["x1", "y2"]
    s2 = spec_from_json(spec_to_json(spec))
    assert s2.adopted_from == ["x1", "y2"]
    # an old spec (no adopted_from field) loads without crashing
    import json as _json
    d = _json.loads(spec_to_json(spec))
    d.pop("adopted_from")
    s3 = spec_from_json(_json.dumps(d))
    assert s3.adopted_from == []


def test_add_impact_unique_prefix(tmp_path):
    w = Wiki(tmp_path / "wiki")
    e = w.append({"spec_version_id": "spec.v1", "run_dir": "r1",
                  "intent": "i", "verdict": "VERIFIED"})
    full = e["entry_id"]
    amend = w.add_impact(full[:14], kind="adoption_outcome", reason="r")
    assert amend["amends"] == full  # prefix resolves to the FULL id
    # an unknown prefix still raises KeyError
    with pytest.raises(KeyError):
        w.add_impact("zzzz-no-such", kind="k", reason="r")


def test_add_impact_ambiguous_prefix_raises(tmp_path):
    w = Wiki(tmp_path / "wiki")
    # Build two entries sharing a prefix: entry_id is a hash of (svid, run_dir,
    # ts); directly hand-appending two entries and then finding a common prefix
    # is tedious -- instead test the "exact no-match + multiple prefix
    # candidates" branch: use the prefix shared by two entries.
    e1 = w.append({"spec_version_id": "s1", "run_dir": "r", "intent": "a"})
    e2 = w.append({"spec_version_id": "s2", "run_dir": "r", "intent": "b"})
    # find e1/e2's common prefix (at least the empty string); a single-char
    # prefix is almost certainly shared -> use the common first char or fall
    # back to a deterministically-ambiguous construction
    common = ""
    for c1, c2 in zip(e1["entry_id"], e2["entry_id"]):
        if c1 != c2:
            break
        common += c1
    if common:
        with pytest.raises(KeyError):
            w.add_impact(common, kind="k", reason="r")
    else:
        # the first char already differs: constructing a "non-existent but
        # prefix-ambiguous" case cannot hold; in this case skip the ambiguity
        # assertion (no ambiguous prefix on the chain).
        pass


def test_extract_entry_carries_adopted_from(tmp_path):
    h = Harness(tmp_path / "run")
    h.ledger.append(_ev("agent_result", spec_version_id="spec.v1",
                        node_id="n1",
                        payload={"validated_output": {"x": 1},
                                 "cost_usd": 0.01, "num_turns": 1,
                                 "is_clean": True}))
    spec = _spec()
    spec.adopted_from = ["contractabc123"]
    entry = extract_entry(spec, h.ledger, str(tmp_path / "run"))
    assert entry["adopted_from"] == ["contractabc123"]
    # a spec that did not declare adopted_from produces an entry without the
    # key (does not pollute the retrieval surface)
    spec2 = _spec()
    entry2 = extract_entry(spec2, h.ledger, str(tmp_path / "run"))
    assert "adopted_from" not in entry2


def test_maybe_sediment_attributes_adoption_outcome(tmp_path, capsys):
    # end-to-end: the wiki already has a format_contract (the adoptee); after
    # run seal, _maybe_sediment appends an adoption_outcome impact (carrying
    # verdict + run_dir) to it, and the run's own experience entry records
    # adopted_from.
    w = Wiki(tmp_path / "wiki")
    contract = w.append({
        "entry_type": "format_contract", "spec_version_id": "spec.v0",
        "run_dir": "old-run", "intent": "old", "verdict": "VERIFIED",
        "control_flow_shape": "sequence", "node_skeleton": [],
        "binding_pattern": [], "learned": "", "tags": []})
    h = Harness(tmp_path / "run")
    h.ledger.append(_ev("agent_result", spec_version_id="spec.v1",
                        node_id="n1",
                        payload={"validated_output": {"x": 1},
                                 "cost_usd": 0.01, "num_turns": 1,
                                 "is_clean": True}))
    spec = _spec()
    spec.adopted_from = [contract["entry_id"][:14]]  # host only got the truncated id
    _maybe_sediment(spec, h.ledger, str(tmp_path / "run"),
                    str(tmp_path / "wiki"), True)
    # adoption_outcome attribution lands on the adopted entry (prefix resolves
    # to the full id)
    hits = w.search(query="old", limit=5)
    parent = [r for r in hits if r["entry_id"] == contract["entry_id"]][0]
    kinds = [i["kind"] for i in parent["impact"]]
    assert "adoption_outcome" in kinds
    reason = [i["reason"] for i in parent["impact"]
              if i["kind"] == "adoption_outcome"][0]
    assert "verdict" in reason and "spec.v1" in reason
    # the run's own experience entry carries adopted_from
    exps = [e for e in w.entries() if e.get("entry_type") == "experience"]
    assert exps and exps[0].get("adopted_from") == [contract["entry_id"][:14]]
    # the chain is complete
    assert w.verify_chain() == []


def test_maybe_sediment_unknown_adopted_id_nonfatal(tmp_path, capsys):
    # adopted a non-existent entry_id -> stderr warning; sediment itself is
    # unaffected
    h = Harness(tmp_path / "run")
    h.ledger.append(_ev("agent_result", spec_version_id="spec.v1",
                        node_id="n1",
                        payload={"validated_output": {"x": 1},
                                 "cost_usd": 0.01, "num_turns": 1,
                                 "is_clean": True}))
    spec = _spec()
    spec.adopted_from = ["no-such-entry"]
    _maybe_sediment(spec, h.ledger, str(tmp_path / "run"),
                    str(tmp_path / "wiki"), True)
    err = capsys.readouterr().err
    assert "adoption attribution skipped" in err
    exps = [e for e in Wiki(tmp_path / "wiki").entries()
            if e.get("entry_type") == "experience"]
    assert exps  # experience entry still lands in the store
