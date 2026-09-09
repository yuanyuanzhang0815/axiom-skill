"""P-008 agent-verdict fix loop (fixes run 4: after verify FAILED the run went
straight to PARTIAL with no route back to impl).

When a verify node declares fix_loop={"impl":..., "max_rounds":N} and its
verdict_field returns FAILED, the harness re-dispatches the paired impl node
with the issues to fix, then re-verifies, up to N rounds.
Safety: whether the fix is right is adjudicated by verify; max_rounds caps
LLM self-loop cost.
"""
import json
from axiom.harness import Harness


class _SpecStub:
    spec_version_id = "spec.v1"
    max_stagnation = 2
    budget_usd = 100.0
    max_agents = 1000

    def __init__(self, nodes):
        self.nodes = nodes


def _impl_node():
    return {
        "type": "agent", "id": "n_impl", "prompt": "implement", "dispatch": "host",
        "output_schema": {"type": "object",
                          "properties": {"files_modified": {"type": "array"},
                                         "summary": {"type": "string"}},
                          "required": ["files_modified", "summary"]},
        "allowed_tools": ["Read", "Edit", "Write"], "write_areas": ["code.py"],
        "acceptance": ["a"],
        "failure_policy": {"max_retries": 1,
                           "retry_guard": "requires_new_evidence",
                           "on_exhausted": "block"},
    }


def _verify_node(fix_loop=None):
    n = {
        "type": "agent", "id": "n_verify", "prompt": "verify", "dispatch": "host",
        "verdict_field": "verdict",
        "output_schema": {"type": "object",
                          "properties": {"verdict": {"type": "string"},
                                         "issues": {"type": "array"}},
                          "required": ["verdict", "issues"]},
        "allowed_tools": ["Read", "Bash"], "write_areas": [],
        "acceptance": ["a"],
        "failure_policy": {"max_retries": 1,
                           "retry_guard": "requires_new_evidence",
                           "on_exhausted": "degrade"},
    }
    if fix_loop:
        n["fix_loop"] = fix_loop
    return n


def _is_verify(args):
    # _dispatch argv = [host_adapter, ..., prompt, '--output-format','json',
    # '--json-schema', <schema>, ...]. verify/impl have different output_schema:
    # verify's schema contains "verdict", impl's contains "files_modified". Use the
    # schema feature to determine node type -- more stable than relying on argv
    # position or prompt text.
    blob = " ".join(str(a) for a in args)
    return '"verdict"' in blob


def _res(payload, cost=0.5):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 2,
        "result": json.dumps(payload),
        "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    }))


def test_fix_loop_repairs_and_verifies(tmp_path):
    # run-4 scenario: impl writes code with an await bug -> verify FAILED(issues)
    # -> fix_loop triggers impl to fix per the issues -> verify re-checks VERIFIED.
    state = {"verify_calls": 0, "impl_calls": 0}

    def runner(args, cwd=None):
        is_verify = _is_verify(args)
        # simplified check: a verify node (schema has "verdict"); otherwise impl
        if is_verify:
            state["verify_calls"] += 1
            # read the real file to judge: an await bug -> FAILED
            try:
                code = (tmp_path / "code.py").read_text()
            except OSError:
                code = ""
            if "await provider.generate_json" in code:
                return _res({"verdict": "FAILED",
                             "issues": ["gen_state.py:41 await on a sync method"]})
            return _res({"verdict": "VERIFIED", "issues": []})
        else:
            state["impl_calls"] += 1
            if state["impl_calls"] == 1:
                (tmp_path / "code.py").write_text("x = await provider.generate_json()")
            else:
                # round 2: fix per the issues (remove await)
                (tmp_path / "code.py").write_text("x = provider.generate_json()")
            return _res({"files_modified": ["code.py"], "summary": "done"})

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    nodes = {"n_impl": _impl_node(),
             "n_verify": _verify_node(fix_loop={"impl": "n_impl", "max_rounds": 2})}
    spec = _SpecStub(nodes)
    out = h._run_steps(["n_impl", "n_verify"], {}, "spec.v1", spec)
    # fixed -> final VERIFIED; impl ran 2 times, verify ran 2 times
    assert out["validated_output"]["verdict"] == "VERIFIED"
    assert state["impl_calls"] == 2, "fix_loop should re-run impl once"
    assert state["verify_calls"] == 2, "fix_loop should re-verify once"
    # a fix_loop_iteration event is recorded
    assert any(e.get("kind") == "fix_loop_iteration"
               for e in h.ledger.events())


