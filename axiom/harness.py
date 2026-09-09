"""Deterministic workflow harness.

Node-level execution lives here; intermediate state lives in the ledger. The
orchestrator never sees node raw results -- only projected checkpoints
(Harness.project_checkpoint).

Type boundary: raw worker stdout never flows downstream; only
validated_output / artifact_ref / ledger_event.

Concurrency (16) / total-agent (1000) / budget caps are enforced as
honest-exit conditions; v1 executes sequentially so the concurrency cap is
structural (a real semaphore is v2).
"""
from __future__ import annotations
import json
import re
import sys
import hashlib
import copy
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from axiom.ledger import Ledger
from axiom.dispatch import dispatch as _dispatch
from axiom.ir import validate_spec, spec_from_json, contract_hash, budget_limit
from axiom.state import derive_verdict, derive_claim_status, derive_dry_count


# v2 Task 8: body event kinds that carry loop_id+iteration in their payload
# when emitted inside a loop body (spec §3). A kind SUFFIX (agent_result_iter1)
# would silently break kind-filtered projections like derive_claim_status; the
# payload-preserves them (invariant 6).
_BODY_EVENT_KINDS = frozenset({
    "agent_result", "verify_verdict", "stagnating", "artifact_write",
})

# P-017: event kinds that carry a real dispatch's cost_usd. Every actual
# worker dispatch must leave EXACTLY ONE cost-bearing event so the ledger
# (not the in-memory _cost_total) is the complete spend record -- checkpoint
# cost_usd_total and build_replay_cache's resume-rebuild both sum the ledger.
# operational_attempt: the worker call failed operationally (timeout/conn/
# crash -> retry_class operational) or was unretriable; the cognitive capped
# bail also records cognitive_attempt. Before P-017 those paths returned
# without recording, so spent money vanished from checkpoint/resume (the
# cost_other_failures hole: actual $0.08 -> checkpoint $0.02/$0).
# P-023: the canonical definition lives in state.py (ATTEMPT_FAILURE_KINDS /
# COSTED_EVENT_KINDS) so every projection -- verdict staleness, replay cache,
# checkpoint cost -- reads ONE tuple. Re-exported here for back-compat
# (tests + SKILL docs import harness.COSTED_EVENT_KINDS).
from axiom.state import COSTED_EVENT_KINDS, ATTEMPT_FAILURE_KINDS
_FAILED_COST_KINDS = ATTEMPT_FAILURE_KINDS
# R6-1: event kinds that materialize one real worker ATTEMPT in the replay
# cache's slot state. These carry the exec_occurrence tag (injected by
# _record from the _exec_ctx thread-local) and open a new run-batch boundary
# during build_replay_cache's pass 1. gate_open is NOT here: a gated node
# never dispatched (no attempt) -> no slot state; the position is re-tagged
# on the next walk's pop.
_EXEC_EVENT_KINDS = ATTEMPT_FAILURE_KINDS + ("agent_result",)

# parses RepeatNode's built-in dry metric "dry<N" -> N. UntilNode's general
# predicate (Task 9) does not match here; _run_repeat leaves it as an extension
# point (falls through to exhausted).
_DRY_RE = re.compile(r"^dry<(\d+)$")


class TypeBoundaryViolation(Exception):
    """Raised when raw stdout would leak past the type boundary."""


class GateHalt(Exception):
    def __init__(self, gate_id):
        self.gate_id = gate_id
        super().__init__(gate_id)


def _default_runner(args, cwd=None):
    from axiom.dispatch import _real_runner
    return _real_runner(args, cwd)


