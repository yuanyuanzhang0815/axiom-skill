"""Cognitive-state projections over the ledger.

These are REBUILDABLE projections, not truth. derive_claim_status and
derive_verdict project from events.jsonl. VERIFIED is only ever the result of
a verify node's survived verdict flowing through derive_claim_status — an
agent cannot self-certify a claim to VERIFIED.

Two reducers, deliberately separate:
  - derive_claim_status(): the truth of a single claim.
  - derive_verdict(): task completion (PASS/PARTIAL/BLOCKED/UNVERIFIED).
"""
from __future__ import annotations
import json
import re
from typing import Literal

ClaimStatus = Literal["PROPOSED", "SUPPORTED", "CHALLENGED", "CONFLICTED", "REFUTED", "VERIFIED"]
Verdict = Literal["VERIFIED", "PARTIAL", "BLOCKED", "UNVERIFIED"]

# P-022/P-023: canonical attempt-cost / attempt-failure event kinds, shared
# by EVERY projection (verdict, replay cache, checkpoint cost, agent-count).
# They used to be re-listed per consumer: harness.checkpoint summed one
# tuple, _agent_verdict_verified checked a DIFFERENT tuple, so the
# operational_attempt kind added for cost accounting (P-017) never
# invalidated a stale VERIFIED (review4
# test_operational_failure_invalidates_prior_verified). One definition here
# (state.py has no harness dependency; harness re-exports for back-compat).
COSTED_EVENT_KINDS = (
    "agent_result", "stagnating", "cognitive_attempt", "operational_attempt",
)
# Kinds whose very existence means "this attempt produced no usable output".
# agent_result is excluded: it carries its own is_clean / recovered flag and
# is judged per-payload, not per-kind.
ATTEMPT_FAILURE_KINDS = ("stagnating", "cognitive_attempt", "operational_attempt")