def test_fix_loop_stops_at_max_rounds(tmp_path):
    # verify keeps FAILED (impl cannot fix it) -> runs max_rounds then stops,
    # honestly returning FAILED.
    state = {"impl_calls": 0}

    def runner(args, cwd=None):
        is_verify = _is_verify(args)
        if is_verify:
            return _res({"verdict": "FAILED", "issues": ["still broken"]})
        state["impl_calls"] += 1
        (tmp_path / "code.py").write_text(f"attempt {state['impl_calls']}")
        return _res({"files_modified": ["code.py"], "summary": "done"})

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    nodes = {"n_impl": _impl_node(),
             "n_verify": _verify_node(fix_loop={"impl": "n_impl", "max_rounds": 2})}
    spec = _SpecStub(nodes)
    out = h._run_steps(["n_impl", "n_verify"], {}, "spec.v1", spec)
    assert out["validated_output"]["verdict"] == "FAILED", "unfixable should honestly FAILED"
    # impl first time + max_rounds(2) re-fixes = 3 times
    assert state["impl_calls"] == 3
    # no more than max_rounds fix_loop_iterations
    iters = [e for e in h.ledger.events() if e.get("kind") == "fix_loop_iteration"]
    assert len(iters) == 2


def test_no_fix_loop_without_declaration(tmp_path):
    # a verify node that did NOT declare fix_loop and FAILED -> does not re-run
    # impl (zero behavior change)
    state = {"impl_calls": 0}

    def runner(args, cwd=None):
        is_verify = _is_verify(args)
        if is_verify:
            return _res({"verdict": "FAILED", "issues": ["x"]})
        state["impl_calls"] += 1
        (tmp_path / "code.py").write_text("code")
        return _res({"files_modified": ["code.py"], "summary": "done"})

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    nodes = {"n_impl": _impl_node(), "n_verify": _verify_node(fix_loop=None)}
    spec = _SpecStub(nodes)
    out = h._run_steps(["n_impl", "n_verify"], {}, "spec.v1", spec)
    assert out["validated_output"]["verdict"] == "FAILED"
    assert state["impl_calls"] == 1, "no fix_loop declared should not re-run impl"
    assert not any(e.get("kind") == "fix_loop_iteration"
                   for e in h.ledger.events())


def test_no_fix_loop_when_verified_first_pass(tmp_path):
    # verify VERIFIED on the first pass -> does not trigger fix_loop
    state = {"impl_calls": 0}

    def runner(args, cwd=None):
        is_verify = _is_verify(args)
        if is_verify:
            return _res({"verdict": "VERIFIED", "issues": []})
        state["impl_calls"] += 1
        (tmp_path / "code.py").write_text("good code")
        return _res({"files_modified": ["code.py"], "summary": "done"})

    h = Harness(tmp_path / "run", worker_runner=runner, project_root=str(tmp_path))
    nodes = {"n_impl": _impl_node(),
             "n_verify": _verify_node(fix_loop={"impl": "n_impl", "max_rounds": 2})}
    spec = _SpecStub(nodes)
    out = h._run_steps(["n_impl", "n_verify"], {}, "spec.v1", spec)
    assert out["validated_output"]["verdict"] == "VERIFIED"
    assert state["impl_calls"] == 1, "passing on the first try should not re-run impl"
    assert not any(e.get("kind") == "fix_loop_iteration"
                   for e in h.ledger.events())
