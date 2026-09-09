"""R9-4 (review9 P2): anti-self-review rules, machine-checked at validate time.

The reviewer holds an independent context (requirement + pinned code +
machine evidence); the implementer can never self-certify. These rules are
VALIDATE-TIME errors, not conventions -- a spec that mixes write access with
verdict ownership, or dangles an evidence_from, fails before the run starts.
"""
from axiom.ir import Spec, Requirement, validate_spec


def _agent(nid, verdict=False, write_areas=None, evidence_from=None):
    n = {
        "type": "agent", "id": nid, "prompt": "p",
        "output_schema": {"type": "object",
                          "required": ["verdict" if verdict else "x"]},
        "allowed_tools": [], "write_areas": write_areas or [],
        "acceptance": ["a"], "failure_policy": {},
    }
    if verdict:
        n["verdict_field"] = "verdict"
    if evidence_from:
        n["evidence_from"] = evidence_from
    return n


def _script(nid, scope=None):
    n = {"type": "script", "id": nid, "script_path": "/tmp/x.py",
         "output_schema": {"required": ["ok"]}}
    if scope:
        n["evidence_scope"] = scope
    return n


def _spec(nodes, se="S1:R1=node:r"):
    return Spec(spec_version_id="v", parent_spec_id=None, revision=1,
                intent="i", requirements=[Requirement("R1", "r", "required")],
                boundaries=["b"], success_evidence=[se],
                nodes={n["id"]: n for n in nodes},
                control_flow={"type": "sequence",
                              "steps": [n["id"] for n in nodes]},
                decision_trace=[], budget_usd=5.0, max_concurrent=2,
                max_agents=10, max_stagnation=1)


def test_verdict_owner_cannot_write_code():
    s = _spec([_agent("r", verdict=True, write_areas=["src/"])])
    errs = validate_spec(s)
    assert any("anti-self-review" in e for e in errs), errs


def test_verdict_owner_readonly_passes():
    s = _spec([_agent("impl"), _agent("r", verdict=True)],
              se="S1:R1=node:r")
    errs = validate_spec(s)
    assert not any("anti-self-review" in e for e in errs), errs


def test_evidence_from_unknown_node_rejected():
    s = _spec([_agent("r", verdict=True, evidence_from="ghost")])
    errs = validate_spec(s)
    assert any("unknown node" in e for e in errs), errs


def test_evidence_from_non_script_rejected():
    s = _spec([_agent("producer"), _agent("r", verdict=True,
                                          evidence_from="producer")])
    errs = validate_spec(s)
    assert any("must point at a script node" in e for e in errs), errs


def test_evidence_from_script_without_scope_rejected():
    s = _spec([_script("s1"), _agent("r", verdict=True, evidence_from="s1")])
    errs = validate_spec(s)
    assert any("without evidence_scope" in e for e in errs), errs


def test_evidence_from_self_rejected():
    n = _agent("r", verdict=True, evidence_from="r")
    s = _spec([n])
    errs = validate_spec(s)
    assert any("must not reference itself" in e for e in errs), errs


def test_valid_evidence_chain_passes(tmp_path, monkeypatch):
    """script(evidence_scope) -> reviewer(evidence_from) is the legal shape."""
    script = tmp_path / "check.py"
    script.write_text("print('{\"ok\": true}')")
    s = _spec([_script("s1", scope=["src/"]),  # script_path must exist; patch
               _agent("r", verdict=True, evidence_from="s1")])
    # point the script node at the real file so the existence check passes
    s.nodes["s1"]["script_path"] = str(script)
    errs = validate_spec(s)
    assert errs == [], errs


def test_evidence_from_counts_as_machine_gate_no_optout_needed(tmp_path):
    """R9-3: a code-touching spec whose verdict node binds evidence_from
    satisfies the machine-gate rule WITHOUT assurance_hook or opt_out."""
    script = tmp_path / "check.py"
    script.write_text("print('{\"ok\": true}')")
    impl = _agent("impl", write_areas=["src/app.py"])  # code-touching
    impl["allowed_tools"] = ["Edit"]
    rev = _agent("r", verdict=True, evidence_from="s1")
    s = _spec([impl, _script("s1", scope=["src/app.py"]), rev],
              se="S1:R1=node:r")
    s.nodes["s1"]["script_path"] = str(script)
    s.assurance_opt_out = ""  # no escape hatch
    errs = validate_spec(s)
    assert not any("assurance" in e or "self-report" in e for e in errs), errs
