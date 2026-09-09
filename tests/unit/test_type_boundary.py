"""v1.1 type-boundary hardening tests.

The multi-node smoke relay (2026-08-22) proved the IR wiring is correct but
exposed three real gaps, all rooted in one place: the host dispatch's `--json-schema`
flag does NOT reliably enforce the schema, so a worker returning non-structured
output (`{}` or plain text) flowed past the type boundary as a clean
`validated_output`. The consequences:

  (a) a reviewer returning `{}` flowed downstream as a real finding;
  (b) a skeptic returning `{}` (missing `refuted`) was NOT counted as a
      refute vote -> `refute_votes=0` -> hollow `survived=True`;
  (c) a failed agent (retries/stagnation exhausted) silently returned None
      with no Gate, contradicting the declared `on_exhausted="block"`.

These tests pin each fix TDD-style: they FAIL on current harness.py and
PASS after the v1.1 hardening.
"""
import json
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement


# --- shared runners / fixtures ---------------------------------------------

def _result_line(payload):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps(payload),
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    })


class _SpecStub:
    """Minimal spec-like object for retry/stagnation tests."""
    spec_version_id = "spec.v1"
    max_stagnation = 1
    budget_usd = 5.0
    max_agents = 1000


def _strict_agent_node(max_retries=1, on_exhausted="block"):
    """An agent whose schema REQUIRES `claim_id` -- so `{}` is non-conforming."""
    return {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"],
        "failure_policy": {
            "max_retries": max_retries,
            "retry_guard": "requires_new_evidence",
            "on_exhausted": on_exhausted,
        },
    }


def _verify_node(sc=3):
    return {
        "type": "verify", "id": "v", "target": "{{findings}}",
        "skeptic_count": sc, "skeptic_prompt": "SKEPTIC {{finding}}",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }


def _skeptic_survive(args, cwd=None):
    return (0, _result_line({"refuted": False, "evidence_ref": "e"}))


# --- (a) schema validation at the type boundary -----------------------------

def test_nonconforming_output_blocked_at_boundary(tmp_path):
    """Worker returns `{}` but schema requires `claim_id` -> must NOT pass as
    `validated_output={}`; type boundary blocks it (returns None)."""
    def bad_runner(args, cwd=None):
        return (0, _result_line({}))  # missing required claim_id
    h = Harness(tmp_path / "run", worker_runner=bad_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None, "non-conforming {} must not flow past the type boundary"


def test_non_json_output_blocked_at_boundary(tmp_path):
    """Worker returns plain text (not JSON) -> _parse_validated {} -> blocked."""
    def text_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": "I think the answer is 42",  # not JSON
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=text_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None


def test_conforming_output_passes_boundary(tmp_path):
    """Guard: a conforming output still passes cleanly."""
    def good_runner(args, cwd=None):
        return (0, _result_line({"claim_id": "C1"}))
    h = Harness(tmp_path / "run", worker_runner=good_runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out == {"validated_output": {"claim_id": "C1"}}


def test_loose_schema_passes_any_dict(tmp_path):
    """Guard: schema `{type:object}` (no required) accepts any dict incl `{}`.
    A schema that requires nothing is satisfied by an empty object -- the fix
    must not over-strictly reject loose schemas existing tests rely on."""
    def runner(args, cwd=None):
        return (0, _result_line({"x": 1}))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"},
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out == {"validated_output": {"x": 1}}


def test_wrong_type_blocked(tmp_path):
    """A field present but wrong type (string where integer expected) is blocked."""
    def runner(args, cwd=None):
        return (0, _result_line({"count": "two"}))  # string, not integer
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None


# --- (a) retry path: non-conforming is a cognitive failure ------------------

def test_nonconforming_output_retries_then_blocks(tmp_path):
    """Worker always returns `{}` (non-conforming) -> cognitive failure each
    attempt, same signature -> stagnation -> on_exhausted=block -> Gate."""
    def bad_runner(args, cwd=None):
        return (0, _result_line({}))
    h = Harness(tmp_path / "run", worker_runner=bad_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=1), {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(e.get("kind") == "gate_open" for e in h.ledger.events()), \
        "on_exhausted=block must open a Gate when retries/stagnation exhaust"


def test_nonconforming_then_conforming_succeeds(tmp_path):
    """First attempt non-conforming, second conforming -> cognitive delta ->
    not stagnation -> succeeds."""
    seq = [_result_line({}), _result_line({"claim_id": "C1"})]
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0, line)

    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=3), {}, "spec.v1", _SpecStub())
    assert out is not None
    assert out["validated_output"] == {"claim_id": "C1"}


# --- (b) run_verify None / empty / non-conforming-skeptic guards -----------

