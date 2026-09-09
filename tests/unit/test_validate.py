from axiom.ir import Spec, Requirement, validate_spec, check_ownership


def _spec(nodes, cf):
    return Spec(
        spec_version_id="spec.v1",
        parent_spec_id=None,
        revision=1,
        intent="i",
        requirements=[Requirement("R1", "r", "required")],
        boundaries=["b"],
        success_evidence=["S1:R1=s"],
        nodes=nodes,
        control_flow=cf,
        decision_trace=[],
        budget_usd=5.0,
        max_concurrent=16,
        max_agents=1000,
        max_stagnation=1,
    )


def _agent(nid, write_areas):
    return {
        "type": "agent",
        "id": nid,
        "prompt": "p",
        "output_schema": {"type": "object"},
        "acceptance": ["a"],
        "write_areas": write_areas,
        "failure_policy": {},
    }


def test_agent_node_requires_output_schema():
    n = {
        "type": "agent",
        "id": "n1",
        "prompt": "p",
        "output_schema": {},
        "acceptance": ["a"],
        "write_areas": [],
        "failure_policy": {},
    }
    errs = validate_spec(_spec({"n1": n}, {"type": "sequence", "steps": ["n1"]}))
    assert any("output_schema" in e for e in errs)


def test_missing_acceptance_flagged():
    n = {
        "type": "agent",
        "id": "n1",
        "prompt": "p",
        "output_schema": {"type": "object"},
        "acceptance": [],
        "write_areas": [],
        "failure_policy": {},
    }
    errs = validate_spec(_spec({"n1": n}, {"type": "sequence", "steps": ["n1"]}))
    assert any("acceptance" in e for e in errs)


def test_ownership_overlap_rejected():
    a = _agent("a", ["src/auth/**"])
    b = _agent("b", ["src/auth/login.py"])
    spec = _spec({"a": a, "b": b}, {"type": "sequence", "steps": [["parallel", "a", "b"]]})
    errs = check_ownership(spec)
    assert errs  # overlap detected


def test_ownership_no_overlap_ok():
    a = _agent("a", ["src/auth/**"])
    b = _agent("b", ["src/pay/**"])
    spec = _spec({"a": a, "b": b}, {"type": "sequence", "steps": [["parallel", "a", "b"]]})
    assert check_ownership(spec) == []


def test_sequential_agents_not_checked_for_overlap():
    # agents in a sequence (not parallel) may share areas by design
    a = _agent("a", ["src/**"])
    b = _agent("b", ["src/**"])
    spec = _spec({"a": a, "b": b}, {"type": "sequence", "steps": ["a", "b"]})
    assert check_ownership(spec) == []


def test_valid_spec_returns_no_errors():
    a = _agent("a", ["src/auth/**"])
    spec = _spec({"a": a}, {"type": "sequence", "steps": ["a"]})
    assert validate_spec(spec) == []


def test_missing_node_id_flagged():
    # the harness hard-subscripts node["id"] in ~15 places (first at
    # dispatch_agent/_replay_pop); a spec missing it passes validate but
    # crashes at run. The SKILL.md walkthrough once omitted id -> KeyError on
    # first dispatch -> manual python patch -> rm -rf -> re-run (flight-price
    # relay). validate must catch it.
    n = dict(_agent("n1", []))
    del n["id"]
    errs = validate_spec(_spec({"n1": n}, {"type": "sequence", "steps": ["n1"]}))
    assert any("missing id field" in e for e in errs)


def test_node_id_dict_key_mismatch_flagged():
    # the dict key is the canonical id; node["id"] must match or the replay
    # cache key (spec_version_id, node["id"]) diverges from the nodes-dict key.
    n = dict(_agent("n1", []))
    n["id"] = "n_other"
    errs = validate_spec(_spec({"n1": n}, {"type": "sequence", "steps": ["n1"]}))
    assert any("does not match dict key" in e for e in errs)


# --- B5: decision_trace iron rules -----------------------------------------


def _spec_with_dt(decision_trace):
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1, intent="i",
        requirements=[Requirement("R1", "r", "required")], boundaries=["b"],
        success_evidence=["S1:R1=s"], nodes={}, control_flow={"type": "sequence", "steps": []},
        decision_trace=decision_trace, budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    )


def test_decision_phase_enum_enforced():
    dt = [{"decision_id": "d1", "phase": "orientate", "observation": "o"}]
    errs = validate_spec(_spec_with_dt(dt))
    assert any("phase" in e for e in errs)


def test_decompose_requires_alternatives_rejected():
    dt = [{"decision_id": "d1", "phase": "decompose", "observation": "o",
           "alternatives_rejected": []}]
    errs = validate_spec(_spec_with_dt(dt))
    assert any("alternatives_rejected" in e for e in errs)


