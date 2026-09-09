"""F1 conform_strict_json decision-layer hard-enforcement tests.

Semantics (boundary vs P-006):
- Default: a verdict node (verdict_field) is auto-strict -- the verdict must
  come from pure-JSON text; a {...} dug out of prose is not a verdict; an impl
  node is tolerant (lenient parse + P-006 file-evidence recovery).
- An explicit conform_strict_json:true/false overrides the default both ways.
- strict parse: the entire reply is one JSON document (leading/trailing
  whitespace tolerated; a single lone fence block is tolerated by stripping);
  first-object prose extraction is forbidden.
- strict nodes skip P-006 prose recovery (JSON or bust).
"""
import json
from axiom.harness import Harness


class _SpecStub:
    spec_version_id = "spec.v1"
    max_stagnation = 2
    budget_usd = 50.0
    max_agents = 1000


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"},
                   "issues": {"type": "array"}},
    "required": ["verdict"],
}

_IMPL_SCHEMA = {
    "type": "object",
    "properties": {"files_modified": {"type": "array"},
                   "summary": {"type": "string"}},
    "required": ["files_modified", "summary"],
}


def _verdict_node(strict_flag="__absent__", max_retries=2):
    n = {
        "type": "agent", "id": "n_verify", "prompt": "p", "dispatch": "host",
        "output_schema": _VERDICT_SCHEMA,
        "allowed_tools": ["Read"],
        "write_areas": [],
        "verdict_field": "verdict",
        "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries, "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        },
    }
    if strict_flag != "__absent__":
        n["conform_strict_json"] = strict_flag
    return n


def _impl_node(write_areas, strict_flag="__absent__", max_retries=2):
    n = {
        "type": "agent", "id": "n_impl", "prompt": "p", "dispatch": "host",
        "output_schema": _IMPL_SCHEMA,
        "allowed_tools": ["Read", "Edit", "Write"],
        "write_areas": write_areas,
        "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries, "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        },
    }
    if strict_flag != "__absent__":
        n["conform_strict_json"] = strict_flag
    return n


def _runner_returning(result_text, side_effect=None):
    def runner(args, cwd=None):
        if side_effect:
            side_effect()
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": result_text,
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    return runner


# --- _parse_validated_strict unit layer ---------------------------------

def test_strict_parse_pure_json():
    assert Harness._parse_validated_strict('{"verdict": "VERIFIED"}') == \
        {"verdict": "VERIFIED"}


def test_strict_parse_whitespace_tolerated():
    assert Harness._parse_validated_strict('  \n {"verdict": "V"} \n') == \
        {"verdict": "V"}


def test_strict_parse_lone_fence_tolerated():
    # The whole reply is one fence block = the worker meant JSON; stripping is unambiguous
    text = '```json\n{"verdict": "VERIFIED"}\n```'
    assert Harness._parse_validated_strict(text) == {"verdict": "VERIFIED"}


def test_strict_parse_prose_embedded_rejected():
    # JSON buried in prose = ambiguous (could be an example/quotation); strict refuses to extract
    text = 'Verification done. Result: {"verdict": "VERIFIED"} as above.'
    assert Harness._parse_validated_strict(text) == {}


def test_strict_parse_pure_prose_rejected():
    assert Harness._parse_validated_strict("Verification passed, no problem.") == {}


# --- _strict_json decision parse ----------------------------------------

def test_strict_json_default_verdict_node():
    assert Harness._strict_json({"verdict_field": "verdict"}) is True


def test_strict_json_default_impl_node():
    assert Harness._strict_json({"write_areas": ["f.py"]}) is False


def test_strict_json_explicit_overrides():
    assert Harness._strict_json(
        {"verdict_field": "v", "conform_strict_json": False}) is False
    assert Harness._strict_json(
        {"write_areas": ["f.py"], "conform_strict_json": True}) is True


def test_strict_json_non_dict():
    assert Harness._strict_json(None) is False


# --- dispatch behavior layer ---------------------------------------------

def test_verdict_node_embedded_json_fails_then_pure_json_succeeds(tmp_path):
    """Verdict node default strict: prose-embedded JSON rejected -> G3 strict
    feedback -> next round worker gives pure JSON -> passes. Feedback text
    must be in strict form."""
    calls = []
    prompts = []

    def runner(args, cwd=None):
        calls.append(1)
        # args is the host dispatch arg list; the prompt is among them; find the one with the task text
        prompts.append(" ".join(str(a) for a in args))
        if len(calls) == 1:
            text = 'Verification done. Conclusion is {"verdict": "VERIFIED", "issues": []} .'
        else:
            text = '{"verdict": "VERIFIED", "issues": []}'
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": text, "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    out = h._dispatch_with_retry(_verdict_node(), {}, "spec.v1", _SpecStub())
    assert out is not None, "second-round pure JSON should pass"
    assert out["validated_output"]["verdict"] == "VERIFIED"
    assert len(calls) == 2, "strict rejecting prose-embedded JSON should retry one round"
    assert "pure JSON" in prompts[1], "retry prompt must carry strict-form feedback"


