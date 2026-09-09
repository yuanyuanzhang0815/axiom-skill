import pytest


@pytest.fixture(autouse=True)
def _axiom_global_pointer_isolated(monkeypatch, tmp_path):
    """Redirect axiom's global active-run pointer (~/.axiom/active.json) to a
    tmp_path-scoped path so the unit suite never clobbers the user's real
    global pointer.

    `axiom run` / `resume` call `_write_active`, which writes the global mirror
    to `_active_global_path()` (real path: ~/.axiom/active.json). Without this
    fixture every test that calls `main(["run", ...])` overwrites the user's
    real pointer with a tmp run-dir that vanishes after the session.
    """
    monkeypatch.setattr("axiom.cli._active_global_path",
                        lambda: tmp_path / "global_active.json")


# Process-level env the CLI mutates mid-test. cmd_run / _resume_backend set
# these with os.environ[...] = ... directly (NOT via monkeypatch), so the
# value LEAKS into later tests in the same worker process. The toxic case:
# AXIOM_RUNTIME=host (set when resolving a pi/cc backend) routes
# dispatch module into the file-protocol poller, which ignores injected
# fake runners and polls forever -> a later harness test hangs instead of
# failing (review10: test_backend_entry's leak survived to
# test_review8_upgrade_compat when no main(['run']) intervened to reset it).
_ENV_TO_RESTORE = ("AXIOM_RUNTIME", "AXIOM_RUN_DIR")


@pytest.fixture(autouse=True)
def _axiom_env_isolated():
    """Snapshot + restore axiom's process-env knobs around every test.

    Default AXIOM_RUNTIME=runner so tests that go through `main(['run'])` with
    an injected fake _default_runner route to the runner path (not the host
    file protocol, which would spawn a real host_adapter and bypass the fake).
    Tests that exercise the host file protocol set AXIOM_RUNTIME=host explicitly.
    """
    import os
    saved = {k: os.environ.get(k) for k in _ENV_TO_RESTORE}
    os.environ["AXIOM_RUNTIME"] = "runner"
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