def derive_claim_status(claim_id: str, ledger, svid=None) -> ClaimStatus:
    """Project a claim's status from ledger events.

    Lifecycle: PROPOSED -> SUPPORTED -> CHALLENGED -> {REFUTED | VERIFIED};
    plus CONFLICTED (different verify nodes contradicting) which a human
    resolves to SUPPORTED via CLAIM_RESOLVED.
    - VERIFIED only via a verify_verdict event with survived=True.
    - SUPPORTED only when an agent_result records the claim with strength='supported'.
    - inferred / no strength => PROPOSED (inference is not support).

    svid scopes the scan to events of one spec_version_id. Without it, a
    spec.v2 re-run in the same run-dir inherits spec.v1's verify_verdict /
    agent_result events (stale-evidence leak -> wrong VERIFIED). Events with
    no spec_version_id field count as belonging to `svid` (test-fixture
    compatibility; real harness events always carry svid).

    Last-write-wins (Q9, spec §4): when a claim is re-verified across loop
    iterations (verify_verdict at k=1,2,3), the LATEST verify_verdict IS the
    current truth about that claim. Correct for iterative refinement (the
    primary B+C use case: defect closure -- an agent fixes a refuted claim
    in a later iteration, the latest survived=True verdict wins). v1 used to
    accumulate (refuted|survived flags OR'd across all events, refuted-checked-
    first); that was wrong for the refuted->survived direction. Now the latest
    matching verify_verdict event determines the outcome.

    CONFLICTED (absorbed from the original axiom's stricter gates): when
    DIFFERENT verify nodes (distinct node_id) contradict on a claim (one
    survived, one refuted), the claim is CONFLICTED -- the signal that
    last-write-wins used to erase. Same-node cross-iteration survived+refuted
    stays last-write-wins (iterative refinement, NOT a conflict). CONFLICTED
    projects to PARTIAL (not auto-BLOCKED; BLOCKED is gate-driven only).

    CLAIM_RESOLVED (axiom M3 grounded resolution): a human clears a CONFLICTED
    claim to SUPPORTED via a claim_resolved event (basis_ref points to an
    evidence artifact). ONLY CONFLICTED -> SUPPORTED (other statuses unchanged);
    a human cannot self-certify VERIFIED -- the claim still needs verify to
    reach a terminal. The resolution is TEMPORAL: it only clears the conflict
    if the claim_resolved event is NEWER than the latest verify_verdict -- so a
    post-resolution verify that re-contradicts re-surfaces as CONFLICTED
    (the human must re-resolve with fresh evidence). A sticky resolution that
    masks a new contradiction would be the same information-loss class
    CONFLICTED was introduced to fix.
    """
    strength = None
    latest_verify = None  # the LAST verify_verdict for this claim (svid-scoped)
    latest_verify_idx = -1  # position of latest verify in the event stream
    resolution_idx = -1  # position of latest claim_resolved (temporal guard)
    # per-node latest verdict: node_id -> "survived"|"refuted"|"challenged".
    # CONFLICTED = different verify NODES contradicting (survived AND refuted
    # across nodes); same-node cross-iteration is iterative refinement (Q9:
    # defect closure -- the latest verify_verdict wins, NOT a conflict).
    per_node: dict[str, str] = {}
    for i, e in enumerate(ledger.events()):
        if svid is not None and e.get("spec_version_id", svid) != svid:
            continue
        if e.get("kind") == "agent_result":
            for c in e.get("claims", []):
                if c.get("claim_id") == claim_id:
                    strength = c.get("strength")  # last write wins
        if e.get("kind") == "verify_verdict" and e.get("claim_id") == claim_id:
            latest_verify = e  # last write wins (append order = ledger order)
            latest_verify_idx = i
            nid = e.get("node_id")  # None for anonymous (test fixtures)
            if e.get("refuted") is True:
                per_node[nid] = "refuted"
            elif e.get("survived") is True:
                per_node[nid] = "survived"
            else:
                per_node[nid] = "challenged"
        if e.get("kind") == "claim_resolved" and e.get("claim_id") == claim_id:
            resolution_idx = i  # last write wins (explicit anchor)
    if latest_verify is not None:
        verdicts = set(per_node.values())
        # CONFLICTED: >= 2 distinct verify nodes contradict (survived AND
        # refuted across nodes) -- the signal last-write-wins used to erase.
        # Same node cross-iteration survived+refuted stays last-write-wins
        # (iterative refinement). CONFLICTED -> PARTIAL (not auto-BLOCKED;
        # BLOCKED is gate-driven only). A human resolves via CLAIM_RESOLVED.
        if "survived" in verdicts and "refuted" in verdicts and len(per_node) > 1:
            status = "CONFLICTED"
        elif latest_verify.get("refuted") is True:
            status = "REFUTED"
        elif latest_verify.get("survived") is True:
            status = "VERIFIED"
        else:
            status = "CHALLENGED"
    elif strength == "supported":
        status = "SUPPORTED"
    else:
        status = "PROPOSED"
    # CLAIM_RESOLVED (absorbed from the original axiom M3 grounded resolution):
    # ONLY CONFLICTED -> SUPPORTED, and only if the resolution is NEWER than the
    # latest verify_verdict (temporal guard). A human grounds the resolution on
    # an evidence basis_ref; the conflict is cleared to SUPPORTED (NOT VERIFIED
    # -- still needs verify to reach a terminal; a human cannot self-certify
    # VERIFIED). If a new verify re-contradicts AFTER the resolution, the claim
    # re-surfaces as CONFLICTED (the human must re-resolve) -- the resolution
    # does not stickily mask new evidence.
    if (status == "CONFLICTED" and resolution_idx >= 0
            and resolution_idx > latest_verify_idx):
        return "SUPPORTED"
    return status


def derive_dry_count(ledger, svid, loop_id, iteration) -> int:
    """Count consecutive dry rounds ending at the current iteration (spec §4).

    A round (iteration) is dry iff NO `verify_verdict` with survived=false AND
    no `stagnating` event, tagged with the given loop_id+iteration. dry_count
    is a RE-DERIVATION from events (invariant 6), never a cached state: the
    `dry_count` field recorded on `iteration_close` for auditability is NOT
    read here -- the truth is rebuilt from verify_verdict/stagnating events.

    loop_id/iteration None -> 0 (no loop context). Used by both the Task 2
    `dry_count()` predicate built-in (harness._derive_dry_count delegates here)
    and the state.py projection -- single source of truth.
    """
    if loop_id is None or iteration is None:
        return 0
    count = 0
    for k in range(1, iteration + 1):
        is_dry = True
        for e in ledger.events():
            if e.get("spec_version_id", svid) != svid:
                continue
            payload = e.get("payload", {})
            e_loop = payload.get("loop_id") or e.get("loop_id")
            e_iter = payload.get("iteration") or e.get("iteration")
            if e_loop != loop_id or e_iter != k:
                continue
            if (e.get("kind") == "verify_verdict"
                    and e.get("survived") is False):
                is_dry = False
                break
            if e.get("kind") == "stagnating":
                is_dry = False
                break
        if is_dry:
            count += 1
        else:
            count = 0  # reset the consecutive streak
    return count


def _evidence_to_claim(ev_str: str) -> str | None:
    m = re.search(r"claim:([A-Z]+\d+|dec_\w+)", ev_str)
    return m.group(1) if m else None


