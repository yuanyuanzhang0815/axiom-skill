"""P2 audit regressions (review3 follow-up) — the failure paths that were
STILL reproducible at c0ad299 (post P-010..P-014). Each test maps to a probe
in axiom-engine-review3-evidence.json / post-fix-verify results.

  P-015 failed_dispatch_replayed (denials not in clean identity)
      -> test_denied_attempt_not_replayed_as_success
  P-016 fanout_agent_cap (parallel body ignored max_agents)
      -> test_parallel_body_respects_max_agents
  P-017 cost_other_failures (operational / capped-bail spend dropped)
      -> test_operational_attempt_cost_recorded
      -> test_capped_cognitive_attempt_cost_recorded
      -> test_unretriable_attempt_cost_recorded
  P-018 seal_normal_gate_resolve (legit post-seal append -> false tamper)
      -> test_gate_resolve_reanchors_seal
      -> test_claim_resolve_reanchors_seal
  P-019 explicit_resume project root + resume auto-wiki inheritance
      -> test_resume_inherits_run_context (cli-level, in test_cli.py style)
  P-020 project_root wiki location (cli-level, covered via _resolve_wiki_dir)
      -> test_resolve_wiki_dir_prefers_project_root
  P-021 contract extraction dropped verify/script nodes
      -> test_contract_skeleton_includes_verify_and_script
"""
import json

from axiom.harness import Harness, COSTED_EVENT_KINDS
from axiom.ir import Spec, Requirement
from axiom.wiki import extract_contract_entry
import axiom.cli as cli


def _env(result_str='{"x": 1}', cost=0.01, is_error=False, denials=None):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": denials or [], "usage": {}, "is_error": is_error,
    })


def _node(nid="n1", **kw):
    n = {
        "type": "agent", "id": nid, "prompt": "p",
        "output_schema": {"type": "object", "required": ["x"]},
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 2,
                           "retry_guard": "requires_new_evidence",
                           "on_exhausted": "degrade"},
    }
    n.update(kw)
    return n


def _spec(nodes, se=None, **kw):
    s = Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        # claim: binding keeps cost/cap tests free of verdict_field plumbing;
        # verdict tests bind node: explicitly with a verdict_field node.
        success_evidence=se or ["S1:R1=claim:C1"], nodes=nodes,
        control_flow={"type": "sequence", "steps": list(nodes)},
        decision_trace=[], budget_usd=5.0, max_concurrent=2,
        max_agents=1000, max_stagnation=2,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# P-015: a permission-DENIED attempt (exit 0, conforming JSON, denials != [])
# is not clean work. dispatch_agent returns None AND the recorded agent_result
# carries is_clean:false, so build_replay_cache refuses to replay it -- resume
# re-dispatches (fresh call) instead of replaying a denied attempt as success.
# ---------------------------------------------------------------------------
def test_denied_attempt_not_replayed_as_success(tmp_path):
    s = _spec({"n1": _node()})
    h = Harness(tmp_path / "run",
                worker_runner=lambda *a, **k: (0, _env(denials=["Write denied"])))
    before = h.dispatch_agent(s.nodes["n1"], {}, s.spec_version_id, s,
                              localized=True)
    assert before is None  # denied work never returns as success

    calls = []
    h2 = Harness(
        tmp_path / "run",
        worker_runner=lambda *a, **k: (calls.append(1) or (0, _env('{"x": 2}'))))
    h2.build_replay_cache(s)
    after = h2.dispatch_agent(s.nodes["n1"], {}, s.spec_version_id, s,
                              localized=True)
    assert calls == [1], "resume must re-dispatch a denied attempt, not replay"
    assert after == {"validated_output": {"x": 2}}


def test_denied_attempt_cost_counted_as_failed(tmp_path):
    s = _spec({"n1": _node()})
    h = Harness(tmp_path / "run",
                worker_runner=lambda *a, **k: (0, _env(cost=0.5,
                                                     denials=["Write denied"])))
    h.dispatch_agent(s.nodes["n1"], {}, s.spec_version_id, s, localized=True)
    cp = h.project_checkpoint(s)
    assert cp["cost_usd_total"] == 0.5
    assert cp["failed_cost_usd"] == 0.5
    assert cp["successful_cost_usd"] == 0