def test_verdict_node_embedded_json_exhausts_without_recovery(tmp_path):
    """Verdict node default strict: worker always gives prose-embedded JSON ->
    retries exhausted -> None, and never falls back to P-006 recovery (a
    verdict is never fabricated)."""
    h = Harness(
        tmp_path / "run",
        worker_runner=_runner_returning('Result {"verdict": "VERIFIED"} trust me'),
        project_root=str(tmp_path))
    out = h._dispatch_with_retry(_verdict_node(), {}, "spec.v1", _SpecStub())
    assert out is None


def test_impl_node_embedded_json_still_extracted(tmp_path):
    """Impl node default tolerant: a compliant JSON embedded in prose is still
    extracted and accepted (behavior unchanged)."""
    text = ('Done. {"files_modified": ["f.py"], "summary": "done"} finished.')
    h = Harness(tmp_path / "run", worker_runner=_runner_returning(text),
                project_root=str(tmp_path))
    out = h._dispatch_with_retry(_impl_node(["f.py"]), {}, "spec.v1", _SpecStub())
    assert out is not None
    assert out["validated_output"]["files_modified"] == ["f.py"]


def test_impl_node_explicit_strict_disables_extraction(tmp_path):
    """Impl node explicit strict: prose-embedded JSON is no longer extracted -> conform-fail."""
    text = ('Done. {"files_modified": ["f.py"], "summary": "done"} finished.')
    h = Harness(tmp_path / "run", worker_runner=_runner_returning(text),
                project_root=str(tmp_path))
    out = h._dispatch_with_retry(
        _impl_node(["f.py"], strict_flag=True), {}, "spec.v1", _SpecStub())
    assert out is None, "an explicit-strict impl should not extract JSON from prose"


def test_impl_node_explicit_strict_skips_p006_recovery(tmp_path):
    """Impl node explicit strict: even if the file was really changed, it does
    not take the P-006 pass (JSON or bust)."""
    (tmp_path / "f.py").write_text("old")

    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("new-content-by-worker")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": "Task done, structured output submitted.",  # pure prose, zero JSON
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    out = h._dispatch_with_retry(
        _impl_node(["f.py"], strict_flag=True), {}, "spec.v1", _SpecStub())
    assert out is None, "a strict impl should not take the P-006 pass even if files changed"


def test_impl_node_default_p006_recovery_intact(tmp_path):
    """Control group: impl node default tolerant, P-006 recovery unaffected by F1."""
    (tmp_path / "f.py").write_text("old")

    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("new-content-by-worker")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": "Task done, structured output submitted.",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    out = h._dispatch_with_retry(_impl_node(["f.py"]), {}, "spec.v1", _SpecStub())
    assert out is not None
    assert out["validated_output"].get("_recovered") is True


def test_verdict_node_explicit_false_restores_tolerant(tmp_path):
    """Verdict node explicit conform_strict_json:false: back to lenient extraction (opt-out)."""
    text = 'Conclusion {"verdict": "VERIFIED", "issues": []} as above.'
    h = Harness(tmp_path / "run", worker_runner=_runner_returning(text),
                project_root=str(tmp_path))
    out = h._dispatch_with_retry(
        _verdict_node(strict_flag=False), {}, "spec.v1", _SpecStub())
    assert out is not None
    assert out["validated_output"]["verdict"] == "VERIFIED"


# --- lint_spec: a verdict node explicitly disabling strict should warn -----

def _lint_spec(nodes):
    from axiom.ir import Spec, Requirement, lint_spec
    spec = Spec(spec_version_id="spec.v1", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement(id="R1", text="t")],
                boundaries=[], success_evidence=[], nodes=nodes,
                control_flow={"steps": list(nodes.keys())},
                decision_trace=[], budget_usd=10, max_concurrent=4,
                max_agents=10, max_stagnation=2)
    return lint_spec(spec)


def test_lint_warns_verdict_explicit_strict_false():
    node = _verdict_node(strict_flag=False)
    warns = _lint_spec({"n_verify": node})
    hits = [w for w in warns if "conform_strict_json" in w]
    assert len(hits) == 1, "a verdict node explicitly disabling strict should trigger a lint warning"


def test_lint_no_warn_verdict_default_strict():
    node = _verdict_node()  # default = auto strict, no warning
    warns = _lint_spec({"n_verify": node})
    assert not [w for w in warns if "conform_strict_json" in w]


def test_lint_no_warn_impl_explicit_strict_false():
    # an impl node explicit false matches default behavior (tolerant), not worth warning
    node = _impl_node(["f.py"], strict_flag=False)
    warns = _lint_spec({"n_impl": node})
    assert not [w for w in warns if "conform_strict_json" in w]