def test_verify_skips_none_finding_no_crash(tmp_path):
    """A failed parallel reviewer yields None in the findings list; verify must
    skip it, not crash on `None.get(...)`."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive)
    out = h.run_verify(
        _verify_node(1),
        {"findings": [{"claim_id": "C1", "text": "t"}, None]},
        "spec.v1")
    vvs = [e for e in h.ledger.events() if e.get("kind") == "verify_verdict"]
    assert len(vvs) == 1, "None finding must be skipped, not verdicted"
    assert vvs[0]["claim_id"] == "C1"
    assert any(f["claim_id"] == "C1" for f in out["survivors"])


def test_verify_empty_findings_no_hollow_verdict(tmp_path):
    """Empty findings -> no verify_verdict events, no hollow survivors."""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_survive)
    out = h.run_verify(_verify_node(2), {"findings": []}, "spec.v1")
    assert out["survivors"] == []
    assert out["refuted"] == []
    assert not any(e.get("kind") == "verify_verdict" for e in h.ledger.events())


def test_nonconforming_skeptic_abstains(tmp_path):
    """THE relay gap (v1.2 G4 fix): a skeptic returning `{}` (missing
    `refuted`) ABSTAINS -- it could not deliver a verdict, so it is NOT a
    refute vote. The relay's two prose skeptics both said "accurate" but were
    counted as refutations because they returned no JSON (a FALSE refutation
    that killed a correct claim). Abstention over half -> the claim is
    unverifiable (neither survived nor refuted -> CHALLENGED), NOT falsely
    refuted."""
    def bad_skeptic(args, cwd=None):
        return (0, _result_line({}))  # missing required 'refuted'
    h = Harness(tmp_path / "run", worker_runner=bad_skeptic)
    # sc=2: both skeptics return {} -> both abstain -> 2 abstentions > 1 ->
    # unverifiable, NOT refuted.
    out = h.run_verify(
        _verify_node(2),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == [], "no conforming verdict -> no survivor"
    assert out["refuted"] == [], "abstention is NOT a false refutation"
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["survived"] is False
    assert vv["refuted"] is False  # abstention, not refutation
    assert vv["payload"]["abstain_votes"] == 2


# --- (c) on_exhausted implementation ---------------------------------------

def test_operational_exhaustion_blocks_when_policy_block(tmp_path):
    """on_exhausted=block + operational failure exhausting -> gate_open
    (retries_exhausted), not a silent None."""
    calls = {"n": 0}

    def op_runner(args, cwd=None):
        calls["n"] += 1
        return (1, "")  # operational failure, no result line

    h = Harness(tmp_path / "run", worker_runner=op_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=2, on_exhausted="block"),
        {}, "spec.v1", _SpecStub())
    assert out is None
    assert calls["n"] == 3, "attempts 0,1,2 -> 3 calls (while <= max_retries=2)"
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "retries_exhausted"
        for e in h.ledger.events())


def test_cognitive_exhaustion_blocks_when_policy_block(tmp_path):
    """on_exhausted=block + cognitive denials exhausting (different denials =
    delta, not stagnation) -> gate_open (retries_exhausted)."""
    seq = [
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"claim_id": "C1"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        }),
        json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"claim_id": "C1"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["Glob"], "usage": {},
        }),
    ]
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0, line)

    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=1, on_exhausted="block"),
        {}, "spec.v1", _SpecStub())
    assert out is None
    assert not any(e.get("kind") == "stagnating" for e in h.ledger.events()), \
        "different denial sets = cognitive delta, not stagnation"
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "retries_exhausted"
        for e in h.ledger.events())


def test_stagnation_exhaustion_blocks_when_policy_block(tmp_path):
    """on_exhausted=block + identical cognitive failure -> stagnation budget
    hit -> gate_open (stagnation_exhausted)."""
    def denial_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"claim_id": "C1"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": ["WebSearch"], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=denial_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=5, on_exhausted="block"),
        {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(e.get("kind") == "stagnating" for e in h.ledger.events())
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "stagnation_exhausted"
        for e in h.ledger.events())


def test_cognitive_sig_normalizes_dict_denials():
    """#14: the worker may return permission_denials as dict objects (not just str);
    frozenset(dict) -> TypeError: unhashable. _cognitive_sig must normalize
    non-str denials to a stable json key (same dict -> same sig)."""
    sig = Harness._cognitive_sig([{"tool": "WebSearch", "reason": "denied"}], conforms=True)
    assert sig is not None and sig[0] == "denials"
    sig2 = Harness._cognitive_sig([{"tool": "WebSearch", "reason": "denied"}], conforms=True)
    assert sig == sig2, "same dict denial -> same sig (stagnation detection stable)"
    # mixed str + dict also must not crash
    sig3 = Harness._cognitive_sig(["WebSearch", {"tool": "Bash"}], conforms=True)
    assert sig3 is not None and sig3[0] == "denials"


def test_max_stagnation_2_gives_third_attempt_before_exhaust(tmp_path):
    """#10: max_stagnation=2 (default) -> a 2nd identical failure does NOT
    exhaust (stagnation_count=1<2, G3 schema-feedback injects); only a 3rd
    identical failure trips stagnation_exhausted. Also exercises #14: the
    dict denial must not crash frozenset across 3 identical dispatches."""
    class _Spec2:
        spec_version_id = "spec.v1"
        max_stagnation = 2
        budget_usd = 5.0
        max_agents = 1000

    def dict_denial_runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": json.dumps({"claim_id": "C1"}),
            "session_id": "s", "total_cost_usd": 0.01,
            "permission_denials": [{"tool": "WebSearch", "reason": "denied"}],
            "usage": {},
        }))

    h = Harness(tmp_path / "run", worker_runner=dict_denial_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=5, on_exhausted="block"),
        {}, "spec.v1", _Spec2())
    assert out is None
    stags = [e for e in h.ledger.events() if e.get("kind") == "stagnating"]
    assert len(stags) == 2, (
        f"max_stagnation=2 -> exactly 2 stagnating events (2nd + 3rd identical "
        f"fail), got {len(stags)}"
    )
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "stagnation_exhausted"
        for e in h.ledger.events())


def test_on_exhausted_degrade_no_gate(tmp_path):
    """on_exhausted=degrade -> exhaustion returns None WITHOUT a Gate (soft)."""
    calls = {"n": 0}

    def op_runner(args, cwd=None):
        calls["n"] += 1
        return (1, "")

    h = Harness(tmp_path / "run", worker_runner=op_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=1, on_exhausted="degrade"),
        {}, "spec.v1", _SpecStub())
    assert out is None
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events()), \
        "degrade must not open a Gate"


# --- F3: on_stagnation cause-level override (stagnation auto-resolves, does
#     not touch other exhausted causes) --------------------------------------

def _stagnation_node(on_stagnation):
    n = _strict_agent_node(max_retries=5, on_exhausted="block")
    n["failure_policy"]["on_stagnation"] = on_stagnation
    return n


def _same_failure_runner(args, cwd=None):
    """Fails with the same signature every time (missing claim_id) -> stagnating
    from the 2nd attempt on."""
    return (0, json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": json.dumps({"foo": "bar"}),
        "session_id": "s", "total_cost_usd": 0.01,
        "permission_denials": [], "usage": {},
    }))


def test_on_stagnation_degrade_skips_gate(tmp_path):
    """F3: on_exhausted=block but on_stagnation=degrade -> stagnation auto-passes
    through without opening a gate (the downstream verify adjudicates), while
    retries_exhausted still blocks."""
    h = Harness(tmp_path / "run", worker_runner=_same_failure_runner)
    out = h._dispatch_with_retry(
        _stagnation_node("degrade"), {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(e.get("kind") == "stagnating" for e in h.ledger.events())
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events()), \
        "on_stagnation=degrade should not open a gate on stagnation"


def test_on_stagnation_absent_falls_back_to_on_exhausted(tmp_path):
    """When on_stagnation is not declared, stagnation still goes through
    on_exhausted (block -> gate) -- behavior unchanged."""
    h = Harness(tmp_path / "run", worker_runner=_same_failure_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=5, on_exhausted="block"),
        {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "stagnation_exhausted"
        for e in h.ledger.events())


def test_on_stagnation_does_not_override_retries_exhausted(tmp_path):
    """on_stagnation only covers stagnation_exhausted; an operational failure
    exhausting (retries_exhausted) still goes through on_exhausted=block to
    open a gate."""
    def op_runner(args, cwd=None):
        return (1, "")  # operational failure, never cognitive
    n = _stagnation_node("degrade")
    n["failure_policy"]["max_retries"] = 1
    h = Harness(tmp_path / "run", worker_runner=op_runner)
    out = h._dispatch_with_retry(n, {}, "spec.v1", _SpecStub())
    assert out is None
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "retries_exhausted"
        for e in h.ledger.events()), \
        "on_stagnation should not affect retries_exhausted gate behavior"



# --- sequence-level regression: the actual relay gap ------------------------

_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_id": {"type": "string"},
        "module": {"type": "string"},
        "excerpt": {"type": "string"},
        "summary": {"type": "string"},
        "severity": {"type": "string"},
    },
    "required": ["claim_id", "module", "excerpt", "summary", "severity"],
}


def _claim(cid, mod):
    return {
        "claim_id": cid, "module": mod, "excerpt": "x",
        "summary": "s", "severity": "info",
    }


def _smoke_spec(n_r2_conforming=True):
    n_r2_out = _claim("C2", "harness") if n_r2_conforming else {}
    # runner closure is built by the caller; nodes here carry distinct prompt
    # markers so the fake runner can route by substring.
    return Spec(
        spec_version_id="spec.v1", parent_spec_id=None, revision=1,
        intent="smoke: parallel->verify->synthesize",
        requirements=[Requirement(
            "R1", "verify node confirms at least one claim survives skeptic",
            "required")],
        boundaries=["read-only"],
        success_evidence=["S1:R1=claim:C1"],
        nodes={
            "n_r1": {
                "type": "agent", "id": "n_r1", "prompt": "REVIEW_HOST_DISPATCH",
                "dispatch": "host", "output_schema": _CLAIM_SCHEMA,
                "allowed_tools": ["Read"], "write_areas": [],
                "acceptance": ["a"], "failure_policy": {
                    "max_retries": 1, "retry_guard": "requires_new_evidence",
                    "on_exhausted": "block"},
            },
            "n_r2": {
                "type": "agent", "id": "n_r2", "prompt": "REVIEW_HARNESS",
                "dispatch": "host", "output_schema": _CLAIM_SCHEMA,
                "allowed_tools": ["Read"], "write_areas": [],
                "acceptance": ["a"], "failure_policy": {
                    "max_retries": 1, "retry_guard": "requires_new_evidence",
                    "on_exhausted": "block"},
            },
            "n_verify": {
                "type": "verify", "id": "n_verify",
                "target": "{{par.validated_output}}", "skeptic_count": 2,
                "skeptic_prompt": "SKEPTIC {{finding}}",
                "survival_rule": "majority_unrefuted", "independent_session": True,
                "output_schema": {
                    "type": "object",
                    "properties": {"survivors": {"type": "array"},
                                   "refuted": {"type": "array"}},
                    "required": ["survivors", "refuted"],
                },
                "verification_policy": "independent",
            },
            "n_report": {
                "type": "synthesize", "id": "n_report",
                "inputs": ["SYNTHESIZE {{n_verify.validated_output}}"],
                "output_schema": {
                    "type": "object",
                    "properties": {"survived_count": {"type": "integer"},
                                   "refuted_count": {"type": "integer"},
                                   "report": {"type": "string"}},
                    "required": ["survived_count", "refuted_count", "report"],
                },
                "acceptance": ["a"],
            },
        },
        control_flow={"type": "sequence",
                      "steps": [["parallel", "n_r1", "n_r2"], "n_verify", "n_report"]},
        decision_trace=[], budget_usd=5.0, max_concurrent=16,
        max_agents=1000, max_stagnation=1,
    ), n_r2_out


def _smoke_runner(n_r2_out):
    def runner(args, cwd=None):
        prompt = args[0] if len(args) > 0 else ""
        if "REVIEW_HOST_DISPATCH" in prompt:
            out = _claim("C1", "host")
        elif "REVIEW_HARNESS" in prompt:
            out = n_r2_out
        elif "SKEPTIC" in prompt:
            out = {"refuted": False, "evidence_ref": "excerpt supports summary"}
        elif "SYNTHESIZE" in prompt:
            n_r2_failed = (n_r2_out == {})
            sc = 1 if n_r2_failed else 2
            out = {"survived_count": sc, "refuted_count": 0,
                   "report": "claims survived"}
        else:
            out = {}
        return (0, _result_line(out))
    return runner


def test_smoke_clean_verified(tmp_path):
    """Regression guard: both reviewers conforming -> verdict VERIFIED via the
    verify->claim:C1 path (the happy path must stay green)."""
    spec, n_r2_out = _smoke_spec(n_r2_conforming=True)
    h = Harness(tmp_path / "run", worker_runner=_smoke_runner(n_r2_out))
    h.run(spec)
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "VERIFIED"
    assert cp["blocked_items"] == []


def test_smoke_one_reviewer_fails_no_hollow_verdict(tmp_path):
    """THE relay gap, end-to-end: n_r2 returns `{}` (non-conforming). Before
    the fix, `{}` flowed through as a finding with claim_id="" and produced a
    HOLLOW verify_verdict{claim_id:"", survived:True}. After the fix, n_r2 is
    blocked (None), verify skips it, and only C1 gets a real verdict -- so the
    verdict is honestly VERIFIED (C1 genuinely survived) with no hollow ""."""
    spec, n_r2_out = _smoke_spec(n_r2_conforming=False)
    h = Harness(tmp_path / "run", worker_runner=_smoke_runner(n_r2_out))
    h.run(spec)
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] == "VERIFIED", \
        "C1 (conforming, survived) legitimately satisfies R1=claim:C1"
    vvs = [e for e in h.ledger.events() if e.get("kind") == "verify_verdict"]
    claim_ids = {e["claim_id"] for e in vvs}
    assert "" not in claim_ids, \
        "n_r2's {} must NOT produce a hollow verify_verdict for claim_id=''"
    assert "C1" in claim_ids


def test_smoke_both_reviewers_fail_honest_unverified(tmp_path):
    """If BOTH reviewers return `{}` (both blocked), no claim survives verify ->
    R1 (claim:C1) is not VERIFIED -> honest UNVERIFIED/PARTIAL, NOT a hollow
    VERIFIED. This is the deepest guarantee: garbage workers can't fake a pass."""
    def runner(args, cwd=None):
        prompt = args[0] if len(args) > 0 else ""
        if "SKEPTIC" in prompt:
            out = {"refuted": False, "evidence_ref": "e"}
        elif "SYNTHESIZE" in prompt:
            out = {"survived_count": 0, "refuted_count": 0, "report": "none"}
        else:
            out = {}  # both reviewers non-conforming
        return (0, _result_line(out))
    spec, _ = _smoke_spec(n_r2_conforming=False)
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.run(spec)
    cp = h.project_checkpoint(spec)
    assert cp["verdict"] in ("UNVERIFIED", "PARTIAL"), \
        f"both reviewers failing must NOT yield VERIFIED, got {cp['verdict']}"
    assert cp["verdict"] != "VERIFIED", \
        "garbage workers must not fake a VERIFIED pass"


