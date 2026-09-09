"""C2: a verify node may grant its skeptics allowed_tools / read_areas so the
skeptic can ground its verdict in the actual evidence (e.g. Read the
screenshot), not just the finding's prose. Default unchanged."""
import json
from axiom.harness import Harness


def _env(result_str='{"refuted": false, "evidence_ref": "ok"}', cost=0.1):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _verify_node(allowed_tools=None, read_areas=None):
    n = {
        "type": "verify", "id": "n_v", "target": "{{findings}}",
        "skeptic_count": 1, "skeptic_prompt": "Refute finding {{finding}}.",
        "survival_rule": "majority_unrefuted", "independent_session": True,
        "output_schema": {"type": "object"}, "verification_policy": "independent",
    }
    if allowed_tools is not None:
        n["allowed_tools"] = allowed_tools
    if read_areas is not None:
        n["read_areas"] = read_areas
    return n


def test_skeptic_inherits_verify_allowed_tools(tmp_path):
    seen = []
    def runner(args, cwd=None):
        seen.append(args)
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.run_verify(_verify_node(allowed_tools=["Read"], read_areas=["/tmp/**"]),
                {"findings": [{"claim_id": "C1"}]}, "spec.v1", None)
    assert seen, "skeptic was not dispatched"
    # the skeptic dispatch must carry --allowedTools Read (inherited)
    flat = [a for run in seen for a in run]
    assert "Read" in flat
    assert "--allowedTools" in flat


def test_skeptic_default_tools_when_verify_undeclared(tmp_path):
    seen = []
    def runner(args, cwd=None):
        seen.append(args)
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.run_verify(_verify_node(), {"findings": [{"claim_id": "C1"}]}, "spec.v1", None)
    flat = [a for run in seen for a in run]
    assert "WebSearch" in flat and "WebFetch" in flat


def test_skeptic_read_areas_inherited_for_audit(tmp_path):
    # read_areas is a spec/audit concept; it must travel onto the skeptic node
    # so the journal records what the skeptic was entitled to read.
    captured = {}
    def runner(args, cwd=None):
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    # patch dispatch_agent to capture the skeptic node shape
    orig = h.dispatch_agent
    def cap(node, values, svid, spec=None, localized=False):
        captured["read_areas"] = node.get("read_areas")
        captured["allowed_tools"] = node.get("allowed_tools")
        return orig(node, values, svid, spec, localized=localized)
    h.dispatch_agent = cap
    h.run_verify(_verify_node(allowed_tools=["Read"], read_areas=["/tmp/**"]),
                {"findings": [{"claim_id": "C1"}]}, "spec.v1", None)
    assert captured["read_areas"] == ["/tmp/**"]
    assert captured["allowed_tools"] == ["Read"]
