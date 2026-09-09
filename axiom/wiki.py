"""Spec-experience wiki: append-only, hash-chained, searchable.

Corresponds to WikiSkill's Wiki layer (never rolled back). Three asymmetric
layers over axiom:

  Raw    = ledger events.jsonl   (already hash-chained, append-only)
  Wiki   = wiki.jsonl            (THIS module; same hash-chain philosophy)
  Skills = Spec version chain    (spec.v{n}, already, rollable back)

derive_debug_envelope (state.py:437) already distills failure patterns from a
run's ledger = the Raw->Wiki distiller exists. This module gives it an
append-only store + retrieval surface, not built from scratch.

Why this exists (friction elimination, not a new ceremony layer):
A spec run dies when it finishes; spec-design experience (which shape worked,
which cognitive signature stagnated, which replan was denied and why) is only
held in the calling agent's head and is lost. `plan --wiki-suggest` retrieves
"how did a similar intent fare last time" BEFORE the agent designs the next
spec -- so the agent gets the format/shape it needs before producing, not
after. This eliminates format friction at the source (the D4 read-back
assumption drift, the frontmatter-contract-not-in-skill-text grep, the
output_schema-template-null failures are all symptoms of "agent produced
without the format in hand"; wiki is where the format lands).

Append-only semantics:
  - experience entries are never rewritten (their bytes are sealed).
  - impact records (denied replans, failed gates, verify failures) are
    appended as AMENDMENT entries that reference the original via `amends:
    <entry_id>`, never as in-place field mutation. search() aggregates them
    back onto the parent for display. This is the 'knowledge grows, never
    rolls back' direction; spec.v{n} (the Skills layer) rolls back, wiki does
    not.
"""
from __future__ import annotations
import json
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timezone

_GENESIS_HASH = "sha256:" + "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _entry_hash(entry: dict) -> str:
    """sha256 over the entry minus its own event_hash field (mirrors ledger)."""
    payload = {k: v for k, v in entry.items() if k != "event_hash"}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _entry_id(spec_version_id: str, run_dir: str, ts: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"{spec_version_id}|{run_dir}|{ts}".encode("utf-8")
    ).hexdigest()