# --- observability: raw worker output audited on failure (v1.1 follow-up) ---
#
# The 2026-08-22 real-worker smoke returned PARTIAL with both reviewers
# `conforms:false`, but the journal only said conforms=False -- the worker's
# raw output was NOT recorded (agent_result is logged only on clean success;
# stagnating carried no result_text). That made it impossible to tell whether
# the workers returned free-form prose (the schema was not enforced -> (A),
# v1.1 correct) or a valid JSON _conforms wrongly rejected ((B), a v1.1
# regression). These tests pin the audit: a blocked dispatch and a stagnating
# retry both leave a result_text_preview of WHAT the worker returned.

def test_agent_result_includes_raw_preview(tmp_path):
    """A blocked non-conforming dispatch must audit the worker's raw output in
    the agent_result event, so conforms=False alone never hides the cause."""
    def runner(args, cwd=None):
        return (0, json.dumps({
            "type": "result", "subtype": "success", "num_turns": 1,
            "result": "I think the claim is: the host adapter picks the wrapper",
            "session_id": "s", "total_cost_usd": 0.0,
            "permission_denials": [], "usage": {},
        }))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
        },
        "allowed_tools": [], "write_areas": [],
        "acceptance": ["a"], "failure_policy": {},
    }
    out = h.dispatch_agent(node, upstream_values={}, spec_version_id="spec.v1")
    assert out is None  # non-conforming -> blocked
    ev = next(e for e in h.ledger.events() if e.get("kind") == "agent_result")
    assert "I think the claim is" in ev["payload"]["result_text_preview"], \
        "raw worker output must be audited to diagnose why conforms=False"


