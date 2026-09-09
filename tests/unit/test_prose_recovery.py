"""conform-fail artifact-recovery (fixes runs 1-5 stalls) tests.

Core scenario: the worker finished the file work but returned pure prose
(zero JSON); the harness must NOT treat it as a cognitive-failure stall/gate,
but rebuild the output from write_areas disk-hash evidence and pass it through.
Safety boundary: a verdict_field node (verify) is NEVER recovered (a verdict
must be real JSON); if files were not actually changed = a real failure,
which still goes through the normal stall/gate.
"""
import json
from axiom.harness import Harness


class _SpecStub:
    spec_version_id = "spec.v1"
    max_stagnation = 2
    budget_usd = 50.0
    max_agents = 1000


def _impl_node(write_areas, verdict_field=None, max_retries=2):
    n = {
        "type": "agent", "id": "n_impl", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"files_modified": {"type": "array"},
                           "summary": {"type": "string"}},
            "required": ["files_modified", "summary"],
        },
        "allowed_tools": ["Read", "Edit", "Write"],
        "write_areas": write_areas,
        "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries, "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        },
    }
    if verdict_field:
        n["verdict_field"] = verdict_field
    return n


def _prose_runner(text):
    """Worker does the work but returns pure prose (zero JSON) -- reproduces
    the run 3/5 stall."""
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": text,
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    return runner


