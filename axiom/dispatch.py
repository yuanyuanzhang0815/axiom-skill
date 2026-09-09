"""axiom dispatch layer — runtime-agnostic worker dispatch.

axiom is a control layer, not an execution layer. Workers are dispatched by
an Agent Runtime that fulfills the axiom worker protocol; this module is the
routing seam between the harness and that runtime.

Two paths:

- `dispatch_via_host` (file protocol) — the pi + cc backends. axiom writes a
  `dispatch_req_{rid}.json` request, the host adapter (`scripts/host_adapter.py
  --pi|--cc`) picks it up, runs the worker, and writes `dispatch_res_{rid}.json`.
  axiom polls for the result. This is the production path.

- `run_via_runner` (injected runner) — test / direct-subprocess mode. Calls an
  injected `runner(args, cwd) -> (exit_code, stdout)` and parses the result
  envelope from stdout. Used by the unit-test suite (fake runners) and any
  future direct-subprocess backend.

Routing: `AXIOM_RUNTIME=host` (set by cli for pi/cc real runs) -> file protocol;
anything else -> `run_via_runner`. The default is the runner path so the test
suite (which injects fakes without setting the env) works unchanged.

`DispatchResult` is the canonical worker-result envelope shared by BOTH paths
and by the host adapter (one serialization on both ends of the file protocol).
"""
from __future__ import annotations
import glob
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

Runner = Callable[[list[str], "str | None"], "tuple[int, str]"]


@dataclass
class DispatchResult:
    """Canonical worker-result envelope.

    Produced by every dispatch path and by the host adapter (which writes this
    shape to `dispatch_res_{rid}.json`). `to_dict`/`from_dict` are the single
    serialization for both ends of the file protocol so a field can never
    silently evaporate at an adapter boundary.
    """
    session_id: str
    result_text: str
    num_turns: int
    cost_usd: float
    permission_denials: list[str]
    exit_code: int
    retry_class: Literal["cognitive", "operational", "unretriable"]
    # Carry the envelope's is_error flag through so _is_clean_attempt can reject
    # envelope failures: a worker can return exit_code=0 with is_error=true
    # (e.g. an auth failure) — retry_class routes it, but a conforming JSON body
    # with verdict:VERIFIED would otherwise pass the success gate because
    # exit_code==0.
    is_error: bool = False
    # Cost honesty. False = the backend reported NO cost (cost_usd is a 0.0
    # placeholder, not a measurement). Budget accounting must not read an
    # unknown cost as free; the timeout/exception paths never report cost ->
    # False.
    cost_known: bool = True
    # Provider-agnostic usage signal (token total). Some providers price cost at
    # 0 but still meter tokens; 0 means 'not reported'.
    tokens_total: int = 0

    # Protocol version stamp so the reader can reject a result written by an
    # incompatible future shape instead of misreading it.
    PROTOCOL = 1

    def to_dict(self) -> dict:
        return {
            "protocol": self.PROTOCOL,
            "session_id": self.session_id,
            "result_text": self.result_text,
            "num_turns": self.num_turns,
            "cost_usd": self.cost_usd,
            "permission_denials": list(self.permission_denials),
            "exit_code": self.exit_code,
            "retry_class": self.retry_class,
            "is_error": self.is_error,
            "cost_known": self.cost_known,
            "tokens_total": self.tokens_total,
        }

    @classmethod
    def from_dict(cls, res: dict) -> "DispatchResult":
        """Reader side of the canonical relay protocol."""
        return cls(
            session_id=res.get("session_id", ""),
            result_text=res.get("result_text", ""),
            num_turns=res.get("num_turns", 0),
            cost_usd=res.get("cost_usd", 0.0),
            permission_denials=res.get("permission_denials", []),
            exit_code=res.get("exit_code", 0),
            retry_class=res.get("retry_class", "cognitive"),
            is_error=bool(res.get("is_error", False)),
            cost_known=bool(res.get("cost_known", True)),
            tokens_total=int(res.get("tokens_total", 0)),
        )