def test_stagnating_includes_raw_preview(tmp_path):
    """A schema-failing retry that stagnates must audit the raw worker output
    in the stagnating event."""
    def bad_runner(args, cwd=None):
        return (0, _result_line({"oops": "no claim_id"}))
    h = Harness(tmp_path / "run", worker_runner=bad_runner)
    h._dispatch_with_retry(
        _strict_agent_node(max_retries=1), {}, "spec.v1", _SpecStub())
    stag = next(e for e in h.ledger.events() if e.get("kind") == "stagnating")
    assert "result_text_preview" in stag["payload"]
    assert "oops" in stag["payload"]["result_text_preview"]


def test_preview_truncates_long_output():
    long = "x" * 2000
    p = Harness._preview(long, limit=800)
    assert len(p) < len(long)
    assert p.startswith("x")
    assert "truncated" in p


def test_preview_handles_empty_and_short():
    assert Harness._preview("") == ""
    assert Harness._preview("short") == "short"


# --- v1.1.1 adversarial-verified fixes (2026-08-22) -------------------------
# A 5-dimension find->verify adversarial workflow confirmed 7 bugs the original
# v1.1 tests missed because they only exercised ENDPOINTS of each behavior
# (refute=0 / refute=sc; conforming / totally-empty; same denial / clean). The
# bugs all hide in the intermediate + edge cases. These pin them.