def _evidence_to_node(ev_str: str) -> str | None:
    """Bind a success_evidence criterion to an agent node's verdict (v1.3
    agent-verdict): 'S1:R1=node:n5' -> 'n5'. The node must have declared
    verdict_field and emitted an agent_verdict event on clean conform."""
    m = re.search(r"node:(\w+)", ev_str)
    return m.group(1) if m else None


def _agent_verdict_verified(node_id: str, ledger, svid=None,
                            spec=None, evidence_probe=None) -> bool:
    """True iff the agent node's LATEST agent_verdict event has verdict VERIFIED.
    An agent (e.g. a code-review verifier) owns a requirement's verdict the way
    a verify node's survived claim would.

    Last-write-wins (P1-2 fix): the LATEST matching agent_verdict determines
    the outcome, matching derive_claim_status's last-write-wins semantics. The
    prior form returned the FIRST matching event, so a VERIFIED->FAILED sequence
    on the same node (retry / re-dispatch) still projected VERIFIED -- a stale
    verdict masking a later FAILED. Now a later FAILED wins, projecting PARTIAL.

    svid scopes the scan to the current spec version. The parent spec chain
    never participates in the verdict -- it only serves resume's dispatch cache
    (savings); verdict evidence is always the current svid's own.
    """
    latest = None
    for e in ledger.events():
        if svid is not None and e.get("spec_version_id", svid) != svid:
            continue
        if e.get("kind") == "agent_verdict" and e.get("node_id") == node_id:
            latest = e  # last write wins (append order = ledger order)
    if latest is None:
        return False
    # P1-7: a VERIFIED verdict is stale if a LATER attempt for this node
    # failed (any ATTEMPT_FAILURE_KINDS event, or agent_result with
    # is_clean=false). Last-write-wins on agent_verdict alone missed the case
    # where attempt 1 returned VERIFIED, attempt 2 failed and degraded
    # WITHOUT emitting a new agent_verdict -> latest agent_verdict stayed
    # VERIFIED. Now any later failure-indicator event invalidates the prior
    # verdict (R unmet -> PARTIAL), so a failed re-dispatch cannot inherit an
    # earlier VERIFIED. P-023: the kinds come from the shared canonical
    # ATTEMPT_FAILURE_KINDS (operational_attempt added in P-017 was missing
    # here -> a later operational failure left the old VERIFIED standing,
    # review4).
    latest_seq = latest.get("seq", 0)
    for e in ledger.events():
        if svid is not None and e.get("spec_version_id", svid) != svid:
            continue
        if e.get("node_id") != node_id:
            continue
        if e.get("seq", 0) <= latest_seq:
            continue
        kind = e.get("kind")
        if kind in ATTEMPT_FAILURE_KINDS:
            return False  # a later attempt failed -> prior VERIFIED is stale
        if kind == "agent_result":
            if e.get("payload", {}).get("is_clean") is False:
                return False  # a later attempt was not clean -> stale
    # R10-2: evidence declaration is a GATE, not a label. A verdict whose
    # payload declares evidence_from but carries NO bound pack
    # (evidence_id=None, evidence_grade=none -- the source node never ran in
    # this ledger) must NOT project VERIFIED: the reviewer declared machine
    # evidence as its basis and judged without it. Previously the freshness
    # block below required a bound_id, so the no-pack case fell straight
    # through to the verdict value -- 'evidence_grade=none' was honest
    # labeling but not a gate (review10: VERIFIED + grade none + exit 0).
    p = latest.get("payload", {})
    ev_from = p.get("evidence_from")
    bound_id = p.get("evidence_id")
    if ev_from and not bound_id:
        return False  # declared machine evidence, none recorded -> unmet
    # R9-3: evidence freshness. A verdict bound to machine evidence
    # (payload.evidence_from + evidence_id) is only as fresh as the code it
    # reviewed. The probe recomputes the source's CURRENT fingerprint from
    # the spec's evidence_scope; a mismatch means the evidence is stale and
    # this verdict must NOT project VERIFIED -- the reviewer re-examines.
    if spec is not None and evidence_probe is not None:
        if ev_from and bound_id:
            current_id = evidence_probe(spec, ev_from)
            if current_id is not None and current_id != bound_id:
                return False  # code changed after the verdict -> stale
    return latest.get("verdict") == "VERIFIED"