def classify_retry_class(
    exit_code: int,
    stdout: str,
    result: "dict | None",
    http_status: "int | None" = None,
) -> str:
    """Classify a worker outcome for retry policy (provider-agnostic).

    - unretriable: 401/403 (auth/env) OR an envelope is_error with an
      auth/login message -> Gate, do not retry. (A backend may return exit 0
      with is_error=true and an auth message.)
    - operational: 429/timeout/crash with no result -> backoff, retryable.
    - cognitive: completed run; if permission_denials, needs new
      evidence/strategy.
    """
    if http_status in (401, 403):
        return "unretriable"
    if http_status == 429:
        return "operational"
    if result and result.get("is_error"):
        msg = str(result.get("result", "")).lower()
        if any(s in msg for s in (
            "not logged in", "login", "unauthor", "auth", "401", "api key")):
            return "unretriable"
        if any(s in msg for s in (
            "unable to connect", "connectionrefused", "connection refused",
            "econnrefused", "timeout", "temporarily")):
            return "operational"  # transient infra/network -> backoff retry
        return "cognitive"  # other in-result errors -> retryable w/ new strategy
    if exit_code != 0 and result is None:
        return "operational"
    if result and result.get("permission_denials"):
        return "cognitive"
    return "cognitive"


def _real_runner(args: list[str], cwd: "str | None" = None) -> "tuple[int, str]":
    """Generic subprocess runner with a stall heartbeat.

    Popen + dual-drain so a worker hung at a tool_use is killed and retried
    (operational) instead of blocking the dispatch thread. stdout carries the
    result JSON; stderr carries logs. Both update last_activity so a
    totally-silent worker is killed. stdin=DEVNULL yields immediate EOF in
    every environment (interactive AND non-interactive).

    Used as the default runner for the direct-subprocess path; the production
    pi/cc backends do NOT call this (they route through `dispatch_via_host`).
    """
    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except (FileNotFoundError, PermissionError):
        # binary missing / not executable -> operational failure, not a crash
        return (127, "")

    last_activity = [time.monotonic()]
    stdout_buf: list[str] = []

    def _drain(stream, buf, track):
        try:
            for line in stream:
                buf.append(line)
                if track is not None:
                    track[0] = time.monotonic()
        finally:
            stream.close()

    t_out = threading.Thread(
        target=_drain, args=(proc.stdout, stdout_buf, last_activity), daemon=True)
    t_err = threading.Thread(
        target=_drain, args=(proc.stderr, [], last_activity), daemon=True)
    t_out.start()
    t_err.start()

    stall_s = float(os.environ.get("AXIOM_STALL_SECONDS", "300"))
    hard_s = float(os.environ.get("AXIOM_HARD_TIMEOUT", "1800"))
    start = time.monotonic()
    killed = False
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now - last_activity[0] > stall_s:
            proc.kill()
            killed = True
            break
        if now - start > hard_s:
            proc.kill()
            killed = True
            break
        time.sleep(0.5)
    t_out.join(timeout=10)
    t_err.join(timeout=2)
    rc = proc.returncode if proc.returncode is not None else 1
    if killed and rc == 0:
        rc = 124  # timeout-style; classify_retry_class -> operational (retryable)
    return (rc, "".join(stdout_buf))


