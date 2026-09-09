#!/usr/bin/env python3
"""Agent-agnostic host adapter for axiom's file-protocol dispatch.

axiom (control layer, `AXIOM_RUNTIME=host`) writes `dispatch_req.json` and
polls for `dispatch_res.json`. This script IS the host agent — the role that
pi / Claude Code / any agent CLI plays. It polls for requests, invokes
the configured agent command with the prompt, captures the output, and writes
the DispatchResult envelope as `dispatch_res.json`.

This is the only host-side code any agent runtime needs to plug into axiom.
The contract is proven by `scripts/e2e_host_runtime.py`; this script extracts
that inline host into a reusable, tested tool so pi/CC can drop it in.

Usage
-----
  # real agent (pi) — {prompt} is shell-safe-quoted then substituted:
  python3 scripts/host_adapter.py --run-dir .axiom/run \
      --agent-cmd "pi --prompt {prompt} --json"

  # Claude Code (cc) via -p flag, reading prompt from stdin:
  python3 scripts/host_adapter.py --run-dir .axiom/run \
      --agent-cmd "cc -p" --prompt-via stdin

  # mock agent (e2e / smoke) — echo canned JSON as the result:
  python3 scripts/host_adapter.py --run-dir .axiom/run \
      --mock-result '{"answer": 42}' --axiom-pid 12345

The DispatchResult envelope (what this script writes to dispatch_res.json)
----------------------------------------------------------------------
  result_text    str    the agent's output (JSON matching the request schema)
  exit_code      int    0 = success, non-zero = failure
  session_id     str    optional, host-assigned
  num_turns      int    optional, agent turn count
  cost_usd       float  optional, agent-reported cost
  permission_denials list optional
  retry_class    str    "cognitive" | "operational" | "unretriable"
                       (cognitive = the agent tried and failed the task;
                        operational = infra/timeout, harness retries;
                        unretriable = don't retry)

Placeholders in --agent-cmd: {prompt} (shell-quoted), {cwd} (shell-quoted).
With --prompt-via stdin, the prompt is piped to the agent's stdin instead.
"""
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path


def _axiom_alive(pid):
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # not ours, assume alive


def _write_result_atomic(res_path, payload):
    """Atomic write so the axiom poller never reads a partial JSON."""
    tmp = res_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, res_path)


def _parse_pi_json(stdout):
    """Parse pi's `--mode json` NDJSON event stream into DispatchResult fields.

    pi emits one JSON object per line (session/turn_start/message_start/
    message_end/turn_end/agent_end/agent_settled). The assistant text content
    is duplicated across message_start+message_end+turn_end+agent_end, so we
    collect ONLY from message_end (one per message) to dedupe. cost is in
    usage.cost.total; success = stopReason not in {"error"} and text present.
    """
    texts = []
    cost = 0.0
    cost_known = False
    tokens_total = 0
    num_turns = 0
    stop = None
    error = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = e.get("type")
        if t == "turn_start":
            num_turns += 1
        if t == "message_end":
            m = e.get("message", {})
            if m.get("role") == "assistant":
                for b in m.get("content", []):
                    if b.get("type") == "text" and b.get("text"):
                        texts.append(b["text"])
                u = m.get("usage", {})
                c = u.get("cost", {})
                if isinstance(c, dict) and c.get("total") is not None:
                    # R9-1 (review9 P0): ACCUMULATE per-message cost -- each
                    # message_end's usage.cost.total is that message's own
                    # spend (pi's official subagent extension sums per
                    # message). Overwriting kept only the LAST message's cost
                    # ($0.15 + $0.25 reported as $0.25), silently under-
                    # charging budget accounting on multi-turn dispatches.
                    cost += c["total"]
                    cost_known = True
                # token usage is the provider-agnostic spend signal: some
                # providers (alibaba-cloud GLM) report tokens but price cost
                # at 0 -- record tokens so budget/audit has a real number.
                if isinstance(u.get("totalTokens"), (int, float)):
                    tokens_total += u["totalTokens"]
                sr = m.get("stopReason")
                if sr:
                    stop = sr
                if m.get("errorMessage"):
                    error = m["errorMessage"]
    result_text = "".join(texts).strip()
    ok = bool(result_text) and stop not in (None, "error") and not error
    # R9-1: parsed_ok is only the CONTENT verdict. The caller must AND it
    # with the real process exit code -- a dying pi (exit 7) can still leave
    # a success-looking final message in the stream, and content must not
    # launder a crashed process into success.
    exit_code = 0 if ok else 1
    # retry_class: operational = retriable infra (backoff + retry); cognitive =
    # the worker's output/reasoning is the problem (re-dispatch with a new
    # prompt). Rate-limit / 429 / Throttling is retriable infra, NOT a worker
    # reasoning defect — classifying it cognitive burns max_retries on back-to-
    # back identical throttles instead of backing off (friction: alibaba-cloud
    # 429 "Throttling" was misrouted cognitive, cost 94s of wasted retries).
    _err = error or ""
    _operational_signs = ("Connection", "Throttl", "429", "Too Many Requests",
                          "RateLimit", "rate_limit")
    retry_class = ("operational"
                   if stop == "error" or any(s in _err for s in _operational_signs)
                   else "cognitive")
    return (result_text, cost, num_turns, exit_code, retry_class, error,
            cost_known, tokens_total)


