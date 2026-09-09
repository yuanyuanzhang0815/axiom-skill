"""Tests for host_adapter's pi-NDJSON parser, esp. retry_class classification.

The classifier must route retriable infra failures (rate-limit / 429 /
Throttling / connection) to "operational" so the harness backoff-retries,
not burn max_retries as if the worker's reasoning were defective. Friction:
alibaba-cloud 429 "Throttling" was misrouted cognitive, costing 94s of
wasted identical retries on the Devin research run.
"""
import os, sys
from pathlib import Path

# host_adapter lives in scripts/, not a package; import by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import host_adapter  # noqa: E402
from host_adapter import _parse_pi_json  # noqa: E402


def _msg_end(text="", stop=None, error=None, cost_total=0.0):
    """Build one pi message_end NDJSON line."""
    m = {"role": "assistant", "content": [{"type": "text", "text": text}],
         "usage": {"cost": {"total": cost_total}}}
    if stop:
        m["stopReason"] = stop
    if error:
        m["errorMessage"] = error
    import json
    return json.dumps({"type": "message_end", "message": m})


def test_429_throttling_is_operational():
    """alibaba-cloud 429 returns errorMessage containing 'Throttling'."""
    line = _msg_end(stop="error", error="Throttling Request too fast")
    r = _parse_pi_json(line)
    # r = (text, cost, turns, exit_code, retry_class, error)
    assert r[4] == "operational", f"429 Throttling misrouted as {r[4]}"


def test_429_numeric_is_operational():
    line = _msg_end(stop="error", error="HTTP 429 Too Many Requests")
    assert _parse_pi_json(line)[4] == "operational"


def test_connection_error_is_operational():
    line = _msg_end(stop="error", error="Connection refused to upstream")
    assert _parse_pi_json(line)[4] == "operational"


def test_rate_limit_is_operational():
    line = _msg_end(stop="error", error="RateLimit exceeded")
    assert _parse_pi_json(line)[4] == "operational"


def test_worker_reasoning_error_stays_cognitive():
    """A non-infra stop (worker produced bad output) stays cognitive."""
    line = _msg_end(text="some answer", stop="max_tokens")
    r = _parse_pi_json(line)
    assert r[4] == "cognitive"


def test_stop_error_alone_is_operational():
    """stop == 'error' → operational regardless of error text (existing behavior)."""
    line = _msg_end(stop="error", error="something undefined broke")
    assert _parse_pi_json(line)[4] == "operational"


def test_clean_success_is_not_classified_for_retry():
    """A clean assistant message has no error → retry_class cognitive, but ok=True."""
    line = _msg_end(text="answer", stop="end_turn", cost_total=0.01)
    r = _parse_pi_json(line)
    assert r[3] == 0  # exit_code ok


def _fake_run(captured):
    """Build a fake subprocess.run that records kwargs into `captured`."""
    class _P:
        returncode = 0
        stdout = '{"verdict":"VERIFIED"}'
        stderr = ""
    def _run(argv, **kwargs):
        captured.update(kwargs)
        return _P()
    return _run


def test_prompt_via_stdin_passes_input(monkeypatch):
    """prompt_via='stdin' must pipe prompt as input= to subprocess.

    Friction: --prompt-via stdin was a no-op flag — serve() received it via
    argparse but never passed it to _run_agent_cmd (the function signature
    lacked the param), so subprocess.run got no input= and `claude -p` saw
    empty stdin → 'Input must be provided through stdin' exit=1 every dispatch
    → conform-fail → stagnating on a real relay's verify-only run."""
    captured = {}
    monkeypatch.setattr(host_adapter.subprocess, "run", _fake_run(captured))
    host_adapter._run_agent_cmd("claude -p", "do the thing", None, prompt_via="stdin")
    assert captured.get("input") == "do the thing", \
        "stdin mode must pass input=prompt to subprocess.run"


def test_prompt_via_arg_does_not_set_input(monkeypatch):
    """Default arg mode ({prompt} substituted into --agent-cmd) must NOT set input=."""
    captured = {}
    monkeypatch.setattr(host_adapter.subprocess, "run", _fake_run(captured))
    host_adapter._run_agent_cmd("echo {prompt}", "hi", None)
    assert "input" not in captured, "arg mode should not set input="


def test_cc_preset_sets_agent_cmd_with_settings(monkeypatch, tmp_path):
    """--cc preset must set agent_cmd='claude -p --settings <tmpfile>' and
    prompt_via='stdin' so the non-interactive run reuses the interactive cc
    session's provider config (direct provider connection), not ~/.claude/settings.json (which
    502s under cc switch). Friction: claude -p was deemed unusable under cc
    switch and pi was used instead — the fix is to reuse the launcher tmpfile."""
    monkeypatch.setattr(host_adapter, "_find_cc_switch_settings",
                        lambda: "/tmp/fake_cc_switch.json")
    captured = {}

    def fake_serve(run_dir, agent_cmd=None, prompt_via=None, **kw):
        captured["agent_cmd"] = agent_cmd
        captured["prompt_via"] = prompt_via
        return 0
    monkeypatch.setattr(host_adapter, "serve", fake_serve)
    rc = host_adapter.main(["--cc", "--axiom-pid", "999999",
                            "--run-dir", str(tmp_path)])
    assert rc == 0
    assert captured["agent_cmd"] == "claude -p --settings /tmp/fake_cc_switch.json", \
        f"--cc must build agent_cmd with the tmpfile, got {captured['agent_cmd']}"
    assert captured["prompt_via"] == "stdin", \
        "--cc must pipe prompt via stdin (claude -p waits 3s otherwise)"


def test_find_cc_switch_settings_none_when_no_proxy(monkeypatch):
    """No :15721 listener (cc-switch not running) → returns None, and --cc
    falls back to bare `claude -p` rather than crashing."""
    class _NoLsof:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(host_adapter.subprocess, "run",
                        lambda *a, **k: _NoLsof())
    assert host_adapter._find_cc_switch_settings() is None
