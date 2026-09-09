"""R9-1 (review9 P0): host_adapter backend CONTRACT tests.

These pin the boundary every backend must honor, regardless of agent flavor
(pi / cc / raw). A contract violation here is what silently flipped a failed
dispatch into a clean success (review9: cost under-reported, real exit code
swallowed). The harness can only enforce budget/failure semantics as well as
the adapter reports them.

Contract covered:
  1. multi-turn cost ACCUMULATES (not last-message-wins)
  2. cost unknown is represented honestly (cost_known=False, not 0.0-as-free)
  3. real process exit code participates (content success cannot launder a
     crashed process)
  4. non-zero exit without parseable output is a failure
  5. timeout -> exit 124, operational (retriable), no fake success
  6. output truncation / malformed stream -> not a clean success
  7. process killed by signal -> failure, operational
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import host_adapter  # noqa: E402
from host_adapter import _parse_pi_json, _run_agent_cmd  # noqa: E402


def _pi_line(text="", stop=None, error=None, cost_total=None):
    m = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if cost_total is not None:
        m["usage"] = {"cost": {"total": cost_total}}
    if stop:
        m["stopReason"] = stop
    if error:
        m["errorMessage"] = error
    return json.dumps({"type": "message_end", "message": m})


# --- 1. multi-turn cost accumulation --------------------------------------

def test_multi_message_cost_accumulates():
    """$0.15 + $0.25 across two assistant messages must total $0.40."""
    out = "\n".join([
        _pi_line(text="a", cost_total=0.15),
        _pi_line(text="b", cost_total=0.25),
    ])
    _, cost, _, _, _, _, cost_known, _ = _parse_pi_json(out)
    assert abs(cost - 0.40) < 1e-9, f"multi-turn cost {cost}, expected 0.40"
    assert cost_known is True


def test_turn_start_and_message_end_not_double_counted():
    """Only assistant message_end usage counts; turn-level dupes must not
    double the bill."""
    msg = _pi_line(text="x", cost_total=0.10)
    out = "\n".join([
        json.dumps({"type": "turn_start"}),
        msg,
        json.dumps({"type": "turn_end",
                    "message": {"role": "assistant",  # dup, must be ignored
                                "content": [{"type": "text", "text": "x"}],
                                "usage": {"cost": {"total": 0.10}}}}),
    ])
    _, cost, turns, _, _, _, _, _ = _parse_pi_json(out)
    assert abs(cost - 0.10) < 1e-9, f"turn_end dup double-counted: {cost}"
    assert turns == 1


# --- 2. cost unknown honesty ----------------------------------------------

def test_no_cost_reported_is_unknown_not_zero():
    """A stream with no usage.cost must say cost_known=False, so the budget
    ledger does not read 'unknown' as 'free'."""
    out = _pi_line(text="answer", stop="end_turn")  # no usage block
    _, cost, _, ec, _, _, cost_known, _ = _parse_pi_json(out)
    assert ec == 0
    assert cost_known is False, "unreported cost must NOT masquerade as 0.0"


def test_raw_parse_mode_never_claims_cost(tmp_path):
    """Raw (non-pi) backends have no cost channel -> cost_known=False."""
    script = tmp_path / "ok.sh"
    script.write_text("echo '{\"x\":1}'")
    _, ec, _, cost, _, _, cost_known, _ = _run_agent_cmd(
        f"bash {script}", "p", None, parse="raw")
    assert ec == 0 and cost_known is False and cost == 0.0


# --- 3. real exit code participates (pi-json path) -------------------------

def test_real_exit_code_overrides_success_looking_content(tmp_path):
    """Process exits 7 AFTER emitting a success-looking final message ->
    result must be failure (exit 7), operational, not a clean success."""
    script = tmp_path / "die.sh"
    script.write_text(
        f"printf '%s' '{_pi_line(text=chr(34)+'x'+chr(34)+':1', stop='stop', cost_total=0.1)}'\n"
        "exit 7\n")
    text, ec, err, _, _, rc, _, _ = _run_agent_cmd(
        f"bash {script}", "p", None, parse="pi-json")
    assert ec == 7, f"exit {ec}: content success laundered a crashed process"
    assert rc == "operational", f"crashed process misrouted as {rc}"
    assert err and "7" in err


def test_nonzero_exit_with_no_parseable_output_is_failure(tmp_path):
    """Process exits non-zero and emits no assistant message -> failure."""
    script = tmp_path / "silent_die.sh"
    script.write_text("echo 'garbage not json'\nexit 3\n")
    text, ec, err, _, _, rc, _, _ = _run_agent_cmd(
        f"bash {script}", "p", None, parse="pi-json")
    assert ec == 3
    assert not text


def test_signal_kill_is_failure_operational(tmp_path):
    """SIGKILL mid-stream (real exit -9) must be a failure, not a success."""
    script = tmp_path / "killed.sh"
    script.write_text("kill -9 $$\n")
    text, ec, err, _, _, rc, _, _ = _run_agent_cmd(
        f"bash {script}", "p", None, parse="pi-json")
    assert ec != 0, f"SIGKILLed process reported success (exit {ec})"
    assert rc == "operational"


# --- 5. timeout -------------------------------------------------------------

def test_timeout_is_operational_124(monkeypatch):
    """AXIOM_HOST_TIMEOUT exceeded -> exit 124, operational (retriable),
    no fabricated result text."""
    def _timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
    monkeypatch.setattr(host_adapter.subprocess, "run", _timeout)
    text, ec, err, cost, turns, rc, cost_known, _ = _run_agent_cmd(
        "sleep 999", "p", None, parse="pi-json")
    assert ec == 124 and rc == "operational" and not text
    assert cost_known is False


# --- 6. output truncation / malformed stream --------------------------------

def test_truncated_mid_message_is_not_clean_success():
    """A stream cut off mid-message (no stopReason, dangling text) -> the
    content gate must not call it a clean success."""
    out = _pi_line(text="partial answer with no stop")  # stop=None
    _, _, _, ec, _, _, _, _ = _parse_pi_json(out)
    assert ec == 1, "no stopReason (truncated stream) must not be ok"


def test_malformed_json_lines_are_skipped_not_fatal():
    """Garbage lines interleaved with one good message -> parse survives."""
    out = "\n".join([
        "not json at all",
        _pi_line(text='{"x":1}', stop="stop", cost_total=0.02),
        "{broken json",
    ])
    text, cost, _, ec, _, _, cost_known, _ = _parse_pi_json(out)
    assert ec == 0 and text == '{"x":1}' and abs(cost - 0.02) < 1e-9
    assert cost_known is True


# --- serve() payload carries cost_known to the harness ----------------------

def test_serve_writes_cost_known_field(tmp_path):
    """The dispatch_res payload must include cost_known so the harness can
    distinguish 'free' from 'unknown' (P0 cost-honesty end of the wire)."""
    import threading, time
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "dispatch_req_t1.json").write_text(json.dumps(
        {"prompt": "p", "schema": {}, "request_id": "t1"}))

    def kill_soon():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=kill_soon, daemon=True).start()

    host_adapter.serve(str(run_dir), mock_result_json='{"x": 1}',
                       once=True, poll_interval=0.05)
    res = json.loads((run_dir / "dispatch_res_t1.json").read_text())
    assert "cost_known" in res, "dispatch_res must carry cost_known"
    assert res["cost_known"] is True  # mock cost is definitional