# F1: verify majority is off-by-one -- `refute_votes < (sc/2 + 1)` lets a
# near-majority (or even a tie / unanimous single refute) SURVIVE, defeating
# the declared `majority_unrefuted` ("a strict majority did NOT refute").

def _skeptic_refute_runner(refute_at):
    """Skeptic refutes on the Nth skeptic call (0-indexed) and survives
    otherwise, keyed by call order among SKEPTIC prompts."""
    state = {"n": 0}

    def runner(args, cwd=None):
        prompt = args[0] if len(args) > 0 else ""
        if "SKEPTIC" in prompt:
            i = state["n"]
            state["n"] += 1
            out = {"refuted": i in refute_at, "evidence_ref": "e"}
            return (0, _result_line(out))
        return (0, _result_line({}))
    return runner


def test_verify_majority_kills_two_of_three_refute(tmp_path):
    """sc=3, 2 skeptics refute -> 1 non-refute -> NOT a majority -> KILLED.
    (The `+1` formula wrongly survives this: 2 < 3/2+1=2.5 -> True.)"""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner({0, 1}))
    out = h.run_verify(_verify_node(3),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == [], "2-of-3 refute must kill the claim"
    assert any(f["claim_id"] == "C1" for f in out["refuted"])
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["survived"] is False


def test_verify_tie_kills_claim(tmp_path):
    """sc=2, 1 skeptic refutes (a 1-1 tie) -> 1 non-refute of 2 -> NOT a
    majority -> KILLED. (The `+1` formula survives a tie: 1 < 2/2+1=2.)"""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner({0}))
    out = h.run_verify(_verify_node(2),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == []
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["survived"] is False


def test_verify_unanimous_single_skeptic_refute_kills(tmp_path):
    """sc=1, the sole skeptic refutes -> 0 non-refute -> KILLED. (The `+1`
    formula survives even a unanimous refute: 1 < 1/2+1=1.5.)"""
    h = Harness(tmp_path / "run", worker_runner=_skeptic_refute_runner({0}))
    out = h.run_verify(_verify_node(1),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == []


# F2: integer schema must accept a zero-fractional float (5.0) -- JSON-Schema
# defines an integer as "a number with a zero fractional part".

def test_integer_accepts_zero_fractional_float(tmp_path):
    """json.loads('5.0') -> Python float 5.0 (5.0.is_integer() True), which is a
    valid JSON-Schema integer. Rejecting it wrongly blocks a conforming worker
    and triggers retries/stagnation/Gate."""
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, "{}"))
    ISCH = {"type": "object", "properties": {"count": {"type": "integer"}},
            "required": ["count"]}
    assert h._conforms({"count": 5.0}, ISCH) is True
    assert h._conforms({"count": 0.0}, ISCH) is True


def test_integer_rejects_nonzero_float_and_bool(tmp_path):
    """Regression: a non-integer float (5.5) and a bool (True) stay rejected;
    a bare int (5) still passes."""
    h = Harness(tmp_path / "run", worker_runner=lambda a, cwd=None: (0, "{}"))
    ISCH = {"type": "object", "properties": {"count": {"type": "integer"}},
            "required": ["count"]}
    assert h._conforms({"count": 5.5}, ISCH) is False
    assert h._conforms({"count": True}, ISCH) is False
    assert h._conforms({"count": 5}, ISCH) is True


# F3: unretriable failure in a fan-out (skeptic / parallel / pipeline) must NOT
# open a Gate -- failure localization. Only a TOP-LEVEL agent's unretriable
# failure Gates (honest hard stop). Before the fix one skeptic / one parallel
# branch hitting an auth error forced the whole run BLOCKED.

def _unretriable_runner(args, cwd=None):
    """is_error 'Not logged in' envelope -> classify_retry_class unretriable."""
    return (0, json.dumps({
        "type": "result", "subtype": "success",
        "is_error": True, "result": "Not logged in",
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    }))


def test_skeptic_unretriable_does_not_gate(tmp_path):
    """A skeptic hitting an unretriable (auth) failure counts as a refute vote,
    NOT a Gate that forces the whole run BLOCKED. The verify fan-out is
    failure-tolerant by design."""
    h = Harness(tmp_path / "run", worker_runner=_unretriable_runner)
    out = h.run_verify(_verify_node(2),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == [], "unretriable skeptics -> refute votes -> killed"
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events()), \
        "skeptic unretriable failure must NOT open a Gate"


def test_top_level_agent_unretriable_does_gate(tmp_path):
    """Contrast: a TOP-LEVEL agent (localized=False) hitting unretriable MUST
    open a Gate (honest hard stop -> BLOCKED). The localized suppression applies
    only to fan-out branches, not solo required agents."""
    h = Harness(tmp_path / "run", worker_runner=_unretriable_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=1, on_exhausted="block"),
        {}, "spec.v1", _SpecStub())  # localized defaults False
    assert out is None
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "unretriable_failure"
        for e in h.ledger.events()), \
        "top-level unretriable failure MUST Gate (honest hard stop)"


def test_parallel_branch_unretriable_does_not_gate(tmp_path):
    """A parallel-node body agent hitting unretriable returns None for that
    branch WITHOUT a Gate, so the verdict is not forced BLOCKED."""
    h = Harness(tmp_path / "run", worker_runner=_unretriable_runner)
    node = {
        "type": "parallel", "id": "p", "over": "{{items}}",
        "body": {
            "type": "agent", "id": "b", "prompt": "P", "dispatch": "host",
            "output_schema": {"type": "object"},
            "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
            "failure_policy": {},
        },
    }
    out = h.run_parallel(node, {"items": ["x", "y", "z"]}, "spec.v1")
    assert out == [None, None, None], "each branch soft-returns None"
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events())