class Wiki:
    """Append-only hash-chained store at wiki_dir/<filename>.

    Default filename is 'wiki.jsonl' (the run-experience store). The same
    class manages the skill self-evolution store at the skill root as
    'skill_patterns.jsonl' — pass filename='skill_patterns.jsonl'. Both are
    structured (fielded) + agent-consumable: sediment is for the agent, not
    humans. Run-experience entries are experience/format_contract; skill
    entries are skill_pattern (see append_skill_pattern)."""

    def __init__(self, wiki_dir: Path | str, filename: str = "wiki.jsonl"):
        self.wiki_dir = Path(wiki_dir)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_path = self.wiki_dir / filename
        self._lock = threading.Lock()

    # --- read ------------------------------------------------------

    def _lines(self) -> list[dict]:
        if not self.wiki_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.wiki_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _last_hash(self) -> str:
        lines = self._lines()
        return lines[-1]["event_hash"] if lines else _GENESIS_HASH

    def entries(self) -> list[dict]:
        return self._lines()

    def get(self, entry_id: str) -> dict | None:
        for e in self._lines():
            if e.get("entry_id") == entry_id:
                return e
        return None

    # --- append ----------------------------------------------------

    def append(self, entry: dict) -> dict:
        """Seal + append an entry. Fills ts/entry_id/prev_hash/event_hash.

        The caller supplies the semantic fields (spec_version_id, run_dir,
        intent, spec_shape, verdict, patterns, learned, tags, entry_type).
        The chain fields are filled here so a caller cannot forge a broken
        link. Returns the sealed entry.
        """
        with self._lock:
            ts = entry.get("ts") or _now_iso()
            entry["ts"] = ts
            entry.setdefault("entry_type", "experience")
            # entry_id derived from semantic identity (svid+run_dir+ts) so a
            # re-extract of the same run at a different time is a NEW entry
            # (knowledge accumulates), not a silent overwrite.
            entry["entry_id"] = _entry_id(
                entry.get("spec_version_id", ""), entry.get("run_dir", ""), ts)
            entry["prev_hash"] = self._last_hash()
            entry["event_hash"] = _entry_hash(entry)
            with self.wiki_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def add_impact(self, entry_id: str, kind: str, reason: str) -> dict:
        """Append an impact amendment onto an existing entry.

        Never rewrites the parent entry (append-only). Appends a new entry
        with entry_type='impact' and `amends: <entry_id>` so the chain
        integrity of the original is preserved and the impact is itself
        hash-chained knowledge. search() aggregates amendments onto parents.

        kind: replan_denied | gate_denied | verify_failed | format_drift |
              adoption_outcome | other (free-form; the closed set is
              advisory, not enforced -- the lesson is more valuable than the
              taxonomy).

        entry_id accepts a UNIQUE PREFIX (git-short-hash style) so a caller
        holding only the truncated id printed by `plan --wiki-suggest` /
        `wiki list` can still amend; exact match wins, ambiguous prefix
        raises KeyError listing the candidates.
        """
        parent = self.get(entry_id)
        if parent is None:
            candidates = [e for e in self._lines()
                          if str(e.get("entry_id", "")).startswith(entry_id)]
            if len(candidates) == 1:
                parent = candidates[0]
            elif len(candidates) > 1:
                raise KeyError(
                    f"ambiguous entry_id prefix {entry_id!r}: "
                    f"{[c['entry_id'] for c in candidates]}")
        if parent is None:
            raise KeyError(f"no wiki entry {entry_id} to amend")
        amend = {
            "entry_type": "impact",
            "amends": parent["entry_id"],
            "impact_kind": kind,
            "reason": reason,
        }
        return self.append(amend)

    # --- skill self-evolution patterns (structured sediment) --------------

    def get_by_pattern_id(self, pattern_id: str) -> dict | None:
        """Find the latest skill_pattern entry with this pattern_id (or None).

        pattern_id is the stable human/agent anchor ('P-001') that SKILL.md's
        'Why this exists' links reference; entry_id is the sha256 chain id.
        Returns the latest so a superseded pattern's successor wins.
        """
        for e in reversed(self._lines()):
            if (e.get("entry_type") == "skill_pattern"
                    and e.get("pattern_id") == pattern_id):
                return e
        return None

    def append_skill_pattern(
        self, pattern_id: str, feature: str, friction: str, fix: str,
        commit_sha: str, rejected: list | None = None,
        open_questions: list | None = None, sub_lessons: list | None = None,
        status: str = "active",
    ) -> dict:
        """Append a STRUCTURED skill self-evolution pattern
        (entry_type='skill_pattern').

        Sediment is for the agent, not humans: structured fields, agent-
        consumable, never a markdown doc. Travels with the skill when this
        store lives at the skill root (filename='skill_patterns.jsonl').
        Rejected approaches + open questions are structured lists so a future
        change reads 'we tried X, rejected because Y' instead of re-walking
        the friction blind (knowledge grows, never rolls back -- the same
        property as experience entries).
        """
        entry = {
            "entry_type": "skill_pattern",
            "pattern_id": pattern_id,
            "feature": feature,
            "friction": friction,
            "fix": fix,
            "commit_sha": commit_sha,
            "rejected": rejected or [],
            "open_questions": open_questions or [],
            "sub_lessons": sub_lessons or [],
            "status": status,
        }
        return self.append(entry)

    # --- search (pure python, no LLM, no vector deps) --------------

    def search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        verdict: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Keyword + tag + verdict filtered retrieval, ranked by hit count.

        No embedding/DB dependency (axiom principle: retrieval fast, system-
        managed, not model-bound). query tokens match against intent +
        requirement text + learned + spec_shape + impact reasons. Returns
        aggregated summaries (parent experience entry with its impact
        amendments merged) so the caller sees one row per experience, not one
        row per append.
        """
        tags = tags or []
        tokens = [t.lower() for t in (query or "").split() if t]
        entries = self._lines()
        # index amendments by parent
        amendments: dict[str, list[dict]] = {}
        for e in entries:
            if e.get("entry_type") == "impact" and e.get("amends"):
                amendments.setdefault(e["amends"], []).append(e)
        results: list[dict] = []
        for e in entries:
            etype = e.get("entry_type", "experience")
            if etype not in ("experience", "format_contract",
                             "skill_pattern", "domain_contract"):
                continue
            if verdict and e.get("verdict") != verdict:
                continue
            if tags:
                e_tags = set(e.get("tags") or [])
                if not set(tags).issubset(e_tags):
                    continue
            # domain_contract retrieval semantics live in intent_pattern/surfaces/
            # required_roles/binding_pattern/verdict_rule (adjudication-contract
            # keywords, not run-bound intent). These fields also enter the hay so
            # token matches hit them.
            hay = " ".join([
                str(e.get("intent", "")),
                str(e.get("intent_pattern", "")),
                str(e.get("domain", "")),
                " ".join(str(s) for s in (e.get("surfaces") or [])),
                str(e.get("required_roles", "")),
                str(e.get("binding_pattern", "")),
                str(e.get("verdict_rule", "")),
                " ".join(r.get("text", "") for r in (e.get("requirements") or [])),
                str(e.get("learned", "")),
                str(e.get("spec_shape", "")),
                str(e.get("control_flow_shape", "")),
                str(e.get("pattern_id", "")),
                str(e.get("feature", "")),
                str(e.get("friction", "")),
                str(e.get("fix", "")),
                str(e.get("commit_sha", "")),
                " ".join(a.get("reason", "") for a in amendments.get(e["entry_id"], [])),
            ]).lower()
            hits = sum(1 for t in tokens if t in hay)
            if tokens and hits == 0:
                continue
            impacts = amendments.get(e["entry_id"], [])
            if etype == "format_contract":
                results.append({
                    "entry_id": e["entry_id"],
                    "entry_type": "format_contract",
                    "intent": e.get("intent"),
                    "control_flow_shape": e.get("control_flow_shape"),
                    "node_skeleton": e.get("node_skeleton", []),
                    "binding_pattern": e.get("binding_pattern", []),
                    "learned": e.get("learned"),
                    "tags": e.get("tags", []),
                    "impact": [
                        {"kind": a.get("impact_kind"), "reason": a.get("reason")}
                        for a in impacts
                    ],
                    "hits": hits,
                })
            elif etype == "skill_pattern":
                results.append({
                    "entry_id": e["entry_id"],
                    "entry_type": "skill_pattern",
                    "pattern_id": e.get("pattern_id"),
                    "feature": e.get("feature"),
                    "friction": e.get("friction"),
                    "fix": e.get("fix"),
                    "commit_sha": e.get("commit_sha"),
                    "status": e.get("status", "active"),
                    "impact": [
                        {"kind": a.get("impact_kind"), "reason": a.get("reason")}
                        for a in impacts
                    ],
                    "hits": hits,
                })
            elif etype == "domain_contract":
                results.append({
                    "entry_id": e["entry_id"],
                    "entry_type": "domain_contract",
                    "contract_id": e.get("contract_id"),
                    "domain": e.get("domain"),
                    "intent_pattern": e.get("intent_pattern"),
                    "surfaces": e.get("surfaces", []),
                    "risk_floor": e.get("risk_floor"),
                    "required_roles": e.get("required_roles", {}),
                    "auth_gate_required": e.get("auth_gate_required", False),
                    "auth_gate_rule": e.get("auth_gate_rule"),
                    "binding_pattern": e.get("binding_pattern"),
                    "verdict_rule": e.get("verdict_rule"),
                    "learned": e.get("learned"),
                    "source": e.get("source"),
                    "tags": e.get("tags", []),
                    "impact": [
                        {"kind": a.get("impact_kind"), "reason": a.get("reason")}
                        for a in impacts
                    ],
                    "hits": hits,
                })
            else:
                results.append({
                    "entry_id": e["entry_id"],
                    "entry_type": "experience",
                    "intent": e.get("intent"),
                    "spec_shape": e.get("spec_shape"),
                    "verdict": e.get("verdict"),
                    "learned": e.get("learned"),
                    "tags": e.get("tags", []),
                    "patterns": e.get("patterns", []),
                    "impact": [
                        {"kind": a.get("impact_kind"), "reason": a.get("reason")}
                        for a in impacts
                    ],
                    "hits": hits,
                })
        results.sort(key=lambda r: (-r["hits"], r["entry_id"]))
        return results[:limit]

    # --- verify ----------------------------------------------------

    def verify_chain(self) -> list[str]:
        """Return list of chain breakages; empty == intact (mirrors ledger)."""
        errs: list[str] = []
        prev = _GENESIS_HASH
        for i, e in enumerate(self._lines()):
            if e.get("prev_hash") != prev:
                errs.append(f"prev_hash break at {e.get('entry_id')}")
            stored = e.get("event_hash")
            recomputed = _entry_hash(e)
            if stored != recomputed:
                errs.append(f"event_hash mismatch at {e.get('entry_id')} (tamper)")
            prev = e["event_hash"]
        return errs


def extract_entry(spec, ledger, run_dir: str, tags: list[str] | None = None,
                  learned: str | None = None) -> dict:
    """Distill a wiki experience entry from a finished run's spec + ledger.

    Reuses derive_verdict + derive_debug_envelope (the Raw->Wiki distiller at
    state.py:437) + derive_spec_shape (state.py) so the wiki never re-derives
    truth the ledger already holds. Returns an UNSEALED entry dict; the caller
    passes it to Wiki.append() which fills the chain fields.
    """
    from axiom.state import derive_verdict, derive_debug_envelope, derive_spec_shape
    env = derive_debug_envelope(spec, ledger)
    patterns = []
    for f in env.get("failures", []):
        sig = f.get("cognitive_signature")
        patterns.append({
            "node_id": f.get("node_id"),
            "failure_class": f.get("failure_class"),
            "cognitive_signature": sig,
            "recovery_action": f.get("recovery_action"),
        })
    entry = {
        "entry_type": "experience",
        "spec_version_id": spec.spec_version_id,
        "run_dir": str(run_dir),
        "intent": spec.intent,
        "requirements": [
            {"id": r.id, "text": r.text} for r in spec.requirements
        ],
        "spec_shape": derive_spec_shape(spec),
        "verdict": derive_verdict(spec, ledger),
        "patterns": patterns,
        "impact": [],
        "learned": learned or "",
        "tags": tags or [],
    }
    # co-evolution loop (P-014): record which wiki entries this spec adopted, so the
    # adoption edge is queryable from the run side too (not only as impact
    # amendments on the adopted entries).
    adopted = list(getattr(spec, "adopted_from", None) or [])
    if adopted:
        entry["adopted_from"] = adopted
    return entry


def extract_contract_entry(spec, ledger, run_dir: str,
                           tags: list[str] | None = None,
                           learned: str | None = None) -> dict | None:
    """Distill a format_contract entry from a VERIFIED run's spec.

    Admittance gate (D2): only a VERIFIED run's structure is a proven-correct
    format worth copying. Non-VERIFIED runs return None (their lessons live in
    experience entries' patterns, not in contracts). This is the deepening of
    the friction-elimination thesis: `plan --wiki-suggest` returns not just a
    shape fingerprint but the concrete proven structure (node skeleton +
    output_schema required fields + binding pattern) so the agent gets the
    EXACT format in hand before producing, not just "this shape worked".

    Pure Spec serialization (D3): does NOT read ledger failure events (that's
    experience's job). Reads ledger exactly once for the VERIFIED gate.
    """
    from axiom.state import derive_verdict, derive_spec_shape
    if derive_verdict(spec, ledger) != "VERIFIED":
        return None
    # P-021: the contract must describe the WHOLE proven structure, not only
    # agent nodes. A source+verify run's contract used to drop the verify
    # node from the skeleton, so `plan --wiki-suggest` recommended copying a
    # structure with NO verification stage -- the exact shape that produces
    # self-certified false VERIFIEDs. Include every node with
    # type-appropriate fields; agents keep the richer schema fields.
    node_skeleton = []
    for nid, n in (spec.nodes.items() if hasattr(spec, "nodes") else []):
        if not isinstance(n, dict):
            continue
        ntype = n.get("type", "agent")
        entry = {
            "id": n.get("id", nid),
            "type": ntype,
            "on_exhausted": (n.get("failure_policy") or {}).get("on_exhausted"),
        }
        if ntype == "agent":
            schema = n.get("output_schema") or {}
            entry.update({
                "output_schema_required": list(schema.get("required", []) or []),
                "verdict_field": bool(n.get("verdict_field")),
                "write_areas_empty": not (n.get("write_areas") or []),
            })
        elif ntype == "verify":
            entry.update({
                "skeptic_count": n.get("skeptic_count", 3),
                "has_fix_loop": bool(n.get("fix_loop")),
            })
        elif ntype == "script":
            entry.update({"script_path": n.get("script_path")})
        node_skeleton.append(entry)
    return {
        "entry_type": "format_contract",
        "spec_version_id": spec.spec_version_id,
        "run_dir": str(run_dir),
        "intent": spec.intent,
        "tags": tags or [],
        "node_skeleton": node_skeleton,
        "binding_pattern": list(spec.success_evidence),
        "control_flow_shape": f"{derive_spec_shape(spec)}|{spec.control_flow.get('type', '?')}",
        "learned": learned or "",
    }

