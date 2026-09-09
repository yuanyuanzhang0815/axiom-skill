"""validate_spec assurance default-on (invariant #9): a spec whose write_areas
touch runtime/code must machine-gate its completion verdict. The node bound by
success_evidence (node:<id>) must declare assurance_hook; bare LLM self-report
VERIFIED on a code change is not a valid completion signal.

Born from a real relay's admin-edit-user run: a 3-node split (n_backend_edit /
n_frontend_edit with write_areas, n_rt_verify with verdict_field but no
write_areas) legally declared no assurance_hook, so the entire A/B/C/D machine
chain was bypassed and n_rt_verify's self-reported VERIFIED was trusted
directly. This rule keys off the SPEC's write_areas + the success_evidence
binding (not the node's own write_areas) so the impl/verify split can't slip through.
"""
from axiom.ir import Spec, Requirement, validate_spec, lint_spec


def _agent(nid, verdict_field=None, write_areas=None, assurance_hook=None,
           allowed_tools=None):
    n = {"type": "agent", "id": nid, "prompt": "p",
         "output_schema": {"type": "object"}, "acceptance": ["a"],
         "failure_policy": {}, "write_areas": write_areas or []}
    if verdict_field:
        n["verdict_field"] = verdict_field
    if assurance_hook:
        n["assurance_hook"] = assurance_hook
    if allowed_tools:
        n["allowed_tools"] = allowed_tools
    return n


def _spec(nodes, success_evidence=None, assurance_opt_out=None):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="i", requirements=[Requirement(id="R1", text="t")],
        boundaries=[], success_evidence=success_evidence or [],
        nodes=nodes,
        control_flow={"type": "sequence", "steps": list(nodes.keys())},
        decision_trace=[], budget_usd=10, max_concurrent=4,
        max_agents=10, max_stagnation=2,
        assurance_opt_out=assurance_opt_out,
    )


def _errs(spec):
    return [e for e in validate_spec(spec) if "assurance_hook" in e or "opt-out" in e or "completion-verdict" in e]


def test_runtime_touching_requires_assurance_hook():
    # single agent: writes runtime code + claims verdict + bound by success_evidence
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["backend/app/main.py"])}
    spec = _spec(nodes, success_evidence=["S1:R1=node:n1"])
    errs = _errs(spec)
    assert any("n1" in e and "assurance_hook" in e for e in errs), errs


def test_runtime_touching_with_hook_ok():
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["backend/app/main.py"],
                          assurance_hook={"receipt_path": "receipt.json"})}
    spec = _spec(nodes, success_evidence=["S1:R1=node:n1"])
    assert _errs(spec) == []


def test_opt_out_suppresses_error_to_warning():
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["backend/app/main.py"])}
    spec = _spec(nodes, success_evidence=["S1:R1=node:n1"],
                 assurance_opt_out="experimental; manual review instead")
    # validate passes (no error)...
    assert _errs(spec) == []
    # ...but lint discloses the bypass
    warns = lint_spec(spec)
    assert any("assurance opt-out" in w and "manual review" in w for w in warns), warns


def test_docs_only_spec_no_assurance_required():
    # pure docs/config write_areas -> no code touched -> rule doesn't fire
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["docs/readme.md", "package.json"])}
    spec = _spec(nodes, success_evidence=["S1:R1=node:n1"])
    assert _errs(spec) == []


def test_frontend_tsx_touches_code():
    # .tsx is frontend code -> rule fires
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["frontend/src/pages/Admin.tsx"])}
    spec = _spec(nodes, success_evidence=["S1:R1=node:n1"])
    assert _errs(spec), "frontend .tsx should trigger the code-touch gate"


def test_impl_verify_split_binds_verifier_node():
    # THE impl/verify split pattern: implementation nodes (write_areas, no verdict_field)
    # + a verifier node (verdict_field, NO write_areas) bound by success_evidence.
    # The verifier claims completion for code it didn't write itself -- it MUST
    # be machine-gated. A per-node rule (write_areas+verdict_field same node)
    # would miss this; the spec-level rule catches it.
    nodes = {
        "n_backend_edit": _agent("n_backend_edit",
                                 write_areas=["backend/app/main.py"]),
        "n_frontend_edit": _agent("n_frontend_edit",
                                   write_areas=["frontend/src/Panel.tsx"]),
        "n_rt_verify": _agent("n_rt_verify", verdict_field="verdict",
                              write_areas=[]),  # pure verifier, no write_areas
    }
    spec = _spec(nodes, success_evidence=["S1:R1=node:n_rt_verify"])
    errs = _errs(spec)
    assert any("n_rt_verify" in e and "assurance_hook" in e for e in errs), (
        "impl/verify split: n_rt_verify (bound completion node, no own write_areas) "
        "must still be required to declare assurance_hook"
    )


def test_impl_verify_split_with_hook_ok():
    nodes = {
        "n_backend_edit": _agent("n_backend_edit",
                                 write_areas=["backend/app/main.py"]),
        "n_rt_verify": _agent("n_rt_verify", verdict_field="verdict",
                              write_areas=[],
                              assurance_hook={"receipt_path": "receipt.json"}),
    }
    spec = _spec(nodes, success_evidence=["S1:R1=node:n_rt_verify"])
    assert _errs(spec) == []


def test_no_node_binding_no_error():
    # success_evidence uses claim: (not node:) -> can't structurally locate the
    # completion node -> no validate error (degrades to lint warning).
    nodes = {"n1": _agent("n1", verdict_field="verdict",
                          write_areas=["backend/app/main.py"])}
    spec = _spec(nodes, success_evidence=["S1:R1=claim:manual"])
    assert _errs(spec) == []
    # but lint warns (the spec claims completion yet can't be machine-gated)
    warns = lint_spec(spec)
    assert any("no success_evidence node" in w for w in warns), warns


def test_empty_success_evidence_no_warn():
    # a spec fragment with no completion claim at all -> no opt-out warning
    nodes = {"n1": _agent("n1", write_areas=["backend/app/main.py"])}
    spec = _spec(nodes, success_evidence=[])
    warns = [w for w in lint_spec(spec) if "opt-out" in w or "no success_evidence node" in w]
    assert warns == []