def test_pipeline_stage_unretriable_does_not_gate(tmp_path):
    """A pipeline stage hitting unretriable drops that item WITHOUT a Gate."""
    h = Harness(tmp_path / "run", worker_runner=_unretriable_runner)
    node = {
        "type": "pipeline", "id": "pl", "items": "{{items}}",
        "stages": [{
            "type": "agent", "id": "s", "prompt": "P", "dispatch": "host",
            "output_schema": {"type": "object"},
            "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
            "failure_policy": {},
        }],
    }
    out = h.run_pipeline(node, {"items": ["x", "y"]}, "spec.v1")
    assert out == [None, None], "each item dropped (None), no Gate"
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events())


def test_sequence_parallel_step_unretriable_does_not_gate(tmp_path):
    """A sequence-level ["parallel", nid] step dispatches its agents with
    localized=True via _dispatch_with_retry -> _run_agent_with_retry. An
    unretriable failure on such a branch must NOT open a Gate (this is the
    exact path the adversarial pass flagged at the old line 302)."""
    h = Harness(tmp_path / "run", worker_runner=_unretriable_runner)
    out = h._dispatch_with_retry(
        _strict_agent_node(max_retries=1, on_exhausted="block"),
        {}, "spec.v1", _SpecStub(), localized=True)
    assert out is None
    assert not any(e.get("kind") == "gate_open" for e in h.ledger.events()), \
        "parallel-step agent unretriable must NOT Gate (localized)"


# F4: stagnation signature must distinguish DIFFERENT schema failures (a worker
# that changes WHICH field it gets wrong is a cognitive delta, not stagnation),
# and the stagnation streak must RESET on a delta.

def test_different_schema_failures_not_stagnation(tmp_path):
    """Attempt 1 missing claim_id, attempt 2 missing module -> DIFFERENT failure
    keys -> cognitive delta, NOT stagnation. (The coarse ("schema",) sig falsely
    stagnates this.)"""
    seq = [_result_line({"module": "x"}),       # missing claim_id
           _result_line({"claim_id": "C1"})]    # missing module
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return (0, line)

    node = {
        "type": "agent", "id": "n", "prompt": "p", "dispatch": "host",
        "output_schema": _CLAIM_SCHEMA,
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 1, "retry_guard": "requires_new_evidence",
                            "on_exhausted": "block"},
    }
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert not any(e.get("kind") == "stagnating" for e in h.ledger.events()), \
        "different failing fields = cognitive delta, not stagnation"


def test_same_schema_failure_still_stagnates(tmp_path):
    """Regression: the SAME schema failure twice (both missing claim_id) still
    stagnates (no false delta)."""
    def runner(args, cwd=None):
        return (0, _result_line({"module": "x"}))  # always missing claim_id

    node = {
        "type": "agent", "id": "n", "prompt": "p", "dispatch": "host",
        "output_schema": _CLAIM_SCHEMA,
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 2, "retry_guard": "requires_new_evidence",
                            "on_exhausted": "block"},
    }
    spec = _SpecStub()
    spec.max_stagnation = 1
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._dispatch_with_retry(node, {}, "spec.v1", spec)
    assert any(e.get("kind") == "stagnating" for e in h.ledger.events())
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "stagnation_exhausted"
        for e in h.ledger.events())


def test_stagnation_count_resets_on_cognitive_delta(tmp_path):
    """max_stagnation=2, sigs A,A,B,A,A. With reset-on-delta the B breaks the
    streak so count never reaches 2 -> exhaust is retries_exhausted (honest
    budget), NOT stagnation_exhausted. Without the reset, count accumulates to 2
    at the final A,A -> premature stagnation_exhausted (the B delta ignored)."""
    a = _result_line({"module": "x"})        # missing claim_id  (sig A)
    b = _result_line({"claim_id": "C1"})     # missing module     (sig B, different)
    seq = [a, a, b, a, a]
    i = {"n": 0}

    def runner(args, cwd=None):
        line = seq[i["n"] if i["n"] < len(seq) else len(seq) - 1]
        i["n"] += 1
        return (0, line)

    node = {
        "type": "agent", "id": "n", "prompt": "p", "dispatch": "host",
        "output_schema": _CLAIM_SCHEMA,
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {"max_retries": 4, "retry_guard": "requires_new_evidence",
                            "on_exhausted": "block"},
    }
    spec = _SpecStub()
    spec.max_stagnation = 2
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._dispatch_with_retry(node, {}, "spec.v1", spec)
    assert not any(
        e.get("kind") == "gate_open" and e.get("reason") == "stagnation_exhausted"
        for e in h.ledger.events()), \
        "stagnation streak must reset on a cognitive delta; no premature exhaust"
    assert any(
        e.get("kind") == "gate_open" and e.get("reason") == "retries_exhausted"
        for e in h.ledger.events())


# --- v1.2 (a) P1: markdown-fence strip + JSON extraction ---------------------
# The 2026-08-24 relay journal showed workers ignore --json-schema and return
# prose. Pure-prose has no extractable JSON (handled by G3 feedback retry +
# G4 abstain), but workers that *try* to comply often wrap JSON in a markdown
# fence or embed it in prose. _parse_validated must recover those.

def test_parse_pure_json():
    assert Harness._parse_validated('{"claim_id":"C1","x":1}') == \
        {"claim_id": "C1", "x": 1}

def test_parse_markdown_fenced_json():
    assert Harness._parse_validated('```json\n{"claim_id":"C1","x":1}\n```') == \
        {"claim_id": "C1", "x": 1}

def test_parse_bare_fence_json():
    assert Harness._parse_validated('```\n{"claim_id":"C1","x":1}\n```') == \
        {"claim_id": "C1", "x": 1}

