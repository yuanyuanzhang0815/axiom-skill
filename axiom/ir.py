"""Workflow IR: spec dataclasses, contract hash, validation, ownership, revisions.

This module is the design-truth surface (one of the two truth sources). The
contract hash covers only the contract-four (intent+requirements+boundaries+
success_evidence); plan/nodes/decision_trace changes are non-contract
revisions, not contract drift.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
import copy
import hashlib
import json
import fnmatch
import re
from pathlib import Path


@dataclass
class Requirement:
    id: str
    text: str
    criticality: Literal["required", "optional", "advisory"] = "required"


@dataclass
class Spec:
    spec_version_id: str
    parent_spec_id: str | None
    revision: int
    intent: str
    requirements: list[Requirement]
    boundaries: list[str]
    success_evidence: list[str]
    nodes: dict[str, dict[str, Any]]
    control_flow: dict[str, Any]
    decision_trace: list[dict[str, Any]]
    # budget_usd is nullable on the NEW delivery-contract path only (spec
    # §11.1: null = no amount limit set, still recorded; NOT zero/free).
    # Legacy specs keep a finite float. Harness comparisons go through
    # budget_limit() -- a raw None must never reach a `<` comparison.
    budget_usd: float | None
    max_concurrent: int
    max_agents: int
    max_stagnation: int
    contract_drift: bool = False
    contract_hash_value: str | None = None
    # co-evolution loop (P-014): entry_ids of the wiki experience/format_contract
    # entries this spec was derived from (the host's adoption judgment -- axiom
    # records it, the host makes it). Durable + versioned on the spec itself so
    # a resume re-walk attributes outcome without re-passing a CLI flag. The
    # `--adopted-from` CLI flag merges into this list at run time.
    adopted_from: list[str] = field(default_factory=list)
    # assurance default-on (change-assurance invariant #9): a runtime/code-editing
    # spec must machine-gate its completion verdict via assurance_hook. Setting a
    # non-empty reason here is the ONLY way to opt out -- bare LLM self-report
    # VERIFIED on a code change is not a valid completion signal.
    assurance_opt_out: str | None = None
    # --- delivery-contract binding (axiom.delivery-contract.v1, spec §13.1) ---
    # All three fields are None together (legacy path) or all set together
    # (new protocol). A partial binding is INVALID and must be rejected, never
    # silently downgraded to legacy. delivery_contract_ref points at the
    # contract JSON (path relative to the spec file, or absolute).
    task_id: str | None = None
    delivery_contract_ref: str | None = None
    delivery_contract_digest: str | None = None


# --- node types ---------------------------------------------------------------


@dataclass
class AgentNode:
    id: str
    prompt: str
    dispatch: Literal["host", "native"] = "host"
    model: str | None = None
    write_areas: list[str] = field(default_factory=list)
    read_areas: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] = field(default_factory=dict)
    acceptance: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    risk: Literal["low", "high"] = "low"
    verification_policy: Literal["self_declared", "independent", "human"] = "self_declared"
    failure_policy: dict[str, Any] = field(
        default_factory=lambda: {
            "max_retries": 2,
            "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        }
    )
    # v2 Task 12 (spec §5): worktree isolation. NOT a new node type -- a field
    # on AgentNode. When isolation=="worktree", dispatch_agent intercepts:
    # create a fresh git worktree (only tracked files), symlink runtime_assets
    # globs (data/.env/namespace pkgs -- the runtime-asset symlink fix) from the main
    # checkout into the worktree, override cwd+snapshot glob root to the
    # worktree, pin output content-addressed to run_dir/artifacts, cleanup in
    # a finally. empty runtime_assets auto-detects data/, .env, and untracked
    # importable dirs. No auto-merge (Q3): artifact_refs are the merge unit.
    isolation: Literal["none", "worktree"] = "none"
    runtime_assets: list[str] = field(default_factory=list)


@dataclass
class ScriptNode:
    """Deterministic subprocess node -- runs a script directly, no LLM dispatch.

    The evidence-producer counterpart to VerifyNode's adjudicator: a script
    node produces structured facts (tsc output, grep hits, schema diff) that a
    downstream verify node adjudicates. It MUST NOT declare verdict_field or
    bind success_evidence (it produces evidence, not verdicts). This fixes the
    change-assurance fact-layer vs adjudicator-layer separation into the node
    type system, so deterministic work never needlessly pays for an LLM round.
    """
    id: str
    script_path: str          # repo-relative or absolute; resolved at validate
    args: list[str] = field(default_factory=list)   # {{upstream.out.field}} templates
    output_schema: dict[str, Any] = field(default_factory=dict)  # required non-empty
    failure_policy: dict[str, Any] = field(
        default_factory=lambda: {
            "max_retries": 2,
            "retry_guard": "requires_new_evidence",
            "on_exhausted": "block",
        }
    )
    isolation: str | None = None   # "worktree" opt-in; default None (read-mostly)


@dataclass
class ParallelNode:
    id: str
    over: str
    body: dict[str, Any]
    concurrency: int = 16
    barrier: bool = True


@dataclass
class PipelineNode:
    id: str
    items: str
    stages: list[dict[str, Any]]


@dataclass
class VerifyNode:
    id: str
    target: str
    skeptic_count: int = 3
    skeptic_prompt: str = ""
    survival_rule: str = "majority_unrefuted"
    independent_session: bool = True
    output_schema: dict[str, Any] = field(default_factory=dict)
    verification_policy: str = "independent"


@dataclass
class SynthesizeNode:
    id: str
    inputs: list[str]
    output_schema: dict[str, Any] = field(default_factory=dict)
    acceptance: list[str] = field(default_factory=list)


@dataclass
class WorkflowNode:
    id: str
    spec: str
    args: dict[str, Any] = field(default_factory=dict)


# --- v2 control-flow node kinds (opt-in; spec §1.1–§1.4) ---------------------
# as_node_dict auto-derives `type` via __class__.__name__.replace("Node","").lower()
# so these become "condition"/"repeat"/"until"/"gate" with no special-casing.


@dataclass
class ConditionNode:
    id: str
    predicate: str                 # deterministic expression, §2 DSL
    then_branch: list               # step refs (same shape as control_flow steps)
    else_branch: list = field(default_factory=list)
    gate: bool = False              # if True, route through gate_open/gate_resolve
    output_schema: dict = field(default_factory=dict)


@dataclass
class RepeatNode:
    id: str
    body: list                      # loop body, step refs
    max_iterations: int             # hard bound, REQUIRED (no default — forces bounding)
    until: str = "dry<2"            # convergence predicate, built-in dry metric
    failure_policy: dict = field(
        default_factory=lambda: {"on_exhausted": "block"})


@dataclass
class UntilNode:
    id: str
    body: list
    cond: str                       # arbitrary deterministic predicate, §2 DSL
    max_iterations: int             # hard bound, REQUIRED
    budget_aware: bool = True       # exit on budget_usd exhaustion even if cond not met
    failure_policy: dict = field(
        default_factory=lambda: {"on_exhausted": "degrade"})


@dataclass
class GateNode:
    id: str
    trigger: str                    # escalation predicate, §2 DSL
    escalation_tiers: list          # [{model, allowed_tools, skeptic_count, ...}]
    body: list                      # subtree to execute after escalation decision
    on_trigger: str = "pause"       # "pause"|"auto"|"replan"
    output_schema: dict = field(default_factory=dict)


# --- contract hash -----------------------------------------------------------


def _contract_payload(spec: Spec) -> bytes:
    """Canonical bytes over the contract-four only."""
    return json.dumps(
        {
            "intent": spec.intent,
            "requirements": [(r.id, r.text, r.criticality) for r in spec.requirements],
            "boundaries": spec.boundaries,
            "success_evidence": spec.success_evidence,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def contract_hash(spec: Spec) -> str:
    """sha256 over intent+requirements+boundaries+success_evidence only.

    Two specs with the same contract-four but different nodes/plan/decision_trace
    produce the same hash (no contract drift). Any change to the contract-four
    changes the hash (contract drift).
    """
    return "sha256:" + hashlib.sha256(_contract_payload(spec)).hexdigest()


# --- serialization ------------------------------------------------------------


def as_node_dict(n) -> dict:
    """Serialize a node dataclass to a plain dict with a `type` discriminator."""
    d = asdict(n)
    d["type"] = n.__class__.__name__.replace("Node", "").lower()
    return d


def _node_to_dict(v) -> dict:
    return as_node_dict(v) if hasattr(v, "__dataclass_fields__") else v


def spec_to_json(spec: Spec) -> str:
    d = asdict(spec)
    d["requirements"] = [asdict(r) for r in spec.requirements]
    d["nodes"] = {k: _node_to_dict(v) for k, v in spec.nodes.items()}
    return json.dumps(d, ensure_ascii=False, indent=2, sort_keys=False)


def spec_from_json(s: str) -> Spec:
    d = json.loads(s)
    d["requirements"] = [Requirement(**r) for r in d.get("requirements", [])]
    d.pop("contract_hash_value", None)
    return Spec(**d)


def budget_limit(spec: Spec) -> float:
    """Effective dispatch budget: None (new-protocol 'no amount limit') maps
    to +inf for comparisons; the raw None is never compared directly."""
    b = getattr(spec, "budget_usd", None)
    return float("inf") if b is None else b


# --- ownership + validation --------------------------------------------------


def _glob_overlap(a: str, b: str) -> bool:
    """Conservative overlap test for two write_area globs.

    v1 rule: shared literal base directory, or one path is a prefix of the
    other, or fnmatch says either glob matches the other's literal form.
    """
    pa = a.rstrip("/").replace("/**", "")
    pb = b.rstrip("/").replace("/**", "")
    return (
        pa == pb
        or pa.startswith(pb + "/")
        or pb.startswith(pa + "/")
        or a == b
        or fnmatch.fnmatch(b, a)
        or fnmatch.fnmatch(a, b)
    )


def check_ownership(spec: Spec) -> list[str]:
    """Reject overlapping write_areas among agents that run in the same
    parallel step (spec §5 Q-walk; v1 spec line 551 isolated-merge exception).

    v2 RECURSIVE walk: descends into condition.then_branch/else_branch,
    repeat.body, until.body, gate.body, parallel.body, and pipeline.stages
    -- not just the top-level control_flow.steps parallel fan-out. A nested
    `["parallel", a1, a2"]` step inside a condition.then_branch forms a
    parallel group just like a top-level one; v1's flat walk missed it.

    Cross-subtree merge (Fix 1): when a fan-out step references a control-flow
    node (condition/repeat/until/gate) as a sibling, that node's body agents
    are MERGED into the fan-out group -- not collected as an independent
    nested group -- so an agent inside a condition.then_branch is
    overlap-checked against the parallel siblings. Branch bodies run
    concurrently with parallel siblings; the previous separate-descend missed
    cross-subtree conflicts (invariant 4 violation).

    Cycle defense (Fix 2): a visited set of node ids prevents infinite
    recursion on self-referential / cyclic bodies (e.g.
    cond1.then_branch referencing cond1). Task 3's validate_spec only checks
    ids EXIST, not acyclicity, so a cyclic body passes validate and reaches
    here -- without the visited set, check_ownership would hang.

    All-worktree exception (v1 line 551): overlap is ALLOWED when every node
    in the overlapping PAIR has `isolation='worktree'` (intentional
    isolated-merge). A mix of worktree + non-worktree overlapping nodes is
    still rejected. This is the ONLY relaxation of invariant (4) ownership.

    `isolation` is read from the node DICT (`node.get("isolation")`); the
    AgentNode dataclass gains the field in Task 12 -- until then specs carry
    it as a plain dict key, which is all validate/check ever sees.
    """
    errs: list[str] = []
    groups: list[list[tuple[str, dict]]] = []
    _collect_parallel_groups(spec, spec.control_flow.get("steps", []), groups, set())
    for group in groups:
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                id_i, ni = group[i]
                id_j, nj = group[j]
                wa_i = ni.get("write_areas", []) or []
                wa_j = nj.get("write_areas", []) or []
                for x in wa_i:
                    for y in wa_j:
                        if _glob_overlap(x, y):
                            # all-worktree exception (v1 line 551 isolated
                            # merge): overlap allowed only when BOTH overlapping
                            # nodes are worktree-isolated. A mix is rejected.
                            if (ni.get("isolation") == "worktree"
                                    and nj.get("isolation") == "worktree"):
                                continue
                            errs.append(
                                f"ownership overlap: {id_i}[{x}] ∩ {id_j}[{y}]"
                            )
    return errs


def _node_agents(spec: Spec, nid: str) -> list[tuple[str, dict]]:
    """Return the (label, node_dict) agent tuples that a referenced node
    contributes DIRECTLY to a parallel fan-out group: agent -> itself;
    parallel -> its inline body agent; pipeline -> each inline stage agent.
    Control-flow nodes (condition/repeat/until/gate) return [] here -- their
    body agents are merged via _collect_subtree_agents so they overlap-check
    against fan-out siblings (Fix 1: cross-subtree overlap)."""
    node = spec.nodes.get(nid, {})
    t = node.get("type")
    if t == "agent":
        return [(nid, node)]
    if t == "parallel":
        body = node.get("body", {})
        if isinstance(body, dict) and body.get("type") == "agent":
            return [(f"{nid}#body", body)]
    elif t == "pipeline":
        # pipeline stages within one pipeline run sequentially, but a pipeline
        # node alongside sibling agents in a fan-out step runs concurrently
        # with those siblings -- so each stage agent's write_areas join this
        # group. (intra-pipeline sequential stages checked pairwise is the
        # deferred false-positive class; cross-subtree sibling conflicts are
        # the real hazard this catches.)
        out: list[tuple[str, dict]] = []
        for i, st in enumerate(node.get("stages", []) or []):
            if isinstance(st, dict) and st.get("type") == "agent":
                out.append((f"{nid}#stage{i}", st))
        return out
    return []


def _body_steps(node: dict, t: str) -> list:
    """Return the body step-list(s) of a control-flow node as a single flat
    list for subtree-agent collection. condition -> then_branch + else_branch
    (both branches may fire, so both branches' agents are concurrent with
    fan-out siblings); repeat/until/gate -> body. Flattening both condition
    branches means then-vs-else agents are pairwise-checked -- a deferred
    false-positive class (mutually-exclusive branches checked as concurrent),
    the same class as pipeline intra-stages; the cross-subtree conflicts
    this catches (branch body vs fan-out sibling) are real invariant-4
    hazards that must be prevented."""
    if t == "condition":
        return list(node.get("then_branch", []) or []) + list(
            node.get("else_branch", []) or [])
    return list(node.get("body", []) or [])


def _collect_parallel_groups(spec: Spec, steps, groups, visited) -> None:
    """Walk a step-list (control_flow.steps shape: str | ["parallel", ...] |
    nested list) and append each parallel fan-out group to `groups`. Each
    group is a list of (label, node_dict) tuples for agents that run
    concurrently.

    Fan-out step: agents/parallel/pipeline contribute their direct agents
    (_node_agents); control-flow nodes (condition/repeat/until/gate) MERGE
    their body agents into the group via _collect_subtree_agents (Fix 1) so
    branch-body agents are overlap-checked against the parallel siblings, not
    collected as an independent nested group.

    Sequential step (str): descend into control-flow node bodies via
    _descend_node_body to find nested intra-body parallel groups (the
    fan-out-merge path does not apply -- there are no siblings to
    cross-check, only intra-body pairs).

    `visited` is the shared cycle-defense set (Fix 2): a node id is added
    before descending into its body; a cyclic body stops at the visited
    check instead of infinite-looping.

    Existence-checking (Task 3) and overlap-checking (this) walk the same
    bodies but check different things; two separate walkers avoid premature
    coupling (the shapes are similar, the concerns are not).
    """
    for step in steps:
        if isinstance(step, str):
            _descend_node_body(spec, step, groups, visited)
        elif isinstance(step, list) and step and step[0] == "parallel":
            group: list[tuple[str, dict]] = []
            for nid in step[1:]:
                if not isinstance(nid, str):
                    continue
                node = spec.nodes.get(nid, {})
                t = node.get("type")
                if t in ("agent", "parallel", "pipeline"):
                    group.extend(_node_agents(spec, nid))
                elif t in ("condition", "repeat", "until", "gate"):
                    # Fix 1: merge this control-flow node's body agents into
                    # the fan-out group (not a separate descend) so they're
                    # overlap-checked against the parallel siblings. The merged
                    # group covers both intra-body and cross-subtree pairs.
                    # Visited guard prevents re-walking a node already merged
                    # via another sibling's subtree (diamond DAG) or cycled to.
                    if nid not in visited:
                        visited.add(nid)
                        _collect_subtree_agents(
                            spec, _body_steps(node, t), group, visited)
            if group:
                groups.append(group)
        elif isinstance(step, list):
            # a nested list that isn't a ["parallel", ...] keyword step --
            # recurse (defensive: future control-flow keywords).
            _collect_parallel_groups(spec, step, groups, visited)
        # non-str/non-list: ignore (defensive)


def _collect_subtree_agents(spec, steps, acc, visited) -> None:
    """Flatten ALL agents reachable in a step-list subtree into `acc` (list
    of (label, node_dict) tuples). Used to merge a control-flow node's body
    agents into a fan-out group (Fix 1) so they overlap-check against the
    fan-out siblings. Walks str steps, ["parallel", ...] fan-out steps (all
    their agents join acc), and nested lists; recurses into nested
    control-flow nodes with cycle defense (visited set, Fix 2).

    Agents that are sequential relative to each other within the subtree are
    flattened together -- they're pairwise-checked, which is the deferred
    false-positive class (same as pipeline intra-stages and condition
    then-vs-else mutual exclusivity). The cross-subtree conflicts this
    catches (branch-body agent vs fan-out sibling) are real concurrent-write
    hazards invariant (4) must prevent.
    """
    for step in steps:
        if isinstance(step, str):
            node = spec.nodes.get(step, {})
            t = node.get("type")
            if t in ("agent", "parallel", "pipeline"):
                acc.extend(_node_agents(spec, step))
            elif t in ("condition", "repeat", "until", "gate") and step not in visited:
                visited.add(step)
                _collect_subtree_agents(
                    spec, _body_steps(node, t), acc, visited)
        elif isinstance(step, list) and step and step[0] == "parallel":
            for nid in step[1:]:
                if not isinstance(nid, str):
                    continue
                node = spec.nodes.get(nid, {})
                t = node.get("type")
                if t in ("agent", "parallel", "pipeline"):
                    acc.extend(_node_agents(spec, nid))
                elif t in ("condition", "repeat", "until", "gate") and nid not in visited:
                    visited.add(nid)
                    _collect_subtree_agents(
                        spec, _body_steps(node, t), acc, visited)
        elif isinstance(step, list):
            _collect_subtree_agents(spec, step, acc, visited)
        # non-str/non-list: ignore (defensive)


def _descend_node_body(spec: Spec, nid: str, groups, visited) -> None:
    """If node `nid` is a v2 control-flow node referenced from a SEQUENTIAL
    step, recurse into its body step-lists to find nested intra-body parallel
    groups. This is the sequential-step path -- when a control-flow node is
    referenced from a FAN-OUT step, its body agents are merged into the fan-out
    group by _collect_parallel_groups instead (Fix 1), so this function is
    NOT called for fan-out siblings.

    Cycle defense (Fix 2): `nid` is checked against `visited` before
    descending; a cyclic body (cond1.then_branch referencing cond1) stops
    here instead of infinite-looping. A node visited once (in any context --
    sequential descend or fan-out subtree-merge) is not re-descended: its
    groups/agents were already collected, so no overlap is missed (DFS
    visited-set semantics; the diamond DAG case is covered because the
    shared group from the first visit already contains the pairs).
    """
    if nid in visited:
        return
    visited.add(nid)
    node = spec.nodes.get(nid, {})
    t = node.get("type")
    if t == "condition":
        _collect_parallel_groups(spec, node.get("then_branch", []) or [], groups, visited)
        _collect_parallel_groups(spec, node.get("else_branch", []) or [], groups, visited)
    elif t in ("repeat", "until", "gate"):
        _collect_parallel_groups(spec, node.get("body", []) or [], groups, visited)
    # agent/synthesize/verify/workflow/parallel/pipeline: no step-list body
    # to recurse into (parallel.body & pipeline.stages are unwrapped at the
    # fan-out step; a standalone parallel/pipeline node -- referenced from a
    # sequential step -- has no sibling to overlap with, so no group forms).


def _validate_decision_trace(spec: Spec) -> list[str]:
    """B5: enforce the decision_trace iron rules (§4.2). Referential integrity
    for event: refs is checked at run time (events don't exist at validate
    time); here we enforce structure + phase + alternatives + replan_reason +
    evidence_refs format + assumption-id uniqueness."""
    errs: list[str] = []
    phases = {"frame", "orient", "decompose", "plan", "replan"}
    for d in spec.decision_trace:
        did = d.get("decision_id", "?")
        phase = d.get("phase")
        if phase not in phases:
            errs.append(
                f"decision {did}: invalid phase {phase!r} (must be one of {sorted(phases)})"
            )
        if phase in ("decompose", "replan") and not d.get("alternatives_rejected"):
            errs.append(
                f"decision {did}: phase {phase} requires non-empty alternatives_rejected"
            )
        if phase == "replan" and not d.get("replan_reason"):
            errs.append(
                f"decision {did}: replan phase requires replan_reason (name the falsified assumption)"
            )
        for r in d.get("evidence_refs", []):
            if not isinstance(r, str) or not re.match(
                r"^(event|artifact|decision|checkpoint):", r
            ):
                errs.append(
                    f"decision {did}: evidence_refs must use event:/artifact:/decision:/checkpoint: prefix (got {r!r})"
                )
        aids = [a.get("id") for a in d.get("assumptions", []) if isinstance(a, dict)]
        dups = sorted({x for x in aids if aids.count(x) > 1})
        if dups:
            errs.append(f"decision {did}: assumption ids must be unique (dup: {dups})")
    return errs


# --- v2 validation extensions (spec §6) -------------------------------------

# Closed-set of known node `type` values. v1 kinds: agent/parallel/pipeline/
# verify/synthesize/workflow (the six @dataclass node kinds above). v2 kinds:
# condition/repeat/until/gate (§1.1-§1.4). An unknown type used to pass
# validate_spec silently then crash at harness._dispatch (harness.py:996,
# `raise ValueError(f"unknown node type {t}")`) -- risk R12. Now rejected
# here at validate time (invariant 4: fail-fast).
KNOWN_KINDS = frozenset({
    "agent", "parallel", "pipeline", "verify", "synthesize", "workflow",
    "condition", "repeat", "until", "gate", "script",
})

# Fields that may carry node-id references, walked recursively for referential
# integrity. Step-list fields (body/then_branch/else_branch for v2 control-flow
# kinds) hold the same shape as control_flow.steps: bare node-id strings or
# ["parallel", ...] lists or nested lists. The string fields (target/inputs/
# stages/over/items) are usually {{ref}} templates to upstream values; a bare
# identifier there is also checked (it's a node id), but {{...}} templates and
# non-identifier strings (e.g. "SYNTHESIZE {{n1.out}}") are not node ids and
# are skipped. Dicts (parallel.body, pipeline stage templates) are inline
# templates, not id lists, and are skipped.
_REF_FIELDS = (
    "body", "then_branch", "else_branch", "target", "inputs",
    "stages", "over", "items", "args",
)
_NODE_ID_RE = re.compile(r"^\w+$")


def _collect_referenced_ids(value, acc: list) -> None:
    """Recursively collect node-id strings from a reference-bearing field value.

    Walks step-list shapes (str | ["parallel", ...] | nested lists). A string
    is treated as a node-id reference only if it is a bare identifier
    (``^\\w+$``, no ``{{`` / spaces / dots) and not the ``"parallel"`` keyword.
    Dicts (inline agent/stage templates) and other non-str/non-list values are
    skipped -- they are templates, not id lists. This is existence-checking
    only; overlap-checking is Task 4 (its own recursive walk).
    """
    if isinstance(value, str):
        if value and value != "parallel" and _NODE_ID_RE.match(value):
            acc.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_referenced_ids(item, acc)
    # dict / None / other: inline template or absent -- not an id reference.


_CODE_EXTS = {
    ".py", ".tsx", ".ts", ".jsx", ".js", ".vue", ".svelte", ".go", ".rs",
    ".rb", ".java", ".kt", ".swift", ".cs", ".sql", ".php", ".css", ".scss",
    ".less",
}
# Path segments that name a code-bearing directory. Catches write_areas like
# backend/app/main.py, frontend/src/..., src/lib/..., app/routes/... even when
# the extension alone is ambiguous (e.g. a config .json under app/ that drives
# runtime). Coarse on purpose -- this is the opt-out GATE (does this spec touch
# code at all), not the fine surface classification (classify_change.py).
_CODE_PATH_RE = re.compile(r"(^|/)(src|app|backend|frontend|api|routes?|components|services|handlers|controllers|models|stores?|lib)/", re.IGNORECASE)


def _touches_code(write_areas: list[str]) -> bool:
    """Coarse gate: do these write_areas touch runtime/code (not pure
    docs/config)? Used by the assurance default-on rule (invariant #9) to
    decide whether a spec's completion verdict must be machine-gated.

    Pure .md/.json/.txt/.yml/.toml + editor configs -> not code. Anything with a
    code extension OR under a code-bearing directory -> code. False negatives
    are acceptable (a missed code file just means the gate doesn't fire -- the
    fine classification in classify_change.py still runs at adjudicate time);
    false positives are the safe direction (over-gate, never under-gate)."""
    for wa in write_areas:
        if not isinstance(wa, str) or not wa:
            continue
        p = wa.split("?")[0].split("#")[0]  # strip query/anchor if any
        ext = p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
        if ext in _CODE_EXTS:
            return True
        if _CODE_PATH_RE.search(p):
            return True
    return False


def _success_evidence_bound_nodes(spec: Spec) -> list[str]:
    """Node ids bound by success_evidence entries of the form 'S<n>:R<m>=node:<id>'.
    These are the nodes whose verdict defines task completion. Returns [] if no
    node: bindings exist (e.g. all claim: bindings -- then the completion node
    is structurally ambiguous and the opt-out rule degrades to a lint warning)."""
    bound: list[str] = []
    for se in spec.success_evidence:
        if not isinstance(se, str):
            continue
        # format: S<step>:R<req>=<binding>; binding may be node:<id> or claim:...
        rhs = se.split("=", 1)[-1] if "=" in se else ""
        rhs = rhs.split(";", 1)[0].strip()
        if rhs.startswith("node:"):
            nid = rhs[len("node:"):].strip()
            if nid and nid in spec.nodes:
                bound.append(nid)
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for nid in bound:
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


def validate_spec(spec: Spec) -> list[str]:
    """Schema completeness check. Returns list of error strings; empty == valid."""
    errs: list[str] = []
    # delivery-contract binding: all-or-nothing (spec §13.1). A partial
    # binding means the author intended the new protocol but broke the link;
    # silently treating it as legacy would lose the task-level obligations.
    bound = [spec.task_id is not None,
             spec.delivery_contract_ref is not None,
             spec.delivery_contract_digest is not None]
    if any(bound) and not all(bound):
        errs.append(
            "delivery-contract binding is partial: task_id, "
            "delivery_contract_ref and delivery_contract_digest must all be "
            "set (new protocol) or all absent (legacy); refusing to guess")
    if all(bound) and not (spec.delivery_contract_digest or "").startswith("sha256:"):
        errs.append("delivery_contract_digest must be a sha256: digest")
    for nid, n in spec.nodes.items():
        # node id field: the harness hard-subscripts node["id"] in ~15 places
        # (first at dispatch_agent/_replay_pop); a spec missing it passes
        # validate but crashes at run. The dict key is the canonical id; they
        # must match (the SKILL.md walkthrough once omitted id -> KeyError on
        # first dispatch -> manual python patch -> rm -rf -> re-run).
        if "id" not in n:
            errs.append(f"node {nid}: missing id field (must equal its dict key {nid!r})")
        elif n.get("id") != nid:
            errs.append(f"node {nid}: id field {n.get('id')!r} does not match dict key {nid!r}")
        t = n.get("type")
        if t in ("agent", "synthesize") and not n.get("output_schema"):
            errs.append(f"node {nid}: missing output_schema")
        # verify nodes: output_schema is NOT required -- run_verify uses a
        # canonical hardcoded skeptic schema {refuted, evidence_ref} (a loose
        # node schema would let prose -> {} conform -> false survive). A
        # declared verify output_schema is ignored (dead field, kept only if
        # the author wants to document the skeptic shape).
        if t == "agent":
            if not n.get("prompt"):
                errs.append(f"node {nid}: missing prompt")
            if not n.get("acceptance"):
                errs.append(f"node {nid}: missing acceptance")
            if "failure_policy" not in n:
                errs.append(f"node {nid}: missing failure_policy")
            if "write_areas" not in n:
                errs.append(f"node {nid}: missing write_areas (use [] for read-only)")
        if t == "script":
            sp = n.get("script_path")
            if not sp:
                errs.append(f"node {nid}: script node missing script_path")
            else:
                resolved = Path(sp)
                if not resolved.is_absolute():
                    resolved = Path(__file__).resolve().parent.parent / sp
                if not resolved.is_file():
                    errs.append(
                        f"node {nid}: script_path {sp!r} does not exist "
                        f"(resolved {resolved})"
                    )
            osch = n.get("output_schema") or {}
            if not osch.get("required"):
                errs.append(
                    f"node {nid}: script requires non-empty output_schema.required "
                    f"(script must declare a structured output contract)"
                )
            if n.get("verdict_field"):
                errs.append(
                    f"node {nid}: script node must not declare verdict_field "
                    f"(script is evidence-producer, not adjudicator; "
                    f"bind a verify node to adjudicate)"
                )
            # args {{node_id.out.field}} templates are NOT caught by the
            # _REF_FIELDS referential walk (it skips {{...}} templates as
            # non-bare-identifiers) -- validate them explicitly here so a
            # dangling arg reference fails at validate time, not at dispatch.
            for a in n.get("args", []) or []:
                for m in re.finditer(r"\{\{(\w+)\.", str(a)):
                    ref_nid = m.group(1)
                    if ref_nid not in spec.nodes:
                        errs.append(
                            f"node {nid}: args template {a!r} references "
                            f"unknown node {ref_nid!r}"
                        )
        # §6 closed-set type check (risk R12): reject unknown node `type` at
        # validate time, NOT a silent pass + later crash at _dispatch
        # (harness.py:996 `raise ValueError(f"unknown node type {t}")`).
        if t not in KNOWN_KINDS:
            errs.append(
                f"node {nid}: unknown type {t!r} (known kinds: "
                f"{', '.join(sorted(KNOWN_KINDS))})"
            )
        # R9-4 (review9 P2): anti-self-review rules, machine-checked.
        # A node that OWNS a requirement verdict (verdict_field) must not
        # also touch the code it judges -- an implementer cannot self-certify
        # its own work. The verifier holds an independent context: the
        # requirement, the pinned code, and the machine evidence.
        if n.get("verdict_field") and (n.get("write_areas") or []):
            errs.append(
                f"node {nid}: verdict_field node must have write_areas [] "
                f"(anti-self-review: the node that owns a requirement's "
                f"verdict must not write the code it judges)"
            )
        # evidence_from must point at a script node that actually produces
        # machine evidence (evidence_scope); anything else is a dangling
        # declaration that degrades to evidence_grade=none at run time.
        ev_from = n.get("evidence_from")
        if ev_from:
            src = spec.nodes.get(ev_from)
            if src is None:
                errs.append(
                    f"node {nid}: evidence_from {ev_from!r} names an "
                    f"unknown node"
                )
            elif src.get("type") != "script":
                errs.append(
                    f"node {nid}: evidence_from {ev_from!r} must point at a "
                    f"script node (got type {src.get('type')!r}); only script "
                    f"nodes produce machine evidence packs"
                )
            elif not src.get("evidence_scope"):
                errs.append(
                    f"node {nid}: evidence_from {ev_from!r} points at a "
                    f"script node without evidence_scope (no pack will be "
                    f"emitted; the verdict would grade 'none')"
                )
            if nid == ev_from:
                errs.append(
                    f"node {nid}: evidence_from must not reference itself"
                )
        # §6 per-kind rules for v2 control-flow nodes (opt-in: v1 specs
        # never use these kinds, so v1 behavior is unchanged).
        if t == "condition":
            if not n.get("predicate"):
                errs.append(f"node {nid}: condition requires non-empty predicate")
            if not n.get("then_branch"):
                errs.append(f"node {nid}: condition requires non-empty then_branch")
        elif t == "repeat":
            if not n.get("body"):
                errs.append(f"node {nid}: repeat requires non-empty body")
            mi = n.get("max_iterations")
            if not (isinstance(mi, int) and not isinstance(mi, bool) and mi > 0):
                errs.append(f"node {nid}: repeat requires max_iterations > 0 (got {mi!r})")
            if not n.get("until"):
                errs.append(
                    f"node {nid}: repeat requires non-empty until "
                    f"(termination predicate)"
                )
        elif t == "until":
            if not n.get("body"):
                errs.append(f"node {nid}: until requires non-empty body")
            mi = n.get("max_iterations")
            if not (isinstance(mi, int) and not isinstance(mi, bool) and mi > 0):
                errs.append(f"node {nid}: until requires max_iterations > 0 (got {mi!r})")
            if not n.get("cond"):
                errs.append(f"node {nid}: until requires non-empty cond")
        elif t == "gate":
            if not n.get("trigger"):
                errs.append(f"node {nid}: gate requires non-empty trigger")
            if not n.get("body"):
                errs.append(f"node {nid}: gate requires non-empty body")
        # §6 referential integrity for ALL node kinds: every node id referenced
        # in any body/then_branch/else_branch/target/inputs/stages/over/items
        # must exist in spec.nodes. Recursive walk handles bare node-id strings,
        # ["parallel", "n1", "n2"] lists, and nested lists; {{ref}} templates
        # and inline dict templates (parallel.body, pipeline stages) are
        # skipped. Existence-checking only -- overlap-checking is Task 4.
        refs: list = []
        for f in _REF_FIELDS:
            if f in n:
                _collect_referenced_ids(n[f], refs)
        for ref in dict.fromkeys(refs):  # dedup, preserve order
            if ref not in spec.nodes:
                errs.append(
                    f"node {nid}: references unknown node {ref!r} "
                    f"(in body/then_branch/else_branch/target/inputs/stages/over/items)"
                )
    # control_flow steps: each step is a str (existing node id) or a list
    # whose [0] is a control-flow keyword (v1: "parallel"). A bare list of
    # node ids like ["n2","n3","n4"] is a common mistake that validate MUST
    # catch -- otherwise _run_sequence silently skips the step (the elif
    # branch checks step[0]=="parallel" and a bare list fails it). This is
    # the validate/run parity gap a real relay hit.
    steps = spec.control_flow.get("steps", [])
    for i, step in enumerate(steps):
        if isinstance(step, str):
            if step not in spec.nodes:
                errs.append(f"control_flow step {i}: unknown node '{step}'")
        elif isinstance(step, list):
            if not step:
                errs.append(f"control_flow step {i}: empty list")
            elif step[0] != "parallel":
                errs.append(
                    f"control_flow step {i}: list step must start with a "
                    f"control-flow keyword (got {step[0]!r}); use "
                    f'["parallel", ...node-ids] for a fan-out step'
                )
            else:
                for nid in step[1:]:
                    if not isinstance(nid, str) or nid not in spec.nodes:
                        errs.append(
                            f"control_flow step {i}: unknown node {nid!r}")
        else:
            errs.append(
                f"control_flow step {i}: must be str or list, "
                f"got {type(step).__name__}")
    errs += check_ownership(spec)
    # verdict pairing: success_evidence node:<id> requires the node to declare
    # verdict_field (else no agent_verdict event -> PARTIAL despite correct work,
    # the flight-price-monitor relay); claim:<id> must match the claim_id format
    # (C\d+|dec_\w+, per state._evidence_to_claim) -- a node id is not a claim id.
    for se in spec.success_evidence:
        if not isinstance(se, str):
            continue
        m = re.search(r"node:(\w+)", se)
        if m:
            nid = m.group(1)
            if nid not in spec.nodes:
                errs.append(f"success_evidence '{se}': node:{nid} is not a node")
            elif spec.nodes[nid].get("type") == "script":
                errs.append(
                    f"success_evidence '{se}': node:{nid} is a script node; "
                    f"completion cannot bind to script (it produces no verdict) -- "
                    f"bind a verify/agent node that reads the script output instead"
                )
            elif not spec.nodes[nid].get("verdict_field"):
                errs.append(
                    f"success_evidence '{se}': node:{nid} has no verdict_field "
                    f"(node: binding requires the agent to declare verdict_field "
                    f"and return {{verdict:...}}; else no agent_verdict event -> PARTIAL)"
                )
            continue
        m = re.search(r"claim:(\S+)", se)
        if m:
            cid = m.group(1)
            if not re.match(r"^([A-Z]+\d+|dec_\w+)$", cid):
                errs.append(
                    f"success_evidence '{se}': claim:{cid} is not a valid claim id "
                    f"(must be <PREFIX><digits> (e.g. C1, or SP1/SEC1 to partition "
                    f"claim namespaces across verify targets so two verifiers never "
                    f"collide on the same id) or dec_<word>; a lowercase node id like "
                    f"n5 is wrong)"
                )
    # P1-1 fix: a required R must not be silently skipped (unverified). If SOME required R is
    # bound to success_evidence but a required sibling is NOT, that sibling
    # would slip past derive_verdict's old form -> false VERIFIED. derive_verdict
    # now caps it at PARTIAL at runtime; validate rejects it earlier so the
    # author fixes the spec before a run. A spec with NO required R bound at
    # all is valid (verdict UNVERIFIED -- nothing to verify, not a skipped requirement).
    bound_rids = set()
    for se in spec.success_evidence:
        if not isinstance(se, str):
            continue
        m = re.search(r":(R\d+)", se)
        if m:
            bound_rids.add(m.group(1))
    required_ids = {r.id for r in spec.requirements
                    if getattr(r, "criticality", None) == "required"}
    if required_ids and (required_ids & bound_rids):
        for rid in sorted(required_ids):
            if rid not in bound_rids:
                errs.append(
                    f"requirement {rid}: required but has no success_evidence "
                    f"binding (a required R must be bound to a verify/agent node "
                    f"or claim, else it is silently skipped -> false VERIFIED; either "
                    f"bind it or mark it optional)"
                )
    # seam (b): assurance_hook nodes -- the harness re-derives verdict from
    # change-assurance's adjudicate.py, overriding the LLM's verdict_field
    # transcription. Validate the hook shape so a malformed hook fails at
    # validate time, not silently at verdict time.
    for nid, n in spec.nodes.items():
        hook = n.get("assurance_hook") if isinstance(n, dict) else None
        if not hook:
            continue
        if n.get("type") != "agent":
            errs.append(f"node {nid}: assurance_hook requires type:agent (is {n.get('type')})")
            continue
        if not n.get("verdict_field"):
            errs.append(
                f"node {nid}: assurance_hook requires verdict_field "
                f"(binding still routes through verdict_field -> agent_verdict)")
        if not isinstance(hook, dict):
            errs.append(f"node {nid}: assurance_hook must be a dict")
            continue
        rp = hook.get("receipt_path")
        if rp is not None and not isinstance(rp, str):
            errs.append(f"node {nid}: assurance_hook.receipt_path must be a string")
        sd = hook.get("skill_dir")
        if sd is not None and not isinstance(sd, str):
            errs.append(f"node {nid}: assurance_hook.skill_dir must be a string")
    # assurance default-on (change-assurance invariant #9): a spec whose
    # write_areas touch runtime/code must machine-gate its completion verdict.
    # The node bound by success_evidence (node:<id>) is the completion-claiming
    # node; without assurance_hook its VERIFIED is the LLM's self-report, trusted
    # directly -- the exact "self-write self-run self-graduate" path change-assurance
    # exists to close. A real relay's split (verifier node has no write_areas of its
    # own) is why this rule keys off the SPEC's write_areas + the success_evidence
    # binding, not the node's own write_areas. Bare opt-out is not allowed -- a
    # non-empty spec.assurance_opt_out reason is the only escape hatch.
    all_write_areas: list[str] = []
    for n in spec.nodes.values():
        if isinstance(n, dict) and n.get("type") == "agent":
            for wa in n.get("write_areas", []) or []:
                if isinstance(wa, str):
                    all_write_areas.append(wa)
    if _touches_code(all_write_areas):
        bound = _success_evidence_bound_nodes(spec)
        opt_out = (spec.assurance_opt_out or "").strip()
        if not bound:
            # No structural completion node (all claim: bindings). Can't enforce
            # machine gate without a target; degrade to lint warning below.
            pass
        else:
            for nid in bound:
                n = spec.nodes.get(nid)
                hook = n.get("assurance_hook") if isinstance(n, dict) else None
                # R9-3: evidence_from binding is ALSO a machine gate -- the
                # reviewer's verdict is bound to a runtime-generated evidence
                # fingerprint, not the LLM's bare self-report.
                ev_bound = isinstance(n, dict) and bool(n.get("evidence_from"))
                if not hook and not ev_bound and not opt_out:
                    errs.append(
                        f"node {nid}: spec touches runtime code; the completion-verdict "
                        f"node bound by success_evidence must declare assurance_hook "
                        f"(legacy receipt gate) or evidence_from (R9-3 machine evidence) "
                        f"-- otherwise the LLM self-reports VERIFIED with no machine "
                        f"grounding. To opt out, set spec.assurance_opt_out with a reason."
                    )
    errs += _validate_decision_trace(spec)
    return errs


def lint_spec(spec: Spec) -> list[str]:
    """Advisory warnings (NOT errors -- validate passes regardless). Nudges the
    author toward catching bugs at verify time instead of the user side.

    Static-verifier-on-runtime-task: if a verifier (agent with verdict_field)
    lacks Bash but another agent's write_areas touch runtime code (backend *.py,
    frontend *.tsx/vue), warn -- static Read/Grep verify won't catch reload/state
    bugs (a real relay's endpoint-not-reloaded + state-not-refreshed
    were both missed by static verify, found by the user)."""
    warns: list[str] = []
    runtime_touchers: list[str] = []
    static_verifiers: list[str] = []
    for nid, n in spec.nodes.items():
        if not isinstance(n, dict) or n.get("type") != "agent":
            continue
        for wa in n.get("write_areas", []) or []:
            if not isinstance(wa, str):
                continue
            if (wa.endswith(".py") and "backend/" in wa) or wa.endswith(".tsx") \
                    or wa.endswith(".vue") or "/main.py" in wa:
                runtime_touchers.append(f"{nid}:{wa}")
        if n.get("verdict_field"):
            tools = n.get("allowed_tools", []) or []
            if "Bash" not in tools:
                static_verifiers.append(nid)
    if runtime_touchers and static_verifiers:
        warns.append(
            f"static verifier {static_verifiers} on a task touching runtime files "
            f"({runtime_touchers[:3]}); static verify (Read/Grep) won't catch "
            f"reload/state bugs -- consider runtime-verifier "
            f"(allowed_tools:[Read,Bash], restart+curl+assert). TS/import clean != "
            f"functional (a real relay: endpoint-not-reloaded + state-not-refreshed "
            f"were both missed by static verify)."
        )
    # assurance opt-out disclosure (invariant #9): an opt-out reason was set,
    # meaning the completion verdict is NOT machine-gated. Surface it so a
    # reviewer sees the bypass rather than reading a clean VALID as if it were
    # enforced. Also warn when a runtime-touching spec has no structural
    # completion node (all claim: bindings) -- the opt-out rule can't fire there.
    opt_out = (spec.assurance_opt_out or "").strip()
    if opt_out:
        warns.append(
            f"assurance opt-out: machine assurance bypassed. reason: {opt_out}. "
            f"The completion verdict will be the LLM's self-report, trusted directly "
            f"(no adjudicate override). Review the reason before accepting VERIFIED."
        )
    else:
        all_was: list[str] = []
        for n in spec.nodes.values():
            if isinstance(n, dict) and n.get("type") == "agent":
                all_was.extend(wa for wa in n.get("write_areas", []) or [] if isinstance(wa, str))
        # Only warn when the spec actually CLAIMS completion (non-empty
        # success_evidence) but can't be structurally gated. A spec with empty
        # success_evidence is a fragment/test fixture -- no completion claim,
        # no opt-out concern.
        if (_touches_code(all_was)
                and spec.success_evidence
                and not _success_evidence_bound_nodes(spec)):
            warns.append(
                "spec touches runtime code but no success_evidence node:<id> binding "
                "exists; the opt-out rule cannot structurally locate the completion node "
                "to require assurance_hook. Add a node: binding so the machine gate can "
                "target the right node."
            )
    # max_stagnation < 2 on an agent-bearing spec: G3 schema-feedback needs one
    # injection before the 2nd identical failure trips stagnation. Hard rules +
    # plan template default 2; max_stagnation:1 trips on the first conform-fail
    # with no schema-feedback chance (a prose-stubborn worker model needs that 3rd
    # attempt). script nodes are zero-LLM (no conform), so warn only when agent
    # nodes exist. (a real relay's first run set 1, the worker conform-failed once and
    # exhausted before any schema-feedback injection — wasted a full run.)
    has_agent = any(isinstance(n, dict) and n.get("type") == "agent"
                    for n in spec.nodes.values())
    if has_agent and getattr(spec, "max_stagnation", 2) < 2:
        warns.append(
            f"max_stagnation={spec.max_stagnation} on an agent-bearing spec; "
            f"G3 schema-feedback needs one injection before the 2nd identical "
            f"failure trips stagnation (default 2, not 1). max_stagnation:1 "
            f"trips on first conform-fail with no feedback chance — set >=2 "
            f"(a real relay's first run set 1, wasted a full run)."
        )
    # P-005: verify cost warn — verify cost scales O(findings × skeptic_count /
    # max_concurrent). Two static multipliers: (a) target is a fan-out node
    # (parallel/pipeline -> a LIST of findings, N batches of skeptic_count
    # dispatches); (b) skeptic_count > 5. The fan-out branch names skeptic_count
    # so the elif must not double-warn on a fan-out + high-skeptic verify.
    _ref_re = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+?)(?:\.[A-Za-z0-9_.-]+)?\s*\}\}")
    for nid, n in spec.nodes.items():
        if not isinstance(n, dict) or n.get("type") != "verify":
            continue
        target = n.get("target", "") or ""
        m = _ref_re.search(target)
        target_node = spec.nodes.get(m.group(1)) if m else None
        t_type = (target_node.get("type") if isinstance(target_node, dict) else None)
        sc = n.get("skeptic_count", 3)
        if t_type in ("parallel", "pipeline"):
            warns.append(
                f"verify node {nid}: target points at a {t_type} fan-out node "
                f"(a LIST of findings) — verify cost = N findings × "
                f"skeptic_count={sc} dispatches and can blow the budget with no "
                f"warning. Narrow the target or budget for N×{sc}."
            )
        elif sc > 5:
            warns.append(
                f"verify node {nid}: skeptic_count={sc} > 5 — each finding costs "
                f"{sc} skeptic dispatches; cost scales O(findings × skeptic_count / "
                f"max_concurrent). Lower skeptic_count or budget accordingly."
            )
    # F1: conform_strict_json explicit false + verdict_field = deliberately
    # weakening verdict integrity (the verdict can come from a {...} dug out of
    # prose). Verdict nodes are strict by default; an explicit opt-out deserves a
    # warning so the spec author knows they are allowing a prose verdict.
    for nid, n in spec.nodes.items():
        if not isinstance(n, dict):
            continue
        if (n.get("conform_strict_json") is False
                and n.get("verdict_field")):
            warns.append(
                f"node {nid}: conform_strict_json:false on a verdict_field node "
                f"— verdict may then be recovered from JSON embedded in prose "
                f"(an example/quote, not the worker's structured verdict). "
                f"Default is strict for verdict nodes; only weaken when the "
                f"worker provably cannot emit pure JSON."
            )
    return warns


# --- revisions ---------------------------------------------------------------


def make_revision(
    parent: Spec,
    revision_reason: str,
    trigger: dict,
    diff: dict,
    new_intent: str | None = None,
    new_requirements: list[Requirement] | None = None,
    new_boundaries: list[str] | None = None,
    new_success: list[str] | None = None,
) -> Spec:
    """Create a versioned child spec. Never mutates parent.

    contract_drift is True iff the contract-four changed. Plan/nodes/control_flow
    changes do not drift the contract.
    """
    rev = parent.revision + 1
    intent = new_intent if new_intent is not None else parent.intent
    reqs = new_requirements if new_requirements is not None else parent.requirements
    boundaries = new_boundaries if new_boundaries is not None else parent.boundaries
    success = new_success if new_success is not None else parent.success_evidence
    drift = (
        intent != parent.intent
        or [asdict(r) for r in reqs] != [asdict(r) for r in parent.requirements]
        or boundaries != parent.boundaries
        or success != parent.success_evidence
    )
    return Spec(
        spec_version_id=f"spec.v{rev}",
        parent_spec_id=parent.spec_version_id,
        revision=rev,
        intent=intent,
        requirements=reqs,
        boundaries=boundaries,
        success_evidence=success,
        nodes=copy.deepcopy(parent.nodes),
        control_flow=copy.deepcopy(parent.control_flow),
        decision_trace=parent.decision_trace
        + [
            {
                "decision_id": f"dec_replan_{rev}",
                "spec_version_id": f"spec.v{rev}",
                "phase": "replan",
                "revision_reason": revision_reason,
                "revision_trigger": trigger,
                "diff_from_parent": diff,
            }
        ],
        budget_usd=parent.budget_usd,
        max_concurrent=parent.max_concurrent,
        max_agents=parent.max_agents,
        max_stagnation=parent.max_stagnation,
        contract_drift=drift,
        # delivery-contract binding is task-level identity: a plan revision
        # re-binds the SAME task contract (spec §8.2 -- plan changes leave
        # the contract digest unchanged; a contract change is a new contract
        # revision, not a spec rewrite). Dropping these fields here would
        # silently restore the legacy path.
        task_id=parent.task_id,
        delivery_contract_ref=parent.delivery_contract_ref,
        delivery_contract_digest=parent.delivery_contract_digest,
        # spec §13.2 make_revision regression: assurance opt-out and wiki
        # adoption lineage must survive a replan; resetting them to defaults
        # silently re-enables gates the author explicitly configured.
        adopted_from=list(parent.adopted_from),
        assurance_opt_out=parent.assurance_opt_out,
    )