def _find_cc_switch_settings():
    """Locate the cc-switch desktop app's current launcher settings tmpfile.

    cc-switch (com.ccswitch.desktop) runs a local proxy on :15721 and, per
    active provider, writes a launcher script + a settings JSON to a temp
    dir. Interactive `claude` is launched as `claude --settings <tmpfile>`;
    that tmpfile's `env` block carries the active provider's BASE_URL /
    AUTH_TOKEN / MODEL (e.g. a direct provider endpoint → dashscope.aliyuncs.com).

    Non-interactive `claude -p` WITHOUT --settings reads ~/.claude/settings.json
    (the proxy-15721 mode) and 502s, because the inherited ANTHROPIC_MODEL=
    <custom name> mismatches what the proxy expects. Reusing the launcher
    tmpfile makes `claude -p` use the identical provider config the
    interactive cc session uses. Returns the tmpfile path or None.

    This is an OPTIONAL convenience for cc-switch users, not a requirement:
    when it returns None, the `--cc` preset falls back to bare `claude -p`,
    which uses whatever claude config / API key the caller already has. The
    whole function may simply never find a tmpfile on a machine without
    cc-switch, and that is the normal, supported path.

    Friction (a real editing-task relay, Path A): `claude -p` was deemed unusable as
    an axiom worker under cc switch — env ANTHROPIC_DEFAULT_*_MODEL are set
    to custom names (glm/qwen/llama) the model catalog doesn't recognize, and
    the fallback ~/.claude/settings.json hits the 15721 proxy which 502s on
    the custom model name. The fix is not to abandon cc but to reuse the
    launcher tmpfile so `claude -p` = interactive cc config.

    cc-switch is an OPTIONAL local convenience for its users; bare `claude -p`
    with a standard ANTHROPIC_API_KEY works without it (the --cc preset falls
    back to `claude -p` when no cc-switch tmpfile is found).
    """
    import glob
    pid = None
    try:
        out = subprocess.run(["lsof", "-ti", ":15721"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            pid = out.splitlines()[0].strip()
    except Exception:
        pass
    if not pid:
        return None
    dirs = [os.environ.get("TMPDIR"), "/tmp"]
    # also scan /var/folders (macOS per-user temp) in case TMPDIR differs
    dirs += [p for p in glob.glob("/var/folders/*/T") if os.path.isdir(p)]
    seen, cands = set(), []
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        cands += [os.path.join(d, f) for f in
                  glob.glob(os.path.join(d, f"claude_*_{pid}.json"))]
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _run_agent_cmd(agent_cmd, prompt, cwd, source_file=None, parse=None, prompt_via=None):
    """Invoke the configured agent command, return (result_text, exit_code, err,
    cost, num_turns). source_file (e.g. ~/.zshrc) is sourced first so provider
    API keys exported in the interactive profile load (friction: DASHSCOPE_API_KEY
    lives in .zshrc, non-interactive subprocesss don't see it). parse='pi-json'
    extracts result_text/cost/turns from pi's NDJSON instead of raw stdout.
    prompt_via='stdin' pipes the prompt to the agent's stdin (e.g. `claude -p`)
    instead of substituting it into --agent-cmd as a shell arg."""
    cmd = agent_cmd
    if "{prompt}" in cmd:
        cmd = cmd.replace("{prompt}", shlex.quote(prompt))
    if "{cwd}" in cmd and cwd:
        cmd = cmd.replace("{cwd}", shlex.quote(cwd))
    if source_file:
        # run via zsh so `source` works; load the interactive profile (keys)
        shell_cmd = f"source {shlex.quote(source_file)} 2>/dev/null; {cmd}"
        argv = ["zsh", "-c", shell_cmd]
        use_shell = False
    else:
        argv = cmd
        use_shell = True
    try:
        run_kwargs = dict(shell=use_shell, capture_output=True, text=True,
            cwd=cwd or None, timeout=int(os.environ.get("AXIOM_HOST_TIMEOUT", "600")))
        if prompt_via == "stdin":
            run_kwargs["input"] = prompt
        proc = subprocess.run(argv, **run_kwargs)
        if parse == "pi-json":
            (result_text, cost, num_turns, parsed_ec, retry_class, err,
             cost_known, tokens) = _parse_pi_json(proc.stdout)
            # R9-1 (review9 P0): the REAL process exit code participates.
            # Content-parsed success (parsed_ec==0) cannot override a
            # non-zero process exit -- pi died mid-stream (OOM/kill/exit 7)
            # after emitting a success-looking final message. A failed
            # process whose stream carries no error of its own (crash,
            # SIGKILL, empty output) is infra -> operational/retriable; a
            # stream that DID record an error keeps its own classification
            # (e.g. a 429 already classified operational, or a cognitive
            # failure the worker reported before exiting non-zero).
            if proc.returncode != 0:
                if not err:
                    err = f"agent process exited {proc.returncode}"
                    retry_class = "operational"
                exit_code = proc.returncode
            else:
                exit_code = parsed_ec
            return (result_text, exit_code, err, cost, num_turns, retry_class,
                    cost_known, tokens)
        return proc.stdout.strip(), proc.returncode, \
            (proc.stderr.strip() or None), 0.0, 1, "cognitive", False, 0
    except subprocess.TimeoutExpired:
        return "", 124, "agent command timed out", 0.0, 0, "operational", False, 0
    except Exception as e:
        return "", 1, f"agent command failed: {e}", 0.0, 0, "cognitive", False, 0


def _mock_result(mock_result_json, prompt, schema):
    """Produce a canned result_text. If mock_result_json is given, echo it;
    otherwise synthesize a minimal JSON object with nulls for each required key
    (enough to flow through the harness for smoke tests)."""
    if mock_result_json is not None:
        return mock_result_json, 0
    reqs = (schema or {}).get("required", [])
    obj = {k: None for k in reqs}
    return json.dumps(obj), 0


def serve(run_dir, agent_cmd=None, prompt_via=None, mock_result_json=None,
          axiom_pid=None, poll_interval=1.0, once=False,
          source_file=None, parse=None):
    """Main host-adapter loop. Returns the count of dispatches served."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    is_mock = agent_cmd is None
    served = 0
    stop = {"flag": False}

    def _on_signal(sig, _frame):
        stop["flag"] = True
        print(f"host_adapter: caught signal {sig}, stopping after current dispatch",
              file=sys.stderr)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    mode = "mock" if is_mock else f"agent={agent_cmd}"
    if source_file:
        mode += f" source={source_file}"
    if parse:
        mode += f" parse={parse}"
    print(f"host_adapter: watching {run_dir} ({mode})", file=sys.stderr)

    while _axiom_alive(axiom_pid) and not stop["flag"]:
        # P1-4: discover request. New protocol: dispatch_req_{rid}.json
        # (per-dispatch request_id, no overwrite race under concurrency).
        # Legacy fallback: dispatch_req.json (single-writer only).
        import glob as _glob
        new_reqs = sorted(_glob.glob(str(run_dir / "dispatch_req_*.json")))
        if new_reqs:
            cur_req = Path(new_reqs[0])
            rid = cur_req.stem.replace("dispatch_req_", "")
            cur_res = run_dir / f"dispatch_res_{rid}.json"
        else:
            legacy_req = run_dir / "dispatch_req.json"
            if not legacy_req.exists():
                time.sleep(poll_interval)
                continue
            cur_req = legacy_req
            cur_res = run_dir / "dispatch_res.json"
        # read request
        try:
            with open(cur_req) as f:
                req = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"host_adapter: bad request: {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue
        # remove req to signal "processing" (mirrors cmd_dispatch_serve)
        try:
            os.remove(cur_req)
        except OSError:
            pass

        prompt = req.get("prompt", "")
        schema = req.get("schema", {})
        cwd = req.get("cwd")
        started = time.time()

        if is_mock:
            result_text, exit_code = _mock_result(
                mock_result_json, prompt, schema)
            err = None
            cost = 0.0
            cost_known = True  # mock cost is definitional, not unknown
            num_turns = 1
            tokens = 0
            retry_class = "cognitive"
        else:
            (result_text, exit_code, err, cost, num_turns, retry_class,
             cost_known, tokens) = _run_agent_cmd(
                agent_cmd, prompt, cwd,
                source_file=source_file, parse=parse,
                prompt_via=prompt_via)

        payload = {
            # P-022: every field the harness reads, incl. is_error -- the
            # adapter relay is a serialization boundary, and a dropped
            # failure flag there flips a failed worker into clean success
            # downstream (review4). is_error: pi-json = the NDJSON
            # errorMessage; raw = the process failed (stderr noise on exit 0
            # is NOT an error -- workers log warnings to stderr routinely).
            "session_id": f"host-{os.getpid()}-{served}",
            "result_text": result_text,
            "num_turns": num_turns,
            "cost_usd": cost,
            # R9-1: cost honesty. False = the agent stream reported NO cost
            # (cost_usd is a 0.0 placeholder, not a measurement) -- budget
            # accounting must not read an unknown cost as free. raw-parse
            # mode never reports cost -> always False there.
            "cost_known": cost_known,
            "tokens_total": tokens,
            "permission_denials": [],
            "exit_code": exit_code,
            "retry_class": retry_class,
            "is_error": (bool(err) if parse == "pi-json"
                         else exit_code != 0),
        }
        _write_result_atomic(cur_res, payload)
        served += 1
        dt = time.time() - started
        print(f"host_adapter: served #{served} (exit={exit_code}, "
              f"rc={retry_class}, {dt:.1f}s"
              f"{', err=' + err if err else ''})", file=sys.stderr)
        if once:
            break
    print(f"host_adapter: done ({served} dispatches served)", file=sys.stderr)
    return served


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="host_adapter",
        description="Agent-agnostic host adapter for axiom file-protocol dispatch "
                    "(the pi/cc side of AXIOM_RUNTIME=host).")
    p.add_argument("--run-dir", default=".axiom/run",
                   help="axiom run dir (matches --run-dir / AXIOM_RUN_DIR)")
    p.add_argument("--agent-cmd",
                   help="agent command with {prompt}/{cwd} placeholders, e.g. "
                        "'pi --prompt {prompt} --json'. Omit for mock mode.")
    p.add_argument("--pi", action="store_true",
                   help="pi preset: pi NDJSON (--mode json) with thinking off. "
                        "Defaults are EXAMPLES for alibaba-cloud/glm — set "
                        "--provider/--model/--source to YOUR provider and the "
                        "shell file exporting its API key. See 'Backend setup' "
                        "in SKILL.md.")
    p.add_argument("--cc", action="store_true",
                   help="Claude Code preset: dispatch via `claude -p` (uses your "
                        "local claude CLI config / ANTHROPIC_API_KEY). If the "
                        "cc-switch desktop app is running, its launcher settings "
                        "tmpfile is auto-reused so `claude -p` matches your "
                        "interactive cc session's provider; otherwise falls back "
                        "to bare `claude -p`. Prompt piped via stdin. cc-switch "
                        "is an OPTIONAL local convenience, not required.")
    p.add_argument("--provider", default="alibaba-cloud",
                   help="pi provider (with --pi); default is an EXAMPLE for "
                        "alibaba-cloud — set to YOUR provider (e.g. openai, "
                        "anthropic, deepseek, ...).")
    p.add_argument("--model", default="glm-4.5",
                   help="pi model (with --pi); default is an EXAMPLE model — "
                        "set to a model YOUR provider serves.")
    p.add_argument("--tools", action="store_true",
                   help="with --pi: allow pi's tools (for coding dispatches). "
                        "Default off (pure text answer, cheapest).")
    p.add_argument("--source",
                   help="shell file to source before the agent cmd (exports "
                        "YOUR provider's API key; example ~/.zshrc — set to "
                        "wherever your key lives).")
    p.add_argument("--parse", choices=["raw", "pi-json"], default="raw",
                   help="raw: stdout=result_text (default); pi-json: parse pi's "
                        "--mode json NDJSON into result_text/cost/turns.")
    p.add_argument("--prompt-via", choices=["arg", "stdin"], default="arg",
                   help="arg: {prompt} substituted into --agent-cmd (shell-quoted); "
                        "stdin: prompt piped to agent's stdin.")
    p.add_argument("--mock-result",
                   help="canned JSON to echo as result_text in mock mode.")
    p.add_argument("--axiom-pid", type=int,
                   help="axiom process PID; adapter exits when it dies.")
    p.add_argument("--once", action="store_true",
                   help="serve one dispatch then exit (smoke).")
    p.add_argument("--poll-interval", type=float, default=1.0)
    args = p.parse_args(argv)

    source_file = args.source
    parse_mode = args.parse

    # --pi preset: bundle pi's auth + NDJSON + provider defaults
    if args.pi:
        source_file = source_file or os.path.expanduser("~/.zshrc")
        parse_mode = "pi-json"
        if not args.agent_cmd:
            tools_flag = "" if args.tools else "--no-tools"
            args.agent_cmd = (
                f"pi -p --mode json --provider {shlex.quote(args.provider)} "
                f"--model {shlex.quote(args.model)} --thinking off "
                f"{tools_flag} {{prompt}}".replace("  ", " "))
    # --cc preset: reuse the cc-switch launcher settings tmpfile so
    # `claude -p` matches the interactive cc session's provider config
    # (a direct provider endpoint like DashScope, etc.). Without this, `claude -p` reads
    # ~/.claude/settings.json (proxy-15721) and 502s on the custom model
    # name; with it, `claude -p` = interactive cc config. stdin carries
    # the prompt (claude -p waits 3s for stdin otherwise).
    #
    # Web tool caveat (a research relay): under `claude -p` +
    # a provider's apps/anthropic, WebSearch is structurally unavailable — no
    # server-side search impl on apps/anthropic; -p falls back to static
    # Google/Bing/DDG search-page URL strings, not real search. WebFetch /
    # Read / Bash all work. For cc worker web-research specs: drop WebSearch
    # from allowed_tools, add Read (local pre-downloaded docs in pages/md/)
    # + WebFetch (works under the provider) + Bash curl --noproxy '*' (bypassing
    # a local broken proxy, direct connection). Put an AGENTS.md in the worker cwd stating the
    # data-gathering discipline so all nodes share it. Proven by
    # smoke_cc_web.v1 VERIFIED (Read+WebFetch+Bash returned real content;
    # fetch+bash agreed — not a false-VERIFIED).
    if args.cc:
        tmpfile = _find_cc_switch_settings()
        if not tmpfile:
            print("host_adapter: --cc found no cc-switch settings tmpfile "
                  "(is the cc-switch app running with an active cc session? "
                  "lsof :15721 found no listener, or no claude_*_<pid>.json "
                  "under TMPDIR). Falling back to bare `claude -p`.",
                  file=sys.stderr)
            args.agent_cmd = args.agent_cmd or "claude -p"
        else:
            args.agent_cmd = f"claude -p --settings {shlex.quote(tmpfile)}"
        args.prompt_via = "stdin"
        print(f"host_adapter: --cc using cc-switch settings {tmpfile}",
              file=sys.stderr)
    mock_json = None
    if args.mock_result is not None:
        # validate it's JSON
        try:
            json.loads(args.mock_result)
            mock_json = args.mock_result
        except json.JSONDecodeError as e:
            print(f"host_adapter: --mock-result is not valid JSON: {e}",
                  file=sys.stderr)
            return 2
    if args.agent_cmd is None and mock_json is None:
        # mock mode with no canned result: synthesize from schema (smoke)
        print("host_adapter: no --agent-cmd/--pi and no --mock-result → mock mode "
              "(synthesize minimal schema-conforming JSON)", file=sys.stderr)
    served = serve(
        args.run_dir, agent_cmd=args.agent_cmd, prompt_via=args.prompt_via,
        mock_result_json=mock_json, axiom_pid=args.axiom_pid,
        poll_interval=args.poll_interval, once=args.once,
        source_file=source_file, parse=parse_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