def _parse_result_line(stdout: str) -> "dict | None":
    """Find the last JSON line with type==result in worker stdout.

    The worker-result envelope is a single JSON line carrying type=="result"
    with fields: result, session_id, num_turns, total_cost_usd,
    permission_denials, is_error. Trailing non-JSON noise (warnings) tolerated.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "result":
            return d
    return None


def run_via_runner(
    prompt: str,
    schema: dict,
    allowed_tools: "list[str] | None" = None,
    model: "str | None" = None,
    cwd: "str | None" = None,
    runner: Runner = _real_runner,
) -> DispatchResult:
    """Dispatch a worker via an injected runner (test / direct-subprocess path).

    Builds a generic worker-result args list, calls `runner(args, cwd) ->
    (exit_code, stdout)`, and parses the result envelope from stdout. The runner
    is a callable — unit tests inject fakes that return canned output; a real
    subprocess backend would pass `_real_runner` (or a custom runner). The
    production pi/cc backends do NOT use this path.
    """
    args = [prompt, "--output-format", "json", "--json-schema", json.dumps(schema)]
    if allowed_tools:
        args += ["--allowedTools"] + list(allowed_tools)
    if model:
        args += ["--model", model]

    exit_code, stdout = runner(args, cwd=cwd)
    result = _parse_result_line(stdout)
    rc = classify_retry_class(exit_code, stdout, result)

    if result is None:
        return DispatchResult(
            session_id="", result_text="", num_turns=0, cost_usd=0.0,
            permission_denials=[], exit_code=exit_code, retry_class=rc,
        )
    return DispatchResult(
        session_id=result.get("session_id", ""),
        result_text=result.get("result", ""),
        num_turns=result.get("num_turns", 0),
        cost_usd=result.get("total_cost_usd", 0.0),
        permission_denials=[
            d if isinstance(d, str) else json.dumps(d, sort_keys=True, ensure_ascii=False)
            for d in result.get("permission_denials", [])
        ],
        exit_code=exit_code,
        retry_class=rc,
        is_error=bool(result.get("is_error", False)),
    )


def dispatch_via_host(
    prompt: str,
    schema: dict,
    allowed_tools: "list[str] | None" = None,
    model: "str | None" = None,
    cwd: "str | None" = None,
) -> DispatchResult:
    """Dispatch a worker via the host adapter file protocol (pi + cc backends).

    Protocol:
    1. axiom writes a dispatch request to {run_dir}/dispatch_req_{rid}.json
    2. The host adapter (`scripts/host_adapter.py --pi|--cc`) picks it up,
       runs the worker, and writes {run_dir}/dispatch_res_{rid}.json
    3. axiom polls for the result file, parses it, returns DispatchResult.

    If the host does not respond within AXIOM_CLI_DISPATCH_TIMEOUT (default
    600s), this returns an operational-retry DispatchResult.
    """
    import time as _time
    import uuid as _uuid
    run_dir = os.environ.get("AXIOM_RUN_DIR", ".axiom/run")
    timeout = int(os.environ.get("AXIOM_CLI_DISPATCH_TIMEOUT", "600"))
    # Per-dispatch request_id in the filename so concurrent dispatches (parallel
    # node fan-out, or two axiom processes sharing a run-dir) do not overwrite
    # each other's dispatch_req.json. Each dispatch gets its own
    # dispatch_req_{rid}.json / dispatch_res_{rid}.json pair; the host discovers
    # requests by globbing dispatch_req_*.json.
    rid = _uuid.uuid4().hex[:12]
    req_path = os.path.join(run_dir, f"dispatch_req_{rid}.json")
    res_path = os.path.join(run_dir, f"dispatch_res_{rid}.json")

    # Clean stale result for THIS rid (shouldn't exist, but be safe)
    if os.path.exists(res_path):
        os.remove(res_path)

    # Write dispatch request
    req = {
        "prompt": prompt,
        "schema": schema,
        "allowed_tools": allowed_tools,
        "model": model,
        "cwd": cwd,
        "requested_at": _time.time(),
        "request_id": rid,
    }
    os.makedirs(run_dir, exist_ok=True)
    with open(req_path, "w") as f:
        json.dump(req, f, ensure_ascii=False, indent=2)

    # Poll for result
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path) as f:
                    res = json.load(f)
                # Validate minimal shape
                if "result_text" in res or "exit_code" in res:
                    os.remove(res_path)  # clean up
                    # One canonical deserialization (a hand-rolled field list
                    # silently defaulted a missing is_error to False — paired
                    # with the writer's dropped field, a failed envelope crossed
                    # the relay as clean).
                    return DispatchResult.from_dict(res)
            except (json.JSONDecodeError, OSError):
                pass  # incomplete write, retry
        _time.sleep(float(os.environ.get("AXIOM_CLI_POLL_INTERVAL", "2")))

    # Timeout — operational retry class so harness retries
    return DispatchResult(
        session_id="", result_text="", num_turns=0, cost_usd=0.0,
        permission_denials=[], exit_code=124,
        retry_class="operational",
    )


def dispatch(
    prompt: str,
    schema: dict,
    allowed_tools: "list[str] | None" = None,
    model: "str | None" = None,
    cwd: "str | None" = None,
    runner: Runner = _real_runner,
) -> DispatchResult:
    """Runtime-aware dispatch.

    `AXIOM_RUNTIME=host` (set by cli for the pi/cc production backends) ->
    `dispatch_via_host` (file protocol). Anything else ->
    `run_via_runner` (injected runner — the test/direct-subprocess path). The
    default (env unset) is the runner path so the test suite, which injects
    fakes without setting the env, routes to the runner and its fake, not the
    file-protocol poller.
    """
    runtime = os.environ.get("AXIOM_RUNTIME", "runner")
    if runtime == "host":
        return dispatch_via_host(
            prompt, schema, allowed_tools=allowed_tools,
            model=model, cwd=cwd,
        )
    return run_via_runner(
        prompt, schema, allowed_tools=allowed_tools,
        model=model, cwd=cwd, runner=runner,
    )