def derive_verdict(spec, ledger, evidence_probe=None) -> Verdict:
    """Project task completion from spec requirements + ledger evidence.

    - BLOCKED: any gate_open unresolved.
    - VERIFIED: every required success_evidence criterion bound to a VERIFIED claim.
    - PARTIAL: some required criterion unmet (or not VERIFIED).
    - UNVERIFIED: no required criteria bound at all.
    Optional/advisory unmet does NOT cap the verdict.

    All evidence is scoped to spec.spec_version_id: a re-run in the same
    run-dir never inherits a prior spec version's verdict (stale-evidence leak
    fix). The parent spec chain serves only resume's dispatch cache (savings),
    never the verdict.

    M2 (spec §7.4, single ruling entry): when the spec carries a
    delivery-contract binding (task_id set), the workflow verdict above is
    only the SEGMENT result. The task-level delivery projection (open
    blocking/material gaps, unmet quality-sensitive comparisons) can lower
    the final verdict, never raise it -- a segment VERIFIED cannot mask an
    open task gap (AC-20). Legacy specs (task_id None) are untouched.
    """
    workflow = _derive_workflow_verdict(spec, ledger,
                                        evidence_probe=evidence_probe)
    task_id = getattr(spec, "task_id", None)
    if not task_id:
        return workflow
    try:
        from axiom.delivery import derive_delivery_verdict, project_delivery
        projection = project_delivery(ledger.events(), task_id, contract=None)
        return derive_delivery_verdict(workflow, projection)
    except Exception:
        # A corrupt/under-specified task projection must not silently
        # upgrade to a clean verdict -- but it also must not crash the
        # ruling entry. Fall back to the workflow verdict; the delivery
        # block in the checkpoint surfaces the projection state honestly.
        return workflow


def _derive_workflow_verdict(spec, ledger, evidence_probe=None) -> Verdict:
    """The segment (workflow) verdict -- the pre-M2 derive_verdict logic."""
    svid = spec.spec_version_id
    gates_open = [e for e in ledger.events()
                  if e.get("kind") == "gate_open"
                  and e.get("spec_version_id", svid) == svid]
    gates_resolved = {e["gate_id"] for e in ledger.events()
                      if e.get("kind") == "gate_resolve"
                      and e.get("spec_version_id", svid) == svid}
    if any(g["gate_id"] not in gates_resolved for g in gates_open):
        return "BLOCKED"
    crit = {r.id: r.criticality for r in spec.requirements}
    required_unmet = False
    # P1-1 fix: a required R with NO success_evidence binding was silently
    # skipped (unverified) -> false VERIFIED (derive_verdict only walked success_evidence, so an
    # unbound required R never tripped required_unmet when a sibling R was bound
    # and VERIFIED). Now: if at least one required R IS bound, an unbound
    # required sibling is unmet (PARTIAL, not VERIFIED). If NO required R is
    # bound at all, the original UNVERIFIED semantics are preserved (a spec
    # that binds nothing is unverifiable, not partially-done).
    bound_rids = set()
    for se in spec.success_evidence:
        m = re.search(r":(R\d+)", se)
        if m:
            bound_rids.add(m.group(1))
    required_ids = {r.id for r in spec.requirements
                    if crit.get(r.id) == "required"}
    has_bound_required = bool(required_ids & bound_rids)
    if has_bound_required:
        for r in spec.requirements:
            if (crit.get(r.id) == "required" and r.id not in bound_rids):
                required_unmet = True  # a bound sibling exists -> this gap caps
    for se in spec.success_evidence:
        rid_match = re.search(r":(R\d+)", se)
        if not rid_match:
            continue
        rid = rid_match.group(1)
        if crit.get(rid) != "required":
            continue
        any_required = True
        claim = _evidence_to_claim(se)
        node = _evidence_to_node(se)
        if claim is not None:
            if derive_claim_status(claim, ledger, svid) != "VERIFIED":
                required_unmet = True
        elif node is not None:
            # v1.3 agent-verdict: an agent node (verdict_field) emitted an
            # agent_verdict event; R is satisfied iff that verdict is VERIFIED.
            if not _agent_verdict_verified(node, ledger, svid,
                                           spec=spec,
                                           evidence_probe=evidence_probe):
                required_unmet = True
        else:
            required_unmet = True
    if not required_ids or not has_bound_required:
        return "UNVERIFIED"
    return "VERIFIED" if not required_unmet else "PARTIAL"


