"""B3: worker file outputs declared by write_areas are content-addressed
(sha256) into run_dir/artifacts/ and pinned via artifact_refs in an
artifact_write event. Read-only scouts produce nothing."""
import json
import hashlib
from axiom.harness import Harness


def _env(result_str='{"x": 1}', cost=0.01):
    return json.dumps({
        "type": "result", "subtype": "success", "num_turns": 1,
        "result": result_str, "session_id": "s", "total_cost_usd": cost,
        "permission_denials": [], "usage": {},
    })


def _agent(write_areas):
    return {
        "type": "agent", "id": "n1", "prompt": "p", "dispatch": "host",
        "output_schema": {"type": "object"}, "allowed_tools": [],
        "write_areas": write_areas, "acceptance": ["a"], "failure_policy": {},
    }


def test_artifact_pinned_on_success(tmp_path):
    def runner(args, cwd=None):
        # worker writes a file into its cwd (project_root, not run_dir)
        (tmp_path / "out").mkdir(parents=True, exist_ok=True)
        (tmp_path / "out" / "hello.txt").write_text("hi", encoding="utf-8")
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=tmp_path)
    out = h.dispatch_agent(_agent(["out/*.txt"]), {}, "spec.v1")
    assert out is not None
    evs = [e for e in h.ledger.events() if e.get("kind") == "artifact_write"]
    assert len(evs) == 1
    refs = evs[0]["payload"]["artifact_refs"]
    expected = "sha256:" + hashlib.sha256(b"hi").hexdigest()
    assert expected in refs
    # the pinned artifact exists on disk with the right content
    assert (tmp_path / "run" / "artifacts" / expected).read_bytes() == b"hi"


def test_no_artifact_for_readonly_scout(tmp_path):
    def runner(args, cwd=None):
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.dispatch_agent(_agent([]), {}, "spec.v1")
    assert not any(e.get("kind") == "artifact_write" for e in h.ledger.events())
    assert not (tmp_path / "run" / "artifacts").exists()


def test_no_artifact_on_schema_failure(tmp_path):
    def runner(args, cwd=None):
        return (0, _env("not json at all"))  # prose -> non-conforming
    h = Harness(tmp_path / "run", worker_runner=runner)
    h.dispatch_agent(_agent(["out/*.txt"]), {}, "spec.v1")
    assert not any(e.get("kind") == "artifact_write" for e in h.ledger.events())


def test_artifact_dedup_same_content(tmp_path):
    def runner(args, cwd=None):
        d = tmp_path / "out"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.txt").write_text("same")
        (d / "b.txt").write_text("same")
        return (0, _env())
    h = Harness(tmp_path / "run", worker_runner=runner, project_root=tmp_path)
    h.dispatch_agent(_agent(["out/*.txt"]), {}, "spec.v1")
    ev = [e for e in h.ledger.events() if e.get("kind") == "artifact_write"][0]
    # two files, same content -> one dedup'd ref
    assert len(ev["payload"]["artifact_refs"]) == 1