class Harness:
    def __init__(self, run_dir, worker_runner=None, project_root=None,
                 keep_worktrees=False):
        self.run_dir = Path(run_dir)
        self.ledger = Ledger(self.run_dir)
        self._runner = worker_runner  # None -> real runner
        # v2 Task 12: when True, the worktree-isolation intercept leaves the
        # fresh git worktree on disk after dispatch (debugging); default False
        # removes it in the finally (spec §5 step 7).
        self.keep_worktrees = keep_worktrees
        # worker cwd + write_areas glob root: the project the spec operates on.
        # Default to cwd (axiom run is invoked from the project root). Fixes
        # the run-dir cwd bug where workers wrote into .axiom/run instead of
        # the project, and n_verify greped the wrong tree -> false FAILED.
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self._cost_total = 0.0
        self._agent_count = 0
        # A1: concurrent dispatchers (parallel/pipeline body agents) tally cost
        # and agent-count from multiple threads. event_id/seq must be reserved
        # atomically too, or two appends race to the same id. A dedicated state
        # lock guards the counters; the ledger's own lock guards seq/hash write.
        self._state_lock = threading.Lock()
        self._ev_lock = threading.Lock()
        self._ev_counter = len(self.ledger.events())
        # v1.4-S2: parallel skeptics pop the same (svid, node_id) replay deque
        # concurrently -- the pop must be locked or two skeptics steal the
        # same cached output / race an empty deque.
        self._replay_lock = threading.Lock()
        # v2 Task 8: per-thread current-loop context {loop_id, iteration} or
        # None. Set by _run_repeat before each body run so body events
        # (agent_result/verify_verdict/stagnating/artifact_write) carry
        # loop_id+iteration IN THEIR PAYLOAD (spec §3, invariant 6). Thread-
        # local (not param-threaded) so the existing dispatch_agent /
        # _run_agent_with_retry / run_verify signatures are untouched; the
        # fan-out runners (run_parallel/run_pipeline/run_verify) capture the
        # parent's ctx and re-set it in worker threads (ThreadPoolExecutor
        # workers do NOT inherit threading.local from the parent).
        self._loop_ctx = threading.local()
        # R6-1: execution-identity state (review6). _exec_cursor counts, per
        # dispatch key, how many control-flow arrivals this walk has consumed
        # (only keys in _scoped_keys advance; fan-out/skeptic keys share
        # position 0). _exec_ctx is a thread-local holding the CURRENT
        # arrival's position tag k; _record injects it as
        # payload.exec_occurrence on every _EXEC_EVENT_KINDS event so an
        # attempt's occurrence position is a recorded FACT, not re-derived
        # by counting events at rebuild. _replay_run is the next run-batch
        # number (set by build_replay_cache; audit only).
        self._exec_cursor: dict = {}
        self._scoped_keys: set = set()
        self._exec_ctx = threading.local()
        self._replay_run = 0

    # --- helpers ----------------------------------------------------

    def _next_event_seq(self) -> int:
        # atomic reserve (never just read len() -- concurrent callers would
        # collide on the same count). The reserved number need not equal the
        # ledger's file seq (that is assigned under the ledger lock); it only
        # has to be unique.
        with self._ev_lock:
            self._ev_counter += 1
            return self._ev_counter

    def _ev_id(self) -> str:
        with self._ev_lock:
            self._ev_counter += 1
            return f"ev_{self._ev_counter}"

    def _reserve_agent_slot(self, spec) -> bool:
        """P-025: atomically reserve one dispatch slot BEFORE the worker call.

        The old shape was check-then-act across two separate locks: a
        pre-dispatch READ of _agent_count (_agent_cap_already_exceeded), the
        worker call, then _tally's increment -- so two concurrent threads
        both passed the read and both dispatched (review4
        test_max_agents_is_enforced_with_two_concurrent_workers: max_agents=1,
        2 real concurrent calls). _agent_count now means "dispatches STARTED"
        (reserved), incremented atomically here under _state_lock; _tally
        only settles cost. A replay cache hit returns before this, so cached
        nodes don't consume slots; build_replay_cache rebuilds the counter
        from ledger costed events (dispatches that actually happened),
        consistent with the started-count semantics."""
        with self._state_lock:
            if spec is None:
                self._agent_count += 1
                return True
            if self._agent_count >= getattr(spec, "max_agents", 1000):
                return False
            self._agent_count += 1
            return True

    def _release_agent_slot(self) -> None:
        """R5-3: give back a reservation when the dispatch never happens.

        _reserve_agent_slot spends a slot BEFORE admission; if the caller
        then refuses the dispatch (risk gate opened, worktree setup
        impossible), the reservation must be returned or the run's started-
        count exceeds reality and later legitimate dispatches are refused
        (review5 test_gated_parallel_branch_does_not_spend_dispatch_slot:
        a gated parallel body left _agent_count=1 with zero real calls, so
        the post-resolve re-dispatch under max_agents=1 was refused).
        Ledger-rebuilt counts (build_replay_cache counts COSTED events) are
        unaffected -- a never-dispatched reservation leaves no costed event,
        so release keeps the in-memory counter consistent with the ledger."""
        with self._state_lock:
            if self._agent_count > 0:
                self._agent_count -= 1

    def _tally(self, cost: float, spec, spec_version_id, node) -> bool:
        """Atomic cost increment followed by cap check. Returns True if
        a cap (budget / agent-count) is now exceeded -- the caller treats that
        as a soft stop (subsequent dispatches refuse honestly).

        P-025: the agent-count half moved to _reserve_agent_slot (pre-dispatch
        atomic reservation); _tally only settles cost after the worker
        returns. The cap check still covers agent_count (a reservation may
        have raced the count to exactly max+... no -- reserve refuses at >=,
        so count never exceeds max_agents; the check stays for budget)."""
        with self._state_lock:
            self._cost_total += cost
            if spec is not None and self._check_caps(spec):
                return True
        return False

    # --- provenance (B2) -------------------------------------------

    def _build_evidence_for(self, spec) -> None:
        """Build the event->decisions reverse index from the spec's forward
        decision_trace refs (decision -> evidence). The reverse direction is a
        DERIVED index: agents never write it; the harness injects it onto
        every event at append time so each event self-describes which decisions
        it evidences (bidirectional provenance, §4.4/4.5)."""
        idx: dict[str, list[str]] = {}
        for d in spec.decision_trace:
            did = d.get("decision_id")
            for ref in d.get("evidence_refs", []):
                if isinstance(ref, str) and ref.startswith("event:"):
                    idx.setdefault(ref.split(":", 1)[1], []).append(did)
        self._evidence_for_index = idx

    def _record(self, event: dict) -> dict:
        """Append an event with its derived evidence_for provenance attached.
        The reverse index is built once from the spec at run start; events the
        spec did not anticipate (most runtime events) simply get an empty list.
        Agents cannot forge this field -- it is overwritten here, never read."""
        if getattr(self, "_evidence_for_index", None) is not None:
            # agent-supplied evidence_for is ignored (forged provenance guard)
            event["evidence_for"] = list(
                self._evidence_for_index.get(event.get("event_id", ""), [])
            )
        # v2 Task 8: inject loop context into BODY event payloads (spec §3).
        # Body events (agent_result/verify_verdict/stagnating/artifact_write)
        # emitted inside a loop carry loop_id+iteration in their payload so
        # derive_dry_count (§4) and kind-filtered projections see the
        # iteration. The context is set per-iteration by _run_repeat via a
        # thread-local; fan-out workers capture+re-set it. Non-body kinds
        # (loop_open/iteration_open/iteration_close/loop_close/gate_open/...)
        # already carry loop_id+iteration in their own payloads via the emit
        # helpers, so they are skipped here (no double injection).
        ctx = getattr(self._loop_ctx, "ctx", None)
        if ctx is not None and event.get("kind") in _BODY_EVENT_KINDS:
            payload = event.get("payload")
            if isinstance(payload, dict):
                payload.setdefault("loop_id", ctx["loop_id"])
                payload.setdefault("iteration", ctx["iteration"])
        # R6-1: inject the occurrence-position tag into attempt events
        # (agent_result/stagnating/cognitive_attempt/operational_attempt).
        # The tag is set by _replay_pop's position consume in the SAME
        # thread (fan-out workers pop in their own thread). A retry loop
        # re-emits the same k for every attempt -- a retry is NOT a new
        # control-flow occurrence (review6 scenario 1). Events recorded
        # without a preceding pop (never dispatched through the two agent
        # paths) simply lack the field and rebuild falls back to legacy
        # sequential counting.
        pos = getattr(self._exec_ctx, "pos", None)
        if pos is not None and event.get("kind") in _EXEC_EVENT_KINDS:
            payload = event.get("payload")
            if isinstance(payload, dict):
                payload.setdefault("exec_occurrence", pos)
        return self.ledger.append(event)

    # --- v2 control-flow ledger events (spec §3) -------------------------
    # Five new event kinds, all thin wrappers over _record -> ledger.append.
    # The hash chain is KIND-AGNOSTIC (_event_hash hashes event-minus-
    # event_hash with sort_keys); these need NO ledger.py schema change.
    # Body events (agent_result/verify_verdict/stagnating/artifact_write)
    # carry loop_id + iteration:k IN THEIR PAYLOAD — not as a kind suffix —
    # so kind-filtered projections (derive_claim_status filters
    # kind=='agent_result') still see iterations. Dispatch (Tasks 7-10)
    # emits these; this task only provides the emit surface.

    def emit_loop_open(self, *, spec_version_id, loop_id, node_id,
                       max_iterations, until=None, cond=None):
        """loop_open {loop_id, node_id, max_iterations, until|cond}.

        `until` (RepeatNode's dry metric) OR `cond` (UntilNode's arbitrary
        predicate) — never both. The node kind determines which; the caller
        passes the one that applies."""
        payload = {
            "loop_id": loop_id, "node_id": node_id,
            "max_iterations": max_iterations,
        }
        if until is not None:
            payload["until"] = until
        if cond is not None:
            payload["cond"] = cond
        self._record({
            "event_id": self._ev_id(), "kind": "loop_open",
            "spec_version_id": spec_version_id, "node_id": node_id,
            "payload": payload,
        })

    def emit_iteration_open(self, *, spec_version_id, loop_id, iteration):
        """iteration_open {loop_id, iteration:k}.

        `iteration` is a PAYLOAD field, not a kind suffix — preserves
        kind-filtered projections."""
        self._record({
            "event_id": self._ev_id(), "kind": "iteration_open",
            "spec_version_id": spec_version_id,
            "payload": {"loop_id": loop_id, "iteration": iteration},
        })

    def emit_iteration_close(self, *, spec_version_id, loop_id, iteration,
                             condition_eval, dry_count, claim_strength=None,
                             exit_reason=None):
        """iteration_close {loop_id, iteration:k, condition_eval, dry_count,
        [claim_strength], [exit_reason]}.

        `dry_count` is a deterministic re-derivation from events (§4), not a
        cached state; recorded here for auditability of mid-loop convergence.
        `claim_strength` (optional, UntilNode Task 9) records whether the cond
        read VERIFIED truth ('verified', binding), a PROPOSED {{ref}}
        ('proposed', advisory §2 Q2), or a pure projection ('deterministic').
        Omitted (None) for RepeatNode (dry metric is always deterministic).
        `exit_reason` (optional, Task 11 Q4): set to 'budget_exhausted' when
        the iteration was cut short mid-body by the budget cap. Omitted for a
        normally-closing iteration (converged/exhausted/stagnation_cap are
        recorded on loop_close; only the partial-budget exit needs an
        iteration-level marker so the partial iteration's work is auditable)."""
        payload = {
            "loop_id": loop_id, "iteration": iteration,
            "condition_eval": condition_eval, "dry_count": dry_count,
        }
        if claim_strength is not None:
            payload["claim_strength"] = claim_strength
        if exit_reason is not None:
            payload["exit_reason"] = exit_reason
        self._record({
            "event_id": self._ev_id(), "kind": "iteration_close",
            "spec_version_id": spec_version_id,
            "payload": payload,
        })

    def emit_loop_close(self, *, spec_version_id, loop_id, final_iteration,
                        exit_reason, terminal_output=None, input_hash=None):
        """loop_close {loop_id, final_iteration:k, exit_reason,
        terminal_output, input_hash}.

        exit_reason ∈ converged | exhausted | budget_exhausted | stagnation_cap.
        Closed set: callers (Tasks 7-11) pass one of these; no default to
        force an honest reason.

        P1-10: terminal_output + input_hash let resume replay the loop's ACTUAL
        converged output (last body step's validated_output) instead of
        grabbing the last agent_result of the body (wrong when the terminal
        body node is a script). input_hash gates the replay: if the loop's
        outer input changed since the converged run, the cached terminal is
        stale -> resume re-enters the loop instead of replaying the old output.
        """
        payload = {
            "loop_id": loop_id, "final_iteration": final_iteration,
            "exit_reason": exit_reason,
        }
        if terminal_output is not None:
            payload["terminal_output"] = terminal_output
        if input_hash is not None:
            payload["input_hash"] = input_hash
        self._record({
            "event_id": self._ev_id(), "kind": "loop_close",
            "spec_version_id": spec_version_id,
            "payload": payload,
        })

    def emit_branch_decision(self, *, spec_version_id, node_id, predicate,
                             eval_result, branch_taken, degraded,
                             claim_strength):
        """branch_decision {node_id, predicate, eval_result: bool|'undefined',
        branch_taken: 'then'|'else', degraded: bool,
        claim_strength: verified|proposed|deterministic}.

        A deterministic predicate evaluation is a FACT (tamper-evident event),
        not a claim — derive_claim_status does NOT read these (§4)."""
        self._record({
            "event_id": self._ev_id(), "kind": "branch_decision",
            "spec_version_id": spec_version_id, "node_id": node_id,
            "payload": {
                "node_id": node_id, "predicate": predicate,
                "eval_result": eval_result, "branch_taken": branch_taken,
                "degraded": degraded, "claim_strength": claim_strength,
            },
        })

    # --- replay cache (A4) ----------------------------------------

    _SKEPTIC_SCHEMA = {
        "type": "object",
        "properties": {"refuted": {"type": "boolean"},
                       "evidence_ref": {"type": "string"}},
        "required": ["refuted"],
    }

    def _schema_for(self, spec, node_id: str):
        if spec is not None and node_id in spec.nodes:
            return spec.nodes[node_id].get("output_schema", {})
        if node_id.startswith("_skeptic_"):
            return Harness._SKEPTIC_SCHEMA
        return None

    # --- replay cache (A4 + v2 Task 14 §7) ------------------------------

    @staticmethod
    def _input_hash(upstream_values):
        """v2 Task 14 (§7): sha256 of the upstream_values JSON for cache
        keying. Used as secondary validation on a replay hit: mismatch ->
        cache miss + re-dispatch (guards over-list-change). Returns None
        when upstream_values is None or unhashable (skip the secondary
        check -- v1 backward compat and non-dict upstreams)."""
        if upstream_values is None:
            return None
        try:
            return hashlib.sha256(
                json.dumps(upstream_values, sort_keys=True).encode()
            ).hexdigest()
        except (TypeError, ValueError):
            return None

    def _node_occurrence_counts(self, spec) -> dict:
        """R5-2: map each agent dispatch key to how many times its node
        appears in the control flow. Only nodes that appear MORE THAN ONCE
        (and are not inside a loop, where the per-iteration key already
        distinguishes occurrences) are returned -- single-occurrence nodes
        keep the pre-R5-2 key space (occ 0) so all existing cache behavior
        and tests are unchanged.

        The key mirrors _replay_pop's (svid, node_id, loop_id, iteration);
        for a non-loop top-level sequence step that is (svid, nid, None, 0).
        Parallel/pipeline fan-out bodies are dispatched per item (a shared
        node id, occurrences == item count) -- they already share one deque
        by design, so they are excluded here (occurrence 0) just like
        before."""
        counts: dict = {}
        try:
            svid = getattr(spec, "spec_version_id", None)
            cf = getattr(spec, "control_flow", None) or {}
            steps = cf.get("steps", []) if isinstance(cf, dict) else []
            tally: dict = {}
            for step in steps:
                if isinstance(step, str):
                    tally[step] = tally.get(step, 0) + 1
            for nid, n in tally.items():
                if n > 1:
                    counts[(svid, nid, None, 0)] = n
        except Exception:  # noqa: BLE001 -- advisory; never blocks caching
            return {}
        return counts

    def _reset_exec_identity(self, spec, next_run: int = 0) -> None:
        """R6-1: (re)arm the execution-identity state for a fresh walk.

        Called by run() and build_replay_cache. Resets the pop-side
        occurrence cursor (every walk re-arrives at each control-flow
        position from 0), recomputes the scoped-key set (nodes appearing
        more than once as top-level sequence steps), and records the next
        run-batch number (audit only). Never touches the built slot cache."""
        self._exec_cursor = {}
        self._scoped_keys = set(self._node_occurrence_counts(spec))
        self._replay_run = next_run
        self._exec_ctx.pos = None

    def build_replay_cache(self, spec) -> None:
        """A4 + v2 Task 14 (§7) + R6-1: rebuild the replay cache from the
        ledger so a resume re-walk returns unchanged successful nodes
        instantly (zero dispatch, zero cost) and only re-dispatches
        changed/failed ones.

        R6-1 (review6): the cache is organized by EXECUTION IDENTITY --
        (run batch, occurrence position, attempt) -- instead of inferring
        occurrence positions by counting success/failure events (the old
        FIFO inference shifted positions whenever a retry or a resume
        added events: review6 scenarios 1-2), and it never falls back to
        an older batch's success when the LATEST batch for the position
        failed (review6 scenario 3: historical successes were replayed
        over a newer failure).

        Structure: self._replay[key][occurrence_k] = [(run, input_hash,
        vo_or_None), ...] in ledger order. key = (svid, node_id, loop_id,
        iteration) as before; occurrence_k is the control-flow position
        the attempt was tagged with AT DISPATCH (payload.exec_occurrence,
        injected by _record from the pop-side cursor) -- a retry carries
        its occurrence's k, a repair-on-resume binds to the failed
        position's k. Events without the tag (pre-R6-1 ledgers) fall back
        to legacy sequential counting per scoped key. A failure kind or a
        non-clean agent_result appends a TOMBSTONE (vo=None): the newest
        batch's state for a slot decides replayability, so a later failure
        supersedes earlier successes of the same position without touching
        them (audit preserved).

        Pass 1 derives run-batch boundaries: every attempt event opens a
        new batch; a replay_hit whose key's latest agent_result is the
        immediately preceding attempt also opens one (it consumed the
        cached result of that attempt's position in the same prior walk).
        Batch labels are order-only; a misclassified hit can insert an
        empty batch but never changes slot contents, so the labeling is
        convergent across resumes.

        Scoped keys (nodes appearing >1 time as top-level sequence steps)
        get per-position slots; every other key shares position 0 and
        multiplexes by input_hash (fan-out items, skeptics).

        Persistence protocol (R8-1): R7-1+ fan-out branch attempts are
        tagged with string position ids ("<container>:<idx>"); pre-R7-1
        fan-out attempts carry exec_occurrence=0 (or none) and land in
        the shared slot 0. Both formats rebuild unchanged here -- the
        compat READ lives in _replay_pop (position-slot miss -> probe
        legacy slot 0), so old ledgers keep replaying after upgrade."""
        cache: dict = {}
        events = self.ledger.events()
        # Pass 1: run-batch boundaries + the next batch number.
        boundaries: list = []  # (event_index, run) for run-opening events
        last_result_run: dict = {}
        run = 0
        for idx, e in enumerate(events):
            kind = e.get("kind")
            svid_e = e.get("spec_version_id")
            nid = e.get("node_id")
            payload = e.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            key = (svid_e, nid, payload.get("loop_id"),
                   payload.get("iteration", 0))
            if kind in _EXEC_EVENT_KINDS:
                boundaries.append((idx, run))
                if kind == "agent_result":
                    last_result_run[key] = run
                run += 1
            elif kind == "replay_hit":
                if last_result_run.get(key) == run - 1:
                    boundaries.append((idx, run))
                    run += 1
        # Pass 2: assign every attempt event to its slot.
        legacy_cursor: dict = {}
        scoped = set(self._node_occurrence_counts(spec))
        bi = 0
        cur_run = 0
        for idx, e in enumerate(events):
            while bi < len(boundaries) and boundaries[bi][0] <= idx:
                cur_run = boundaries[bi][1]
                bi += 1
            if e.get("kind") not in _EXEC_EVENT_KINDS:
                continue
            svid_e = e.get("spec_version_id")
            nid = e.get("node_id")
            payload = e.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            key = (svid_e, nid, payload.get("loop_id"),
                   payload.get("iteration", 0))
            occ = payload.get("exec_occurrence")
            if occ is None:
                # Legacy (pre-R6-1) event: sequential counting per scoped
                # key, the pre-R6-1 inference. Non-scoped keys share 0.
                occ = legacy_cursor.get(key, 0) if key in scoped else 0
            if key in scoped:
                legacy_cursor[key] = max(legacy_cursor.get(key, 0), occ + 1)
            # Cache admission (clean-success only) -- same rules as before.
            vo = None
            if e.get("kind") == "agent_result":
                cand = payload.get("validated_output")
                schema = self._schema_for(spec, nid)
                if schema is None:
                    conforms = isinstance(cand, dict) and bool(cand)
                else:
                    conforms = self._conforms(cand, schema)
                # P1-6: prefer the recorded is_clean envelope; back-compat
                # fallback to conforms for pre-P1-6 events.
                is_clean = payload.get("is_clean")
                if is_clean is None:
                    is_clean = conforms
                if is_clean:
                    vo = cand
            inp_hash = payload.get("input_hash")
            cache.setdefault(key, {}).setdefault(occ, []).append(
                (cur_run, inp_hash, vo))
        self._replay = cache
        # Arm the pop-side identity for the upcoming walk: positions count
        # from 0 again; the next batch number is one past the last.
        self._reset_exec_identity(spec, next_run=run)
        # restore cumulative cost/agent-count from the ledger so resume's
        # budget check continues from the prior total (not 0, which would
        # let a resumed run overspend). __init__ zeros them; a resume must
        # not "forget" cost already spent in the prior walk.
        self._cost_total = sum(
            e.get("payload", {}).get("cost_usd", 0)
            for e in self.ledger.events()
            if isinstance(e.get("payload", {}).get("cost_usd"), (int, float))
        )
        self._agent_count = sum(
            1 for e in self.ledger.events()
            if e.get("kind") in COSTED_EVENT_KINDS
        )

    def _replay_pop(self, spec_version_id: str, node_id: str,
                    upstream_values=None, exec_pos=None):
        """Consume one control-flow position and probe the slot cache.

        Returns a {"validated_output": vo} hit + emits replay_hit when the
        slot for THIS position is replayable; returns None on miss (caller
        falls through to real dispatch). Every call consumes one position
        for scoped keys even without a cache, so fresh-run events are
        tagged for future rebuilds.

        Position (R6-1): scoped keys (node appearing >1 time as top-level
        steps) advance a per-key cursor per arrival; fan-out items take the
        caller-supplied exec_pos (container-id:item-index, R7-1 -- duplicate
        inputs share input_hash, so position 0 + hash multiplexing swapped
        their replayed outputs); all other keys share position 0 (skeptics
        multiplex by input_hash, as pre-R6-1). The position becomes
        payload.exec_occurrence on every attempt event via _record's
        thread-local injection -- occurrence identity is recorded at
        dispatch, never re-derived by counting.

        Replayability (R6-1 slot state, newest batch wins): SCOPED slots
        consider only entries of the newest batch -- a batch whose latest
        matching attempt failed is a tombstone and does NOT fall back to
        an older batch's success (review6 scenario 3). UNSCOPED slots scan
        all batches newest-first per input_hash (skeptics/fan-out: N
        same-key successes across batches stay consumable,
        test_resume_skeptic_cache_populated_per_dispatch). input_hash
        secondary validation is unchanged (v2 Task 14): mismatch -> miss;
        skipped when either side is None."""
        ctx = getattr(self._loop_ctx, "ctx", None)
        loop_id = ctx["loop_id"] if ctx else None
        iteration = ctx["iteration"] if ctx else 0
        key = (spec_version_id, node_id, loop_id, iteration)
        cache = getattr(self, "_replay", None)
        # v1.4-S2 + R6-1: lock the cursor advance + entry consumption --
        # parallel skeptics share one key and race the same slot list.
        with self._replay_lock:
            scoped = key in getattr(self, "_scoped_keys", set())
            if scoped:
                k = self._exec_cursor.get(key, 0)
                self._exec_cursor[key] = k + 1
            elif exec_pos is not None:
                # R7-1: fan-out branch identity -- the container's stable
                # position tag ("<parallel/pipeline id>:<item index>").
                # Each item pops its OWN slot, so duplicate inputs replay
                # to their original positions instead of racing one shared
                # slot 0 newest-first (review7 P2: resumed [A,B] -> [B,A]).
                # Retries of the same item re-emit the same tag (a retry is
                # not a new position); the slot keeps the R6-1 newest-first
                # tombstone scan, so a newer failed attempt still blocks an
                # older success for THAT position.
                k = exec_pos
            else:
                k = 0
            self._exec_ctx.pos = k
            if not cache:
                return None  # fresh walk: tag only, never replay
            entries = cache.get(key, {}).get(k)
            legacy_slot = None
            if not entries and exec_pos is not None and k != 0:
                # R8-1 (review8 P2): pre-R7-1 ledgers slot fan-out branch
                # attempts at occurrence 0 (the shared position, multiplexed
                # by input_hash) -- there was no per-item position tag. A
                # resumed walk looking up "<container>:<idx>" would miss and
                # re-dispatch already-succeeded branches (or, with a tight
                # agent cap, exhaust the budget and return None per branch).
                # Fall back to the legacy slot 0 for THIS position slot only
                # when it has no entries of its own: once a new-format
                # attempt (success OR failure tombstone) exists at k, the
                # new data decides and the legacy slot stays untouched. The
                # legacy scan below keeps the same newest-first + input_hash
                # + tombstone rules, so a newer failed legacy attempt still
                # blocks an older legacy success. Entries that cannot be
                # matched unambiguously (duplicate inputs share one hash)
                # behave exactly as the pre-R7-1 engine's own resume did --
                # no worse, and never silently treated as a plain miss.
                legacy_slot = 0
                entries = cache.get(key, {}).get(0)
            if not entries:
                return None
            cur_hash = self._input_hash(upstream_values)
            if scoped:
                newest_run = entries[-1][0]
                candidates = [(i, t) for i, t in enumerate(entries)
                              if t[0] == newest_run]
            else:
                candidates = list(enumerate(entries))
            for i, (run_i, stored_hash, vo) in reversed(candidates):
                if (stored_hash is not None and cur_hash is not None
                        and stored_hash != cur_hash):
                    continue
                if vo is None:
                    return None  # newest matching attempt failed -> miss
                del entries[i]
                break
            else:
                return None
        hit_payload = {"validated_output": vo, "replayed": True,
                       "exec_occurrence": k, "slot_run": run_i}
        if legacy_slot is not None:
            # R8-1: audit trail -- the hit was served from the legacy
            # shared slot 0 while consuming the position tag k.
            hit_payload["legacy_slot"] = legacy_slot
        self._record({
            "event_id": self._ev_id(), "kind": "replay_hit",
            "spec_version_id": spec_version_id, "node_id": node_id,
            "payload": hit_payload,
            "claims": [],
        })
        return {"validated_output": vo}

    # --- artifacts (B3) --------------------------------------------

    def _snapshot_artifacts(self, node, glob_root=None) -> list[str]:
        """Content-address worker file outputs declared by write_areas.

        Globs are resolved against `glob_root` (defaults to project_root --
        the worker cwd), so a worker that wrote files into the project lands
        them where this can pin them. v2 Task 12 (§5 step 5): for a
        worktree-isolated dispatch, glob_root is the worktree path so the
        worker's writes inside the worktree (NOT the main checkout) are
        pinned -- the no-leak property. Each file is sha256-pinned into
        run_dir/artifacts/sha256:<hash> (centralized, content-addressed,
        stable across iterations, independent of worktree path) and the ref
        is recorded; read-only scouts (empty write_areas) produce nothing.
        Absolute/out-of-tree globs are skipped -- the harness only pins what
        it can vouch for by the glob root."""
        refs: list[str] = []
        areas = node.get("write_areas", []) if isinstance(node, dict) else []
        if not areas:
            return refs
        root = glob_root if glob_root is not None else self.project_root
        artifacts_dir = self.run_dir / "artifacts"
        for wa in areas:
            if not isinstance(wa, str) or wa.startswith("/"):
                continue
            for p in root.glob(wa):
                if not p.is_file():
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                if digest not in refs:
                    refs.append(digest)
                target = artifacts_dir / digest
                if not target.exists():
                    artifacts_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        return refs

    @staticmethod
    def _recoverable_impl_node(node) -> bool:
        """Eligibility for conform-fail artifact-recovery.

        Only recover impl nodes that do real file work but own no requirement verdict:
        - write_areas non-empty (it really edits files; work can be judged from disk evidence);
        - no verdict_field (it owns no R's verdict — verdict must come from real
          JSON, prose recovery must never forge a verdict, so verify nodes are
          excluded and follow the normal retry/stagnation/gate path).
        The impl node's self-reported JSON is not assurance evidence anyway (SKILL.md:
        the worker saying "I'm done" is not evidence, verify is), so its format error
        should not decide the gate."""
        if not isinstance(node, dict):
            return False
        if node.get("verdict_field"):
            return False
        areas = node.get("write_areas") or []
        return bool(areas)

    def _write_areas_signature(self, node, glob_root=None) -> dict:
        """{relpath: sha256} for existing files matching write_areas.

        Same glob rules as _snapshot_artifacts (skips absolute/out-of-scope globs), but
        does not write to disk or pin — only takes path + content hash for a before/after
        "was work done" comparison. An empty dict means write_areas currently match no
        file (before the first chapter adds any)."""
        sig: dict = {}
        areas = node.get("write_areas", []) if isinstance(node, dict) else []
        if not areas:
            return sig
        root = glob_root if glob_root is not None else self.project_root
        for wa in areas:
            if not isinstance(wa, str) or wa.startswith("/"):
                continue
            for p in root.glob(wa):
                if not p.is_file():
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                rel = str(p.relative_to(root))
                sig[rel] = hashlib.sha256(data).hexdigest()
        return sig

    def _emit_artifact_write(self, node, spec_version_id, refs: list[str]) -> None:
        """Record an artifact_write event pinning the worker's file outputs
        (§4.4 kind). Only emitted when the dispatch produced artifacts."""
        if not refs:
            return
        self._record({
            "event_id": self._ev_id(), "kind": "artifact_write",
            "spec_version_id": spec_version_id, "node_id": node["id"],
            "payload": {"artifact_refs": refs},
            "claims": [],
        })

    def _emit_agent_verdict(self, node, validated, spec_version_id, wt_path=None) -> None:
        """v1.3: an agent node may declare verdict_field (a key into its
        validated_output whose value is the node's own verdict, e.g. a
        code-review verifier returning {verdict: VERIFIED/FAILED}). On clean
        conform, emit an agent_verdict event so success_evidence can bind the
        requirement to this node via 'node:<id>' -- an agent owns a requirement's
        verdict the way a verify node's survived claim would. A real editing-task relay
        used an agent node to verify 4 files; without this, axiom judged PARTIAL
        despite the node returning verdict:VERIFIED.

        seam (b) assurance hook: if the node declares assurance_hook, the
        verdict is re-derived from change-assurance's adjudicate.py against the
        receipt the agent wrote into the worktree -- NOT trusted from the LLM's
        verdict_field transcription. The LLM's claimed value is recorded in
        the payload for audit; the script's status is authoritative. This
        prevents an agent from outputting assurance_verdict="VERIFIED" while its
        own receipt adjudicates PARTIAL: Agent text claims done != system
        allows done. Safe default on any failure (receipt missing, script
        error, unexpected status) is UNVERIFIED -- never a fake VERIFIED."""
        vf = node.get("verdict_field")
        if not vf or not isinstance(validated, dict):
            return
        if node.get("assurance_hook"):
            self._emit_assurance_verdict(node, validated, vf, spec_version_id, wt_path)
            return
        verdict = validated.get(vf)
        if verdict is None:
            return
        payload = {"verdict_field": vf}
        # R9-3: a reviewer declaring evidence_from binds its verdict to the
        # LATEST evidence pack of that script node. The verdict records the
        # evidence fingerprint it relied on; a later code change makes the
        # fingerprint stale and the projection (state.py) refuses to inherit
        # this verdict -- the reviewer must re-examine, never inherit.
        ev_from = node.get("evidence_from")
        if ev_from:
            ep = self._latest_evidence_pack(ev_from)
            if ep is None:
                # No machine evidence exists for the declared source: the
                # reviewer's judgment has no machine-recorded basis. Record
                # the verdict WITH an explicit degraded evidence grade so a
                # bare VERIFIED never silently means 'machine-verified'.
                payload["evidence_from"] = ev_from
                payload["evidence_id"] = None
                payload["evidence_grade"] = "none"
            else:
                payload["evidence_from"] = ev_from
                payload["evidence_id"] = ep["payload"]["evidence_id"]
                payload["evidence_grade"] = "machine"
                payload["evidence_exit_code"] = ep["payload"].get("exit_code")
        self._record({
            "event_id": self._ev_id(), "kind": "agent_verdict",
            "spec_version_id": spec_version_id, "node_id": node["id"],
            "verdict": str(verdict),
            "payload": payload,
            "claims": [],
        })

    def _latest_evidence_pack(self, node_id):
        """Latest evidence_pack event emitted by node_id (any svid scope of
        this ledger -- the pack is a fact about the worktree, and staleness
        is judged by fingerprint, not by event age)."""
        latest = None
        for e in self.ledger.events():
            if e.get("kind") == "evidence_pack" and e.get("node_id") == node_id:
                latest = e
        return latest

    def _evidence_freshness_probe(self, spec, source_node_id):
        """R9-3: recompute the CURRENT evidence fingerprint for the script
        node `source_node_id` from the spec's evidence_scope -- the same
        sha256 over (relpath, file-hash)+git_HEAD the pack was sealed with.
        Returns None when the node declares no scope (cannot judge -> the
        caller treats the verdict as not-stale-checkable, i.e. accepts).
        """
        node = None
        nodes = getattr(spec, "nodes", {}) if spec is not None else {}
        if isinstance(nodes, dict):
            node = nodes.get(source_node_id)
        if not isinstance(node, dict) or not node.get("evidence_scope"):
            return None
        sig = self._write_areas_signature({"write_areas": node["evidence_scope"]})
        head = None
        try:
            import subprocess as _sp
            head = _sp.run(["git", "rev-parse", "HEAD"],
                           cwd=str(self.project_root), capture_output=True,
                           text=True, timeout=5).stdout.strip() or None
        except Exception:
            head = None
        material = json.dumps({"files": sorted(sig.items()), "git_head": head},
                              sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()

    def _evidence_grade(self, spec) -> str:
        """The evidence level behind the current verdict projection.

        machine -- at least one required criterion is carried by an
            agent_verdict bound to a fresh evidence pack (evidence_id matches
            the current fingerprint); the strongest grade.
        none    -- an agent_verdict declared evidence_from but found no pack.
        claim   -- the classic verify/claim path (no machine packs involved).
        """
        import re as _re
        svid = getattr(spec, "spec_version_id", None)
        saw_node = False
        for se in getattr(spec, "success_evidence", []) or []:
            m = _re.search(r"node:(\w+)", se)
            if not m:
                continue
            saw_node = True
            nid = m.group(1)
            latest = None
            for e in self.ledger.events():
                if e.get("spec_version_id", svid) != svid:
                    continue
                if e.get("kind") == "agent_verdict" \
                        and e.get("node_id") == nid:
                    latest = e
            if latest is not None:
                grade = latest.get("payload", {}).get("evidence_grade")
                if grade == "machine":
                    return "machine"
                if grade == "none":
                    return "none"
        # node-bound verdicts without an evidence declaration, and the
        # classic claim path, share the 'claim' grade.
        return "claim"

    def _evidence_prompt_block(self, node) -> str:
        """Render the machine evidence block for a reviewer node declaring
        evidence_from. Empty string when the node has no such declaration or
        the source never produced an evidence pack (the verdict then records
        evidence_grade=none -- a degraded, honestly-labeled judgment)."""
        if not isinstance(node, dict) or not node.get("evidence_from"):
            return ""
        ep = self._latest_evidence_pack(node["evidence_from"])
        if ep is None:
            # R10-2: say the consequence out loud -- a VERIFIED judged
            # without the declared evidence is gated by the projection
            # (evidence_grade=none -> does not count as VERIFIED). The
            # reviewer should return FAILED/ask for the evidence instead of
            # handing out a VERIFIED that cannot stand.
            return ("[MACHINE EVIDENCE] source node "
                    f"{node['evidence_from']!r} has produced NO evidence pack "
                    "in this ledger. Judge on that basis and say so. Note: "
                    "you declared evidence_from, so a VERIFIED without the "
                    "pack will be recorded evidence_grade=none and will NOT "
                    "pass acceptance -- return FAILED or request the evidence.")
        p = ep["payload"]
        return (
            "[MACHINE EVIDENCE -- recorded by the runtime, not the worker]\n"
            f"  source node:  {node['evidence_from']}\n"
            f"  command:      {p.get('command')}\n"
            f"  exit_code:    {p.get('exit_code')}"
            f"  failure_class: {p.get('failure_class')}\n"
            f"  evidence_id:  {p.get('evidence_id')}\n"
            f"  git_head:     {p.get('git_head')}\n"
            f"  files:        {sorted((p.get('files') or {}).keys())}\n"
            f"  output preview: {p.get('stdout_preview')}\n"
            "Judge whether the requirements are met ON THIS EVIDENCE. If the "
            "evidence is insufficient, say FAILED, not a guess VERIFIED.")

    def _emit_assurance_verdict(self, node, validated, vf, spec_version_id, wt_path) -> None:
        """seam (b): derive the agent_verdict verdict value from
        change-assurance's adjudicate.py (machine), overriding the LLM's
        verdict_field transcription. The receipt must live in the agent's
        worktree (still alive at this point -- _emit_agent_verdict runs before
        _worktree_cleanup)."""
        hook = node["assurance_hook"]
        receipt_rel = hook.get("receipt_path", "receipt.json")
        # P2-11 fix: change-assurance is a SIBLING skill (~/.claude/skills/
        # change-assurance/ when axiom is at ~/.claude/skills/axiom/), not a
        # sub-skill nested under axiom/.claude/. The old default
        # (parent.parent / ".claude" / "skills" / "change-assurance") resolved
        # to <axiom_skill_root>/.claude/skills/change-assurance/ which never
        # exists -> adjudicate_script_missing -> UNVERIFIED on every
        # assurance_hook dispatch. parent.parent.parent points at the shared
        # skills/ directory (axiom and change-assurance are siblings). Env
        # CHANGE_ASSURANCE_SKILL_DIR or hook.skill_dir still overrides (e.g.
        # when axiom is pip-installed away from ~/.claude/skills/).
        skill_dir = (os.environ.get("CHANGE_ASSURANCE_SKILL_DIR")
                     or hook.get("skill_dir")
                     or str(Path(__file__).resolve().parent.parent.parent
                            / "change-assurance"))
        adjudicate = Path(skill_dir) / "scripts" / "adjudicate.py"
        llm_claimed = validated.get(vf)

        script_status: str | None = None
        note = ""
        audit = None
        receipt_path = (Path(wt_path) / receipt_rel) if wt_path else Path(receipt_rel)
        if wt_path is None:
            note = "no_worktree: cannot locate receipt"
        elif not receipt_path.is_file():
            note = f"receipt_missing: {receipt_rel}"
        elif not adjudicate.is_file():
            note = f"adjudicate_script_missing: {adjudicate}"
        else:
            try:
                r = subprocess.run(
                    [sys.executable, str(adjudicate), "--receipt", str(receipt_path)],
                    capture_output=True, text=True, timeout=60)
                if r.returncode == 0 and r.stdout:
                    out = json.loads(r.stdout)
                    script_status = out.get("status")
                    audit = out
                else:
                    note = f"adjudicate_exit_{r.returncode}: {(r.stderr or '').strip()[:200]}"
            except subprocess.TimeoutExpired:
                note = "adjudicate_timeout"
            except (json.JSONDecodeError, OSError) as e:
                note = f"adjudicate_error: {e}"

        allowed = {"VERIFIED", "PARTIAL", "UNVERIFIED", "BLOCKED"}
        if script_status in allowed:
            verdict_val = script_status
        else:
            verdict_val = "UNVERIFIED"  # safe default: never a fake VERIFIED
            if script_status is not None:
                note = f"unexpected_status:{script_status}"

        payload = {"verdict_field": vf, "assurance_hook": True,
                   "llm_claimed": (str(llm_claimed) if llm_claimed is not None else None),
                   "script_status": script_status, "verdict": verdict_val}
        if note:
            payload["note"] = note
        if llm_claimed is not None and str(llm_claimed) != verdict_val:
            payload["mismatch"] = True  # LLM claimed != script; script wins
        if audit is not None:
            payload["audit"] = {
                "risk": audit.get("risk"), "risk_floor": audit.get("risk_floor"),
                "violations": audit.get("violations", []),
                "missing_evidence": audit.get("missing_evidence", []),
                "blockers": audit.get("blockers", []),
            }
        self._record({
            "event_id": self._ev_id(), "kind": "agent_verdict",
            "spec_version_id": spec_version_id, "node_id": node["id"],
            "verdict": verdict_val,
            "payload": payload,
            "claims": [],
        })

    @staticmethod
    def _lookup(key: str, values: dict):
        cur = values
        for p in key.split("."):
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return None
        return cur

    def _render(self, template: str, values: dict) -> str:
        def repl(m):
            v = self._lookup(m.group(1), values)
            return v if isinstance(v, str) else json.dumps(v)
        return re.sub(r"\{\{([^}]+)\}\}", repl, template)

    def _resolve(self, ref: str, values: dict) -> list:
        """Resolve a {{ref}} to a list of items (for over/items)."""
        if ref.startswith("{{") and ref.endswith("}}"):
            v = self._lookup(ref[2:-2], values)
            if isinstance(v, list):
                return v
            return [v] if v is not None else []
        return [ref]

    # --- v2 predicate DSL (spec §2) -----------------------------------

    def _eval_predicate(self, expr, ctx, spec, ledger, svid,
                        node_id=None, loop_id=None, iteration=None):
        """Evaluate a deterministic predicate over ledger projections (§2).

        Restricted `eval(expr, {"__builtins__": {}}, built_ctx)` with NO
        builtins (`__import__` must raise). {{ref}} placeholders are
        pre-resolved via _lookup before eval. Returns (value, claim_strength):
          - value: bool | 'undefined' ('undefined' on unresolvable ref /
            NameError so callers take a safe default)
          - claim_strength: 'verified' | 'proposed' | 'deterministic'

        cannot-branch-on-PROPOSED (§2 Q2): a predicate referencing a raw
        agent validated_output field (a PROPOSED claim, via a {{ref}})
        yields claim_strength='proposed' (advisory); one reading
        claim_status(...) yields 'verified' (binding); otherwise
        'deterministic'.

        NEVER calls an LLM — this preserves invariant 1 (orchestrator reasons
        only between workflows, never between nodes). A predicate evaluation
        is a FACT (invariant 3), not a claim.
        """
        # --- cannot-branch-on-PROPOSED: detect claim_strength from expr ---
        has_ref = bool(re.search(r"\{\{[^}]+\}\}", expr))
        has_claim_status = "claim_status(" in expr
        if has_ref:
            claim_strength = "proposed"      # raw validated_output = PROPOSED
        elif has_claim_status:
            claim_strength = "verified"       # reads VERIFIED truth
        else:
            claim_strength = "deterministic"  # pure projection (dry_count, ...)

        # --- pre-resolve {{ref}} placeholders via _lookup ---
        # unresolvable ref (None from _lookup) -> 'undefined' (safe default)
        for m in re.finditer(r"\{\{([^}]+)\}\}", expr):
            if Harness._lookup(m.group(1).strip(), ctx) is None:
                return ("undefined", claim_strength)

        def _repl(m):
            v = Harness._lookup(m.group(1).strip(), ctx)
            return repr(v)

        resolved = re.sub(r"\{\{([^}]+)\}\}", _repl, expr)

        # --- built-in context functions (closures over ledger/spec/svid) ---
        def _claim_status(claim_id):
            return derive_claim_status(claim_id, ledger, svid)

        def _verdict():
            # R10-1: the predicate reads the SAME freshness-gated projection
            # as checkpoint/verdict -- an until:/repeat-exit predicate must
            # not exit a fix loop on a stale VERIFIED.
            return derive_verdict(spec, ledger,
                                  evidence_probe=self._evidence_freshness_probe)

        def _budget_used():
            return sum(
                e.get("payload", {}).get("cost_usd", 0)
                for e in ledger.events()
                if isinstance(e.get("payload", {}).get("cost_usd"),
                              (int, float))
            )

        def _budget_remaining():
            return budget_limit(spec) - _budget_used()

        def _dry_count():
            return self._derive_dry_count(ledger, svid, loop_id, iteration)

        def _iteration():
            return iteration if iteration is not None else 0

        def _stagnation_count():
            count = 0
            for e in ledger.events():
                if (e.get("kind") == "stagnating"
                        and e.get("spec_version_id", svid) == svid
                        and (node_id is None
                             or e.get("node_id") == node_id)):
                    count += 1
            return count

        built_ctx = {
            "claim_status": _claim_status,
            "verdict": _verdict,
            "budget_used": _budget_used,
            "budget_remaining": _budget_remaining,
            "dry_count": _dry_count,
            "iteration": _iteration,
            "stagnation_count": _stagnation_count,
        }

        # --- restricted eval (NO builtins; __builtins__ empty) ---
        # P1-7 fix: empty __builtins__ alone does NOT close the sandbox --
        # function objects in built_ctx carry module __globals__, so
        # `verdict.__globals__['Path'](...).read_text()` escapes to arbitrary
        # file/network access despite the empty builtins. AST-gate BEFORE eval:
        # reject attribute access, subscripts, comprehensions, lambdas, and
        # any call whose func is not a bare name in built_ctx. A forbidden node
        # degrades the predicate to 'undefined' (safe default -> else-branch).
        import ast as _ast
        try:
            _tree = _ast.parse(resolved, mode="eval")
            for _n in _ast.walk(_tree):
                if isinstance(_n, (_ast.Attribute, _ast.Subscript, _ast.Starred,
                                   _ast.Lambda, _ast.ListComp, _ast.SetComp,
                                   _ast.DictComp, _ast.GeneratorExp, _ast.IfExp)):
                    return ("undefined", claim_strength)
                if isinstance(_n, _ast.Call):
                    if (not isinstance(_n.func, _ast.Name)
                            or _n.func.id not in built_ctx
                            or _n.keywords):
                        return ("undefined", claim_strength)
            value = eval(compile(_tree, "<predicate>", "eval"),
                         {"__builtins__": {}}, built_ctx)
        except Exception:
            return ("undefined", claim_strength)

        return (bool(value), claim_strength)

    def _derive_dry_count(self, ledger, svid, loop_id, iteration):
        """Delegate to the shared state.py projection (single source of truth,
        spec §4). Task 2's `dry_count()` predicate built-in calls this; Task 6's
        state.py projection calls derive_dry_count directly. Both get the same
        re-derivation. The body lives in state.derive_dry_count so invariant
        (6) (honest projection -- rebuildable from events.jsonl) has one impl.
        """
        return derive_dry_count(ledger, svid, loop_id, iteration)

    @staticmethod
    def _extract_first_json_object(text):
        """Scan for the first balanced {...} substring, honoring string
        literals and escapes so a brace inside a string does not close the
        object. Returns the substring or None if no balanced object found.
        Array brackets inside the object are carried along verbatim (the
        outer object's braces bound the scan; json.loads parses the whole).
        """
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None  # unbalanced

    @staticmethod
    def _parse_validated(result_text: str) -> dict:
        """Parse the worker's result_text into a dict, tolerating markdown
        code fences and JSON embedded in prose. the schema directive
        is not reliably enforced, so workers often wrap or surround JSON;
        this recovers it. The recovered value STILL passes _conforms before
        flowing downstream -- the type boundary is not bypassed, only the
        parse is more forgiving. Pure prose with no JSON yields {} (that gap
        is owned by G3 feedback retry + G4 abstain, not by guessing prose).
        """
        if not result_text:
            return {}
        # 1. whole-text parse (the n_r2 relay shape: pure JSON)
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            pass
        # 2. strip a markdown code fence then re-parse
        s = result_text.strip()
        if s.startswith("```"):
            nl = s.find("\n")
            if nl != -1:
                s = s[nl + 1:]
            if s.endswith("```"):
                s = s[:-3]
            s = s.strip()
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
        # 3. extract the first balanced {...} (JSON embedded in prose)
        obj = Harness._extract_first_json_object(result_text)
        if obj is not None:
            try:
                return json.loads(obj)
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _parse_validated_strict(result_text: str) -> dict:
        """F1 conform_strict_json: strict JSON parsing at the verdict level.

        Accepts only "the entire reply is a single JSON document" (leading/trailing
        whitespace tolerated; the whole reply wrapped in a single markdown fence is
        also tolerated — the worker means JSON, stripping the fence is unambiguous).
        Forbids first-object prose extraction: a {...} buried in prose may be an example,
        a quote, or an aside, not the worker's structured conclusion — extracting a
        verdict from prose lets the harness speak for the worker, a hole in assurance
        integrity. If nothing can be parsed, return {} and go through conform-fail retry
        (G3 feedback will tell the worker explicitly that it must output pure JSON).
        """
        if not result_text:
            return {}
        s = result_text.strip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        # The whole reply is a single fence block (starts ``` and ends ```): strip and parse the full text.
        if s.startswith("```") and s.endswith("```") and len(s) > 6:
            inner = s
            nl = inner.find("\n")
            if nl != -1:
                inner = inner[nl + 1:]
            inner = inner[:-3].strip()
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _strict_json(node) -> bool:
        """F1: conform_strict_json resolution rule (explicit > default).

        Explicit true/false takes priority; when omitted, a verdict node (declaring
        verdict_field) defaults to strict — the verdict must come from pure JSON output,
        a {...} extracted from prose is not a verdict; an impl node defaults to tolerant
        (keeps lenient parsing + P-006 file-evidence recovery — whether work was done is
        decided by disk evidence, the report format should not decide success).

        A second effect of strict: P-006 prose recovery does not apply (strict = JSON or bust;
        verdict nodes are already excluded by _recoverable_impl_node; here an explicitly
        strict impl node is also excluded).
        """
        if not isinstance(node, dict):
            return False
        flag = node.get("conform_strict_json")
        if flag is not None:
            return bool(flag)
        return bool(node.get("verdict_field"))

    @staticmethod
    def _action_scope_hash(node) -> "str | None":
        """P1-11: content-addressed scope of a gated node's risky action =
        sha256(write_areas + risk + prompt). Gate authorization must bind to
        this scope, not just node_id: v1 authorizing action A (risk:high on
        write_areas X) must NOT auto-authorize v2's action B (same node_id,
        different write_areas / prompt) without a new gate. Returns None when the
        node has no risk/secrets/delete triggers (check_gate short-circuits
        before this is called)."""
        if not isinstance(node, dict):
            return None
        import hashlib, json
        try:
            return hashlib.sha256(json.dumps({
                "write_areas": sorted(node.get("write_areas", []) or []),
                "risk": node.get("risk"),
                "prompt": node.get("prompt", ""),
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_clean_attempt(r, conforms) -> bool:
        """P1-6: execution success = process exited 0 AND no envelope is_error
        AND output conforms AND no permission_denials. retry_class means "the
        run completed / how to retry", NOT "it succeeded". A worker that
        FAILED (non-zero exit / envelope is_error) but happened to emit
        schema-conforming JSON with verdict:VERIFIED must NOT become success
        evidence. Top-level _run_agent_with_retry checked exit_code==0 (P1-3)
        but not is_error, and dispatch_agent (parallel/pipeline/skeptic path)
        checked neither -- this helper unifies both and is applied at every
        dispatch success gate.

        P-015: denials are part of the clean identity too. dispatch_agent
        returns None on permission_denials (cognitive failure), but the
        agent_result event was recorded with is_clean computed WITHOUT the
        denial -- so build_replay_cache admitted the denied attempt as a
        clean success and resume replayed it with zero fresh dispatches
        (the failed_dispatch_replayed hole). A denied worker was refused a
        tool its plan needed; its output is not verified-completed work."""
        return (r.exit_code == 0 and not r.is_error and bool(conforms)
                and not getattr(r, "permission_denials", None))

    @staticmethod
    def _recovery_disclosure(values) -> str:
        """#8 recovery_schema: surface upstream prose-recovery gaps to EVERY
        downstream consumer, harness-enforced (G1-directive style) -- not via
        a template ref the spec author must remember (P-008 lesson: injected
        values the prompt never references are invisible to the worker).

        Scans the template values for upstream outputs marked
        `_recovered:true` (direct {"validated_output": {...}} entries and
        the parallel-barrier {"validated_output": [vo, ...]} list form) and
        returns a disclosure block naming each recovered node and its
        missing_required_fields. "" = no recovered upstream, prompt
        unchanged. The verdict node's whole job is adjudicating whether the
        recovery was correct -- it cannot do that blind to which fields the
        worker never produced."""
        if not isinstance(values, dict):
            return ""
        hits = []  # (node_id, missing_required_fields)
        for key, val in values.items():
            if not isinstance(val, dict):
                continue
            # bare recovered output (e.g. a skeptic's vu={"finding": f})
            if val.get("_recovered"):
                rec = val.get("_recovery") or {}
                hits.append((key, rec.get("missing_required_fields") or []))
                continue
            vo = val.get("validated_output")
            items = vo if isinstance(vo, list) else [vo]
            for it in items:
                if isinstance(it, dict) and it.get("_recovered"):
                    rec = it.get("_recovery") or {}
                    hits.append((key,
                                 rec.get("missing_required_fields") or []))
        if not hits:
            return ""
        lines = [
            "\n\n[UPSTREAM EVIDENCE QUALITY DISCLOSURE - must read]",
            "The following upstream nodes' output is NOT the worker's normal structured report; "
            "it was rebuilt by the harness from disk file evidence "
            "(the worker returned schema-nonconforming prose, but write_areas files were actually changed).",
            "When adjudicating, note:",
        ]
        for nid, missing in hits:
            if missing:
                lines.append(
                    f"- node {nid}: schema-required fields {missing} were never produced by the worker "
                    "— treat them as MISSING, not as empty/default/confirmed facts.")
            else:
                lines.append(
                    f"- node {nid}: output is a recovery rebuild (files_modified from disk hash, "
                    "summary from a prose excerpt); other field contents cannot be trusted as the worker's intent.")
        lines.append(
            "This information does not affect the format requirements for the JSON you return; "
            "it only affects how you interpret the upstream evidence.")
        return "\n".join(lines)

    @staticmethod
    def _build_worker_prompt(node, rendered_prompt, recovery_disclosure=""):
        """Inject a hard output-format directive into the worker prompt when
        the node declares an output_schema. the schema directive is not
        reliably enforced by the worker, but a text directive naming the
        schema and forbidding prose/markdown fences IS read by the worker --
        this is the primary defense against the pure-prose failure mode the
        relay exposed.

        FRONT + END (v1.2 was end-only). The end-only form lost to the
        worker's habit of reporting results in prose after a long edit
        task -- the directive was buried at the prompt tail. Leading with it
        (and echoing at the end) makes the worker comply: relay-verified on
        a real multi-turn file-edit task (run 1, node n1) where end-only returned
        prose ("feature already exists...") and front+end returned conforming JSON.

        #8: also inject the node's `acceptance` criteria so the worker
        self-checks before returning. acceptance was a dead field (validate
        required it but no agent ever saw it); now it lands in the prompt
        regardless of whether a schema is declared.

        recovery_disclosure (#8 recovery_schema): an optional harness-built
        block disclosing upstream prose-recovery gaps (which schema-required
        fields the recovered node never produced). Placed right after the
        task prompt so the worker reads the evidence-quality context before
        its own output-format duties.
        """
        if not isinstance(node, dict):
            return rendered_prompt
        acceptance = node.get("acceptance") or []
        acc_block = ""
        if acceptance:
            items = "\n".join(f"- {a}" for a in acceptance)
            acc_block = (
                "\n\n[ACCEPTANCE CRITERIA - self-check before returning]\n"
                "Your output must satisfy all of the following to be considered complete:\n"
                f"{items}\n"
            )
        schema = node.get("output_schema")
        if not schema:
            return rendered_prompt + recovery_disclosure + acc_block
        schema_json = json.dumps(schema, ensure_ascii=False)
        front = (
            "[OUTPUT FORMAT - most important - must strictly follow]\n"
            "Your entire reply must be ONLY a JSON object conforming to the JSON Schema below.\n"
            "No natural language, no explanation, no preamble, no markdown code fences (```), "
            "and no text before or after the JSON.\n"
            "Even if you believe the feature already exists or no change is needed, you must "
            "still only return a JSON object reporting status.\n"
            f"JSON Schema: {schema_json}\n"
            "================\n\n"
        )
        end = (
            "\n\n================\n"
            "[REPEAT] After completing the task above, your reply must be ONLY a JSON object "
            "conforming to the Schema above. No natural language. Output JSON only."
        )
        return front + rendered_prompt + recovery_disclosure + acc_block + end

    @staticmethod
    def _validate_node(value, schema) -> bool:
        """Minimal JSON-Schema-subset conformance (stdlib; no `jsonschema` dep).

        Checks type, required keys, nested properties, and array items. A
        schema with no constraints (e.g. {"type":"object"} + any dict, or a
        schema with no type) is satisfied by anything of matching shape.
        Booleans are NOT integers (JSON-Schema convention: `True` is bool,
        not a valid integer/number).
        """
        if not isinstance(schema, dict):
            return True
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False
        elif t == "array":
            if not isinstance(value, list):
                return False
        elif t == "string":
            if not isinstance(value, str):
                return False
        elif t == "integer":
            # JSON-Schema: an integer is "a number with a zero fractional part",
            # so a float like 5.0 / 0.0 (json.loads of "5.0") IS a valid integer
            # (python jsonschema and ajv both accept it). bool is NOT an integer.
            if isinstance(value, bool):
                return False
            if isinstance(value, int):
                pass
            elif isinstance(value, float) and value.is_integer():
                pass
            else:
                return False
        elif t == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
        elif t == "boolean":
            if not isinstance(value, bool):
                return False
        # type omitted / unrecognized -> skip the type check, still check
        # the structural constraints below.
        if isinstance(value, dict):
            for k in schema.get("required", []):
                if k not in value:
                    return False
            props = schema.get("properties", {})
            for k, sub in props.items():
                if k in value and not Harness._validate_node(value[k], sub):
                    return False
        if isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, dict):
                for v in value:
                    if not Harness._validate_node(v, items):
                        return False
        return True

    def _conforms(self, value, schema) -> bool:
        """Does `value` satisfy the node's output_schema?

        An empty/None schema (unconstrained) is always satisfied -- the
        boundary only rejects when a schema declares a constraint the output
        violates. CRITICAL: the schema directive is not reliably
        enforced by the worker, so the harness owns the type boundary here.
        """
        if not schema:
            return True
        return Harness._validate_node(value, schema)

    @staticmethod
    def _schema_fail_keys(value, schema) -> "frozenset":
        """Field-level failure keys for a non-conforming value, so the
        stagnation signature distinguishes 'missing claim_id' from 'missing
        module' -- two DIFFERENT schema failures are a cognitive delta, not
        no-delta stagnation. Stable frozenset; {'schema'} fallback when no
        field can be named (non-dict value / non-object schema).

        Delegates to state.derive_schema_fail_keys (single source of truth;
        the debug-projection command reconstructs the same signature without
        re-implementing the algorithm)."""
        from axiom.state import derive_schema_fail_keys
        return derive_schema_fail_keys(value, schema)

    @staticmethod
    def _cognitive_sig(denials, conforms, schema_fail_detail=None) -> "tuple | None":
        """Signature of a cognitive failure for the stagnation (no-delta) guard.

        Delegates to state.derive_cognitive_signature (single source of truth)
        so the runtime stagnation judgment and the debug-projection stay
        byte-consistent."""
        from axiom.state import derive_cognitive_signature
        return derive_cognitive_signature(denials, conforms, schema_fail_detail)

    def _exhaust(self, node, spec_version_id, reason, localized=False):
        """Handle retry/stagnation exhaustion per the node's failure_policy.

        on_exhausted=block (default) -> open a Gate so the run pauses honestly
        (an unresolved gate makes derive_verdict -> BLOCKED). on_exhausted=
        degrade -> soft return None; the gap surfaces via the verdict machine
        (UNVERIFIED/PARTIAL), with no Gate. This replaces the old silent
        `return None` that contradicted a declared `block` policy.
        on_exhausted=replan -> emit a replan_requested event (no Gate, soft
        None); the orchestrator sees it at the workflow boundary via
        checkpoint.open_questions and draws the next spec. A node-level replan
        is NOT a synchronous callback -- it is a boundary signal (§5.3, §0.1).

        localized=True (parallel-body / pipeline-stage agents) -> always soft:
        a parallel fan-out is failure-tolerant by design, so one branch
        exhausting must not hard-stop the run. The declared on_exhausted
        applies to the agent as a solo step, not as a localized branch.

        F3: on_stagnation cause-level override. When stagnation_exhausted (the
        same failure signature recurs = the worker is genuinely stuck, not a
        transient fault) and failure_policy declares on_stagnation, it is used
        instead of on_exhausted. Typical: on an impl->verify chain,
        on_stagnation:"degrade" -- a stagnating gate auto-passes downstream
        for verify adjudication (relay runs 1-7 showed: manually resolving a
        stagnating gate is almost always allow, and verify is the real
        adjudicator; paired with fix_loop, a FAILED verify auto-routes back to
        impl), while retries_exhausted (operational/cognitive failure) still
        blocks for a human gate.
        """
        if localized:
            return None
        fp = node.get("failure_policy", {})
        mode = fp.get("on_exhausted", "block")
        if reason == "stagnation_exhausted":
            mode = fp.get("on_stagnation", mode)
        if mode == "replan":
            self._record({
                "event_id": self._ev_id(), "kind": "replan_requested",
                "spec_version_id": spec_version_id, "node_id": node["id"],
                "payload": {"reason": reason}, "claims": [],
            })
            return None
        if mode == "block":
            self._gate(reason, node["id"], spec_version_id)
        # degrade -> soft None (no Gate; surfaces via verdict machine)
        return None

    @staticmethod
    def _preview(text, limit=800):
        """Truncate raw worker output for ledger audit.

        On a schema-failure / stagnation the worker's raw result_text is not
        logged in full (it may be large or free-form prose), but `conforms=False`
        alone can't diagnose whether the worker returned non-JSON prose (the
        `--json-schema` not enforced by the worker) or a valid JSON the
        validator wrongly rejected. A short preview of the raw output is
        audited so that distinction is visible without re-running. Empty input
        returns "" (no synthetic marker).
        """
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return text[:limit] + f" …[{len(text) - limit} more chars, truncated]"

    # --- caps -------------------------------------------------------

    def _check_caps(self, spec) -> bool:
        """Return True (and log) if budget or agent-count cap is exceeded."""
        if spec is None:
            return False
        if self._cost_total > budget_limit(spec):
            self._record({
                "event_id": self._ev_id(), "kind": "budget_exhausted",
                "payload": {"cost_total": self._cost_total},
            })
            return True
        if self._agent_count > getattr(spec, "max_agents", 1000):
            # P-025: the reservation path logs the trip once via
            # _log_cap_trip_once; only re-log if that marker was never set
            # (e.g. a resume rebuild put the count over the cap before any
            # reservation was attempted).
            if not getattr(self, "_agent_cap_logged", False):
                self._agent_cap_logged = True
                self._record({
                    "event_id": self._ev_id(), "kind": "agent_cap_exhausted",
                    "payload": {"agent_count": self._agent_count},
                })
            return True
        return False

    def _budget_already_exceeded(self, spec) -> bool:
        """v1.4-S3: pre-dispatch budget check. True if cumulative cost has
        already crossed budget_usd (the budget_exhausted event was logged when
        it first tripped in _check_caps). Callers refuse to dispatch silently
        (no new event) so no further cost is incurred after the boundary."""
        if spec is None:
            return False
        return self._cost_total > budget_limit(spec)

    def _agent_cap_already_exceeded(self, spec) -> bool:
        """P1-6 fix: pre-dispatch agent-count check, symmetric with the budget
        pre-check. True if the cumulative agent dispatch count has already
        reached max_agents (the agent_cap_exhausted event was logged when it
        first tripped in _tally's _check_caps). Before the fix, only budget had
        a pre-check; agent_count was checked POST-dispatch in _tally, so a
        max_agents=1 four-node workflow dispatched all 4 nodes (the cap logged
        but never stopped subsequent dispatches)."""
        if spec is None:
            return False
        return self._agent_count >= getattr(spec, "max_agents", 1000)

    def _log_cap_trip_once(self, spec) -> None:
        """P-025: log the agent_cap_exhausted event on the FIRST refused
        reservation only. Reservation refusals happen pre-dispatch and can be
        many (every fan-out item past the cap); _check_caps logs
        unconditionally, so calling it per refusal would flood the chain with
        duplicate cap events. Idempotent marker, atomic under _state_lock."""
        if spec is None:
            return
        with self._state_lock:
            if getattr(self, "_agent_cap_logged", False):
                return
            if self._agent_count >= getattr(spec, "max_agents", 1000):
                self._agent_cap_logged = True
                self._record({
                    "event_id": self._ev_id(), "kind": "agent_cap_exhausted",
                    "payload": {"agent_count": self._agent_count},
                })

    # --- v2 loop caps (spec §8) -------------------------------------------

    def _iteration_stagnated(self, svid, loop_id, iteration) -> bool:
        """True iff the body of iteration k emitted at least one `stagnating`
        event tagged with loop_id+iteration (a retry hit the same cognitive
        signature). A re-derivation from events (invariant 6) — the truth is
        rebuilt from the ledger, never a cached streak counter. Used by both
        `_run_repeat` and `_run_until` for the stagnation cap (§8)."""
        for e in self.ledger.events():
            if e.get("kind") != "stagnating":
                continue
            if e.get("spec_version_id", svid) != svid:
                continue
            p = e.get("payload", {})
            if (p.get("loop_id") == loop_id
                    and p.get("iteration") == iteration):
                return True
        return False

    def _stagnation_cap_tripped(self, spec, svid, loop_id, iteration,
                                consecutive_stagnating):
        """Stagnation cap (spec §8, Task 11). Single-sourced for BOTH
        `_run_repeat` and `_run_until`. Returns `(tripped, new_count)`.

        An iteration is "stagnating" iff its body emitted a `stagnating` event
        (a retry hit the same cognitive signature — no cognitive delta). The
        cap counts CONSECUTIVE stagnating iterations: a non-stagnating iteration
        resets the streak to 0. When the consecutive count EXCEEDS
        `spec.max_stagnation` (strict >), the loop breaks honestly — stops
        infinite-retry of a body that isn't progressing.

        Called by the loop body AFTER the convergence check, so a converged
        iteration never trips the cap (success wins over stagnation). The
        caller passes the running consecutive count; this returns the updated
        count (reset to 0 on a non-stagnating iteration, +1 on a stagnating
        one) so the caller can carry it to the next iteration.
        """
        stag = self._iteration_stagnated(svid, loop_id, iteration)
        new_count = consecutive_stagnating + 1 if stag else 0
        max_stag = getattr(spec, "max_stagnation", 1) if spec else 1
        return (new_count > max_stag, new_count)

    # --- agent dispatch (type boundary) ----------------------------

    # --- v2 Task 12: worktree isolation intercept (spec §5) -----------
    # When node.get("isolation")=="worktree", dispatch_agent and
    # _run_agent_with_retry wrap the dispatch in: create a fresh git worktree
    # (only tracked files), symlink runtime_assets globs (data/.env/namespace
    # pkgs -- the runtime-asset symlink fix) from the main checkout into the worktree,
    # override cwd + _snapshot_artifacts glob root to the worktree (no leak to
    # main), pin output content-addressed to run_dir/artifacts (already done by
    # _snapshot_artifacts), cleanup in a finally. No ledger events for the
    # mechanics (only artifact_write captures output; invariant 6).

    def _worktree_dispatch_hash(self, node, upstream_values) -> str:
        """Deterministic per (node, upstream_values) worktree path suffix.
        sha256(json.dumps(upstream_values, sort_keys=True))[:12]."""
        try:
            blob = json.dumps(upstream_values, sort_keys=True,
                              ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            blob = repr(upstream_values).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def _auto_detect_runtime_assets(self, project_root) -> list[str]:
        """Empty runtime_assets -> auto-detect the gitignored runtime assets
        the worker's code needs to RUN: data/ (DB), .env (config), and
        top-level importable dirs (namespace packages like a project's gitignored
        app dir). Spec §5 step 2."""
        globs: list[str] = []
        pr = project_root
        if (pr / "data").exists():
            globs.append("data/**")
        if (pr / ".env").exists():
            globs.append(".env")
        for p in sorted(pr.iterdir()):
            name = p.name
            if name in ("data",) or name.startswith("."):
                continue
            if name in ("tests", "test", "node_modules", "venv", ".venv",
                        "dist", "build", "__pycache__", "run", "artifacts"):
                continue
            if p.is_dir() and (p / "__init__.py").exists():
                globs.append(f"{name}/**")
        return globs

    def _provision_runtime_assets(self, wt_path, project_root, globs):
        """Symlink each runtime_assets glob's top-level segment from the main
        checkout into the worktree. A fresh git worktree holds only tracked
        files; gitignored runtime assets (data/ DB, .env, namespace pkgs) are
        absent -> the worker's import app / DB / config fail (a real relay
        manually symlinked app+.env+data in v1; v2 automates it). Symlink (preferred
        -- shared state, no copy) the root-relative prefix (the part of the
        glob before any wildcard) so the worker finds it at the same relative
        path from inside the worktree."""
        if not globs:
            globs = self._auto_detect_runtime_assets(project_root)
        for g in globs:
            if not isinstance(g, str) or g.startswith("/"):
                continue
            # root-relative prefix: everything before the first wildcard char
            prefix = g.split("*")[0].split("?")[0].rstrip("/")
            if not prefix:
                continue
            src = project_root / prefix
            if not src.exists() and not src.is_symlink():
                continue
            dst = wt_path / prefix
            if dst.is_symlink() or dst.exists():
                continue  # already provisioned (or tracked file in worktree)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(src, dst)
            except (OSError, FileExistsError):
                pass  # best-effort; the worker will surface a real failure

    def _create_worktree(self, wt_path, project_root) -> bool:
        """Create a fresh git worktree at wt_path (checks out only tracked
        files -- gitignored/untracked runtime assets stay absent). Returns
        True on success. Falls back to a plain mkdir if project_root is not a
        git repo (the worker must then create dirs itself); the runtime_assets
        symlinks still bring in data/.env/namespace pkgs."""
        wt_path = Path(wt_path)
        if wt_path.exists():
            self._remove_worktree_dir(wt_path, project_root)
        try:
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", "-f", str(wt_path)],
                cwd=str(project_root),
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return True
        except (subprocess.SubprocessError, OSError):
            pass
        # non-git fallback: an empty dir; the worker creates what it needs.
        wt_path.mkdir(parents=True, exist_ok=True)
        return False

    def _remove_worktree_dir(self, wt_path, project_root):
        """Remove the worktree directory. Symlinks we created are unlinked
        FIRST so rmtree/git-worktree-remove cannot follow them into the main
        checkout (the no-leak / no-destroy guarantee)."""
        wt_path = Path(wt_path)
        if not wt_path.exists() and not wt_path.is_symlink():
            return
        # 1. unlink every symlink inside the worktree (never follow them)
        for p in wt_path.rglob("*"):
            try:
                if p.is_symlink():
                    p.unlink()
            except OSError:
                pass
        # 2. git worktree remove --force (also clears .git/worktrees metadata)
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=str(project_root),
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            pass
        # 3. fallback rmtree (symlinks already gone -> cannot follow out)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

    def _worktree_setup(self, node, upstream_values, spec_version_id):
        """Spec §5 steps 1-2: create the worktree + provision runtime_assets.
        Returns the worktree Path (the worker's project_root for this
        dispatch), or None when isolation != "worktree". Emits NO ledger
        events (runtime mechanics, invariant 6)."""
        if node.get("isolation") != "worktree":
            return None
        node_id = node.get("id", "agent")
        dh = self._worktree_dispatch_hash(node, upstream_values)
        wt_path = self.run_dir / f"wt_{node_id}_{dh}"
        self._create_worktree(wt_path, self.project_root)
        self._provision_runtime_assets(
            wt_path, self.project_root, list(node.get("runtime_assets", [])))
        return wt_path

    def _worktree_cleanup(self, wt_path):
        """Spec §5 step 7: remove the worktree dir + tracked-file edits;
        symlinked runtime assets are left untouched (they point into the main
        checkout and were unlinked in _remove_worktree_dir, NOT followed).
        Skipped when self.keep_worktrees (debugging)."""
        if wt_path is None:
            return
        if self.keep_worktrees:
            return
        self._remove_worktree_dir(wt_path, self.project_root)

    def dispatch_agent(self, node, upstream_values, spec_version_id, spec=None, localized=False, exec_pos=None):
        """Single worker dispatch.

        Returns {'validated_output': ...} on clean success, None on failure
        (operational / unretriable / cognitive-with-denials). Logs an
        agent_result event. Type boundary: only validated_output leaves.

        localized=True marks a fan-out branch (parallel-body / pipeline-stage /
        skeptic): its unretriable failure is soft (no Gate) -- failure
        localization, so one branch's auth error does not hard-stop the run.

        exec_pos (R7-1): fan-out callers pass the branch's stable identity
        ("<container id>:<item index>") so the replay cache slots this
        dispatch at its own position -- duplicate fan-out inputs replay to
        their original positions instead of multiplexing one shared slot
        by input_hash alone (review7: resumed output order swapped).
        """
        # A4: replay cache -- a prior successful dispatch for this (svid, node)
        # is returned instantly (zero dispatch, zero cost) on resume re-walk.
        # v2 Task 14: pass upstream_values for input_hash secondary validation
        # (§7: guards over-list-change -> stale hit -> miss + re-dispatch).
        hit = self._replay_pop(spec_version_id, node["id"], upstream_values,
                               exec_pos=exec_pos)
        if hit is not None:
            return hit
        # v1.4-S3: refuse to dispatch if the budget was already crossed (the
        # budget_exhausted event was logged when it first tripped). Prevents
        # further cost after the honest-exit boundary.
        # P-016: agent-cap pre-check is symmetric here, not only in
        # _run_agent_with_retry -- parallel/pipeline bodies call dispatch_agent
        # DIRECTLY (localized=True), so a max_agents=1 fan-out dispatched every
        # item (the cap only logged agent_cap_exhausted post-dispatch in
        # _tally, never stopping the next one). Check BEFORE the worker call.
        if self._budget_already_exceeded(spec):
            return None
        # v1.3.2: gate high-risk / secrets / delete nodes BEFORE dispatch.
        # check_gate was dead code (never called) -- a risk:high node ran the
        # worker anyway. Gate it and refuse to dispatch (await human resolve).
        # R5-3: the gate check now runs BEFORE the slot reservation -- a
        # gated node never spends a dispatch slot. (The retry-loop path in
        # _run_agent_with_retry always checked the gate first; this makes
        # the direct dispatch_agent path -- parallel/pipeline/skeptic --
        # symmetric with it.)
        if self.check_gate(node):
            self._gate("risk_high", node["id"], spec_version_id,
                      action_scope_hash=Harness._action_scope_hash(node))
            return None
        # P-025: ATOMIC slot reservation (check-then-increment in one lock) --
        # a plain read pre-check races under real concurrency (two threads
        # both pass a read of _agent_count, both dispatch). The reservation
        # IS the count; _tally no longer increments. R5-3: any refusal AFTER
        # this point must _release_agent_slot before returning None.
        if not self._reserve_agent_slot(spec):
            # log agent_cap_exhausted once (at the transition), not per
            # refused item -- _check_caps logs unconditionally.
            self._log_cap_trip_once(spec)
            return None
        # v2 Task 12 (spec §5): worktree isolation intercept. setup AFTER
        # replay/budget/gate checks so a cache hit or a gate does NOT create a
        # worktree (spec: cached validated_output is replayed without
        # recreating the worktree; the artifact_ref is already pinned).
        wt_path = self._worktree_setup(node, upstream_values, spec_version_id)
        if wt_path is None and node.get("isolation") == "worktree":
            # R5-3: isolation was requested but no worktree could be set up
            # (non-git project_root, etc.) -- the dispatch will not run, so
            # the reservation must be returned.
            self._release_agent_slot()
            return None
        cwd_override = str(wt_path) if wt_path else str(self.project_root)
        try:
            rendered_prompt = self._render(node["prompt"], upstream_values)
            # R9-3: a reviewer declaring evidence_from receives the LATEST
            # machine evidence pack (command, exit code, output preview,
            # scoped-file fingerprint) inside its prompt -- the machine
            # records what happened, the agent judges what it means. No
            # hand-written receipt; no blind trust in the implementer's
            # self-report.
            ev_block = self._evidence_prompt_block(node)
            if ev_block:
                rendered_prompt = rendered_prompt + "\n\n" + ev_block
            r = _dispatch(
                Harness._build_worker_prompt(
                    node, rendered_prompt,
                    recovery_disclosure=Harness._recovery_disclosure(
                        upstream_values)),
                node["output_schema"],
                allowed_tools=node.get("allowed_tools", []),
                model=node.get("model"),
                cwd=cwd_override,
                runner=self._runner or _default_runner,
            )
            capped = self._tally(r.cost_usd, spec, spec_version_id, node)
            cost_known = getattr(r, "cost_known", True)
            # F1: strict nodes (verdict by default / explicit conform_strict_json)
            # only accept pure-JSON text; no prose extraction.
            validated = (Harness._parse_validated_strict(r.result_text)
                         if Harness._strict_json(node)
                         else self._parse_validated(r.result_text))
            conforms = self._conforms(validated, node.get("output_schema", {}))
            is_clean = Harness._is_clean_attempt(r, conforms)
            self._record({
                "event_id": self._ev_id(),
                "kind": "agent_result",
                "spec_version_id": spec_version_id,
                "node_id": node["id"],
                "payload": {
                    "validated_output": validated, "cost_usd": r.cost_usd,
                    # R9-1: cost honesty -- False means cost_usd is a 0.0
                    # placeholder (backend reported nothing), so the budget
                    # ledger must not read it as free. Pre-R9-1 events lack
                    # the field; the checkpoint projection defaults True.
                    "cost_known": cost_known,
                    "tokens_total": getattr(r, "tokens_total", 0),
                    "num_turns": r.num_turns, "permission_denials": r.permission_denials,
                    "result_text_preview": self._preview(r.result_text),
                    # v2 Task 14 (§7): input_hash for resume cache keying.
                    "input_hash": self._input_hash(upstream_values),
                    # P1-6: execution-success envelope on the event so
                    # build_replay_cache admission and _agent_verdict_verified
                    # recency can reject failed / envelope-is_error attempts
                    # that happened to emit conforming JSON (false VERIFIED).
                    "is_clean": is_clean,
                    "exit_code": r.exit_code,
                    "is_error": r.is_error,
                },
                "claims": [],
            })
            if capped:
                return None
            if r.retry_class == "unretriable":
                # localized fan-out branch -> soft (no Gate); top-level -> Gate.
                if not localized:
                    self._gate("unretriable_failure", node["id"], spec_version_id)
                return None
            if r.retry_class == "operational":
                return None
            if r.permission_denials:  # cognitive failure (denials)
                return None
            if not is_clean:
                # P1-6: execution failure (non-zero exit OR envelope is_error) OR
                # schema-nonconformance -> this attempt is not clean success. A
                # failed worker whose stdout happens to parse as schema-conforming
                # JSON with verdict:VERIFIED must NOT be promoted to success
                # evidence (skeptic -> survived -> VERIFIED was the false-VERIFIED
                # hole). Returns None -> skeptic abstains (G4) / top-level -> R
                # unmet. The agent_result event above still records the attempt
                # (is_clean:false) so _agent_verdict_verified can detect a later
                # failed attempt invalidating an earlier VERIFIED.
                return None
            # B3 + v2 §5 step 5: pin worker file outputs (content-addressed)
            # from the worktree (glob_root=wt_path) so writes inside the
            # worktree -- NOT the main checkout -- are pinned (no leak).
            self._emit_artifact_write(
                node, spec_version_id,
                self._snapshot_artifacts(node, glob_root=wt_path))
            # v1.3: if this agent owns a requirement's verdict (verdict_field),
            # emit the agent_verdict event for success_evidence node: binding.
            self._emit_agent_verdict(node, validated, spec_version_id, wt_path)
            return {"validated_output": validated}
        finally:
            # §5 step 7: cleanup the worktree (symlinks unlinked, not followed;
            # tracked-file edits removed). Skipped when keep_worktrees=True.
            self._worktree_cleanup(wt_path)

    # --- retry + STAGNATING (agent only) ---------------------------

    def _run_agent_with_retry(self, node, values, spec_version_id, spec, localized=False):
        """Retry an agent dispatch with STAGNATING detection.

        A failure worth retrying = operational (crash/timeout/429) or
        cognitive-with-denials (needs new strategy/evidence). Clean success
        returns immediately. A cognitive-failure retry whose denial set is
        unchanged (no cognitive delta) is STAGNATING; after max_stagnation it
        is an honest exit (None). STAGNATING does NOT consume max_retries.
        """
        # A4: replay cache -- a prior successful run of this top-level agent is
        # returned instantly on resume (zero dispatch). Only the SUCCESSFUL
        # outcome is cached; an originally-exhausted node is a miss and re-runs.
        # v2 Task 14: pass values for input_hash secondary validation (§7).
        # R9-3: an evidence-bound reviewer skips the cache when its evidence
        # went stale (code changed after the verdict) -- replaying the cached
        # output would re-emit a judgment of OLD code.
        _skip_replay = False
        if node.get("evidence_from") and spec is not None:
            bound = None
            for e in reversed(self.ledger.events()):
                if e.get("kind") == "agent_verdict" \
                        and e.get("node_id") == node["id"]:
                    bound = e.get("payload", {}).get("evidence_id")
                    break
            cur_id = self._evidence_freshness_probe(spec,
                                                    node["evidence_from"])
            if bound and cur_id and cur_id != bound:
                _skip_replay = True
        hit = None if _skip_replay else self._replay_pop(
            spec_version_id, node["id"], values)
        if hit is not None:
            return hit
        # R6-1: the pop consumes NO dispatch-slot reservation. On a miss this
        # path will record one attempt event per retry while reserving only
        # once per attempt inside the loop -- but the FIRST reservation below
        # would leave the walk's reservations one short of the reservation
        # the miss consumed... none was consumed. Compensate: release the
        # slot the caller may have reserved for this arrival is NOT
        # applicable here (this path never reserved); instead ensure the
        # first loop iteration's reservation pairs 1:1 with its attempt
        # event. No-op unless a future caller pre-reserves.
        # v1.4-S3: pre-dispatch budget guard (refuse if already over budget).
        if self._budget_already_exceeded(spec):
            return None
        fp = node.get("failure_policy", {})
        max_retries = fp.get("max_retries", 1)
        max_stag = getattr(spec, "max_stagnation", 1) if spec else 1
        retry_guard = fp.get("retry_guard", "requires_new_evidence")
        prev_sig = None
        stagnation_count = 0
        attempts = 0
        # G1 P3 + G3 P2: the worker prompt carries a hard JSON-schema directive
        # (P3, base) and, after a schema failure, a schema-error feedback (P2)
        # so the worker can correct on retry. denials (permission) get no schema
        # feedback (irrelevant). Composes with the stagnation signature: a
        # corrected output with the same failure shape still stagnates honestly.
        base_prompt = Harness._build_worker_prompt(
            node, self._render(node["prompt"], values),
            recovery_disclosure=Harness._recovery_disclosure(values))
        # R9-3: a reviewer declaring evidence_from receives the LATEST
        # machine evidence pack (command, exit code, output preview,
        # scoped-file fingerprint) -- the machine records what happened,
        # the agent judges what it means. No hand-written receipt.
        ev_block = self._evidence_prompt_block(node)
        if ev_block:
            base_prompt = base_prompt + "\n\n" + ev_block
        current_prompt = base_prompt
        # F1: strict JSON decision (verdict nodes are strict by default; an
        # explicit conform_strict_json overrides both ways). strict nodes do
        # no prose extraction and skip P-006 prose recovery -- JSON or bust.
        strict_json = Harness._strict_json(node)
        # v1.3.2: gate high-risk / secrets / delete nodes BEFORE any dispatch
        # (once, outside the retry loop). check_gate was dead code -- a
        # risk:high node ran the worker anyway. Gate + refuse, await resolve.
        if self.check_gate(node):
            self._gate("risk_high", node["id"], spec_version_id,
                      action_scope_hash=Harness._action_scope_hash(node))
            return None
        # v2 Task 12 (spec §5): worktree isolation intercept. setup AFTER the
        # gate check so a gated node does NOT create a worktree. The worktree
        # wraps the WHOLE retry loop (one worktree per dispatch, state carries
        # across retry attempts); cwd override applies to each _dispatch
        # call, the snapshot glob root is the worktree (no leak to main), and
        # cleanup runs in the finally after the loop exits.
        wt_path = self._worktree_setup(node, values, spec_version_id)
        cwd_override = str(wt_path) if wt_path else str(self.project_root)
        # conform-fail artifact-recovery: for a recoverable impl node (write_areas
        # non-empty, no verdict_field), snapshot the write_areas baseline signature
        # before the retry loop. If a later dispatch conform-fails (worker returned
        # pure prose, zero JSON) but the after signature changed = the work was
        # actually done, only the report format is wrong -> rebuild the output from
        # disk evidence and pass through, without stagnating or gating.
        # F1: strict nodes (verdict default / explicit declaration) skip P-006 --
        # JSON or bust; file evidence is not used to pass a "did not return JSON".
        recovery_glob_root = wt_path if wt_path else self.project_root
        recovery_baseline = (
            self._write_areas_signature(node, glob_root=recovery_glob_root)
            if (self._recoverable_impl_node(node) and not strict_json) else None
        )
        try:
            while attempts <= max_retries:
                # budget pre-check BEFORE running the worker -- a dispatch over
                # budget still charges cost. A real relay ran n4's worker, it
                # conformed and edited main.py, but budget tripped AFTER the run
                # and BEFORE agent_result was recorded -> completed work discarded.
                # Pre-check (silent, no event -- _budget_already_exceeded) stops
                # the NEXT dispatch; a conforming current dispatch records its
                # result below even if _tally trips (cost already spent).
                # P1-6: agent-count cap is pre-checked here too, symmetric with
                # budget -- without it a max_agents cap logged in _tally but
                # never stopped the next node's dispatch.
                # P-025: the agent half is now an ATOMIC reservation (check +
                # increment in one lock, pre-dispatch) -- a bare read races
                # when two threads dispatch concurrently.
                if self._budget_already_exceeded(spec):
                    return None
                if not self._reserve_agent_slot(spec):
                    self._log_cap_trip_once(spec)
                    return None
                r = _dispatch(
                    current_prompt, node["output_schema"],
                    allowed_tools=node.get("allowed_tools", []),
                    model=node.get("model"), cwd=cwd_override,
                    runner=self._runner or _default_runner,
                )
                attempts += 1
                # _tally increments cost/count and logs budget_exhausted on the
                # first trip. We do NOT bail here (the old form did): if this
                # dispatch conforms, _record runs first (below) so the completed
                # work is not discarded. A real relay lost n4's result
                # (main.py was edited, worker conformed) because _tally bailed
                # before _record. capped is honored AFTER _record, per dispatch_agent.
                capped = self._tally(r.cost_usd, spec, spec_version_id, node)
                if r.retry_class == "unretriable":
                    # P-017: an unretriable dispatch still SPENT its cost --
                    # record it or the spend vanishes from checkpoint/resume.
                    self._record({
                        "event_id": self._ev_id(), "kind": "operational_attempt",
                        "spec_version_id": spec_version_id, "node_id": node["id"],
                        "payload": {
                            "attempt": attempts, "retry_class": "unretriable",
                            "cost_usd": r.cost_usd,
                            "cost_known": getattr(r, "cost_known", True),
                            "tokens_total": getattr(r, "tokens_total", 0),
                            "num_turns": r.num_turns,
                            "result_text_preview": self._preview(r.result_text),
                        },
                    })
                    # localized (parallel-body / pipeline-stage / skeptic) -> soft:
                    # an unretriable branch failure must not hard-stop a failure-
                    # tolerant fan-out; it surfaces as None (verdict UNVERIFIED/
                    # PARTIAL). Top-level (localized=False) -> honest Gate -> BLOCKED.
                    if not localized:
                        self._gate("unretriable_failure", node["id"], spec_version_id)
                    return None
                if r.retry_class == "operational":
                    # P-017: every operational attempt spent real cost. Record
                    # it BEFORE continue/_exhaust -- before the fix this branch
                    # recorded nothing, so an operational retry's spend lived
                    # only in the in-memory _cost_total and was lost from the
                    # checkpoint + resume rebuild (budget projection understated
                    # -> more dispatches re-admitted past the real spend).
                    self._record({
                        "event_id": self._ev_id(), "kind": "operational_attempt",
                        "spec_version_id": spec_version_id, "node_id": node["id"],
                        "payload": {
                            "attempt": attempts, "retry_class": "operational",
                            "cost_usd": r.cost_usd,
                            "cost_known": getattr(r, "cost_known", True),
                            "tokens_total": getattr(r, "tokens_total", 0),
                            "num_turns": r.num_turns,
                            "result_text_preview": self._preview(r.result_text),
                        },
                    })
                    if capped or attempts > max_retries:
                        return self._exhaust(node, spec_version_id, "retries_exhausted",
                                             localized=localized)
                    continue
                # cognitive
                validated = (Harness._parse_validated_strict(r.result_text)
                             if strict_json
                             else self._parse_validated(r.result_text))
                denials = r.permission_denials
                conforms = self._conforms(validated, node.get("output_schema", {}))
                # P1-3 + P1-6 fix: a worker whose process FAILED (exit_code != 0)
                # OR whose envelope reported is_error must not be promoted to
                # success evidence even if its stdout happens to parse as
                # schema-conforming JSON with verdict:VERIFIED. cognitive
                # (retry_class) means "the run completed", NOT "it succeeded"
                # -- execution status and retry policy are separate. A failed
                # worker's output is untrusted; it falls through to the
                # cognitive-failure retry path below.
                is_clean = Harness._is_clean_attempt(r, conforms)
                if not denials and is_clean:
                    # clean success -- only schema-conforming output passes the
                    # type boundary. _record BEFORE the capped bail so a conforming
                    # dispatch that tripped budget still leaves its result in the
                    # ledger (the work is done; the cost is spent).
                    self._record({
                        "event_id": self._ev_id(), "kind": "agent_result",
                        "spec_version_id": spec_version_id, "node_id": node["id"],
                        "payload": {
                            "validated_output": validated, "cost_usd": r.cost_usd,
                            # R9-1: cost honesty on the retry path too.
                            "cost_known": getattr(r, "cost_known", True),
                            "tokens_total": getattr(r, "tokens_total", 0),
                            "num_turns": r.num_turns, "permission_denials": r.permission_denials,
                            "result_text_preview": self._preview(r.result_text),
                            # v2 Task 14 (§7): input_hash for resume cache keying.
                            "input_hash": self._input_hash(values),
                            # P1-6: execution-success envelope on the event.
                            "is_clean": is_clean,
                            "exit_code": r.exit_code,
                            "is_error": r.is_error,
                        },
                        "claims": [],
                    })
                    # B3 + v2 §5 step 5: pin worker file outputs from the worktree
                    # (glob_root=wt_path) so writes inside the worktree -- NOT
                    # the main checkout -- are pinned (no leak).
                    self._emit_artifact_write(
                        node, spec_version_id,
                        self._snapshot_artifacts(node, glob_root=wt_path))
                    # v1.3: if this agent owns a requirement's verdict, emit it.
                    self._emit_agent_verdict(node, validated, spec_version_id, wt_path)
                    if capped:
                        return None  # work recorded; budget tripped, stop here
                    return {"validated_output": validated}
                # cognitive failure: denials OR schema-nonconformance -> retry with
                # a stagnation guard keyed on the failure signature, and (for schema
                # failures only) a schema-error feedback into the next prompt.
                fail_keys = (Harness._schema_fail_keys(
                    validated, node.get("output_schema", {}))
                    if not conforms else None)
                # conform-fail artifact-recovery: the worker output did not conform
                # (pure prose, zero JSON, or partial JSON missing required fields) but
                # the write_areas files were actually changed = the work was done,
                # only the report is incomplete. Rebuild validated_output from disk
                # evidence (mark _recovered:true) + record agent_result/artifact_write,
                # and pass through to the downstream verify without stagnating or
                # gating. This fixes the root cause of recurring stalls in earlier
                # relay runs: the worker model is good at doing the work but bad at
                # writing a complete JSON report; the old behavior treated "report
                # format / missing fields" as a "cognitive failure" and wrongly killed
                # already-completed work. Safety: only applies to impl nodes without a
                # verdict_field (a verdict is never fabricated); missing schema fields
                # are a self-report gap, not evidence -- whether the work is right is
                # adjudicated independently by downstream verify. If files did not
                # change, fall through to the normal retry/stagnation logic.
                if (recovery_baseline is not None and not denials and not conforms):
                    after_sig = self._write_areas_signature(
                        node, glob_root=recovery_glob_root)
                    changed = sorted(
                        p for p in after_sig
                        if after_sig.get(p) != recovery_baseline.get(p))
                    if changed:
                        refs = self._snapshot_artifacts(
                            node, glob_root=recovery_glob_root)
                        # #8 recovery_schema: the rebuilt output only carries
                        # files_modified/summary/_recovered -- any other
                        # schema-required field was NEVER produced by the
                        # worker. Compute the schema-diff explicitly so the
                        # gap is a recorded fact (event payload + _recovery
                        # block) and downstream consumers (esp. the verdict
                        # node) can see exactly which fields are absent
                        # instead of mistaking a partial object for a whole
                        # one. Missing fields are a self-report-level gap; whether
                        # the work is right is still adjudicated by verify.
                        _req = list((node.get("output_schema") or {})
                                    .get("required") or [])
                        _missing = [k for k in _req if k not in
                                    ("files_modified", "summary")]
                        recovered = {
                            "files_modified": changed,
                            "summary": self._preview(r.result_text, limit=400),
                            "_recovered": True,
                            "_recovery": {
                                "missing_required_fields": _missing,
                                "present_fields": ["files_modified", "summary"],
                                "disclaimer": (
                                    "reconstructed from disk evidence after "
                                    "the worker returned non-conforming "
                                    "output; fields in "
                                    "missing_required_fields were never "
                                    "produced by the worker -- treat them "
                                    "as absent, not as empty/false"),
                            },
                        }
                        self._record({
                            "event_id": self._ev_id(), "kind": "agent_result",
                            "spec_version_id": spec_version_id,
                            "node_id": node["id"],
                            "payload": {
                                "validated_output": recovered,
                                "cost_usd": r.cost_usd, "num_turns": r.num_turns,
                                "permission_denials": r.permission_denials,
                                "result_text_preview": self._preview(r.result_text),
                                "recovered_from_prose": True,
                                "input_hash": self._input_hash(values),
                                # P1-6/#8: recovered-from-prose is a DEGRADED
                                # result, not clean success -- the worker failed
                                # to produce schema-conforming output; only the
                                # file delta proves work happened. Mark
                                # is_clean:false so build_replay_cache does not
                                # replay it as success on resume and
                                # _agent_verdict_verified treats it as a failed
                                # attempt (impl nodes have no verdict_field, so
                                # this mainly guards the cache path).
                                "is_clean": False,
                                "exit_code": r.exit_code,
                                "is_error": r.is_error,
                                # #8: the schema gap is a ledger-level fact,
                                # not only a hint inside validated_output --
                                # state/debug projections can surface it
                                # without unwrapping the output.
                                "missing_required_fields": _missing,
                            },
                            "claims": [],
                        })
                        self._emit_artifact_write(node, spec_version_id, refs)
                        return {"validated_output": recovered}
                if capped:
                    # budget tripped on this dispatch -- do NOT retry (each retry
                    # charges more cost over the already-crossed budget).
                    # P-017: the tripping dispatch still SPENT its cost -- record
                    # it (cognitive_attempt: the output failed schema/denials, i.e.
                    # a cognitive failure) before bailing, or an over-budget run
                    # reports cost 0 in checkpoint/resume while the money is gone.
                    self._record({
                        "event_id": self._ev_id(), "kind": "cognitive_attempt",
                        "spec_version_id": spec_version_id, "node_id": node["id"],
                        "payload": {
                            "attempt": attempts, "denials": denials,
                            "conforms": conforms, "cost_usd": r.cost_usd,
                            "num_turns": r.num_turns,
                            "budget_capped": True,
                            "result_text_preview": self._preview(r.result_text),
                        },
                    })
                    return None
                if retry_guard == "requires_new_evidence":
                    sig = self._cognitive_sig(denials, conforms, fail_keys)
                    if prev_sig is not None and sig == prev_sig:
                        stagnation_count += 1
                        self._record({
                            "event_id": self._ev_id(), "kind": "stagnating",
                            "spec_version_id": spec_version_id, "node_id": node["id"],
                            "payload": {
                                "attempt": attempts, "denials": denials,
                                "conforms": conforms, "cost_usd": r.cost_usd,
                                "num_turns": r.num_turns,
                                "result_text_preview": self._preview(r.result_text),
                            },
                        })
                        if stagnation_count >= max_stag:
                            return self._exhaust(
                                node, spec_version_id, "stagnation_exhausted",
                                localized=localized)
                    else:
                        # P1-6 fix: cognitive delta (different failure signature)
                        # resets the stagnation streak. The attempt still failed
                        # with real cost -- record it as a cognitive_attempt (NOT
                        # stagnating, which is reserved for same-signature repeats)
                        # so checkpoint cost_total and resume rebuild restore the
                        # full spend, not just the final success. Before the fix
                        # this branch skipped _record, so its cost lived only in
                        # the in-memory _cost_total and was lost on resume ->
                        # budget projection understated -> more dispatches
                        # re-admitted.
                        self._record({
                            "event_id": self._ev_id(), "kind": "cognitive_attempt",
                            "spec_version_id": spec_version_id, "node_id": node["id"],
                            "payload": {
                                "attempt": attempts, "denials": denials,
                                "conforms": conforms, "cost_usd": r.cost_usd,
                                "num_turns": r.num_turns,
                                "result_text_preview": self._preview(r.result_text),
                            },
                        })
                        stagnation_count = 0
                    prev_sig = sig
                else:
                    # P-017: a non-standard retry_guard must not silently drop
                    # the failed attempt's cost from the ledger (the invariant
                    # is: every real dispatch leaves exactly one cost-bearing
                    # event). Sig-based stagnation detection only exists for
                    # requires_new_evidence; other guards still record the
                    # spend as a plain cognitive_attempt.
                    self._record({
                        "event_id": self._ev_id(), "kind": "cognitive_attempt",
                        "spec_version_id": spec_version_id, "node_id": node["id"],
                        "payload": {
                            "attempt": attempts, "denials": denials,
                            "conforms": conforms, "cost_usd": r.cost_usd,
                            "num_turns": r.num_turns,
                            "result_text_preview": self._preview(r.result_text),
                        },
                    })
                # G3 P2: schema-nonconformance (not denials) -> feed the schema
                # error back into the next retry's prompt so the worker can
                # correct. denials (permission) get no schema feedback -- the
                # schema is not why a permission-denied dispatch failed.
                if not denials and not conforms:
                    if strict_json:
                        # F1: a strict failure is usually not a schema-field
                        # problem — the output is not pure-JSON text at all
                        # (JSON buried in prose / surrounded by explanation).
                        # The feedback must fix "output shape" before
                        # "schema fields".
                        current_prompt = base_prompt + (
                            "\n\n--- Your last output was not acceptable pure JSON (please fix) ---\n"
                            "Your previous output was not \"the entire reply is one JSON object\" "
                            "(it likely carried natural language, explanation, or embedded the JSON in prose). "
                            "This time it must be: the whole reply contains only one JSON object conforming to "
                            "the schema below; the first character is {, the last character is }, and no text "
                            "before or after is allowed.\n"
                            f"JSON Schema: {json.dumps(node.get('output_schema', {}), ensure_ascii=False)}"
                        )
                    else:
                        current_prompt = base_prompt + (
                            "\n\n--- Your last output did not conform to the schema (please fix) ---\n"
                            "Your previous output failed JSON Schema validation, failed fields: "
                            f"{sorted(fail_keys)}. Please output only a JSON object conforming to "
                            "the schema below; no natural language or markdown fences.\n"
                            f"JSON Schema: {json.dumps(node.get('output_schema', {}), ensure_ascii=False)}"
                        )
                if attempts > max_retries:
                    return self._exhaust(node, spec_version_id, "retries_exhausted",
                                         localized=localized)
            return self._exhaust(node, spec_version_id, "retries_exhausted",
                                 localized=localized)
        finally:
            # §5 step 7: cleanup the worktree after the retry loop (symlinks
            # unlinked, not followed; tracked-file edits removed).
            self._worktree_cleanup(wt_path)

    # --- dispatch by type ------------------------------------------

    def _dispatch(self, node, values, spec_version_id, spec):
        t = node.get("type", "agent")
        if t == "agent":
            return self.dispatch_agent(node, values, spec_version_id, spec)
        if t == "parallel":
            return {"validated_output": self.run_parallel(node, values, spec_version_id, spec)}
        if t == "pipeline":
            return {"validated_output": self.run_pipeline(node, values, spec_version_id, spec)}
        if t == "verify":
            return {"validated_output": self.run_verify(node, values, spec_version_id, spec)}
        if t == "synthesize":
            return self._synthesize(node, values, spec_version_id, spec)
        if t == "workflow":
            return self._run_workflow(node, spec_version_id)
        if t == "condition":
            return self._run_condition(node, values, spec_version_id, spec)
        if t == "repeat":
            return self._run_repeat(node, values, spec_version_id, spec)
        if t == "until":
            return self._run_until(node, values, spec_version_id, spec)
        if t == "gate":
            return self._run_gate(node, values, spec_version_id, spec)
        if t == "script":
            return self.run_script(node, values, spec_version_id, spec)
        raise ValueError(f"unknown node type {t}")

    def _dispatch_with_retry(self, node, values, spec_version_id, spec, localized=False):
        """Top-level dispatch. Agents get retry+STAGNATING; other node types
        dispatch once (their internal failures are localized to null).

        localized=True marks a parallel-body / pipeline-stage agent: its
        exhaustion is soft (failure localization), never a hard Gate."""
        if node.get("type", "agent") == "agent":
            return self._run_agent_with_retry(
                node, values, spec_version_id, spec, localized=localized)
        return self._dispatch(node, values, spec_version_id, spec)

    # --- script (deterministic subprocess, no LLM) -------------------

    def _resolve_script_path(self, script_path: str) -> Path:
        """Absolute path as-is; relative resolved against the repo root
        (harness.py lives in <root>/axiom/, so parent.parent is root)."""
        p = Path(script_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / script_path
        return p

    @staticmethod
    def _last_json_line(stdout: str):
        """Last line that parses as a JSON object. Script nodes emit one JSON
        line on stdout; we tolerate trailing non-JSON noise (warnings)."""
        for line in reversed(stdout.splitlines()):
            s = line.strip()
            if s.startswith("{"):
                try:
                    return json.loads(s)
                except Exception:
                    continue
        raise ValueError("no JSON line in script stdout")

    def run_script(self, node, upstream_values, spec_version_id, spec=None):
        """Deterministic subprocess node: runs script_path directly, no LLM
        dispatch. Failure sources (exit!=0 / timeout / parse-fail /
        schema-nonconform) all count as node failure and retry per
        failure_policy; on exhaustion the branch blocks (no fake success).
        Emits a script_result event (distinct from agent_result so
        derive_debug_envelope separates deterministic-node failures from
        LLM-node failures). Returns {'validated_output': parsed} on success,
        None on failure.
        """
        script_path = self._resolve_script_path(node["script_path"])
        args = [self._render(a, upstream_values) for a in (node.get("args") or [])]
        schema = node.get("output_schema", {})
        fp = node.get("failure_policy", {})
        max_retries = int(fp.get("max_retries", 2))
        from axiom.dispatch import _real_runner
        failure_class = None
        parsed = None
        rc = 0
        stdout = ""
        for _ in range(max_retries + 1):
            try:
                rc, stdout = _real_runner(
                    [sys.executable, str(script_path), *args],
                    cwd=str(self.project_root))
            except Exception as e:
                rc, stdout, failure_class = -1, str(e), "dispatch_error"
                continue
            if rc != 0:
                failure_class = f"exit_{rc}"
                continue
            try:
                parsed = self._last_json_line(stdout)
            except Exception:
                failure_class = "parse_failure"
                continue
            if not self._conforms(parsed, schema):
                failure_class = "schema_nonconform"
                continue
            failure_class = None
            break
        self._record({
            "event_id": self._ev_id(),
            "kind": "script_result",
            "spec_version_id": spec_version_id,
            "node_id": node["id"],
            "payload": {
                "script_path": str(script_path), "args": args,
                "exit_code": rc, "parsed_output": parsed,
                "failure_class": failure_class,
                "stdout_preview": self._preview(stdout),
            },
            "claims": [],
        })
        # R9-3 (review9 P1): auto evidence pack. A script node that declares
        # evidence_scope gets its execution recorded as machine evidence:
        # command + exit code + output + a CONTENT SIGNATURE of the scoped
        # files + the git HEAD. A downstream reviewer binds its verdict to
        # this fingerprint; any change to the scoped files invalidates the
        # evidence (stale evidence never silently backs a fresh verdict).
        # The machine records WHAT HAPPENED; the reviewer judges what it
        # means -- no hand-written receipt.json.
        if node.get("evidence_scope"):
            self._emit_evidence_pack(node, spec_version_id, rc, stdout,
                                     failure_class)
        if failure_class is not None:
            return None  # block branch (never fake success)
        return {"validated_output": parsed}

    def _emit_evidence_pack(self, node, spec_version_id, rc, stdout,
                            failure_class) -> None:
        """Assemble the machine-side acceptance evidence for a script run.

        evidence_id anchors the code version: sha256 over the sorted
        (relpath, file-sha256) pairs of evidence_scope + the git HEAD when
        available. A reviewer verdict carries this id; on re-walk the id is
        recomputed and a mismatch means the evidence (and any verdict based
        on it) is STALE -- the reviewer must re-examine, never inherit."""
        scope = node.get("evidence_scope") or []
        sig = self._write_areas_signature({"write_areas": scope})
        head = None
        try:
            import subprocess as _sp
            head = _sp.run(["git", "rev-parse", "HEAD"],
                           cwd=str(self.project_root), capture_output=True,
                           text=True, timeout=5).stdout.strip() or None
        except Exception:
            head = None
        material = json.dumps({"files": sorted(sig.items()), "git_head": head},
                              sort_keys=True)
        evidence_id = hashlib.sha256(material.encode()).hexdigest()
        self._record({
            "event_id": self._ev_id(),
            "kind": "evidence_pack",
            "spec_version_id": spec_version_id,
            "node_id": node["id"],
            "payload": {
                "evidence_id": evidence_id,
                "command": f"{node.get('script_path')} {' '.join(node.get('args') or [])}".strip(),
                "exit_code": rc,
                "failure_class": failure_class,
                "stdout_preview": self._preview(stdout),
                "files": sig,
                "git_head": head,
            },
            "claims": [],
        })

    # --- parallel / pipeline ---------------------------------------

    def _concurrency(self, node, spec, requested: int | None = None) -> int:
        """Effective worker count for a fan-out: min(declared, spec cap)."""
        conc = requested if requested is not None else node.get("concurrency", 16)
        if spec is not None:
            conc = min(conc, getattr(spec, "max_concurrent", 16))
        return max(1, conc)

    def run_parallel(self, node, upstream_values, spec_version_id, spec=None):
        items = self._resolve(node["over"], upstream_values)
        body = node["body"]
        conc = self._concurrency(node, spec)

        # v2 Task 8: capture the parent thread's loop context so worker threads
        # (which do NOT inherit threading.local) tag their body events with the
        # enclosing loop's loop_id+iteration. None outside a loop -> no-op.
        parent_ctx = getattr(self._loop_ctx, "ctx", None)

        # R7-1: every item gets a stable fan-out identity "<container>:<idx>"
        # -- the replay cache slots per position, so duplicate inputs (same
        # input_hash) replay to their ORIGINAL positions on resume instead
        # of draining one shared slot newest-first (review7 P2: [A,B] ->
        # [B,A] swap). A retry of item i keeps "<id>:i" (not a new position).
        container = node.get("id", "parallel")

        def run_one(idx_item):
            idx, item = idx_item
            self._loop_ctx.ctx = parent_ctx  # inherit into worker thread
            vu = dict(upstream_values)
            vu["item"] = item
            return self.dispatch_agent(body, vu, spec_version_id, spec,
                                        localized=True,  # None on failure
                                        exec_pos=f"{container}:{idx}")

        # A1: parallel body agents dispatch concurrently up to `conc` workers.
        # ex.map preserves input order; the ledger lock serializes appends so
        # the hash chain stays intact under concurrent writes.
        if conc == 1 or len(items) <= 1:
            return [run_one(it) for it in enumerate(items)]
        with ThreadPoolExecutor(max_workers=conc) as ex:
            return list(ex.map(run_one, enumerate(items)))

    def run_pipeline(self, node, upstream_values, spec_version_id, spec=None):
        items = self._resolve(node["items"], upstream_values)
        stages = node["stages"]
        # A1: parallelism is across items; each item's stages still run
        # sequentially (stage N+1 depends on stage N's output).
        conc = self._concurrency(node, spec, requested=None)

        # v2 Task 8: capture parent loop ctx for worker threads (see run_parallel).
        parent_ctx = getattr(self._loop_ctx, "ctx", None)

        # R7-1: same stable fan-out identity as run_parallel -- stage
        # dispatches of duplicate items must replay to their own positions.
        container = node.get("id", "pipeline")

        def run_one(idx_item):
            idx, item = idx_item
            self._loop_ctx.ctx = parent_ctx
            prev = None
            dropped = False
            for stage in stages:
                if dropped:
                    break
                vu = dict(upstream_values)
                vu["item"] = item
                if prev is not None:
                    vu["prev"] = prev
                r = self.dispatch_agent(stage, vu, spec_version_id, spec,
                                        localized=True,
                                        exec_pos=f"{container}:{idx}")
                if r is None:
                    dropped = True
                    prev = None
                else:
                    prev = r.get("validated_output")
            return None if dropped else prev

        if conc == 1 or len(items) <= 1:
            return [run_one(it) for it in enumerate(items)]
        with ThreadPoolExecutor(max_workers=conc) as ex:
            return list(ex.map(run_one, enumerate(items)))

    # --- verify -----------------------------------------------------

    def run_verify(self, node, upstream_values, spec_version_id, spec=None):
        findings = self._resolve(node["target"], upstream_values)
        sc = node.get("skeptic_count", 3)
        survivors, refuted = [], []
        # v2 Task 8: capture parent loop ctx so skeptic worker threads tag
        # their agent_result events with the enclosing loop's loop_id+iteration.
        # verify_verdict itself is emitted in the main thread (after ex.map),
        # so it reads the main thread's ctx directly. None outside a loop.
        parent_ctx = getattr(self._loop_ctx, "ctx", None)
        for f in findings:
            if not isinstance(f, dict):
                # a failed upstream agent yields None / a non-dict in the
                # findings list (failure localization); skip it -- there is
                # no claim to verify -- rather than crash on None.get(...) or
                # emit a hollow verify_verdict for an empty claim_id.
                continue
            cid = f.get("claim_id", f.get("id", ""))
            refute_votes = 0
            abstain_votes = 0
            prompt = node.get(
                "skeptic_prompt",
                "Assume this finding is WRONG. Find evidence refuting it. "
                "Default refuted=true if uncertain.",
            )
            skeptic_node = {
                "type": "agent", "id": f"_skeptic_{cid}", "prompt": prompt,
                "dispatch": "host",
                # The skeptic schema is canonical {refuted, evidence_ref} -- NOT the
                # verify node's output_schema. A verify node's output_schema is
                # required by validate (ir.py) but is a DEAD field at runtime: a
                # loose {type:object} here would let prose -> {} conform -> false
                # survive. Skeptics must always be held to {refuted required}.
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "refuted": {"type": "boolean"},
                        "evidence_ref": {"type": "string"},
                    },
                    "required": ["refuted"],
                },
                # C2: a verify node may grant its skeptics allowed_tools /
                # read_areas so the skeptic can ground its verdict in the
                # actual evidence (e.g. Read a screenshot), not just the
                # finding's prose. Default stays WebSearch/WebFetch (the
                # pre-C2 behavior) when the verify node declares nothing.
                "allowed_tools": node.get(
                    "allowed_tools", ["WebSearch", "WebFetch"]),
                "read_areas": node.get("read_areas", []),
                "write_areas": [],
                "acceptance": ["must attempt to refute"], "failure_policy": {},
                "verification_policy": "independent",
            }

            def _run_one_skeptic(_):
                self._loop_ctx.ctx = parent_ctx  # inherit into skeptic worker
                vu = {"finding": f}
                return self.dispatch_agent(skeptic_node, vu, spec_version_id,
                                           spec, localized=True)

            # v1.4-S2: the sc skeptics for a claim are independent -- dispatch
            # them concurrently (ledger.append is locked, replay pop is locked).
            # The verify_verdict is appended after all skeptics return.
            conc = min(sc, getattr(spec, "max_concurrent", 16) if spec else 16)
            if conc == 1 or sc <= 1:
                skeptic_results = [_run_one_skeptic(_) for _ in range(sc)]
            else:
                with ThreadPoolExecutor(max_workers=conc) as ex:
                    skeptic_results = list(ex.map(_run_one_skeptic, range(sc)))
            for r in skeptic_results:
                if r is None:
                    # G4: a non-conforming / failed skeptic ABSTAINS -- it could
                    # not deliver a verdict. Abstention is neither refutation
                    # nor survival (kills the false-refutation failure mode).
                    abstain_votes += 1
                elif r.get("validated_output", {}).get("refuted"):
                    refute_votes += 1
                # else: a conforming skeptic that did NOT refute -> implicit
                # survive vote (counted via effective - refute below).
            effective = sc - abstain_votes
            if abstain_votes > sc / 2 or effective == 0:
                # over half the skeptics could not even deliver a verdict -> the
                # claim is unverifiable: neither survived nor refuted. Projects to
                # CHALLENGED (honest), NOT a false refutation. (Also covers sc=0.)
                did_survive = False
                did_refute = False
            else:
                # majority_unrefuted over the CONFORMING skeptics only: the claim
                # survives iff a STRICT majority of those who actually voted did
                # NOT refute (refute < effective/2). Abstentions under half are
                # ignored -- they neither cleared nor damned the claim.
                did_survive = refute_votes < effective / 2
                did_refute = not did_survive
            self._record({
                "event_id": self._ev_id(), "kind": "verify_verdict",
                "spec_version_id": spec_version_id, "node_id": node["id"],
                "claim_id": cid, "refuted": did_refute, "survived": did_survive,
                "payload": {"refute_votes": refute_votes,
                            "abstain_votes": abstain_votes,
                            "skeptic_count": sc,
                            "survival_rule": node.get("survival_rule", "majority_unrefuted")},
            })
            if did_survive:
                survivors.append(f)
            elif did_refute:
                refuted.append(f)
            # else (unverifiable): appended to neither -> CHALLENGED
        return {"survivors": survivors, "refuted": refuted}

    # --- synthesize -------------------------------------------------

    @staticmethod
    def _slim_finding(f):
        """Project a finding down to the fields a synthesize worker needs to
        write a report. The full evidence blob drifts the worker model into prose
        (3/3 empirical); claim_id + aspect + one-sentence summary is enough
        and keeps the prompt short."""
        if not isinstance(f, dict):
            return f
        return {
            k: f.get(k) for k in ("claim_id", "aspect", "summary")
            if k in f
        }

    def _slim_input(self, v):
        """Slim a resolved input: if it's a verify-shaped dict (survivors/
        refuted), project each finding to the slim form. Otherwise pass through."""
        if isinstance(v, dict) and ("survivors" in v or "refuted" in v):
            return {
                "survivors": [Harness._slim_finding(f) for f in v.get("survivors", [])],
                "refuted": [Harness._slim_finding(f) for f in v.get("refuted", [])],
            }
        return v

    def _synthesize(self, node, values, spec_version_id, spec=None):
        inputs = []
        for ref in node.get("inputs", []):
            if ref.startswith("{{") and ref.endswith("}}"):
                inputs.append(self._lookup(ref[2:-2], values))
            else:
                inputs.append(ref)
        # v1.4-S1a: slim inputs so the worker sees claim_id/aspect/summary only
        # (the long evidence blob drove 3/3 prose-failure). A2: synthesize now
        # goes through _run_agent_with_retry (G1 + G3 schema-feedback retry).
        # localized=True: a synthesize that still fails after retries degrades
        # softly (None -> {}) and never opens a Gate; the checkpoint projects a
        # degraded report from verify evidence (see project_checkpoint).
        slim_inputs = [self._slim_input(i) for i in inputs]
        synth_node = {
            "type": "agent", "id": node["id"],
            # v1.3.3: honor the node's own prompt if declared (was hardcoded,
            # ignoring the spec author's synthesize prompt). Fallback default.
            "prompt": node.get("prompt") or (
                "Synthesize the following review findings into ONE JSON object "
                "matching the schema (counts + a one-sentence report). "
                "Inputs (JSON): {{inputs}}"
            ),
            "dispatch": "host",
            "output_schema": node.get("output_schema", {"type": "object"}),
            "allowed_tools": [], "write_areas": [],
            "acceptance": node.get("acceptance", []),
            "failure_policy": node.get("failure_policy", {
                "max_retries": 1, "retry_guard": "requires_new_evidence",
                "on_exhausted": "degrade",
            }),
        }
        return self._run_agent_with_retry(
            synth_node, {"inputs": slim_inputs}, spec_version_id, spec, localized=True)

    # --- workflow (sub-spec) ---------------------------------------

    def _run_workflow(self, node, spec_version_id):
        sub = self.run_dir / f"spec.{node['spec']}.json"
        if not sub.exists():
            return {"validated_output": {}, "error": f"sub-spec {node['spec']} not found"}
        sub_spec = spec_from_json(sub.read_text(encoding="utf-8"))
        sub_h = Harness(self.run_dir / f"sub_{node['spec']}", worker_runner=self._runner)
        return sub_h.run(sub_spec)

    # --- sequence control_flow -------------------------------------

    def run(self, spec):
        errs = validate_spec(spec)
        if errs:
            raise ValueError("spec invalid: " + "; ".join(errs))
        self._build_evidence_for(spec)  # B2: reverse provenance index
        # R6-1: arm execution identity for this walk (cursor + scoped keys).
        # Never clears the built slot cache (a resume harness calls
        # build_replay_cache first; run() only re-zeros the pop cursor).
        self._reset_exec_identity(
            spec,
            next_run=self._replay_run if getattr(self, "_replay", None)
            else 0)
        # v2 Task 10: catch GateHalt so gate nodes (on_trigger='pause') AND
        # gate=True conditions (Task 7's binding-branch path) pause+resume at
        # the run() level. The gate_open event is already recorded by _gate()
        # before the raise (v1 mechanism, reused -- no new event kind). run()
        # exits with a gate-halted state so the CLI can surface the unresolved
        # gate (derive_verdict -> BLOCKED, fires for free) and `axiom gate
        # resolve` + `axiom resume` can re-enter after gate_resolve.
        try:
            return self._run_sequence(spec)
        except GateHalt as e:
            return {"gate_halted": e.gate_id}

    def _run_steps(self, steps, values, spec_version_id, spec):
        """Run a list of step refs (str | ["parallel", ...]) against an
        initial values dict (mutated in place as steps produce output) and
        return the last step's {validated_output: ...} (or {} for an empty
        list). Extracted from `_run_sequence` so v2 control-flow nodes
        (condition.then_branch / else_branch, and later repeat/until/gate
        bodies) can run a step list with an inherited upstream context."""
        last = {"validated_output": {}}
        for step in steps:
            if isinstance(step, str):
                node = spec.nodes[step]
                out = self._dispatch_with_retry(node, values, spec_version_id, spec)
                # P-008: agent-verdict fix loop (fixes run 4: after verify FAILED
                # the run went straight to PARTIAL with no one routing back to impl
                # to fix). If this node declares fix_loop and its verdict_field
                # returns FAILED, re-dispatch the paired impl node with the issues
                # to fix, then re-dispatch this node to re-verify, up to max_rounds
                # rounds; continue once fixed or rounds exhausted. The impl/verify
                # nodes each add one JSON line to declare it -- no control_flow
                # change. Safety: whether the fix is right is adjudicated by verify,
                # max_rounds caps LLM self-loop cost; the impl re-run still goes
                # through P-006 recovery / normal retry.
                out = self._maybe_fix_loop(
                    node, out, values, spec_version_id, spec)
                values[step] = out or {}
                if out is not None:
                    last = out
            elif isinstance(step, list) and step and step[0] == "parallel":
                outs = []
                for nid in step[1:]:
                    n = spec.nodes[nid]
                    r = self._dispatch_with_retry(
                        n, values, spec_version_id, spec, localized=True)
                    outs.append(r.get("validated_output") if r else None)
                values["par"] = {"validated_output": outs}
                last = {"validated_output": outs}
        return last

    def _maybe_fix_loop(self, node, out, values, spec_version_id, spec):
        """P-008: if node declares fix_loop and its verdict_field returns FAILED,
        re-run impl + re-verify.

        fix_loop looks like {"impl": "n_impl", "max_rounds": 2}, declared on the
        verdict (verify) node; impl points to the impl node id to re-fix. Triggers
        only when out is not None and validated_output[verdict_field] == "FAILED".
        Each round: inject verify's issues into the impl re-run prompt context
        (values["_fix_feedback"]), re-run impl, then re-run this verify node.
        Returns the final verify out (may be VERIFIED or still FAILED).
        Non-fix_loop node / non-FAILED / no impl reference -> return out unchanged
        (zero behavior change).
        """
        fl = node.get("fix_loop") if isinstance(node, dict) else None
        if not fl or not isinstance(out, dict):
            return out
        vf = node.get("verdict_field")
        if not vf:
            return out
        impl_id = fl.get("impl")
        if not impl_id or impl_id not in spec.nodes:
            return out
        max_rounds = int(fl.get("max_rounds", 2))
        vo = out.get("validated_output") or {}
        rounds = 0
        while vo.get(vf) == "FAILED" and rounds < max_rounds:
            rounds += 1
            issues = vo.get("issues") or []
            # Expose verify's issues as fix feedback to the impl re-run prompt
            # render context. The impl prompt may reference {{_fix_feedback}};
            # if it doesn't, no effect (impl re-reads the current state).
            values["_fix_feedback"] = {
                "round": rounds, "issues": issues,
                "note": "Previous verify round returned FAILED; please fix per the issues and re-report",
            }
            self._record({
                "event_id": self._ev_id(), "kind": "fix_loop_iteration",
                "spec_version_id": spec_version_id, "node_id": node["id"],
                "payload": {"round": rounds, "impl": impl_id,
                            "issues": issues, "max_rounds": max_rounds},
                "claims": [],
            })
            # Re-run impl to fix (goes through P-006 recovery / normal retry;
            # a None failure aborts the loop)
            impl_out = self._dispatch_with_retry(
                spec.nodes[impl_id], values, spec_version_id, spec)
            values[impl_id] = impl_out or {}
            if impl_out is None:
                break
            # Re-run this verify node to re-adjudicate
            out = self._dispatch_with_retry(node, values, spec_version_id, spec)
            if not isinstance(out, dict):
                break
            vo = out.get("validated_output") or {}
        # Clean up the one-shot feedback context so it does not pollute later node rendering
        values.pop("_fix_feedback", None)
        return out

    def _run_sequence(self, spec):
        return self._run_steps(
            spec.control_flow.get("steps", []),
            {}, spec.spec_version_id, spec,
        )

    # --- condition (spec §1.1) ----------------------------------------

    def _run_condition(self, node, values, spec_version_id, spec):
        """Dispatch a condition node: evaluate the deterministic predicate,
        emit branch_decision, then execute then_branch or else_branch.

        Invariant (1): the predicate is a pure ledger-projection read —
        NEVER an LLM call. Invariant (3): cannot-branch-on-PROPOSED (§2 Q2)
        — a PROPOSED-reading predicate is advisory (degraded=True); a
        binding branch requires gate=True or predicate-on-VERIFIED.
        """
        svid = spec_version_id
        node_id = node["id"]
        predicate = node.get("predicate", "")

        # 1. evaluate predicate -> (value, claim_strength)
        value, claim_strength = self._eval_predicate(
            predicate, values, spec, self.ledger, svid, node_id=node_id)

        # 2. branch_taken: True -> then; False or 'undefined' -> else
        #    (undefined takes the safe else default — §2 Q1).
        branch_taken = "then" if value is True else "else"

        # 3. degraded: predicate read a PROPOSED validated_output field ->
        #    advisory branch (downstream success_evidence cannot rely on it;
        #    Task 16's adversarial test enforces, we record the flag).
        degraded = (claim_strength == "proposed")

        # 4. emit branch_decision (FACT — derive_claim_status does NOT read
        #    it, §4). Top-level node_id is authoritative (Task 5 parked
        #    finding: it is also mirrored in the payload for §3 shape parity).
        self.emit_branch_decision(
            spec_version_id=svid, node_id=node_id, predicate=predicate,
            eval_result=value, branch_taken=branch_taken,
            degraded=degraded, claim_strength=claim_strength)

        # 5. binding vs advisory (§2 Q2):
        #    - gate=True -> BINDING: gate_open(reason='binding_branch') +
        #      GateHalt for human approval. On resume:
        #        * gate_resolve(decision='allow') -> execute the chosen branch.
        #        * gate_resolve(decision='deny')  -> TERMINAL: the human rejected
        #          the binding path; do NOT execute (return empty output so
        #          derive_verdict sees the requirement unmet -> PARTIAL/
        #          UNVERIFIED -- the honest deny outcome). deny means "don't
        #          proceed," not "try the other branch."
        #        * unresolved -> raise GateHalt (await human resolve).
        #        * gate_resolve(decision='modify') triggers a replan to a new
        #          spec; this spec's condition is abandoned (does not proceed).
        #    - gate=False + verified -> BINDING: execute directly.
        #    - gate=False + proposed -> ADVISORY (degraded=True): execute.
        if node.get("gate") is True:
            svid_events = [e for e in self.ledger.events()
                           if e.get("spec_version_id", svid) == svid]
            opened = {e["gate_id"] for e in svid_events
                      if e.get("kind") == "gate_open"
                      and e.get("node_id") == node_id}
            # only an ALLOWED gate unblocks the binding branch; a DENIED gate
            # is terminal (do not execute); MODIFY triggers a replan (the
            # current spec is abandoned, so it does not proceed either).
            allowed = {e["gate_id"] for e in svid_events
                       if e.get("kind") == "gate_resolve"
                       and e.get("decision") == "allow"}
            denied = {e["gate_id"] for e in svid_events
                      if e.get("kind") == "gate_resolve"
                      and e.get("decision") == "deny"}
            if not (opened & allowed):
                if opened & denied:
                    # denied binding branch: produce no output; the requirement
                    # goes unmet (derive_verdict -> PARTIAL/UNVERIFIED).
                    return {"validated_output": {}}
                gate_id = self._gate(
                    "binding_branch", node_id, svid, subtree=branch_taken)
                raise GateHalt(gate_id)

        # 6. execute the chosen branch step list via _run_steps. A copy of
        #    the upstream values dict is passed so the branch body inherits
        #    upstream {{ref}} context without mutating the outer sequence's
        #    values (an empty branch -> {"validated_output": {}}).
        branch = (node.get("then_branch") or []) if branch_taken == "then" \
            else (node.get("else_branch") or [])
        return self._run_steps(list(branch), dict(values), svid, spec)

    # --- gate (spec §1.4 -- staged escalation) ----------------------------

    def _run_gate(self, node, values, spec_version_id, spec):
        """Dispatch a gate node: staged escalation (§1.4, §3, Q6).

        1. eval node["trigger"] via _eval_predicate -> (value, claim_strength).
        2. if value is True (triggered):
           - on_trigger='pause'  -> gate_open(reason='escalation') + raise
             GateHalt for human approval; after gate_resolve(allow) execute
             body (resume guard mirrors _run_condition's gate=True path: deny
             is terminal, returns empty; unresolved re-raises).
           - on_trigger='auto'   -> auto-escalate to next tier: gate_open(
             reason='escalation') + gate_resolve(decision='escalate') (binding)
             + execute body with the next tier's config (model/allowed_tools/
             skeptic_count from node["escalation_tiers"]) applied to the body
             agents.
           - on_trigger='replan' -> emit replan_requested (boundary); do NOT
             execute body (invariant 2: replan only at the workflow boundary).
        3. if value is False or 'undefined' (not triggered) -> execute body
           directly (no gate).
        4. REUSES the v1 gate_open/gate_resolve mechanism -- NO new event
           kinds. Distinguished from v1 risk-gates by gate_open.reason=
           'escalation' (v1 risk-gates use 'risk_high'|'contract_drift'|
           'unretriable_failure'). derive_verdict BLOCKED on an unresolved
           gate_open fires for free (v1 state machine, state.py:140-159).
        """
        svid = spec_version_id
        node_id = node["id"]
        trigger = node.get("trigger", "")
        on_trigger = node.get("on_trigger", "pause")
        body = list(node.get("body", []))

        # 1. evaluate the trigger predicate (a FACT, never an LLM call --
        #    invariant 1; same _eval_predicate used by condition/until).
        value, _claim_strength = self._eval_predicate(
            trigger, values, spec, self.ledger, svid, node_id=node_id)

        # 2. not triggered -> execute body directly (no gate).
        if value is not True:
            return self._run_steps(body, dict(values), svid, spec)

        # 3. triggered -- route by on_trigger.
        if on_trigger == "replan":
            # boundary signal: emit replan_requested, do NOT execute body.
            # A node-level replan is a signal, not a synchronous callback
            # (§5.3, invariant 2) -- the orchestrator draws the next spec.
            self._record({
                "event_id": self._ev_id(), "kind": "replan_requested",
                "spec_version_id": svid, "node_id": node_id,
                "payload": {"reason": "gate_replan"}, "claims": [],
            })
            return {"validated_output": {}}

        if on_trigger == "auto":
            # auto-escalate to the next tier: open the gate (reason='escalation')
            # and immediately resolve it with decision='escalate' (binding --
            # no human in the loop). Then execute body with the next tier's
            # config applied to the body agents.
            gate_id = self._gate("escalation", node_id, svid)
            self.resolve_gate(gate_id, "escalate")
            tiers = node.get("escalation_tiers") or []
            if tiers:
                return self._run_gate_body_with_tier(
                    body, values, svid, spec, tiers[0])
            return self._run_steps(body, dict(values), svid, spec)

        # on_trigger == 'pause' (default). Resume guard mirrors _run_condition's
        # gate=True path: only an ALLOWED gate unblocks; a DENIED gate is
        # terminal (body does NOT execute); otherwise open + raise GateHalt.
        svid_events = [e for e in self.ledger.events()
                       if e.get("spec_version_id", svid) == svid]
        opened = {e["gate_id"] for e in svid_events
                  if e.get("kind") == "gate_open"
                  and e.get("node_id") == node_id
                  and e.get("reason") == "escalation"}
        allowed = {e["gate_id"] for e in svid_events
                   if e.get("kind") == "gate_resolve"
                   and e.get("decision") == "allow"}
        denied = {e["gate_id"] for e in svid_events
                  if e.get("kind") == "gate_resolve"
                   and e.get("decision") == "deny"}
        if not (opened & allowed):
            if opened & denied:
                # denied escalation: terminal, do NOT execute body.
                return {"validated_output": {}}
            gate_id = self._gate("escalation", node_id, svid)
            raise GateHalt(gate_id)

        # gate allowed -> execute body.
        return self._run_steps(body, dict(values), svid, spec)

    def _run_gate_body_with_tier(self, body, values, svid, spec, tier):
        """Run the gate body's step list with the next escalation tier's config
        (model/allowed_tools/skeptic_count) merged into each body agent node.

        IMMUTABLE node-override (spec §0 / invariant 6: Spec History is a
        truth source, append-only via revisions; `spec.nodes` is never mutated
        at runtime -- v1's resolve_gate creates spec.v{n+1}.json instead of
        editing in place). We build a SHALLOW copy of spec with an OVERRIDDEN
        nodes dict: the original `spec.nodes` is untouched (no finally-restore
        hack). `_run_steps` reads `spec.nodes[step]` / `spec.nodes[nid]`, so it
        picks up the merged tier config from `spec_copy.nodes`.

        The override applies to ALL agent node ids referenced in the body,
        including ids inside `["parallel", "a1", "a2", ...]` sublists (and
        nested parallel lists) -- `run_parallel` dispatches them from
        `spec.nodes[nid]`, so a string-only walk would miss parallel-sublist
        agents. `_collect_body_agent_ids` recurses the step list to collect
        every referenced id.
        """
        agent_ids = self._collect_body_agent_ids(body)
        new_nodes = dict(spec.nodes)  # shallow: shares node dicts, overrides only the body agents
        for nid in agent_ids:
            if nid in spec.nodes:
                merged = dict(spec.nodes[nid])
                if "model" in tier:
                    merged["model"] = tier["model"]
                if "allowed_tools" in tier:
                    merged["allowed_tools"] = list(tier["allowed_tools"])
                if "skeptic_count" in tier:
                    merged["skeptic_count"] = tier["skeptic_count"]
                new_nodes[nid] = merged  # override only this agent's dict
        spec_copy = copy.copy(spec)
        spec_copy.nodes = new_nodes
        return self._run_steps(body, dict(values), svid, spec_copy)

    @staticmethod
    def _collect_body_agent_ids(steps):
        """Recursively collect every bare node-id string referenced in a body
        step list, including ids inside `["parallel", "a1", "a2"]` sublists
        and arbitrarily nested parallel lists. Returns a de-duplicated list
        preserving first-seen order."""
        out = []
        seen = set()

        def _walk(s):
            if isinstance(s, str):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            elif isinstance(s, list):
                for x in s:
                    _walk(x)
            # inline dict templates (parallel.body, pipeline stages) are not
            # bare ids and are skipped -- they carry their own config inline.

        for step in steps:
            _walk(step)
        return out



    def _run_repeat(self, node, values, spec_version_id, spec):
        """Dispatch a repeat node: loop-until-dry convergence (§1.2/§3/§4/§8).

        1. parse `until` (default "dry<2" -> N=2); generate a unique loop_id.
        2. emit loop_open{loop_id, node_id, max_iterations, until}.
        3. for k in 1..max_iterations:
             emit iteration_open{loop_id, iteration:k};
             run node["body"] via _run_steps with the per-thread loop context
             set so BODY events (agent_result/verify_verdict/stagnating/
             artifact_write) carry loop_id+iteration:k IN THEIR PAYLOAD (§3,
             invariant 6); compute dry_count via state.derive_dry_count
             (RE-DERIVATION, invariant 6, never cached); check the mid-
             iteration budget cap (§8 Q4: if `_budget_already_exceeded`
             tripped during the body, emit iteration_close
             exit_reason='budget_exhausted' and break — partial work is
             auditable); else emit iteration_close{condition_eval, dry_count};
             if dry_count>=N -> converged, break; else check the stagnation
             cap (consecutive stagnating iterations > spec.max_stagnation ->
             break with exit_reason='stagnation_cap').
        4. emit loop_close{final_iteration, exit_reason} — 'converged',
           'exhausted', 'budget_exhausted' (mid-iteration Q4 exit), or
           'stagnation_cap' (consecutive stagnating iterations > max_stagnation).
        5. if NOT converged -> _exhaust(node, svid, "loop_exhausted",
           localized=False) with failure_policy.on_exhausted: block->BLOCKED
           (default, required-convergence); degrade->soft None->PARTIAL;
           replan->replan_requested.

        Invariant (1): loop exit is DETERMINISTIC — dry_count re-derivation +
        the max_iterations HARD bound; NEVER an LLM call decides continuation.
        Invariant (6): dry_count is rebuilt from events every iteration
        (state.derive_dry_count is the single source of truth; the
        iteration_close field is for auditability, not read back here).
        """
        svid = spec_version_id
        node_id = node["id"]
        until = node.get("until") or "dry<2"
        max_iterations = node["max_iterations"]

        # 1. parse the built-in dry metric "dry<N" -> N. UntilNode's general
        #    predicate (Task 9) does not match here; a non-matching `until`
        #    falls through to exhausted (clean extension point for Task 9).
        m = _DRY_RE.match(until)
        dry_n = int(m.group(1)) if m else None

        # P2-8 fix: if a prior run of this repeat already converged (a
        # loop_close{exit_reason='converged'} for a loop_open tagged with this
        # node+svid), do NOT re-enter the loop. The old code always generated a
        # fresh loop_id (uuid4) on re-entry, so the replay cache key
        # (svid, node_id, OLD loop_id, iteration) could never match the NEW
        # loop_id -> cache miss -> the body worker was re-dispatched (zero
        # replay_hit) even though the loop had already converged. Resume now
        # replays the converged body output instead. loop_close carries no
        # node_id, so the prior loop_id is recovered from this node's loop_open.
        # Guarded by self._replay (set by build_replay_cache on resume) so a
        # fresh in-process dispatch still generates a new loop_id (two
        # dispatches of the same node get different loop_ids -- the resume-only
        # replay must not break that contract).
        if getattr(self, "_replay", None):
            prior_loop_ids = [
                e.get("payload", {}).get("loop_id")
                for e in self.ledger.events()
                if e.get("kind") == "loop_open"
                   and e.get("spec_version_id") == svid
                   and e.get("node_id") == node_id
            ]
            prior_converged_loop_id = None
            for lid in prior_loop_ids:
                if lid and any(
                        e.get("kind") == "loop_close"
                        and e.get("spec_version_id") == svid
                        and e.get("payload", {}).get("loop_id") == lid
                        and e.get("payload", {}).get("exit_reason") == "converged"
                        for e in self.ledger.events()):
                    prior_converged_loop_id = lid
                    break
            if prior_converged_loop_id is not None:
                # P1-10: replay the loop's ACTUAL terminal output from
                # loop_close (recorded at convergence), not the last
                # agent_result of the body (which is wrong when the loop's
                # terminal body node is a script, not an agent -- resume
                # returned {x:1} instead of {transformed:2}). Also gate on
                # input_hash: if the loop's outer input changed since the
                # converged run, the cached terminal is stale -> fall through
                # and re-enter the loop (changed_input A->B must not hit A).
                cur_in_hash = Harness._input_hash(values)
                terminal_vo = None
                stored_in_hash = None
                for e in self.ledger.events():
                    pl = e.get("payload", {}) if isinstance(
                        e.get("payload"), dict) else {}
                    if (e.get("kind") == "loop_close"
                            and e.get("spec_version_id") == svid
                            and pl.get("loop_id") == prior_converged_loop_id
                            and pl.get("exit_reason") == "converged"):
                        terminal_vo = pl.get("terminal_output")
                        stored_in_hash = pl.get("input_hash")
                        break
                input_ok = (stored_in_hash is None or cur_in_hash is None
                            or stored_in_hash == cur_in_hash)
                if terminal_vo is not None and input_ok:
                    self._record({
                        "event_id": self._ev_id(), "kind": "replay_hit",
                        "spec_version_id": svid, "node_id": node_id,
                        "payload": {"validated_output": terminal_vo,
                                    "replayed": True,
                                    "replayed_loop": prior_converged_loop_id},
                        "claims": [],
                    })
                    return {"validated_output": terminal_vo}
                # terminal_output missing (pre-P1-10 loop_close) or input
                # changed -> fall through to re-enter the loop.

        # unique per loop instance (uuid4 — thread-safe, no lock). Two
        # dispatches of the same node get different loop_ids.
        loop_id = f"loop_{node_id}_{uuid.uuid4().hex[:8]}"

        # 2. emit loop_open
        self.emit_loop_open(
            spec_version_id=svid, loop_id=loop_id, node_id=node_id,
            max_iterations=max_iterations, until=until,
        )

        # save/restore the prior loop context (defensive for nested loops;
        # the inner _run_repeat overwrites ctx for its body, then restores).
        prior_ctx = getattr(self._loop_ctx, "ctx", None)
        converged = False
        budget_exhausted = False
        stagnation_cap = False
        consecutive_stagnating = 0
        last_out = {"validated_output": {}}
        final_k = 0
        try:
            for k in range(1, max_iterations + 1):
                self.emit_iteration_open(
                    spec_version_id=svid, loop_id=loop_id, iteration=k)
                # set the loop context so body events emitted by _run_steps
                # -> dispatch_agent/_run_agent_with_retry/run_verify carry
                # loop_id+iteration in their payload (thread-local: same-
                # thread dispatch reads it directly; ThreadPoolExecutor
                # fan-outs capture+re-set it in their workers).
                self._loop_ctx.ctx = {"loop_id": loop_id, "iteration": k}
                try:
                    last_out = self._run_steps(
                        list(node.get("body", [])), dict(values), svid, spec)
                finally:
                    # restore BEFORE iteration_close so the lifecycle event
                    # is not double-tagged (it carries loop_id+iteration via
                    # its own emit helper already).
                    self._loop_ctx.ctx = prior_ctx

                # 3. dry_count: re-derivation from events (invariant 6).
                dry_count = self._derive_dry_count(
                    self.ledger, svid, loop_id, k)

                # --- Task 11: mid-iteration budget exit (Q4, §8) ---
                # The budget tripped DURING the body (the `budget_exhausted`
                # event was already emitted inside `_tally` when a body
                # dispatch crossed budget_usd; the per-body-dispatch
                # `_budget_already_exceeded` guard in _run_agent_with_retry
                # then refused further dispatches). SILENT re-check here (no
                # duplicate event): emit iteration_close with the budget
                # exit_reason so the PARTIAL iteration's work + cost are
                # auditable, then break immediately (Q4 — don't complete an
                # iteration that risked exceeding budget).
                if self._budget_already_exceeded(spec):
                    self.emit_iteration_close(
                        spec_version_id=svid, loop_id=loop_id, iteration=k,
                        condition_eval=until, dry_count=dry_count,
                        exit_reason="budget_exhausted",
                    )
                    final_k = k
                    budget_exhausted = True
                    break

                self.emit_iteration_close(
                    spec_version_id=svid, loop_id=loop_id, iteration=k,
                    condition_eval=until, dry_count=dry_count,
                )
                final_k = k

                # 4. deterministic convergence check (invariant 1). Checked
                #    BEFORE the stagnation cap so a converged iteration exits
                #    'converged' (success wins over stagnation).
                if dry_n is not None and dry_count >= dry_n:
                    converged = True
                    break
                # 5. stagnation cap (§8): consecutive stagnating iterations
                #    exceeding spec.max_stagnation -> break honestly. A
                #    non-stagnating iteration resets the streak. Only reached
                #    when the iteration did NOT converge.
                tripped, consecutive_stagnating = self._stagnation_cap_tripped(
                    spec, svid, loop_id, k, consecutive_stagnating)
                if tripped:
                    stagnation_cap = True
                    break
                # else (dry_n is None -> general predicate, Task 9): continue
                # to next iteration; falls through to exhausted at the bound.
        finally:
            self._loop_ctx.ctx = prior_ctx

        # 6. emit loop_close
        if converged:
            exit_reason = "converged"
        elif budget_exhausted:
            exit_reason = "budget_exhausted"
        elif stagnation_cap:
            exit_reason = "stagnation_cap"
        else:
            exit_reason = "exhausted"
        self.emit_loop_close(
            spec_version_id=svid, loop_id=loop_id,
            final_iteration=final_k, exit_reason=exit_reason,
            terminal_output=last_out.get("validated_output", {}),
            input_hash=Harness._input_hash(values),
        )

        # 6. exhaustion routing (§4). Default on_exhausted="block" -> _gate
        #    -> gate_open unresolved -> derive_verdict BLOCKED (required-
        #    convergence semantics). degrade -> soft None -> PARTIAL.
        #    replan -> replan_requested (boundary signal).
        if not converged:
            return self._exhaust(node, svid, "loop_exhausted", localized=False)
        return last_out

    # --- until (spec §1.3) -----------------------------------------------

    def _run_until(self, node, values, spec_version_id, spec):
        """Dispatch an until node: general loop-until-condition (§1.3/§2/§3/§8).

        Like _run_repeat but with an ARBITRARY cond predicate (via
        _eval_predicate) + a budget_aware flag. DO-UNTIL semantics: the body
        runs first, then cond is evaluated; the loop exits at the END of the
        iteration where cond became True (no wasted body iteration). The loop
        also exits when max_iterations is hit (hard bound). If budget_aware=True
        (default), a PRE-iteration check refuses to enter iteration k when
        _budget_already_exceeded (honest partial exit without incurring cost).

        1. generate loop_id; emit loop_open{loop_id, node_id, max_iterations,
           cond} (UntilNode uses cond, NOT until).
        2. for k in 1..max_iterations:
             a. budget_aware PRE-iteration check: if node.budget_aware and
                _budget_already_exceeded(spec) -> break with
                exit_reason='budget_exhausted' (do NOT enter iteration k:
                no iteration_open, no body dispatch). final_iteration=k-1.
             b. emit iteration_open{loop_id, iteration:k}.
             d. run node["body"] via _run_steps with the per-thread loop
                context set so BODY events (agent_result/verify_verdict/
                stagnating/artifact_write) carry loop_id+iteration:k IN THEIR
                PAYLOAD (§3, invariant 6 — REUSES Task 8's _loop_ctx, not
                reinvented).
             c. eval node["cond"] via _eval_predicate -> (value, claim_strength).
                value is True|False|'undefined'; the loop exits when value
                is True. cond on claim_status(...)=='VERIFIED' -> 'verified'
                (binding); cond on a PROPOSED {{ref}} -> 'proposed' (advisory,
                §2 Q2 — recorded but the exit is advisory).
             e. emit iteration_close{condition_eval=cond, dry_count,
                claim_strength} (dry_count via state.derive_dry_count for
                audit, even though until uses cond).
             f. if value is True (cond met) -> converged; break.
        3. emit loop_close{final_iteration, exit_reason} — 'converged' if
           cond met, 'exhausted' if max_iterations hit without cond,
           'budget_exhausted' (pre-iteration budget_aware trip OR mid-
           iteration Q4 exit), 'stagnation_cap' (consecutive stagnating
           iterations > max_stagnation).
        4. if not converged -> _exhaust(node, svid, "loop_exhausted",
           localized=False) with failure_policy.on_exhausted: degrade
           (default) -> soft None -> PARTIAL; block -> BLOCKED; replan ->
           replan_requested.

        Invariant (1): loop exit is DETERMINISTIC — cond eval is a pure ledger
        read + max_iterations HARD bound; NEVER an LLM call decides continuation.
        Invariant (2): exits via cond, NOT mid-loop replan.
        Invariant (3): cond on VERIFIED=binding; cond on PROPOSED=advisory (Q2).
        """
        svid = spec_version_id
        node_id = node["id"]
        cond = node.get("cond", "")
        max_iterations = node["max_iterations"]
        budget_aware = node.get("budget_aware", True)

        # P2-8 fix (same as _run_repeat): if a prior run of this until already
        # converged, do NOT re-enter -- the fresh loop_id would miss the replay
        # cache (key carries the OLD loop_id) and re-dispatch the body. Guarded
        # by self._replay (resume only) so a fresh dispatch still gets a new
        # loop_id.
        if getattr(self, "_replay", None):
            prior_loop_ids = [
                e.get("payload", {}).get("loop_id")
                for e in self.ledger.events()
                if e.get("kind") == "loop_open"
                   and e.get("spec_version_id") == svid
                   and e.get("node_id") == node_id
            ]
            prior_converged_loop_id = None
            for lid in prior_loop_ids:
                if lid and any(
                        e.get("kind") == "loop_close"
                        and e.get("spec_version_id") == svid
                        and e.get("payload", {}).get("loop_id") == lid
                        and e.get("payload", {}).get("exit_reason") == "converged"
                        for e in self.ledger.events()):
                    prior_converged_loop_id = lid
                    break
            if prior_converged_loop_id is not None:
                # P1-10: replay the loop's ACTUAL terminal output from
                # loop_close (recorded at convergence), not the last
                # agent_result of the body. Gate on input_hash: if the loop's
                # outer input changed since the converged run, the cached
                # terminal is stale -> fall through and re-enter the loop.
                cur_in_hash = Harness._input_hash(values)
                terminal_vo = None
                stored_in_hash = None
                for e in self.ledger.events():
                    pl = e.get("payload", {}) if isinstance(
                        e.get("payload"), dict) else {}
                    if (e.get("kind") == "loop_close"
                            and e.get("spec_version_id") == svid
                            and pl.get("loop_id") == prior_converged_loop_id
                            and pl.get("exit_reason") == "converged"):
                        terminal_vo = pl.get("terminal_output")
                        stored_in_hash = pl.get("input_hash")
                        break
                input_ok = (stored_in_hash is None or cur_in_hash is None
                            or stored_in_hash == cur_in_hash)
                if terminal_vo is not None and input_ok:
                    self._record({
                        "event_id": self._ev_id(), "kind": "replay_hit",
                        "spec_version_id": svid, "node_id": node_id,
                        "payload": {"validated_output": terminal_vo,
                                    "replayed": True,
                                    "replayed_loop": prior_converged_loop_id},
                        "claims": [],
                    })
                    return {"validated_output": terminal_vo}
                # terminal_output missing or input changed -> re-enter loop.

        # unique per loop instance (uuid4 — thread-safe, no lock).
        loop_id = f"loop_{node_id}_{uuid.uuid4().hex[:8]}"

        # 1. emit loop_open (cond, not until)
        self.emit_loop_open(
            spec_version_id=svid, loop_id=loop_id, node_id=node_id,
            max_iterations=max_iterations, cond=cond,
        )

        # save/restore the prior loop context (defensive for nested loops;
        # the inner _run_until overwrites ctx for its body, then restores).
        prior_ctx = getattr(self._loop_ctx, "ctx", None)
        converged = False
        budget_exhausted = False
        stagnation_cap = False
        consecutive_stagnating = 0
        last_out = {"validated_output": {}}
        final_k = 0
        try:
            for k in range(1, max_iterations + 1):
                # a. budget_aware PRE-iteration check (§8). If the budget was
                #    already crossed (by prior dispatches in or outside this
                #    loop), do NOT enter iteration k: no iteration_open, no
                #    body dispatch. final_iteration=k-1 (the last completed
                #    iteration, 0 if the budget was exhausted before iter 1).
                if budget_aware and self._budget_already_exceeded(spec):
                    final_k = k - 1
                    budget_exhausted = True
                    break

                # b. iteration_open
                self.emit_iteration_open(
                    spec_version_id=svid, loop_id=loop_id, iteration=k)

                # d. run body FIRST (do-until semantics: run body, then check
                #    cond, exit at the END of the iteration where cond became
                #    True — no wasted body iteration). The loop context is
                #    set so BODY events (agent_result/verify_verdict/
                #    stagnating/artifact_write) carry loop_id+iteration:k IN
                #    THEIR PAYLOAD (§3, invariant 6 — REUSES Task 8's
                #    _loop_ctx, not reinvented).
                self._loop_ctx.ctx = {"loop_id": loop_id, "iteration": k}
                try:
                    last_out = self._run_steps(
                        list(node.get("body", [])), dict(values), svid, spec)
                finally:
                    # restore BEFORE iteration_close so the lifecycle event
                    # is not double-tagged (it carries loop_id+iteration via
                    # its own emit helper already).
                    self._loop_ctx.ctx = prior_ctx

                # c. eval cond via _eval_predicate -> (value, claim_strength).
                #    Evaluated AFTER the body (do-until): a verify body at
                #    iter k may have made claim_status VERIFIED → cond True
                #    → exit at the end of THIS iteration (no extra body run).
                #    cond on claim_status(...)=='VERIFIED' -> 'verified'
                #    (binding); cond on a PROPOSED {{ref}} -> 'proposed'
                #    (advisory, §2 Q2 — recorded but the exit is advisory).
                value, claim_strength = self._eval_predicate(
                    cond, values, spec, self.ledger, svid,
                    node_id=node_id, loop_id=loop_id, iteration=k)

                # e. dry_count via re-derivation for audit, even though until
                #    uses cond; claim_strength records whether the cond was
                #    verified/proposed/deterministic.
                dry_count = self._derive_dry_count(
                    self.ledger, svid, loop_id, k)

                # --- Task 11: mid-iteration budget exit (Q4, §8) ---
                # The budget tripped DURING the body (the `budget_exhausted`
                # event was already emitted inside `_tally`; the per-body-
                # dispatch `_budget_already_exceeded` guard refused further
                # dispatches). SILENT re-check (no duplicate event): emit
                # iteration_close with exit_reason='budget_exhausted' so the
                # PARTIAL iteration's work + cost are auditable, then break
                # immediately (Q4 — don't complete an iteration that risked
                # exceeding budget). Evaluated AFTER cond/dry_count so the
                # partial iteration's audit record is complete.
                if self._budget_already_exceeded(spec):
                    self.emit_iteration_close(
                        spec_version_id=svid, loop_id=loop_id, iteration=k,
                        condition_eval=cond, dry_count=dry_count,
                        claim_strength=claim_strength,
                        exit_reason="budget_exhausted",
                    )
                    final_k = k
                    budget_exhausted = True
                    break

                self.emit_iteration_close(
                    spec_version_id=svid, loop_id=loop_id, iteration=k,
                    condition_eval=cond, dry_count=dry_count,
                    claim_strength=claim_strength,
                )
                final_k = k

                # f. deterministic convergence check (invariant 1). Checked
                #    BEFORE the stagnation cap so a converged iteration exits
                #    'converged' (success wins over stagnation).
                if value is True:
                    converged = True
                    break
                # g. stagnation cap (§8): consecutive stagnating body
                #    iterations exceeding spec.max_stagnation -> break
                #    honestly. A non-stagnating iteration resets the streak.
                #    Only reached when the iteration did NOT converge.
                tripped, consecutive_stagnating = self._stagnation_cap_tripped(
                    spec, svid, loop_id, k, consecutive_stagnating)
                if tripped:
                    stagnation_cap = True
                    break
        finally:
            self._loop_ctx.ctx = prior_ctx

        # 3. emit loop_close
        if converged:
            exit_reason = "converged"
        elif budget_exhausted:
            exit_reason = "budget_exhausted"
        elif stagnation_cap:
            exit_reason = "stagnation_cap"
        else:
            exit_reason = "exhausted"
        self.emit_loop_close(
            spec_version_id=svid, loop_id=loop_id,
            final_iteration=final_k, exit_reason=exit_reason,
            terminal_output=last_out.get("validated_output", {}),
            input_hash=Harness._input_hash(values),
        )

        # 4. exhaustion routing (§4). Default on_exhausted="degrade" -> soft
        #    None -> derive_verdict PARTIAL (best-effort-convergence semantics).
        #    block -> _gate -> BLOCKED. replan -> replan_requested (boundary).
        if not converged:
            return self._exhaust(node, svid, "loop_exhausted", localized=False)
        return last_out

    # --- Gate -------------------------------------------------------

    def check_gate(self, node) -> bool:
        """True if a node triggers a Gate (high risk / secrets / delete areas)
        AND has no prior gate_resolve(allow) for this node in the ledger.

        P2-9 fix: a risk:high node re-entered on resume (or after a gate
        resolve+re-run) re-opened a NEW Gate every dispatch (G_n1_1 -> allow ->
        re-run -> G_n1_4, worker calls stayed 0) because the old check only
        inspected the static node config, not the ledger's authorization
        history. Now an existing gate_resolve(allow) on a gate_open for this
        node authorizes re-entry (no new gate)."""
        triggers = (node.get("risk") == "high" or any(
            "secrets" in wa or "delete" in wa
            for wa in node.get("write_areas", [])))
        if not triggers:
            return False
        nid = node.get("id")
        if nid is None:
            return True
        # P1-11: gate authorization binds to the gated ACTION SCOPE
        # (write_areas + risk + prompt), not just node_id. v1 authorizing
        # action A (risk:high on write_areas X) must NOT auto-authorize v2's
        # action B under the same node_id (different write_areas / prompt) ->
        # a scope mismatch opens a NEW gate. The prior hole: check_gate only
        # asked "has this node_id ever been allowed", so v2's changed action
        # executed without approval (gate_authorization_scope probe).
        cur_scope = Harness._action_scope_hash(node)
        allowed_gids = {
            e.get("gate_id") for e in self.ledger.events()
            if e.get("kind") == "gate_resolve" and e.get("decision") == "allow"
        }
        for e in self.ledger.events():
            if (e.get("kind") == "gate_open"
                    and e.get("node_id") == nid
                    and e.get("gate_id") in allowed_gids):
                # P-024: authorization requires (a) the resolved gate to be a
                # RISK gate and (b) its action scope to equal the current
                # action's scope. The prior form treated `stored_scope is
                # None` as a wildcard -- but a NON-risk gate (unretriable_
                # failure / stagnation) never records a scope, so "allow the
                # retry" silently authorized a LATER, DIFFERENT high-risk
                # action under the same node_id (review4
                # test_retry_approval_cannot_authorize_a_new_high_risk_action).
                # A retry approval is scope-less by nature; it can never
                # authorize risk. Legacy risk gates without a scope (pre-P-011
                # ledgers) are likewise NOT treated as authorization -- they
                # re-gate once (honest) instead of leaking cross-version.
                if e.get("reason") != "risk_high":
                    continue  # retry/operational approvals never authorize risk
                stored_scope = e.get("action_scope_hash")
                if (stored_scope is not None and cur_scope is not None
                        and stored_scope == cur_scope):
                    return False  # already authorized for THIS action scope
                # same node_id but the action scope changed / is unknown ->
                # NOT authorized; fall through to open a new gate.
        return True

    def _gate(self, reason, node_id, spec_version_id, subtree=None,
              action_scope_hash=None) -> str:
        gid = f"G_{node_id}_{self._next_event_seq()}"
        rec = {
            "event_id": self._ev_id(), "kind": "gate_open",
            "spec_version_id": spec_version_id, "node_id": node_id,
            "gate_id": gid, "reason": reason, "subtree": subtree,
        }
        # P1-11: pin the gated action scope so check_gate can detect a
        # changed action under the same node_id (v1 allow action A -> v2
        # same node_id action B must re-gate, not inherit the allow).
        if action_scope_hash is not None:
            rec["action_scope_hash"] = action_scope_hash
        self._record(rec)
        return gid

    def resolve_claim(self, claim_id, basis_ref, svid=None, note=None,
                      resolved_by="human"):
        """Resolve a CONFLICTED claim to SUPPORTED via a CLAIM_RESOLVED event
        (absorbed from the original axiom M3 grounded resolution).

        The claim must be CONFLICTED (different verify nodes contradicting);
        the resolution grounds it on an evidence basis_ref. ONLY CONFLICTED ->
        SUPPORTED -- derive_claim_status ignores claim_resolved on non-CONFLICTED
        claims, and a human cannot self-certify VERIFIED (the claim still needs
        verify to reach a terminal).

        svid is inferred from the latest verify_verdict for this claim if not
        given; CLAIM_RESOLVED is svid-scoped (does not leak across spec
        versions).
        """
        if svid is None:
            last_vv = None
            for e in self.ledger.events():
                if (e.get("kind") == "verify_verdict"
                        and e.get("claim_id") == claim_id):
                    last_vv = e  # last write wins
            svid = (last_vv.get("spec_version_id", "spec.v1")
                    if last_vv else "spec.v1")
        self._record({
            "event_id": self._ev_id(), "kind": "claim_resolved",
            "spec_version_id": svid, "claim_id": claim_id,
            "basis_ref": basis_ref, "resolved_by": resolved_by,
            "payload": {"note": note} if note else {},
        })
        self._reanchor_seal_after_resolution("claim_resolved")

    def _reanchor_seal_after_resolution(self, kind: str) -> None:
        """P-018: re-seal the ledger anchor after a legitimate post-seal append.

        gate_resolve / claim_resolved are HUMAN resolution actions that run
        AFTER a run sealed (a BLOCKED run's documented flow is: gate list ->
        gate resolve -> re-run). The seal manifest anchors final_event_hash to
        detect tail truncation (P2-10); a legitimate post-seal append left the
        anchor stale, so `axiom journal` reported a false "seal manifest
        mismatch" tamper indicator on a healthy ledger (the
        seal_normal_gate_resolve probe). Re-writing the anchor after a
        legitimate resolution append keeps the trusted-root property (the
        anchor always matches the tail).

        P-028: re-anchoring must NOT launder a PRE-EXISTING integrity error.
        If the chain already fails verification (e.g. the sealed tail was
        truncated BEFORE this resolution), re-sealing over the truncated
        tail would erase the evidence -- the old anchor was the only proof
        of what the tail used to be (review4
        test_resolution_cannot_erase_existing_truncation_alarm). In that
        case: keep the old anchor, warn loudly, and let the resolution
        proceed (the append itself is honest); verify_chain keeps reporting
        the original truncation."""
        try:
            if not self.ledger.manifest_path.exists():
                return  # never sealed -> nothing to re-anchor
            manifest = json.loads(
                self.ledger.manifest_path.read_text(encoding="utf-8"))
            sealed_root = manifest.get("final_event_hash")
            lines = self.ledger._lines()
            present = (sealed_root is None
                       or any(e.get("event_hash") == sealed_root
                              for e in lines))
            if not present:
                # The anchor's target event is GONE -- the sealed tail was
                # truncated (or rewritten) BEFORE this resolution. Re-sealing
                # over the truncated tail would launder the evidence; the old
                # anchor is the only proof of what the tail used to be.
                print(
                    f"warning: NOT re-anchoring the seal after {kind}: the "
                    f"anchor's target event {sealed_root!r} is missing from "
                    f"the chain (sealed tail truncated/rewritten). Keeping "
                    f"the old anchor so the truncation stays detectable; "
                    f"investigate before re-sealing.",
                    file=sys.stderr)
                return
            self.ledger.seal()
        except (json.JSONDecodeError, OSError) as e:  # advisory, never block
            print(f"warning: could not re-anchor seal after {kind}: {e}",
                  file=sys.stderr)

    def resolve_gate(self, gate_id, decision, modify_intent=None, modify_spec=None):
        """Resolve a gate: allow / deny / modify.

        modify -> create spec.v{parent_rev+1}.json (never mutate in place) and
        emit a replan event. parent revision is read from the gate_open event's
        spec_version_id.
        """
        gate_open = next(
            (e for e in self.ledger.events()
             if e.get("kind") == "gate_open" and e.get("gate_id") == gate_id),
            None,
        )
        parent_svid = gate_open.get("spec_version_id", "spec.v1") if gate_open else "spec.v1"
        parent_rev = int(parent_svid.split(".v")[-1])
        n = parent_rev + 1
        self._record({
            "event_id": self._ev_id(), "kind": "gate_resolve",
            "spec_version_id": parent_svid, "gate_id": gate_id,
            "decision": decision,
        })
        if decision == "modify":
            new_spec = modify_spec or {
                "spec_version_id": f"spec.v{n}", "parent_spec_id": parent_svid,
                "revision": n, "intent": modify_intent or "", "contract_drift": True,
            }
            (self.run_dir / f"spec.v{n}.json").write_text(
                json.dumps(new_spec, ensure_ascii=False, indent=2), encoding="utf-8")
            self._record({
                "event_id": self._ev_id(), "kind": "replan",
                "checkpoint_ref": f"checkpoint:gate_{gate_id}",
                "revision_trigger": {"gate": gate_id},
            })
        # P-018: re-anchor AFTER every record this resolution appended.
        self._reanchor_seal_after_resolution("gate_resolve")

    # --- checkpoint projection (only agent re-entry) ----------------

    def project_checkpoint(self, spec) -> dict:
        """The ONLY object that re-enters orchestrator context."""
        verdict = derive_verdict(spec, self.ledger,
                                 evidence_probe=self._evidence_freshness_probe)
        svid = spec.spec_version_id
        evs = [e["event_id"] for e in self.ledger.events()]
        # scope gate/drift/budget/blocked to the current svid (consistent with
        # derive_verdict's svid-scoped projection): a v2 re-run must not inherit
        # v1's gate_open / budget_exhausted. Events with no svid field count as
        # the current svid (test-fixture compat; real events carry svid).
        gates_resolved = {
            e["gate_id"] for e in self.ledger.events()
            if e.get("kind") == "gate_resolve"
            and e.get("spec_version_id", svid) == svid
        }
        # contract_drift: the spec declares it (make_revision sets True when the
        # contract-four changed) OR a contract_drift Gate was opened. The old
        # form (gate_open reason only) was a dead field -- always False in a
        # normal run.
        drift = bool(getattr(spec, "contract_drift", False)) or any(
            e.get("kind") == "gate_open" and e.get("reason") == "contract_drift"
            and e.get("spec_version_id", svid) == svid
            for e in self.ledger.events()
        )
        budget_exhausted = any(
            e.get("kind") == "budget_exhausted"
            and e.get("spec_version_id", svid) == svid
            for e in self.ledger.events()
        )
        blocked = [
            {"gate_id": e["gate_id"], "reason": e.get("reason"),
             "node_id": e.get("node_id")}
            for e in self.ledger.events()
            if e.get("kind") == "gate_open"
            and e.get("spec_version_id", svid) == svid
            and e["gate_id"] not in gates_resolved
        ]
        # B1: synthesized_output is a PROJECTION of the last synthesize node's
        # conforming agent_result validated_output (not a hard-coded {}). A
        # synthesize that failed schema (validated_output {}) yields {} only if
        # no earlier conforming output exists -- it never shadows a real report.
        # v1.4-S1b: if no conforming report exists but the synthesize node
        # stagnated (prose after retries -- the 3/3 empirical worker-model mode),
        # DERIVE a degraded report: counts are deterministic from verify_verdict
        # (not fabricated), and the report is the worker's own prose from the
        # stagnating preview. Labeled degraded=True so the orchestrator knows it
        # did NOT pass the type boundary. This never touches the verdict (verify
        # evidence drives that) -- it only recovers a usable report field.
        synth_ids = {
            nid for nid, n in spec.nodes.items()
            if isinstance(n, dict) and n.get("type") == "synthesize"
        }
        synth_out: dict = {}
        for e in self.ledger.events():
            if (e.get("kind") == "agent_result"
                    and e.get("node_id") in synth_ids):
                vo = e.get("payload", {}).get("validated_output")
                if isinstance(vo, dict) and vo:
                    synth_out = vo  # conforming -> carries the report
                elif isinstance(vo, dict) and not synth_out:
                    synth_out = vo  # honest {} placeholder; never overwrites real
        if not synth_out:
            # degrade fallback: derive counts from verify verdicts + report prose
            surv = sum(1 for e in self.ledger.events()
                       if e.get("kind") == "verify_verdict" and e.get("survived"))
            refu = sum(1 for e in self.ledger.events()
                       if e.get("kind") == "verify_verdict" and e.get("refuted"))
            report = ""
            for e in self.ledger.events():
                if (e.get("kind") == "stagnating"
                        and e.get("node_id") in synth_ids):
                    report = e.get("payload", {}).get(
                        "result_text_preview", "") or ""
            if surv or refu or report:
                synth_out = {
                    "survived_count": surv, "refuted_count": refu,
                    "report": report, "degraded": True,
                }
        # B1: cost + dispatch observability (no manual journal tally needed).
        # A failed retry attempt that stagnates is STILL a real worker dispatch
        # dispatch with real cost -- it carries cost_usd on its stagnating
        # event. Sum over every event whose payload records cost_usd so the
        # total is complete (agent_result successes + stagnating failed
        # retries); replay_hit events have no cost (cache, zero dispatch).
        # BL-6: split cost into successful vs failed so budget_exhausted is no
        # longer confusing next to cost_usd_total. The inc1 relay showed the
        # contradiction: budget_exhausted=True (a stagnating n_verify attempt
        # pushed cumulative spend to $25.02 > $25) beside cost_usd_total=$10.33
        # -- the two used different scopes (total spend vs successful-only).
        # Money spent on a failed dispatch never comes back, so report BOTH:
        # cost_usd_total (all spend, == what budget_exhausted trips on) plus a
        # successful/failed breakdown. Failed cost is a fact, not an error.
        # Successful = agent_result with usable output: a clean conform OR a
        # recovered_from_prose (the recovery was the point of the spend).
        # Failed = stagnating + cognitive_attempt + operational_attempt (no
        # usable output) + a non-recovered agent_result with is_clean:False
        # (e.g. a permission-denied attempt recorded for audit, P-015) -- its
        # cost is failed spend. Back-compat: pre-P1-6 events lack is_clean ->
        # fall back to "non-empty validated_output == clean" (the same
        # fallback build_replay_cache uses for conforms).
        costed = [e for e in self.ledger.events()
                  if isinstance(e.get("payload", {}).get("cost_usd"),
                                (int, float))]
        cost_total = sum(e["payload"]["cost_usd"] for e in costed)
        # R9-1: cost honesty. A costed event with cost_known=False reported
        # NO cost -- its cost_usd is a 0.0 placeholder, not a measurement.
        # Expose the unknown share so 'cost $0.00' is never read as 'free'
        # when N dispatches actually reported nothing. Pre-R9-1 events lack
        # the field -> default True (they were measured under the old
        # contract where 0.0 meant free).
        unknown_cost_events = [
            e for e in costed
            if e.get("payload", {}).get("cost_known", True) is False]
        cost_known_total = cost_total  # measured share (unknown contribute 0)
        cost_unknown_dispatches = len(unknown_cost_events)
        # R9-1: token usage is the provider-agnostic spend signal (some
        # providers meter tokens while pricing cost at 0).
        tokens_total = sum(
            e["payload"].get("tokens_total", 0) for e in costed
            if isinstance(e["payload"].get("tokens_total"), (int, float)))

        def _agent_result_successful(e) -> bool:
            if e.get("kind") != "agent_result":
                return False
            p = e.get("payload", {})
            if p.get("recovered_from_prose"):
                return True
            clean = p.get("is_clean")
            if clean is None:
                # pre-P1-6 events carry no is_clean. Old semantics:
                # agent_result == successful spend (the retry path only
                # recorded it on clean success). Current code ALWAYS writes
                # is_clean explicitly, so its absence reliably means "old
                # event" -- not a denied attempt (those postdate the field).
                return True
            return bool(clean)

        successful_cost = sum(
            e["payload"]["cost_usd"] for e in costed
            if _agent_result_successful(e))
        failed_cost = sum(
            e["payload"]["cost_usd"] for e in costed
            if e.get("kind") in _FAILED_COST_KINDS
            or (e.get("kind") == "agent_result"
                and not _agent_result_successful(e)))
        successful_dispatches = sum(
            1 for e in self.ledger.events()
            if _agent_result_successful(e))
        failed_dispatches = sum(
            1 for e in self.ledger.events()
            if e.get("kind") in _FAILED_COST_KINDS)
        # dispatch_count = real worker dispatches = agent_result (clean
        # success or single-shot fail) + stagnating (same-sig failed retry) +
        # cognitive_attempt (different-sig failed retry, P1-6: cost was
        # previously lost on resume). replay_hit is a cache replay, not a
        # dispatch.
        dispatch_count = sum(
            1 for e in self.ledger.events()
            if e.get("kind") in COSTED_EVENT_KINDS
        )
        # open_questions: what the next graph MUST resolve. Sources:
        #   - CHALLENGED claims (verify left neither survived nor refuted --
        #     unverifiable, must be revisited); NOT the old buggy comprehension
        #     that falsified every evidence_ref.
        #   - replan_requested events (A3 boundary signal).
        #   - #7: required success_evidence criteria unmet (PARTIAL) -- which R
        #     failed and why (claim_PROPOSED / agent_verdict_FAILED /
        #     agent_verdict_missing / unbound), so the orchestrator triages
        #     from the checkpoint alone instead of diving the journal.
        open_q = []
        for e in self.ledger.events():
            if (e.get("kind") == "verify_verdict"
                    and e.get("spec_version_id", svid) == svid):
                if not e.get("survived") and not e.get("refuted"):
                    open_q.append({"assumption_id": "A?",
                                   "status": "unverifiable",
                                   "ref": f"verify_verdict:{e.get('claim_id')}"})
            elif (e.get("kind") == "replan_requested"
                    and e.get("spec_version_id", svid) == svid):
                open_q.append({"assumption_id": "A?",
                               "status": "replan_requested",
                               "ref": f"replan_requested:{e.get('node_id')}"})
        if verdict == "PARTIAL":
            from axiom.state import derive_unmet_requirements
            for u in derive_unmet_requirements(
                    spec, self.ledger,
                    evidence_probe=self._evidence_freshness_probe):
                open_q.append({
                    "requirement_id": u["requirement_id"],
                    "binding": u["binding"], "status": u["status"],
                    "ref": u["ref"],
                })
        # M2 (spec §13.3): task-level delivery block for delivery-bound
        # tasks. Rebuilt from the ledger + active contract; None-gated on the
        # binding so legacy tasks are untouched. Reuses the SAME projection
        # the unified verdict (derive_verdict M2 wrapper) reads, so the
        # checkpoint and the verdict cannot disagree (the R10-1 lesson:
        # verdict/checkpoint must share one freshness-gated projection).
        delivery = None
        task_id = getattr(spec, "task_id", None)
        if task_id:
            try:
                from axiom.contract import ContractStore
                from axiom.delivery import project_delivery
                contract = ContractStore(self.run_dir).active(
                    self.ledger.events())
                delivery = project_delivery(
                    self.ledger.events(), task_id, contract=contract)
                delivery["resource"] = {
                    "cost_usd_total": round(cost_total, 6),
                    "cost_complete": cost_unknown_dispatches == 0,
                    "cost_unknown_dispatches": cost_unknown_dispatches,
                    "tokens_total": tokens_total,
                    "budget_exhausted": budget_exhausted,
                }
                # AC-20 surfacing: a segment VERIFIED with an open task gap
                # must name the gap in open_questions, not silently cap.
                if (verdict == "PARTIAL"
                        and delivery["gaps"]["open_blocking_or_material"]):
                    for gid in delivery["gaps"]["open_blocking_or_material"]:
                        open_q.append({
                            "task_gap_id": gid,
                            "status": "open_blocking_or_material",
                            "ref": f"quality_gap:{gid}",
                        })
            except Exception as e:  # projection must not crash the checkpoint
                delivery = {"task_id": task_id, "error": str(e)}
        return {
            "verdict": verdict,
            # R9-3: evidence honesty. A VERIFIED carried by agent-verdict
            # nodes can rest on machine evidence (grade=machine, bound to an
            # evidence fingerprint), on NO recorded evidence (grade=none --
            # the reviewer judged without a pack), or on the classic
            # claim/verify path (grade=claim). A bare VERIFIED must never
            # silently mean 'machine-verified'.
            "evidence_grade": self._evidence_grade(spec),
            "synthesized_output": synth_out,
            "evidence_summary": evs,
            "blocked_items": blocked,
            "drift": {"contract_drift": drift},
            "open_questions": open_q,
            "cost_usd_total": round(cost_total, 6),
            "successful_cost_usd": round(successful_cost, 6),
            "failed_cost_usd": round(failed_cost, 6),
            # R9-1: cost honesty. cost_usd_total is the MEASURED lower bound;
            # cost_unknown_dispatches > 0 means that many dispatches reported
            # no cost at all, so the true spend is strictly greater and the
            # soft budget cap was enforced against an undercount.
            "cost_unknown_dispatches": cost_unknown_dispatches,
            "cost_complete": cost_unknown_dispatches == 0,
            "tokens_total": tokens_total,
            "dispatch_attempts": dispatch_count,
            "successful_dispatches": successful_dispatches,
            "failed_dispatches": failed_dispatches,
            "dispatch_count": dispatch_count,
            "budget_exhausted": budget_exhausted,
            "delivery": delivery,
        }

    # --- materialized projections (B4) ----------------------------

    def materialize(self, spec) -> dict:
        """Write the rebuildable materialized projections: verdict.json,
        checkpoints.jsonl (append-per-run), and packet.md (human-readable
        contract summary). Returns the checkpoint projection."""
        cp = self.project_checkpoint(spec)
        verdict = cp["verdict"]
        chash = contract_hash(spec)
        # verdict.json -- the derive_verdict projection, anchored to the
        # contract hash so a stale verdict vs spec is detectable.
        (self.run_dir / "verdict.json").write_text(
            json.dumps({
                "spec_version_id": spec.spec_version_id,
                "verdict": verdict, "contract_hash": chash,
                "cost_usd_total": cp["cost_usd_total"],
                "dispatch_count": cp["dispatch_count"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # checkpoints.jsonl -- append the run's checkpoint (cache, rebuildable)
        with (self.run_dir / "checkpoints.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(cp, ensure_ascii=False) + "\n")
        # packet.md -- human-readable contract summary (Axiom style)
        reqs = "\n".join(
            f"- {r.id} [{r.criticality}]: {r.text}" for r in spec.requirements
        )
        bnd = "\n".join(f"- {b}" for b in spec.boundaries) or "- (none)"
        se = "\n".join(f"- {s}" for s in spec.success_evidence) or "- (none)"
        nodes = ", ".join(spec.nodes.keys()) or "(none)"
        (self.run_dir / "packet.md").write_text(
            f"# {spec.spec_version_id}\n\n"
            f"**intent**: {spec.intent}\n\n"
            f"**contract_hash**: `{chash}`\n\n"
            f"## requirements\n{reqs}\n\n"
            f"## boundaries\n{bnd}\n\n"
            f"## success_evidence\n{se}\n\n"
            f"## nodes\n{nodes}\n\n"
            f"## verdict\n`{verdict}` — "
            f"cost ${cp['cost_usd_total']:.2f} / "
            f"{cp['dispatch_count']} dispatches\n",
            encoding="utf-8",
        )
        return cp