def derive_unmet_requirements(spec, ledger, evidence_probe=None) -> list[dict]:
    """List required success_evidence criteria NOT VERIFIED, with WHY -- so a
    PARTIAL checkpoint tells the orchestrator which R failed and how, instead
    of an empty open_questions that forces a journal dive. Scopes by svid (a v2
    re-run does not inherit v1's unmet criteria). Mirrors derive_verdict's unmet
    logic so the two stay consistent; derive_verdict stays the single source
    for the verdict, this is the single source for the diagnostic."""
    svid = spec.spec_version_id
    crit = {r.id: r.criticality for r in spec.requirements}
    unmet: list[dict] = []
    # P1-1 fix (mirror derive_verdict): if at least one required R is bound, an
    # unbound required sibling is surfaced as unmet so a PARTIAL checkpoint
    # names the skipped (unverified) R. When NO required R is bound, leave unmet empty (verdict
    # is UNVERIFIED -- nothing to partially-complete).
    bound_rids = set()
    for se in spec.success_evidence:
        m = re.search(r":(R\d+)", se)
        if m:
            bound_rids.add(m.group(1))
    required_ids = {r.id for r in spec.requirements
                    if crit.get(r.id) == "required"}
    if bool(required_ids & bound_rids):
        for r in spec.requirements:
            if crit.get(r.id) != "required":
                continue
            if r.id not in bound_rids:
                unmet.append({
                    "requirement_id": r.id, "binding": "(none)",
                    "status": "unbound_required", "ref": f"requirement:{r.id}",
                })
    for se in spec.success_evidence:
        m = re.search(r":(R\d+)", se)
        if not m:
            continue
        rid = m.group(1)
        if crit.get(rid) != "required":
            continue
        claim = _evidence_to_claim(se)
        node = _evidence_to_node(se)
        if claim is not None:
            status = derive_claim_status(claim, ledger, svid)
            if status != "VERIFIED":
                unmet.append({
                    "requirement_id": rid, "binding": f"claim:{claim}",
                    "status": f"claim_{status.lower()}", "ref": f"claim:{claim}",
                })
        elif node is not None:
            if not _agent_verdict_verified(node, ledger, svid,
                                           spec=spec,
                                           evidence_probe=evidence_probe):
                # R10-2: name WHY the verdict did not project. LATEST
                # agent_verdict wins (last-write-wins, mirroring
                # _agent_verdict_verified) -- the old next() picked the
                # FIRST event and could mislabel a re-dispatch.
                evt = None
                for _e in ledger.events():
                    if (_e.get("kind") == "agent_verdict"
                            and _e.get("node_id") == node
                            and _e.get("spec_version_id", svid) == svid):
                        evt = _e
                if evt is None:
                    status = "agent_verdict_missing"
                elif evt.get("verdict") == "FAILED":
                    status = "agent_verdict_FAILED"
                else:
                    _p = evt.get("payload", {})
                    if _p.get("evidence_from") and not _p.get("evidence_id"):
                        status = "agent_verdict_evidence_missing"
                    elif _p.get("evidence_from"):
                        status = "agent_verdict_evidence_stale"
                    else:
                        status = "agent_verdict_not_current"
                unmet.append({
                    "requirement_id": rid, "binding": f"node:{node}",
                    "status": status, "ref": f"node:{node}",
                })
        else:
            unmet.append({
                "requirement_id": rid, "binding": se,
                "status": "unbound_criterion", "ref": se,
            })
    return unmet


def _project_loops(ledger, svid) -> list[dict]:
    """Project loop lifecycle from loop_open/loop_close events (spec §4).

    Each loop_open (svid-scoped) yields a loop entry:
      - state="active", exit_reason=None  -- no matching loop_close yet.
      - state="closed", exit_reason=<reason> -- loop_close present.
    If the same loop_id opens/closes multiple times (re-entry), the LATEST
    loop_open determines state (last-write-wins over the lifecycle; matches
    derive_claim_status's last-write-wins semantics). svid-scoped: a v2
    re-run's loop events do not leak into v1's projection (v1 #1 guard).
    """
    # track, per loop_id, the latest loop_open and its matching loop_close
    opens: dict[str, dict] = {}   # loop_id -> latest loop_open event
    closes: dict[str, dict] = {}  # loop_id -> latest loop_close event
    for e in ledger.events():
        if e.get("spec_version_id", svid) != svid:
            continue
        if e.get("kind") == "loop_open":
            payload = e.get("payload", {})
            lid = payload.get("loop_id") or e.get("loop_id")
            if lid is not None:
                opens[lid] = e
        elif e.get("kind") == "loop_close":
            payload = e.get("payload", {})
            lid = payload.get("loop_id") or e.get("loop_id")
            if lid is not None:
                closes[lid] = e
    loops: list[dict] = []
    for lid, op in opens.items():
        cl = closes.get(lid)
        if cl is not None:
            loops.append({
                "loop_id": lid,
                "state": "closed",
                "exit_reason": cl.get("payload", {}).get("exit_reason"),
                "node_id": op.get("node_id"),
            })
        else:
            loops.append({
                "loop_id": lid,
                "state": "active",
                "exit_reason": None,
                "node_id": op.get("node_id"),
            })
    return loops


