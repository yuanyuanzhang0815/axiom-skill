"""script node: deterministic subprocess execution, no LLM dispatch.

Tests: validate-time rules + harness execution (success / failure / args
template / script+verify collaboration). Mirrors the design doc D1-D6.
"""
import json
import sys
from pathlib import Path

import pytest

from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec


def _spec(nodes, success_evidence=("S1:R1=claim:C1",)):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i", requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"], success_evidence=list(success_evidence),
        nodes=nodes,
        control_flow={"type": "sequence", "steps": []}, decision_trace=[],
        budget_usd=5.0, max_concurrent=16, max_agents=1000, max_stagnation=1,
    )


def _script_node(script_path, args=None, output_schema=None, **over):
    n = {
        "type": "script", "id": "n_scan", "script_path": str(script_path),
        "args": args or [],
        "output_schema": output_schema if output_schema is not None
                         else {"required": ["violations"]},
        "failure_policy": {"max_retries": 1, "retry_guard": "requires_new_evidence",
                           "on_exhausted": "block"},
    }
    n.update(over)
    return n


def _write_script(tmp_path, body, name="scan.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# --- validate-time rules (D2.4 / §2) ---------------------------------------

def test_validate_script_path_missing_reports_error(tmp_path):
    nodes = {"n_scan": _script_node(tmp_path / "nope.py")}
    errs = validate_spec(_spec(nodes))
    assert any("script_path" in e and "n_scan" in e for e in errs), errs


def test_validate_script_path_existing_passes(tmp_path):
    p = _write_script(tmp_path, "print('{}')")
    errs = validate_spec(_spec({"n_scan": _script_node(p)}))
    assert not any("script_path" in e for e in errs), errs


def test_validate_script_missing_output_schema_reports_error(tmp_path):
    p = _write_script(tmp_path, "print('{}')")
    errs = validate_spec(_spec({"n_scan": _script_node(p, output_schema={})}))
    assert any("output_schema" in e and "n_scan" in e for e in errs), errs


def test_validate_script_args_ref_unknown_node_reports_error(tmp_path):
    p = _write_script(tmp_path, "print('{}')")
    # {{n_missing.validated_output.x}} references a non-existent node
    nodes = {"n_scan": _script_node(p, args=["--x", "{{n_missing.validated_output.x}}"])}
    errs = validate_spec(_spec(nodes))
    assert any("n_missing" in e or "n_scan" in e for e in errs), errs


def test_validate_script_node_cannot_bind_success_evidence(tmp_path):
    p = _write_script(tmp_path, "print('{}')")
    nodes = {"n_scan": _script_node(p)}
    # script bound as completion node -> must be rejected
    errs = validate_spec(_spec(nodes, success_evidence=("S1:R1=node:n_scan",)))
    assert any("n_scan" in e and "script" in e for e in errs), errs


# --- execution (D2-D3) -----------------------------------------------------

def _ok_runner(*a, **k):
    return (0, '{"summary": ""}')


def test_script_success_parses_output_no_llm(tmp_path):
    p = _write_script(tmp_path, 'import json; print(json.dumps({"violations": 0}))')
    nodes = {"n_scan": _script_node(p)}
    spec = _spec(nodes)
    spec.control_flow = {"type": "sequence", "steps": ["n_scan"]}
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    h.run(spec)
    evs = [e for e in h.ledger.events() if e.get("kind") == "script_result"]
    assert evs, "no script_result event emitted"
    payload = evs[0].get("payload", {})
    assert payload.get("failure_class") is None, payload
    assert payload.get("parsed_output", {}).get("violations") == 0, payload


def test_script_exit_nonzero_blocks_no_fake_success(tmp_path):
    p = _write_script(tmp_path, "import sys; sys.exit(1)")
    nodes = {"n_scan": _script_node(p)}
    spec = _spec(nodes)
    spec.control_flow = {"type": "sequence", "steps": ["n_scan"]}
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    h.run(spec)
    evs = [e for e in h.ledger.events() if e.get("kind") == "script_result"]
    assert evs, "expected a script_result failure event"
    payload = evs[-1].get("payload", {})
    assert payload.get("exit_code") != 0 or payload.get("failure_class"), payload


def test_script_non_json_stdout_is_parse_failure(tmp_path):
    p = _write_script(tmp_path, 'print("not json at all")')
    nodes = {"n_scan": _script_node(p)}
    spec = _spec(nodes)
    spec.control_flow = {"type": "sequence", "steps": ["n_scan"]}
    h = Harness(tmp_path / "run", worker_runner=_ok_runner)
    h.run(spec)
    evs = [e for e in h.ledger.events() if e.get("kind") == "script_result"]
    assert evs
    payload = evs[-1].get("payload", {})
    assert payload.get("failure_class") == "parse_failure", payload


def test_script_args_template_substitutes_upstream(tmp_path):
    # upstream agent emits files_modified; script consumes it via
    # {{n1.validated_output.files_modified}} (same convention as agent
    # prompt / synthesize inputs / predicate DSL). _render serialises
    # non-scalar values to JSON, so the script parses the arg back.
    p = _write_script(tmp_path,
        'import json,sys; print(json.dumps({"got": json.loads(sys.argv[1])}))')
    nodes = {
        "n1": {"type": "agent", "id": "n1", "prompt": "p",
               "output_schema": {"required": ["files_modified"]},
               "write_areas": [], "acceptance": ["x"],
               "failure_policy": {"max_retries": 0, "on_exhausted": "block"}},
        "n_scan": _script_node(p, args=["{{n1.validated_output.files_modified}}"],
                               output_schema={"required": ["got"]}),
    }
    spec = _spec(nodes)
    spec.control_flow = {"type": "sequence", "steps": ["n1", "n_scan"]}

    def runner(*a, **k):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"files_modified": ["a.ts"]}),
        }))

    h = Harness(tmp_path / "run", worker_runner=runner)
    h.run(spec)
    evs = [e for e in h.ledger.events() if e.get("kind") == "script_result"]
    assert evs, "no script_result"
    payload = evs[-1].get("payload", {})
    got = payload.get("parsed_output", {}).get("got")
    assert got == ["a.ts"], payload