def test_replan_requires_replan_reason():
    dt = [{"decision_id": "d1", "phase": "replan", "observation": "o",
           "alternatives_rejected": [{"option": "x", "reason": "r"}],
           "replan_reason": None}]
    errs = validate_spec(_spec_with_dt(dt))
    assert any("replan_reason" in e for e in errs)


def test_decision_evidence_refs_format():
    dt = [{"decision_id": "d1", "phase": "frame", "observation": "o",
           "evidence_refs": ["badref:E1"]}]
    errs = validate_spec(_spec_with_dt(dt))
    assert any("evidence_refs" in e for e in errs)


def test_decision_assumption_ids_unique():
    dt = [{"decision_id": "d1", "phase": "frame", "observation": "o",
           "assumptions": [{"id": "A1", "text": "a"}, {"id": "A1", "text": "b"}]}]
    errs = validate_spec(_spec_with_dt(dt))
    assert any("A1" in e for e in errs)


def test_decision_trace_clean_when_valid():
    dt = [{"decision_id": "d1", "phase": "decompose", "observation": "o",
           "alternatives_rejected": [{"option": "x", "reason": "r"}],
           "assumptions": [{"id": "A1", "text": "a"}]}]
    assert validate_spec(_spec_with_dt(dt)) == []


# --- v1.2.1 control_flow step format (validate/run parity) ----------------
# A real relay hit a silent-skip: spec wrote a parallel step as
# ["n2","n3","n4"] (bare list, no keyword). validate accepted it; _run_sequence
# checks step[0]=="parallel" and silently skipped the whole fan-out. Validate
# must now reject a bare list so the mistake surfaces at plan time, not run.

def test_parallel_step_requires_keyword_prefix():
    nodes = {"n1": _agent("n1", []), "n2": _agent("n2", []), "n3": _agent("n3", [])}
    cf = {"type": "sequence", "steps": ["n1", ["n2", "n3"]]}
    errs = validate_spec(_spec(nodes, cf))
    assert any("control_flow step 1" in e and "parallel" in e for e in errs), \
        "a bare list step without the 'parallel' keyword must be rejected"


def test_parallel_step_with_prefix_valid():
    nodes = {"n1": _agent("n1", []), "n2": _agent("n2", []), "n3": _agent("n3", [])}
    cf = {"type": "sequence", "steps": ["n1", ["parallel", "n2", "n3"]]}
    assert validate_spec(_spec(nodes, cf)) == []


def test_unknown_str_step_rejected():
    nodes = {"n1": _agent("n1", [])}
    cf = {"type": "sequence", "steps": ["n1", "nope"]}
    errs = validate_spec(_spec(nodes, cf))
    assert any("nope" in e for e in errs), "unknown node id in steps must be rejected"


# --- v1.3.1 verdict pairing cross-check (validate/run parity) ------------
# The flight-price-monitor relay bound success_evidence to node:n1 but n1 had
# no verdict_field -> no agent_verdict event -> PARTIAL despite correct work.
# validate must catch this at plan time (like it catches a bare parallel list).

def test_node_binding_requires_verdict_field():
    nodes = {"n1": _agent("n1", [])}  # _agent has no verdict_field
    cf = {"type": "sequence", "steps": ["n1"]}
    spec = _spec(nodes, cf)
    spec.success_evidence = ["S1:R1=node:n1"]
    errs = validate_spec(spec)
    assert any("node:n1" in e and "verdict_field" in e for e in errs), \
        "node: binding to an agent without verdict_field must be rejected"


def test_node_binding_with_verdict_field_valid():
    nodes = {"n1": _agent("n1", [])}
    nodes["n1"]["verdict_field"] = "verdict"
    cf = {"type": "sequence", "steps": ["n1"]}
    spec = _spec(nodes, cf)
    spec.success_evidence = ["S1:R1=node:n1"]
    assert validate_spec(spec) == []


def test_claim_binding_bad_format_rejected():
    # claim:<id> must match C\d+ or dec_\w+ (state._evidence_to_claim regex);
    # claim:n4_verify_fixes (a node id) is the common mistake -> reject.
    nodes = {"n1": _agent("n1", [])}
    cf = {"type": "sequence", "steps": ["n1"]}
    spec = _spec(nodes, cf)
    spec.success_evidence = ["S1:R1=claim:n4_verify_fixes"]
    errs = validate_spec(spec)
    assert any("n4_verify_fixes" in e or "claim id" in e for e in errs), \
        "claim: binding a non-claim-id (e.g. a node id) must be rejected"