def project_cognitive_state(spec, ledger, evidence_probe=None) -> dict:
    """Project the cognitive state (rebuildable; not truth).

    v2 (spec §4): loop-aware. An active loop (loop_open without loop_close,
    svid-scoped) contributes PROGRESSING -- no new cognitive state between
    iterations. The `loops` field exposes each loop's lifecycle (active/
    closed + exit_reason) so the orchestrator can see open loops without
    diving the journal. Exhaustion routing is read off the events _exhaust
    emits: block -> gate_open (unresolved) -> BLOCKED; degrade -> soft None
    -> verdict machine (PARTIAL); replan -> replan_requested ->
    checkpoint.open_questions (handled in Harness.project_checkpoint).
    """
    svid = spec.spec_version_id
    progress = "PROGRESSING"
    if any(e.get("kind") == "stagnating"
           and e.get("spec_version_id", svid) == svid
           for e in ledger.events()):
        progress = "STAGNATING"
    gates_open = [e for e in ledger.events()
                  if e.get("kind") == "gate_open"
                  and e.get("spec_version_id", svid) == svid]
    gates_resolved = {e["gate_id"] for e in ledger.events()
                      if e.get("kind") == "gate_resolve"
                      and e.get("spec_version_id", svid) == svid}
    if any(g["gate_id"] not in gates_resolved for g in gates_open):
        progress = "BLOCKED"
    completion = derive_verdict(spec, ledger, evidence_probe=evidence_probe)
    loops = _project_loops(ledger, svid)
    return {
        "goal": "LOCKED",
        "claims": "INFERRED",
        "progress": progress,
        "completion": completion,
        "loops": loops,
    }


# --- debug-gate projection (comet debug-gate concept ported into axiom) ---
# These signatures are the SAME algorithm the harness uses for its stagnation
# guard, lifted here as the single source of truth so the debug projection and
# the runtime stagnation judgment stay byte-consistent. harness._cognitive_sig /
# _schema_fail_keys delegate to these.

def derive_schema_fail_keys(value, schema) -> "frozenset":
    """Field-level failure keys for a non-conforming value, so the stagnation
    signature distinguishes 'missing claim_id' from 'missing module' -- two
    DIFFERENT schema failures are a cognitive delta, not no-delta stagnation.
    Stable frozenset; {'schema'} fallback when no field can be named (non-dict
    value / non-object schema)."""
    if not isinstance(schema, dict) or not isinstance(value, dict):
        return frozenset({"schema"})
    bad = set()
    for k in schema.get("required", []):
        if k not in value:
            bad.add(f"missing:{k}")
    for k, sub in schema.get("properties", {}).items():
        if k in value and not _validate_node_shape(value[k], sub):
            bad.add(f"bad:{k}")
    return frozenset(bad) or frozenset({"schema"})


def _validate_node_shape(value, schema) -> bool:
    """Minimal JSON-Schema-subset conformance for schema_fail_keys. Mirrors
    Harness._validate_node's type/required/properties/items checks (the subset
    that determines a field-level failure key) without pulling the harness
    dependency into state.py."""
    if not isinstance(schema, dict):
        return True
    t = schema.get("type")
    if t == "object" and not isinstance(value, dict):
        return False
    if t == "array" and not isinstance(value, list):
        return False
    if t == "string" and not isinstance(value, str):
        return False
    if t == "integer":
        if isinstance(value, bool):
            return False
        if not (isinstance(value, int) or
                (isinstance(value, float) and value.is_integer())):
            return False
    if t == "number" and (isinstance(value, bool) or
                          not isinstance(value, (int, float))):
        return False
    if t == "boolean" and not isinstance(value, bool):
        return False
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                return False
        for k, sub in schema.get("properties", {}).items():
            if k in value and not _validate_node_shape(value[k], sub):
                return False
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for v in value:
                if not _validate_node_shape(v, items):
                    return False
    return True


def derive_cognitive_signature(denials, conforms,
                                schema_fail_detail=None) -> "tuple | None":
    """Signature of a cognitive failure for the stagnation (no-delta) guard.

    Two consecutive failures with the SAME signature = no cognitive delta =
    STAGNATING. Both the denial SET and the schema-FAILURE-KEY-SET factor in:
    a repeated denial set OR a repeated schema-failure-key-set both count as
    'no progress'; a changed denial set OR a changed failing field is a
    cognitive delta. Returns None for a clean (non-failing) outcome."""
    if denials:
        # denials are normally list[str], but defend in depth: non-str denials
        # (dicts) are normalized to a stable json key so repeated identical
        # denials still trip stagnation, and the same dict -> same sig.
        norm = frozenset(
            d if isinstance(d, str) else json.dumps(d, sort_keys=True,
                                                       ensure_ascii=False)
            for d in denials
        )
        return ("denials", norm)
    if not conforms:
        return ("schema", schema_fail_detail or frozenset({"schema"}))
    return None