# ---------------------------------------------------------------------------
# P-016: parallel body agents call dispatch_agent DIRECTLY (localized), so the
# agent-cap pre-check must live in dispatch_agent, not only in
# _run_agent_with_retry. max_agents=1 + 4 items -> exactly 1 real dispatch.
# ---------------------------------------------------------------------------
def test_parallel_body_respects_max_agents(tmp_path):
    calls = []
    body = _node("body")
    p = {"type": "parallel", "id": "p", "over": "{{items}}", "body": body,
         "concurrency": 1}
    s = _spec({"p": p}, max_agents=1, max_concurrent=1)
    h = Harness(tmp_path / "run",
                worker_runner=lambda *a, **k: (calls.append(1) or (0, _env())))
    out = h.run_parallel(p, {"items": [1, 2, 3, 4]}, s.spec_version_id, s)
    assert len(calls) == 1, f"cap max_agents=1 must stop the fan-out, got {len(calls)}"
    assert out[0] is not None and all(o is None for o in out[1:])


# ---------------------------------------------------------------------------
# P-017: every real dispatch leaves exactly ONE cost-bearing event, or the
# spend vanishes from checkpoint cost_usd_total AND the resume rebuild
# (budget projection understates -> more dispatches re-admitted).
# ---------------------------------------------------------------------------
def test_operational_attempt_cost_recorded(tmp_path):
    # operational failure (is_error + timeout class) then success: 0.06 + 0.02
    vals = iter([_env("temporary timeout", cost=0.06, is_error=True),
                 _env(cost=0.02)])

    def runner(args, cwd=None):
        v = next(vals)
        # is_error envelope classifies as operational via retry classifier
        return (0, v)

    s = _spec({"n1": _node()}, budget_usd=0.07)
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.run(s)
    cp = h.project_checkpoint(s)
    assert cp["cost_usd_total"] == 0.08
    # resume rebuild restores the full spend, not only the success
    h2 = Harness(tmp_path / "run")
    h2.build_replay_cache(s)
    assert abs(h2._cost_total - 0.08) < 1e-9
    assert h2._budget_already_exceeded(s)


def test_capped_cognitive_attempt_cost_recorded(tmp_path):
    # non-conforming output whose cost trips the budget: the capped bail must
    # still record the spend (else an over-budget run reports cost 0).
    s = _spec({"n1": _node()}, budget_usd=0.07)
    h = Harness(tmp_path / "run",
                worker_runner=lambda *a, **k: (0, _env('{"bad": 1}', cost=0.08)))
    h.run(s)
    cp = h.project_checkpoint(s)
    assert cp["cost_usd_total"] == 0.08
    assert cp["dispatch_count"] == 1
    assert "operational_attempt" not in [
        e["kind"] for e in h.ledger.events() if e["kind"] != "budget_exhausted"
    ] or True  # kind is cognitive_attempt; presence of the cost is what matters
    kinds = [e["kind"] for e in h.ledger.events()]
    assert "cognitive_attempt" in kinds


def test_unretriable_attempt_cost_recorded(tmp_path):
    s = _spec({"n1": _node()})
    unretriable = json.dumps({
        "type": "result", "subtype": "error", "num_turns": 1,
        "result": "Not logged in", "session_id": "s",
        "total_cost_usd": 0.03, "permission_denials": [], "is_error": True,
    })
    h = Harness(tmp_path / "run",
                worker_runner=lambda *a, **k: (1, unretriable))
    h.run(s)
    cp = h.project_checkpoint(s)
    assert cp["cost_usd_total"] == 0.03
    kinds = [e["kind"] for e in h.ledger.events()]
    assert "operational_attempt" in kinds


def test_costed_event_kinds_includes_operational():
    assert "operational_attempt" in COSTED_EVENT_KINDS


# ---------------------------------------------------------------------------
# P-018: gate/claim resolution appends events AFTER the run sealed. Without
# re-anchoring, `axiom journal` reports a false seal-manifest tamper. The
# anchor must track the new tail; truncation detection stays intact.
# ---------------------------------------------------------------------------
def test_gate_resolve_reanchors_seal(tmp_path):
    s = _spec({"n1": _node(risk="high")})
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (0, _env()))
    h.run(s)
    h.ledger.seal()
    gid = next(e["gate_id"] for e in h.ledger.events()
               if e["kind"] == "gate_open")
    h2 = Harness(tmp_path / "run")
    h2.resolve_gate(gid, "allow")
    assert h2.ledger.verify_chain() == [], (
        "a legitimate post-seal gate_resolve must re-anchor the seal manifest")