def _bad_json_runner():
    """Returns valid JSON but missing required fields (schema-nonconformance,
    not prose)."""
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 3,
            "result": json.dumps({"foo": "bar"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    return runner


def test_recover_prose_with_file_change(tmp_path):
    # existing file f.py changed by the worker, but returns pure prose ->
    # recovery passes through, no stall, no gate
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
    node = _impl_node(["f.py"])
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None, "files changed -> should recover and pass through, not return None"
    vo = out["validated_output"]
    assert vo["_recovered"] is True
    assert "f.py" in vo["files_modified"]
    # recovery goes through agent_result (marked recovered_from_prose), zero stalls, zero gates
    ar = [e for e in h.ledger.events() if e.get("kind") == "agent_result"]
    assert ar and ar[0]["payload"].get("recovered_from_prose") is True
    assert not any(e.get("kind") == "stagnating" for e in h.ledger.events())
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events())


def test_recover_prose_with_new_file(tmp_path):
    # write_areas did not exist before; the worker adds it -> added also triggers recovery
    h = Harness(tmp_path / "run",
                worker_runner=_prose_runner("done"),
                project_root=str(tmp_path))
    def runner(args, cwd=None):
        (tmp_path / "new.py").write_text("created")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": "New file created",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _impl_node(["new.py"])
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None
    assert "new.py" in out["validated_output"]["files_modified"]


def test_no_recover_when_files_unchanged(tmp_path):
    # files unchanged = real failure -> keep the normal stall/gate, no recovery
    (tmp_path / "f.py").write_text("same")
    h = Harness(tmp_path / "run",
                worker_runner=_prose_runner("I did not change anything"),
                project_root=str(tmp_path))
    node = _impl_node(["f.py"], max_retries=2)
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is None, "files unchanged is a real failure, should not recover"
    assert not any(
        e.get("kind") == "agent_result" and
        e["payload"].get("recovered_from_prose")
        for e in h.ledger.events())


def test_recover_on_partial_json_missing_fields(tmp_path):
    # Returns valid JSON but missing required fields -> also recovers. The
    # missing fields are impl self-report (not evidence; evidence is checked
    # independently by verify); G3 proved ineffective on the worker model (run 3
    # injected 3 times and still produced prose), hard-waiting for it to fill
    # the fields just stalls and burns money. If the work was done, pass it
    # through for verify adjudication.
    (tmp_path / "f.py").write_text("old")
    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("changed")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": json.dumps({"foo": "bar"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _impl_node(["f.py"], max_retries=1)
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None, "partial JSON + files really changed should also recover and pass through"
    assert out["validated_output"]["_recovered"] is True
    assert "f.py" in out["validated_output"]["files_modified"]
    # and recovers in one dispatch, no retry, no stall burn
    assert not any(e.get("kind") == "stagnating" for e in h.ledger.events())


def test_verdict_field_node_not_recovered(tmp_path):
    # a verdict_field node (verify) is NEVER recovered even if files changed ->
    # verdict must be real JSON
    (tmp_path / "f.py").write_text("old")
    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("changed")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": "verdict=VERIFIED (prose, not JSON)",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _impl_node(["f.py"], verdict_field="verdict", max_retries=1)
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is None, "a verdict_field node should not take prose recovery"
    assert not any(
        e.get("kind") == "agent_result" and
        e["payload"].get("recovered_from_prose")
        for e in h.ledger.events())


def test_clean_conform_untouched(tmp_path):
    # clean conform (real JSON) succeeds normally, not marked recovered
    (tmp_path / "f.py").write_text("old")
    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("new")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": json.dumps({"files_modified": ["f.py"], "summary": "ok"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _impl_node(["f.py"])
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None
    assert "_recovered" not in out["validated_output"]
    assert not any(
        e.get("kind") == "agent_result" and
        e["payload"].get("recovered_from_prose")
        for e in h.ledger.events())


# --- #8 recovery_schema: explicit missing-field disclosure (P-013) -----------
#
# The recovery-rebuilt output only has three semantic fields:
# files_modified/summary/_recovered; other schema-required fields were never
# produced by the worker. #8 requires this gap to be explicit: written into the
# recovered _recovery block + agent_result event payload, and auto-appended by
# the harness to all downstream node prompts (G1-style, not relying on the spec
# author referencing a template variable -- the P-008 lesson).


def _rich_impl_node(write_areas):
    """Schema requires a field beyond files_modified/summary (root_cause)."""
    n = _impl_node(write_areas)
    n["output_schema"] = {
        "type": "object",
        "properties": {"files_modified": {"type": "array"},
                       "summary": {"type": "string"},
                       "root_cause": {"type": "string"}},
        "required": ["files_modified", "summary", "root_cause"],
    }
    return n


def test_recovery_marks_missing_required_fields(tmp_path):
    # schema additionally requires root_cause: after recovery,
    # _recovery.missing_required_fields must list it (the worker never produced
    # it); the event payload records it synchronously (queryable at the fact layer).
    (tmp_path / "f.py").write_text("old")
    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("new")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": "Done, did not write JSON.",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _rich_impl_node(["f.py"])
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None
    vo = out["validated_output"]
    assert vo["_recovered"] is True
    assert vo["_recovery"]["missing_required_fields"] == ["root_cause"]
    assert sorted(vo["_recovery"]["present_fields"]) == [
        "files_modified", "summary"]
    ar = [e for e in h.ledger.events()
          if e.get("kind") == "agent_result"
          and e["payload"].get("recovered_from_prose")]
    assert ar and ar[0]["payload"]["missing_required_fields"] == ["root_cause"]


def test_recovery_no_missing_when_schema_satisfied(tmp_path):
    # when the schema only requires files_modified/summary, missing is empty
    # (disclosure still marks the recovery source).
    (tmp_path / "f.py").write_text("old")
    def runner(args, cwd=None):
        (tmp_path / "f.py").write_text("new")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": "done",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    node = _impl_node(["f.py"])
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    vo = out["validated_output"]
    assert vo["_recovery"]["missing_required_fields"] == []


def test_recovery_disclosure_scanner_shapes():
    # _recovery_disclosure covers three values shapes: wrapped / par list / bare.
    rec_vo = {"files_modified": ["f.py"], "summary": "s", "_recovered": True,
              "_recovery": {"missing_required_fields": ["root_cause"]}}
    wrapped = {"n_impl": {"validated_output": dict(rec_vo)}}
    d = Harness._recovery_disclosure(wrapped)
    assert "n_impl" in d and "root_cause" in d
    par = {"par": {"validated_output": [None, dict(rec_vo)]}}
    d = Harness._recovery_disclosure(par)
    assert "par" in d and "root_cause" in d
    bare = {"finding": dict(rec_vo)}
    d = Harness._recovery_disclosure(bare)
    assert "finding" in d
    # no recovery upstream -> empty string (prompt unchanged); _fix_feedback not a false report
    assert Harness._recovery_disclosure({}) == ""
    assert Harness._recovery_disclosure(
        {"_fix_feedback": {"issues": ["x"]}}) == ""
    assert Harness._recovery_disclosure(
        {"n": {"validated_output": {"files_modified": [], "summary": "s"}}
         }) == ""


def test_downstream_prompt_carries_disclosure(tmp_path):
    # end-to-end: after impl recovers, the downstream verify node's worker prompt
    # must auto-contain the disclosure block (not relying on the spec author
    # writing {{_recovery}} in the prompt).
    (tmp_path / "f.py").write_text("old")
    seen_prompts = []
    def runner(args, cwd=None):
        prompt = args[0] if len(args) > 0 else ""
        seen_prompts.append(prompt)
        if "verify" in prompt:
            return (0, json.dumps({
                "type": "result", "subtype": "success", "num_turns": 1,
                "result": json.dumps({"verdict": "VERIFIED", "issues": []}),
                "session_id": "s", "total_cost_usd": 0.01,
                "permission_denials": [], "usage": {},
            }))
        (tmp_path / "f.py").write_text("new")
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "result": "impl done in prose",
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    impl = _rich_impl_node(["f.py"])
    impl_out = h._dispatch_with_retry(impl, {}, "spec.v1", _SpecStub())
    assert impl_out["validated_output"]["_recovered"] is True
    verify = {
        "type": "agent", "id": "n_verify", "prompt": "please verify the fix",
        "dispatch": "host", "verdict_field": "verdict",
        "output_schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string"},
                           "issues": {"type": "array"}},
            "required": ["verdict"],
        },
        "allowed_tools": ["Read"], "write_areas": [],
        "acceptance": ["verdict"],
        "failure_policy": {"max_retries": 0,
                           "retry_guard": "requires_new_evidence",
                           "on_exhausted": "degrade"},
    }
    values = {"n_impl": impl_out}
    out = h._dispatch_with_retry(verify, values, "spec.v1", _SpecStub())
    assert out is not None
    verify_prompt = [p for p in seen_prompts if "please verify" in p][0]
    assert "UPSTREAM EVIDENCE QUALITY DISCLOSURE" in verify_prompt
    assert "n_impl" in verify_prompt and "root_cause" in verify_prompt