# recovery_action is a DETERMINISTIC fact derived from which _exhaust path the
# ledger records -- not an LLM suggestion. It maps the failure class to the
# orchestrator's next move (comet debug-gate: root cause first, then a named
# recovery, not a prose recommendation).
_RECOVERY_ACTION = {
    "gate_open": "gate-resolve",
    "replan_requested": "replan",
    "stagnating": "re-dispatch",
    "retries_exhausted": "re-dispatch",
    "budget_exhausted": "raise-cap-or-replan",
    "agent_cap_exhausted": "raise-cap-or-replan",
    "unretriable": "gate-resolve",
    "agent_verdict_missing": "re-dispatch",
    "agent_verdict_FAILED": "re-dispatch",
}


def _node_schema(spec, node_id):
    """The output_schema a node declared, or None. Used to reconstruct a
    schema-failure signature from a stagnating event's conforms=False."""
    if spec is None:
        return None
    n = spec.nodes.get(node_id) if hasattr(spec, "nodes") else None
    if isinstance(n, dict):
        return n.get("output_schema")
    return None


def derive_spec_shape(spec) -> str:
    """Compact one-line fingerprint of a spec's STRUCTURE (not its contract).

    Used by the wiki layer (wiki.py) so `plan --wiki-suggest` can show "this
    is the shape that worked / failed for a similar intent" without dumping
    the whole spec. Shape = node-type counts + control_flow type + budget +
    max_stagnation -- the levers an author twiddles, NOT the contract-four
    (intent/requirements/boundaries/success_evidence) which live in the spec's
    own hash. Pure Spec extraction; no ledger, no LLM.
    """
    counts: dict[str, int] = {}
    for n in (spec.nodes.values() if hasattr(spec, "nodes") else []):
        if isinstance(n, dict):
            t = n.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
    parts = [f"{t}x{c}" for t, c in sorted(counts.items())]
    cf = spec.control_flow.get("type", "?") if hasattr(spec, "control_flow") else "?"
    budget = spec.budget_usd if hasattr(spec, "budget_usd") else "?"
    if budget is None:
        budget = "none-set"  # new-protocol null: no amount limit (spec §11.1)
    max_stag = spec.max_stagnation if hasattr(spec, "max_stagnation") else "?"
    return f"{'|'.join(parts) or 'empty'}|{cf}|budget={budget}|max_stag={max_stag}"


