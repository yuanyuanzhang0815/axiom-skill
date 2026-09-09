import pytest

from axiom.cli import _check_run_dir_nesting, _cross_task_contaminating


def test_normal_run_dir_not_flagged(tmp_path):
    # a fresh run-dir whose ancestors hold no events.jsonl -> OK
    _check_run_dir_nesting(str(tmp_path / ".axiom" / "run"))


def test_resume_same_run_dir_not_flagged(tmp_path):
    # resume re-runs the SAME run-dir, which legitimately holds events.jsonl;
    # only ANCESTORS are checked, so the run-dir itself must NOT trigger.
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "events.jsonl").write_text("")
    _check_run_dir_nesting(str(rd))


def test_nested_run_dir_refused(tmp_path):
    # outer run-dir already ran (holds events.jsonl); a worker re-invoking
    # axiom from inside its cwd=run-dir creates run-dir/.axiom/run ->
    # nested inside an existing run-dir -> refuse (the docx-review relay bug).
    outer = tmp_path / "run"
    outer.mkdir()
    (outer / "events.jsonl").write_text('{"kind":"genesis"}\n')
    nested = outer / ".axiom" / "run"
    with pytest.raises(ValueError, match="nested"):
        _check_run_dir_nesting(str(nested))


def test_run_manifest_marker_also_triggers(tmp_path):
    # a sealed run-dir writes run_manifest.json; that marker also proves an
    # ancestor is an existing run-dir.
    outer = tmp_path / "run"
    outer.mkdir()
    (outer / "run_manifest.json").write_text("{}")
    with pytest.raises(ValueError):
        _check_run_dir_nesting(str(outer / ".axiom" / "run"))


# --- cross-task run-dir contamination guard (friction #4) ---


def test_clean_run_dir_not_contaminating():
    # no existing events -> clean
    assert _cross_task_contaminating([], "spec.v1", None) is None


def test_same_svid_reappend_not_contaminating():
    # re-run of the same spec -> same svid -> allowed (NOTE, not refuse)
    events = [{"spec_version_id": "spec.v1"}]
    assert _cross_task_contaminating(events, "spec.v1", None) is None


def test_revision_resume_not_contaminating():
    # spec.v2 (parent=spec.v1) resuming a run-dir with spec.v1 events -> allowed
    events = [{"spec_version_id": "spec.v1"}]
    assert _cross_task_contaminating(events, "spec.v2", "spec.v1") is None


def test_cross_task_contaminating_refused():
    # fresh task (parent=null) hitting a run-dir with a DIFFERENT task's events
    # (a real relay's task inheriting model-usage events) -> contaminating set
    events = [{"spec_version_id": "spec.v2"}]  # old task
    got = _cross_task_contaminating(events, "spec.v1", None)  # new task
    assert got == {"spec.v2"}


def test_cross_task_mixed_svids_reports_only_contaminating():
    # run-dir has same-task + different-task events -> only the different one
    events = [{"spec_version_id": "spec.v1"}, {"spec_version_id": "spec.vother"}]
    got = _cross_task_contaminating(events, "spec.v1", None)
    assert got == {"spec.vother"}