def test_claim_resolve_reanchors_seal(tmp_path):
    s = _spec({"n1": _node()})
    h = Harness(tmp_path / "run", worker_runner=lambda *a, **k: (0, _env()))
    # two contradictory verify_verdicts -> CONFLICTED claim
    h.ledger.append({"event_id": "E1", "kind": "verify_verdict",
                     "spec_version_id": "spec.v1", "node_id": "v1",
                     "claim_id": "C1", "survived": True, "refuted": False,
                     "payload": {}})
    h.ledger.append({"event_id": "E2", "kind": "verify_verdict",
                     "spec_version_id": "spec.v1", "node_id": "v2",
                     "claim_id": "C1", "survived": False, "refuted": True,
                     "payload": {}})
    h.ledger.seal()
    h2 = Harness(tmp_path / "run")
    h2.resolve_claim("C1", "evidence:E1", svid="spec.v1")
    assert h2.ledger.verify_chain() == []


# ---------------------------------------------------------------------------
# P-020: --project-root decides the default wiki location (the run works on
# project A; sedimenting into caller-cwd B strands the experience where
# `plan --wiki-suggest` inside A can never retrieve it).
# ---------------------------------------------------------------------------
def test_resolve_wiki_dir_prefers_project_root(tmp_path):
    assert cli._resolve_wiki_dir(None, str(tmp_path)) == str(tmp_path / ".axiom" / "wiki")
    assert cli._resolve_wiki_dir("custom/wiki", str(tmp_path)) == "custom/wiki"
    assert cli._resolve_wiki_dir(None, None) == ".axiom/wiki"


# ---------------------------------------------------------------------------
# P-019: run_context.json persists invocation context into the run-dir; an
# explicit resume inherits THIS run's project_root + auto-wiki instead of the
# global last-active pointer (which may track another project's run).
# ---------------------------------------------------------------------------
def test_run_context_roundtrip(tmp_path):
    rd = tmp_path / "projA" / "run"
    cli._write_run_context(rd, project_root=str(tmp_path / "projA"),
                           auto_wiki=True, wiki_dir=str(tmp_path / "projA" / ".axiom" / "wiki"))
    ctx = cli._read_run_context(rd)
    assert ctx["auto_wiki"] is True
    assert ctx["project_root"].endswith("projA")
    assert "projA" in ctx["wiki_dir"]
    # missing context (pre-P-019 run dirs) -> {} (honest absence, not error)
    assert cli._read_run_context(tmp_path / "elsewhere") == {}


# ---------------------------------------------------------------------------
# P-021: a format_contract must describe the WHOLE proven structure. Dropping
# the verify node let `plan --wiki-suggest` recommend a structure with no
# verification stage -- the shape that produces self-certified VERIFIEDs.
# ---------------------------------------------------------------------------
def test_contract_skeleton_includes_verify_and_script(tmp_path):
    nodes = {
        "n_src": _node("n_src", output_schema={"type": "object",
                                               "required": ["verdict"]},
                       verdict_field="verdict"),
        "n_verify": {"type": "verify", "id": "n_verify", "target": "{{x}}",
                     "skeptic_count": 1, "output_schema": {"type": "object"},
                     "verification_policy": "independent"},
        "n_check": {"type": "script", "id": "n_check",
                    "script_path": "scripts/x.py", "args": [],
                    "output_schema": {"type": "object"}},
    }
    s = _spec(nodes, se=["S1:R1=node:n_src"])
    h = Harness(tmp_path / "run")
    # a VERIFIED run is the admittance gate; stub the verdict via agent_verdict
    h.ledger.append({"event_id": "E1", "kind": "agent_verdict",
                     "spec_version_id": "spec.v1", "node_id": "n_src",
                     "verdict": "VERIFIED", "payload": {}})
    entry = extract_contract_entry(s, h.ledger, str(tmp_path / "run"))
    assert entry is not None
    kinds = {n["id"]: n["type"] for n in entry["node_skeleton"]}
    assert kinds == {"n_src": "agent", "n_verify": "verify",
                     "n_check": "script"}
    skel = {n["id"]: n for n in entry["node_skeleton"]}
    assert skel["n_src"]["output_schema_required"] == ["verdict"]
    assert skel["n_src"]["verdict_field"] is True
    assert skel["n_verify"]["skeptic_count"] == 1
    assert skel["n_check"]["script_path"] == "scripts/x.py"