def derive_debug_envelope(spec, ledger, evidence_probe=None) -> dict:
    """Project a failure-forensic envelope from a run's ledger (comet
    debug-gate, ported into axiom's projection layer).

    Pure read-only: never appends to the ledger, never calls an LLM. For each
    node that the ledger records a failure for (stagnation / gate / budget /
    replan / cap), reconstruct WHY: the cognitive signature, the last output
    preview, the unmet requirements bound to it, and the deterministic
    recovery_action the orchestrator should take. A VERIFIED run with no
    failure events yields no_failures=True.

    Mirrors derive_verdict's svid-scoping: a v2 re-run's failures do not leak
    from v1's events.
    """
    svid = spec.spec_version_id if spec is not None else None
    events = [e for e in ledger.events()
              if svid is None or e.get("spec_version_id", svid) == svid]

    gates_resolved = {e["gate_id"] for e in events
                      if e.get("kind") == "gate_resolve"}

    # per-node aggregation: each node_id -> the failure signals it produced.
    by_node: dict[str, dict] = {}
    top_level_failures: list[dict] = []  # budget/cap have no node_id

    def _slot(nid: str):
        return by_node.setdefault(nid, {
            "node_id": nid, "failure_class": None,
            "cognitive_signature": None, "last_preview": "",
            "loop_id": None, "iteration": None,
        })

    for e in events:
        kind = e.get("kind")
        nid = e.get("node_id")
        p = e.get("payload", {}) if isinstance(e.get("payload"), dict) else {}
        if kind == "stagnating":
            s = _slot(nid)
            s["failure_class"] = "stagnating"
            denials = p.get("denials") or []
            conforms = p.get("conforms")
            # schema_fail_detail is not recorded on the event; a schema failure
            # (conforms=False, no denials) collapses to {'schema'} (coarse but
            # honest -- the fine-grained failing-field keys are not on the event
            # because the harness tripped on them before recording).
            fail_detail = frozenset({"schema"}) if (
                not conforms and not denials) else None
            s["_sig"] = derive_cognitive_signature(
                denials, conforms, fail_detail)
            preview = p.get("result_text_preview")
            if preview:
                s["last_preview"] = preview
            if p.get("loop_id") is not None:
                s["loop_id"] = p.get("loop_id")
                s["iteration"] = p.get("iteration")
        elif kind == "gate_open" and e.get("gate_id") not in gates_resolved:
            nid2 = nid or e.get("gate_id")
            s = _slot(nid2)
            s["failure_class"] = "gate_open"
            s["gate_id"] = e.get("gate_id")
            s["gate_reason"] = e.get("reason")
        elif kind == "replan_requested":
            s = _slot(nid)
            s["failure_class"] = "replan_requested"
            s["replan_reason"] = p.get("reason")
        elif kind == "budget_exhausted":
            top_level_failures.append({
                "node_id": None, "failure_class": "budget_exhausted",
                "recovery_action": "raise-cap-or-replan",
                "cost_total": p.get("cost_total"),
            })
        elif kind == "agent_cap_exhausted":
            top_level_failures.append({
                "node_id": None, "failure_class": "agent_cap_exhausted",
                "recovery_action": "raise-cap-or-replan",
                "agent_count": p.get("agent_count"),
            })
        elif kind == "agent_result":
            # keep the latest result_text_preview per node so a non-stagnating
            # exhaustion still surfaces the last worker output.
            if nid is not None:
                s = _slot(nid)
                preview = p.get("result_text_preview")
                if preview and not s.get("last_preview"):
                    s["last_preview"] = preview
                # stash the validated_output so a later agent_verdict
                # FAILED/missing can derive a cognitive signature (which
                # declared fields came back empty + the agent's notes).
                vo = p.get("validated_output")
                if isinstance(vo, dict) and "_validated_output" not in s:
                    s["_validated_output"] = vo

    # attach recovery_action + bind node-level unmet requirements
    unmet = (derive_unmet_requirements(spec, ledger,
                                       evidence_probe=evidence_probe)
             if spec is not None else [])
    # derive_unmet_requirements already classifies agent-verdict failures
    # (agent_verdict_FAILED / _missing / _evidence_*). The loop above fills
    # failure_class only for PROCESS-failure signals (stagnating/gate/replan/
    # budget/cap); an agent that simply judged FAILED emits agent_result +
    # agent_verdict -- neither branch set failure_class, so the node never
    # reached `failures` and the envelope reported "0 failure nodes" for a
    # plainly PARTIAL run. Bind the unmet classification back onto the node
    # (mirror of derive_unmet_requirements, not a new mechanism) so its
    # recovery_action + cognitive signature surface.
    _AGENT_VERDICT_FAILURE = {
        "agent_verdict_FAILED", "agent_verdict_missing",
        "agent_verdict_evidence_missing", "agent_verdict_evidence_stale",
        "agent_verdict_not_current",
    }
    for u in unmet:
        status = u.get("status")
        if status not in _AGENT_VERDICT_FAILURE:
            continue
        ref = u.get("ref", "")
        if not ref.startswith("node:"):
            continue
        nid = ref[len("node:"):]
        s = _slot(nid)
        if s.get("failure_class") is not None:
            continue  # process-failure signal already classified it
        s["failure_class"] = status
        vo = s.pop("_validated_output", {}) or {}
        sig_parts = []
        for k, v in vo.items():
            if k in ("notes", "cost_usd", "num_turns",
                     "input_hash", "result_text_preview"):
                continue
            if v in (None, "", False, 0) or v == "FAILED":
                sig_parts.append(f"{k}={v!r}")
        notes = vo.get("notes")
        if notes:
            sig_parts.append(f"notes: {str(notes)[:160]}")
        if sig_parts:
            s["_sig"] = ("agent_verdict_failed", frozenset(sig_parts))
    for nid, s in by_node.items():
        s["recovery_action"] = _RECOVERY_ACTION.get(
            s["failure_class"], "re-dispatch")
        # bind unmet requirements whose node binding matches this node
        node_unmet = [u for u in unmet
                      if u.get("ref", "").startswith(f"node:{nid}")]
        s["unmet_requirements"] = node_unmet
        # normalize cognitive_signature to a stable, JSON-serializable repr
        sig = s.pop("_sig", None)
        if sig is not None:
            kind_, norm = sig
            s["cognitive_signature"] = {
                "kind": kind_,
                "value": sorted(norm),
            }
        else:
            s["cognitive_signature"] = None

    failures = [s for s in by_node.values()
                if s.get("failure_class") is not None] + top_level_failures
    verdict = (derive_verdict(spec, ledger, evidence_probe=evidence_probe)
               if spec is not None else "UNVERIFIED")
    no_failures = not failures and verdict == "VERIFIED"
    return {
        "spec_version_id": svid,
        "verdict": verdict,
        "no_failures": no_failures,
        "failures": failures,
        "unmet_requirements": unmet,
    }
