"""lint_spec: advisory warnings (not errors). Static-verifier-on-runtime-task
nudge — a real relay's endpoint-not-reloaded + state-not-refreshed
were both missed by a static Read/Grep verify, found by the user."""
from axiom.ir import Spec, Requirement, lint_spec


def _agent(nid, verdict_field=None, allowed_tools=None, write_areas=None):
    n = {"type": "agent", "id": nid, "prompt": "p",
         "output_schema": {"type": "object"}, "acceptance": ["a"],
         "failure_policy": {}, "write_areas": write_areas or []}
    if verdict_field:
        n["verdict_field"] = verdict_field
    if allowed_tools:
        n["allowed_tools"] = allowed_tools
    return n


def _spec(nodes, max_stagnation=2):
    return Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement(id="R1", text="t")],
                boundaries=[], success_evidence=[], nodes=nodes,
                control_flow={"steps": list(nodes.keys())},
                decision_trace=[], budget_usd=10, max_concurrent=4,
                max_agents=10, max_stagnation=max_stagnation)


def test_static_verifier_on_runtime_warns():
    nodes = {
        "n1": _agent("n1", write_areas=["backend/app/main.py"]),
        "n_verify": _agent("n_verify", verdict_field="verdict",
                           allowed_tools=["Read", "Grep"]),
    }
    w = lint_spec(_spec(nodes))
    assert len(w) == 1
    assert "static verifier" in w[0]
    assert "runtime" in w[0]


def test_runtime_verifier_no_warn():
    # same runtime task but verifier HAS Bash -> no warning
    nodes = {
        "n1": _agent("n1", write_areas=["backend/app/main.py"]),
        "n_verify": _agent("n_verify", verdict_field="verdict",
                           allowed_tools=["Read", "Bash"]),
    }
    assert lint_spec(_spec(nodes)) == []


def test_static_verifier_on_frontend_runtime_warns():
    # frontend .tsx also counts as runtime
    nodes = {
        "n2": _agent("n2", write_areas=["frontend/src/pages/AdminPage.tsx"]),
        "n_verify": _agent("n_verify", verdict_field="verdict",
                           allowed_tools=["Read"]),
    }
    w = lint_spec(_spec(nodes))
    assert len(w) == 1


def test_static_verifier_non_runtime_no_warn():
    # write_areas touch only docs -> static verify is fine
    nodes = {
        "n1": _agent("n1", write_areas=["docs/readme.md"]),
        "n_verify": _agent("n_verify", verdict_field="verdict",
                           allowed_tools=["Read"]),
    }
    assert lint_spec(_spec(nodes)) == []


def test_no_verifier_no_warn():
    # no verdict_field node -> nothing to warn about
    nodes = {"n1": _agent("n1", write_areas=["backend/app/main.py"])}
    assert lint_spec(_spec(nodes)) == []


# ---- P-003: max_stagnation<2 lint warn on agent-bearing specs ----

def test_max_stagnation_lt_2_agent_warns():
    # a) agent-bearing spec + max_stagnation:1 -> warns
    nodes = {"n1": _agent("n1")}
    w = lint_spec(_spec(nodes, max_stagnation=1))
    assert any("max_stagnation" in s for s in w), w


def test_max_stagnation_2_agent_no_warn():
    # b) agent-bearing spec + max_stagnation:2 -> no max_stagnation warn
    nodes = {"n1": _agent("n1")}
    w = lint_spec(_spec(nodes, max_stagnation=2))
    assert not any("max_stagnation" in s for s in w), w


def test_max_stagnation_lt_2_script_no_warn():
    # c) script-only spec + max_stagnation:1 -> no warn (script nodes are
    # zero-LLM, no conform-fail risk, so the lint skips them)
    nodes = {"n1": {"type": "script", "id": "n1", "script_path": "s.py",
                    "args": [], "output_schema": {"type": "object"}}}
    w = lint_spec(_spec(nodes, max_stagnation=1))
    assert not any("max_stagnation" in s for s in w), w


def test_max_stagnation_default_2_agent_no_warn():
    # d) agent-bearing spec + default max_stagnation (2) -> no warn
    nodes = {"n1": _agent("n1")}
    w = lint_spec(_spec(nodes))  # default max_stagnation=2
    assert not any("max_stagnation" in s for s in w), w


# ---- P-005: verify cost warn (fan-out target / high skeptic_count) ----
# SKILL.md v1 limitations: verify cost ~ O(findings x skeptic_count /
# max_concurrent) can blow the budget with no warning. lint closes the
# no-warning half: it flags a fan-out target (parallel/pipeline -> LIST of
# findings) and high skeptic_count (>5), the two static multipliers.

def _verify(nid, target, skeptic_count=3):
    return {"type": "verify", "id": nid, "target": target,
            "skeptic_count": skeptic_count}


def test_verify_fanout_parallel_warns():
    # verify target -> parallel node (fan-out produces a findings LIST) -> warn
    nodes = {
        "n_par": {"type": "parallel", "id": "n_par", "over": "{{items}}", "body": {}},
        "n_v": _verify("n_v", "{{n_par.validated_output}}", skeptic_count=3),
    }
    w = lint_spec(_spec(nodes))
    assert any("fan-out" in s and "parallel" in s for s in w), w


def test_verify_fanout_pipeline_warns():
    nodes = {
        "n_pipe": {"type": "pipeline", "id": "n_pipe", "items": "{{items}}", "stages": []},
        "n_v": _verify("n_v", "{{n_pipe.validated_output}}", skeptic_count=3),
    }
    w = lint_spec(_spec(nodes))
    assert any("fan-out" in s and "pipeline" in s for s in w), w


def test_verify_high_skeptic_warns():
    # high skeptic_count even on a non-fanout (agent) target -> warn
    nodes = {
        "n_src": _agent("n_src"),
        "n_v": _verify("n_v", "{{n_src.validated_output}}", skeptic_count=6),
    }
    w = lint_spec(_spec(nodes))
    assert any("skeptic_count=6" in s for s in w), w


def test_verify_default_no_warn():
    # default skeptic_count=3 + agent (non-fanout) target -> no verify-cost warn
    nodes = {
        "n_src": _agent("n_src"),
        "n_v": _verify("n_v", "{{n_src.validated_output}}", skeptic_count=3),
    }
    w = lint_spec(_spec(nodes))
    assert not any("fan-out" in s or "skeptic_count=" in s for s in w), w


def test_verify_fanout_low_skeptic_still_warns():
    # fan-out is a multiplier; warn even at default skeptic_count=3
    nodes = {
        "n_par": {"type": "parallel", "id": "n_par", "over": "{{items}}", "body": {}},
        "n_v": _verify("n_v", "{{n_par.validated_output}}", skeptic_count=3),
    }
    w = lint_spec(_spec(nodes))
    assert any("fan-out" in s for s in w), w


def test_verify_fanout_high_skeptic_single_warn():
    # fan-out branch hits; high-skeptic elif must NOT also fire (one warn, not
    # two) -- the fan-out warn already names skeptic_count
    nodes = {
        "n_par": {"type": "parallel", "id": "n_par", "over": "{{items}}", "body": {}},
        "n_v": _verify("n_v", "{{n_par.validated_output}}", skeptic_count=6),
    }
    w = lint_spec(_spec(nodes))
    v_warns = [s for s in w if "fan-out" in s or "skeptic_count=" in s]
    assert len(v_warns) == 1, v_warns
    assert "fan-out" in v_warns[0]