def test_parse_json_embedded_in_prose():
    # worker prefaces the JSON with a sentence, as the n_r2 relay nearly did
    assert Harness._parse_validated(
        'Here is the claim: {"claim_id":"C1","x":1} done.') == \
        {"claim_id": "C1", "x": 1}

def test_parse_nested_json_in_prose():
    assert Harness._parse_validated(
        'result {"a":{"b":[1,2]}} tail') == {"a": {"b": [1, 2]}}

def test_parse_no_json_returns_empty():
    # pure prose (the n_r1 / skeptic relay shape) -> no JSON to extract -> {}
    # This is the gap P1 alone cannot close; G3 (feedback retry) + G4 (abstain)
    # own it. P1 must at least not crash and return {}.
    assert Harness._parse_validated('Read the module and submitted a claim.') == {}
    assert Harness._parse_validated('') == {}


# --- v1.2 (b) P3: worker prompt schema directive -----------------------------
# --json-schema is ignored by the worker, but a hard text directive IN the
# prompt (schema + "only JSON, no prose/fence") is read by the worker. This is
# the primary defense against the pure-prose failure mode the relay exposed.

def test_build_prompt_injects_schema_directive():
    node = {"output_schema": {"type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"]}}
    out = Harness._build_worker_prompt(node, "Review the code.")
    # FRONT directive leads, original task text sits in the middle
    assert out.startswith("[OUTPUT FORMAT")
    assert "Review the code." in out
    assert "claim_id" in out              # schema field surfaced to worker
    assert '"type": "object"' in out or '"type":"object"' in out  # schema embedded
    assert "JSON" in out                  # the directive names the format
    # END echo follows the task text (front+end, not end-only)
    assert "REPEAT" in out

def test_build_prompt_no_schema_no_injection():
    node = {}  # no output_schema -> unconstrained -> no directive
    out = Harness._build_worker_prompt(node, "Just talk.")
    assert out == "Just talk."

def test_build_prompt_directive_forbids_prose_and_fence():
    node = {"output_schema": {"type": "object", "required": ["refuted"]}}
    out = Harness._build_worker_prompt(node, "Skeptic task.")
    # the directive must explicitly forbid the two relay failure shapes
    assert "no natural language" in out.lower() or "only" in out.lower()  # forbids natural language
    assert "```" in out or "fence" in out.lower()   # forbids markdown fence


# --- v1.2 (c) P2: schema-error feedback retry ------------------------------
# When the worker returns non-conforming output (pure prose / no JSON), the
# next retry is fed the schema error so it can correct -- this is the backup
# to P3's preventive directive. It composes with the F4 stagnation signature:
# if the corrected output still has the same failure shape, it stagnates
# honestly; a different shape resets the streak.

def _prose_result_line(prose):
    """A worker result whose `result` field is raw prose (not JSON) -- the
    n_r1 / skeptic relay shape."""
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": prose,
        "session_id": "s", "total_cost_usd": 0.0,
        "permission_denials": [], "usage": {},
    })

def test_schema_fail_feedback_retry_then_success(tmp_path):
    calls = {"n": 0}
    def runner(args, cwd=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, _prose_result_line("I read the code and submitted a claim."))
        return (0, _result_line({"claim_id": "C1"}))
    node = _strict_agent_node(max_retries=1)  # 2 attempts
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(node, {}, "spec.v1", _SpecStub())
    assert out is not None, "feedback retry should let the worker correct on attempt 2"
    assert out["validated_output"] == {"claim_id": "C1"}

def test_feedback_retry_injects_schema_error(tmp_path):
    prompts = []
    def runner(args, cwd=None):
        prompts.append(args[0])  # args = [host_adapter, ..., prompt, ...]
        return (0, _prose_result_line("Pure natural language, no JSON."))
    node = _strict_agent_node(max_retries=3)
    spec = _SpecStub(); spec.max_stagnation = 1
    h = Harness(tmp_path / "run", worker_runner=runner)
    h._dispatch_with_retry(node, {}, "spec.v1", spec)
    assert len(prompts) >= 2, "a schema failure must trigger a feedback retry"
    # attempt 1 = base + P3 directive (no error feedback yet)
    assert "did not conform" not in prompts[0] and "last output" not in prompts[0].lower()
    # attempt 2 = base + directive + schema-error feedback
    assert ("did not conform" in prompts[1] or "please fix" in prompts[1].lower() or "schema" in prompts[1].lower()), \
        "the retry prompt must feed back the schema error"

def test_feedback_retry_stagnates_if_still_bad(tmp_path):
    # feedback given, but worker STILL returns the same prose shape ->
    # same schema-fail signature -> honest stagnation (does not loop forever)
    def runner(args, cwd=None):
        return (0, _prose_result_line("Still pure natural language."))
    node = _strict_agent_node(max_retries=4)
    spec = _SpecStub(); spec.max_stagnation = 1
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h._dispatch_with_retry(node, {}, "spec.v1", spec)
    assert out is None
    assert any(e.get("kind") == "stagnating" for e in h.ledger.events())


# --- v1.2 (d) G4: skeptic abstention + false-refutation fix ----------------
# A non-conforming skeptic (schema fail -> None) ABSTAINS rather than refuting.
# majority_unrefuted is now over the CONFORMING skeptics only; abstentions over
# half -> the claim is unverifiable (CHALLENGED), not a false refutation.

def _skeptic_refute(args, cwd=None):
    return (0, _result_line({"refuted": True, "evidence_ref": "e"}))

def _skeptic_prose(args, cwd=None):
    # the relay shape: prose, no JSON -> {} -> schema fail -> None -> abstain
    return (0, _prose_result_line("Summary is accurate, but no JSON returned."))

