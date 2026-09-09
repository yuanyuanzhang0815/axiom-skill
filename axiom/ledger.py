"""Hash-chained execution ledger (one of the two truth sources).

events.jsonl is append-only and hash-chained (seq + prev_event_hash +
event_hash). The chain is tamper-EVIDENT under a trusted root (not
tamper-proof): verify_chain() detects any in-place mutation of a stored
event. run_manifest.json holds the final hash so a trusted root can anchor
the chain.

Provenance: decision_trace evidence_refs point forward (decision -> event).
The reverse index evidence_for (event -> decisions) is DERIVED, never written
by agents. Referential integrity is checked: orphan evidence refs are flagged.
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


def _event_hash(event: dict) -> str:
    """sha256 over the event minus its own event_hash field."""
    payload = {k: v for k, v in event.items() if k != "event_hash"}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class Ledger:
    """Append-only hash-chained event store rooted at run_dir/events.jsonl."""

    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.manifest_path = self.run_dir / "run_manifest.json"
        # A1: concurrent dispatchers (parallel/pipeline) append from multiple
        # threads. seq + prev_event_hash must be assigned atomically w.r.t. the
        # last event, or two appends race to the same seq / prev hash and the
        # chain corrupts. The lock makes the whole assign-and-write critical
        # section atomic; reads (events/verify_chain) stay lock-free snapshots.
        self._lock = threading.Lock()

    # --- append ----------------------------------------------------

    def append(self, event: dict) -> dict:
        """Append an event, filling seq/prev_hash/ts/event_hash. Returns the sealed event."""
        with self._lock:
            event["seq"] = self._next_seq()
            event["prev_event_hash"] = self._last_hash()
            event["ts"] = event.get("ts") or _now_iso()
            event["event_hash"] = _event_hash(event)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    # --- read ------------------------------------------------------

    def _lines(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _last_hash(self) -> str:
        lines = self._lines()
        return lines[-1]["event_hash"] if lines else _GENESIS_HASH

    def _next_seq(self) -> int:
        return len(self._lines()) + 1

    def events(self) -> list[dict]:
        return self._lines()

    # --- reset ---------------------------------------------------

    def reset(self) -> None:
        """Truncate events.jsonl + run_manifest.json (start clean).

        For `axiom run --fresh`. Verdict projection is scoped by
        spec_version_id (no stale-leak across versions), but cost_usd_total /
        dispatch_count still scan all events in the dir, and a re-run of the
        SAME svid appends duplicate events. Reset gives a clean truth source
        for a fresh run. verdict.json / packet.md are overwritten by
        materialize() on the next run; checkpoints.jsonl is append-only and
        cosmetic (not read by derive_verdict), so a stale tail there is harmless.
        """
        with self._lock:
            self.events_path.unlink(missing_ok=True)
            self.manifest_path.unlink(missing_ok=True)

    # --- verify ---------------------------------------------------

    def verify_chain(self) -> list[str]:
        """Return list of chain breakages; empty == intact.

        Detects: seq gaps, prev_hash link breaks, event_hash mismatch
        (in-place tamper of a stored event's content), and (P2-10 + R5-1)
        seal anchors whose target event is no longer in the chain -- a
        sealed ledger whose tail was truncated passes the per-event checks
        above (the surviving chain is internally consistent) but its seal
        anchor(s) are broken. Resume re-seals after re-walk; R5-1 keeps every
        superseded anchor in anchor_history, so a legitimately appended +
        re-sealed ledger still validates (old anchors hash earlier events),
        and only a truncated/rewritten tail trips this check.
        """
        errs: list[str] = []
        prev = _GENESIS_HASH
        lines = self._lines()
        for i, e in enumerate(lines):
            if e.get("seq") != i + 1:
                errs.append(f"seq break at {e.get('event_id')}: {e.get('seq')} != {i + 1}")
            if e.get("prev_event_hash") != prev:
                errs.append(f"prev_hash break at {e.get('event_id')}")
            stored = e.get("event_hash")
            recomputed = _event_hash(e)
            if stored != recomputed:
                errs.append(f"event_hash mismatch at {e.get('event_id')} (tamper)")
            prev = e["event_hash"]
        # P2-10 + R5-1: seal anchor(s) must be present in the surviving chain.
        # A sealed ledger whose tail was truncated keeps an internally-
        # consistent surviving chain but its anchor's target event is GONE;
        # the per-event loop above returns empty, masking the truncation.
        # R5-1: the manifest now also carries anchor_history (every
        # superseded anchor, append-only -- seals never launder old anchors).
        # A legitimate post-seal append + re-anchor keeps the old anchor in
        # history, so it still validates; only a genuinely truncated tail
        # drops an anchor's target. A pre-R5-1 manifest (no history) falls
        # back to checking final_event_hash against the current tail.
        if self.manifest_path.exists():
            try:
                manifest = json.loads(
                    self.manifest_path.read_text(encoding="utf-8"))
                present = {e["event_hash"] for e in lines}
                history = manifest.get("anchor_history")
                if history:
                    for anchor in history:
                        # The genesis hash anchors an EMPTY ledger; it never
                        # corresponds to an event, so it is always
                        # trivially "present" (skipping it is not a gap).
                        if (anchor is not None and anchor != _GENESIS_HASH
                                and anchor not in present):
                            errs.append(
                                f"seal manifest mismatch: historical anchor "
                                f"{anchor!r} has no event in the chain "
                                f"(sealed ledger tail truncated/rewritten)")
                else:
                    sealed_root = manifest.get("final_event_hash")
                    actual_root = lines[-1]["event_hash"] if lines else _GENESIS_HASH
                    if sealed_root != actual_root:
                        errs.append(
                            f"seal manifest mismatch: final_event_hash "
                            f"{sealed_root!r} != current tail {actual_root!r} "
                            f"(sealed ledger tail truncated or appended-to "
                            f"without re-seal)")
            except (json.JSONDecodeError, OSError):
                errs.append("seal manifest unreadable (corrupt run_manifest.json)")
        return errs

    def seal(self) -> None:
        """Write run_manifest.json with the final event hash (trusted root anchor).

        R5-1: seal NEVER discards previous anchors. The superseded
        final_event_hash is appended to anchor_history (deduped), so a
        truncation alarm that survived a gate/claim resolution (P-028 kept
        the stale anchor) also survives a later ordinary re-seal (the review5
        resume_after_resolution probe: resume re-sealed the post-resolution
        tail and the old anchor -- the only remaining proof of the truncated
        event -- vanished). verify_chain checks EVERY historical anchor
        against the surviving chain; a legitimate append keeps old anchors
        present (they hash earlier events), only a truncated/rewritten tail
        drops one."""
        lines = self._lines()
        root = lines[-1]["event_hash"] if lines else _GENESIS_HASH
        history: list = []
        try:
            if self.manifest_path.exists():
                old = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                history = list(old.get("anchor_history") or [])
                prev_root = old.get("final_event_hash")
                if prev_root is not None and prev_root not in history:
                    history.append(prev_root)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt manifest -> re-anchored fresh below (verify flags it)
        if root not in history:
            history.append(root)
        self.manifest_path.write_text(
            json.dumps({"final_event_hash": root, "anchor_history": history},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --- provenance -----------------------------------------------

    def check_refs(self, spec) -> list[str]:
        """Flag decision_trace evidence_refs that point at non-existent events."""
        errs: list[str] = []
        ids = {e["event_id"] for e in self.events()}
        for d in spec.decision_trace:
            for ref in d.get("evidence_refs", []):
                if ref.startswith("event:"):
                    eid = ref.split(":", 1)[1]
                    if eid not in ids:
                        errs.append(
                            f"orphan evidence: {d.get('decision_id')} -> {ref} (no such event)"
                        )
        return errs

    def derive_reverse_index(self, spec=None) -> dict[str, list[str]]:
        """Build the event -> decisions (evidence_for) index.

        If spec is given, rebuild purely from forward refs (canonical:
        decision -> evidence is the spec direction). Otherwise derive from
        the evidence_for field stored on each event.
        """
        idx: dict[str, list[str]] = {}
        if spec is not None:
            for d in spec.decision_trace:
                did = d.get("decision_id")
                for ref in d.get("evidence_refs", []):
                    if ref.startswith("event:"):
                        idx.setdefault(ref.split(":", 1)[1], []).append(did)
        else:
            for e in self.events():
                idx[e["event_id"]] = list(e.get("evidence_for", []))
        return idx