def test_no_disclosure_without_recovery(tmp_path):
    # When upstream conforms normally, the downstream prompt has no disclosure block (no false alarm).
    (tmp_path / "f.py").write_text("old")
    seen_prompts = []
    def runner(args, cwd=None):
        prompt = args[0] if len(args) > 0 else ""
        seen_prompts.append(prompt)
        (tmp_path / "f.py").write_text("newer")
        if "please verify" in prompt:
            return (0, json.dumps({
                "type": "result", "subtype": "success", "num_turns": 1,
                "result": json.dumps({"verdict": "VERIFIED"}),
                "session_id": "s", "total_cost_usd": 0.01,
                "permission_denials": [], "usage": {},
            }))
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"files_modified": ["f.py"], "summary": "ok"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    impl = _impl_node(["f.py"])
    impl_out = h._dispatch_with_retry(impl, {}, "spec.v1", _SpecStub())
    verify = {
        "type": "agent", "id": "n_verify", "prompt": "please verify the fix",
        "dispatch": "host", "verdict_field": "verdict",
        "output_schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        "allowed_tools": ["Read"], "write_areas": [],
        "acceptance": ["verdict"],
        "failure_policy": {"max_retries": 0,
                           "retry_guard": "requires_new_evidence",
                           "on_exhausted": "degrade"},
    }
    out = h._dispatch_with_retry(
        verify, {"n_impl": impl_out}, "spec.v1", _SpecStub())
    assert out is not None
    verify_prompt = [p for p in seen_prompts if "please verify" in p][0]
    assert "UPSTREAM EVIDENCE QUALITY DISCLOSURE" not in verify_prompt