def test_abstain_majority_unverifiable(tmp_path):
    # sc=3: all 3 skeptics return prose -> 3 abstentions > 1.5 -> unverifiable
    h = Harness(tmp_path / "run", worker_runner=_skeptic_prose)
    out = h.run_verify(_verify_node(3),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == [] and out["refuted"] == []
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["survived"] is False and vv["refuted"] is False
    assert vv["payload"]["abstain_votes"] == 3

def test_partial_abstain_effective_majority_refutes(tmp_path):
    # sc=3: 1 abstain, 1 refute, 1 survive(conforming non-refute).
    # effective=2, refute=1 -> 1 < 1 is False -> NOT survived -> refuted (tie).
    calls = {"n": 0}
    def runner(args, cwd=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, _prose_result_line("Cannot adjudicate."))  # abstain
        if calls["n"] == 2:
            return (0, _result_line({"refuted": True}))    # refute
        return (0, _result_line({"refuted": False}))        # survive
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h.run_verify(_verify_node(3),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert out["survivors"] == []
    assert any(f["claim_id"] == "C1" for f in out["refuted"])
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["payload"]["abstain_votes"] == 1
    assert vv["payload"]["refute_votes"] == 1

def test_abstain_under_half_survives(tmp_path):
    # sc=3: 1 abstain, 0 refute, 2 survive. abstain(1) > 1.5? No ->
    # effective=2, refute=0 < 1 -> survived. Abstention under half is ignored.
    calls = {"n": 0}
    def runner(args, cwd=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, _prose_result_line("Abstain."))  # abstain
        return (0, _result_line({"refuted": False}))    # survive x2
    h = Harness(tmp_path / "run", worker_runner=runner)
    out = h.run_verify(_verify_node(3),
        {"findings": [{"claim_id": "C1", "text": "t"}]}, "spec.v1")
    assert any(f["claim_id"] == "C1" for f in out["survivors"])
    assert out["refuted"] == []
    vv = next(e for e in h.ledger.events() if e.get("kind") == "verify_verdict")
    assert vv["survived"] is True
    assert vv["payload"]["abstain_votes"] == 1


# --- v1.3 agent-verdict: verdict_field -> agent_verdict event -------------
# An agent node declaring verdict_field emits an agent_verdict event on clean
# conform, so success_evidence can bind R via 'node:<id>' (like verify's claim:).

def test_agent_verdict_field_emits_agent_verdict(tmp_path):
    """An agent node declaring verdict_field emits an agent_verdict event on
    clean conform, so success_evidence can bind R via 'node:<id>'."""
    def runner(args, cwd=None):
        return (0, _result_line({"verdict": "VERIFIED", "issues": []}))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n5", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object",
                          "properties": {"verdict": {"type": "string"}},
                          "required": ["verdict"]},
        "verdict_field": "verdict",
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {},
    }
    h.dispatch_agent(node, {}, "spec.v1")
    avs = [e for e in h.ledger.events() if e.get("kind") == "agent_verdict"]
    assert len(avs) == 1
    assert avs[0]["node_id"] == "n5"
    assert avs[0]["verdict"] == "VERIFIED"


def test_agent_no_verdict_field_no_agent_verdict(tmp_path):
    """An agent WITHOUT verdict_field does not emit agent_verdict."""
    def runner(args, cwd=None):
        return (0, _result_line({"status": "ok"}))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object",
                          "properties": {"status": {"type": "string"}},
                          "required": ["status"]},
        "allowed_tools": [], "write_areas": [], "acceptance": ["a"],
        "failure_policy": {},
    }
    h.dispatch_agent(node, {}, "spec.v1")
    assert not any(e.get("kind") == "agent_verdict" for e in h.ledger.events())


# --- v1.3.2 check_gate wired into dispatch (was dead code) ---------------
# check_gate existed but was never called -- a risk:high / secrets / delete
# node dispatched the worker anyway. Wire it BEFORE the worker runs.

def test_high_risk_agent_gates_before_dispatch(tmp_path):
    calls = []
    def runner(args, cwd=None):
        calls.append(1)
        return (0, _result_line({"x": 1}))
    h = Harness(tmp_path / "run", worker_runner=runner)
    node = {"type":"agent","id":"n1","prompt":"p","dispatch":"host",
            "output_schema":{"type":"object"},"allowed_tools":[],
            "write_areas":[],"acceptance":["a"],"failure_policy":{},
            "risk":"high"}
    out = h.dispatch_agent(node, {}, "spec.v1")
    assert out is None
    assert any(e.get("kind")=="gate_open" for e in h.ledger.events()), \
        "risk:high node must open a Gate"
    assert calls == [], "risk:high node must NOT dispatch the worker"


def test_secrets_write_area_gates(tmp_path):
    def runner(args, cwd=None):
        return (0, _result_line({"x":1}))
    h = Harness(tmp_path/"run", worker_runner=runner)
    node = {"type":"agent","id":"n1","prompt":"p","dispatch":"host",
            "output_schema":{"type":"object"},"allowed_tools":[],
            "write_areas":["secrets/**"],"acceptance":["a"],"failure_policy":{}}
    out = h.dispatch_agent(node, {}, "spec.v1")
    assert out is None
    assert any(e.get("kind")=="gate_open" for e in h.ledger.events())


def test_normal_node_not_gated(tmp_path):
    def runner(args, cwd=None):
        return (0, _result_line({"x":1}))
    h = Harness(tmp_path/"run", worker_runner=runner)
    node = {"type":"agent","id":"n1","prompt":"p","dispatch":"host",
            "output_schema":{"type":"object"},"allowed_tools":[],
            "write_areas":["src/**"],"acceptance":["a"],"failure_policy":{}}
    out = h.dispatch_agent(node, {}, "spec.v1")
    assert out is not None
    assert not any(e.get("kind")=="gate_open" for e in h.ledger.events())
