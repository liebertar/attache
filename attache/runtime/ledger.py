"""Append-only. Written before the action, closed after it. No entry, no execution."""

import json
import threading
from pathlib import Path

from attache.core.models import Decision, LedgerEntry, Proposal


class Ledger:
    def __init__(self, path: str | Path = "ledger.jsonl", keep_in_memory: int = 200):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._recent: list[dict] = []
        self._keep = keep_in_memory

    def open_entry(self, proposal: Proposal, decision: Decision) -> LedgerEntry:
        entry = LedgerEntry(proposal=proposal.to_dict(), decision=decision.to_dict())
        self._append(entry.to_dict())
        return entry

    def close_entry(self, entry: LedgerEntry, outcome: str) -> None:
        entry.outcome = outcome
        self._append(entry.to_dict())

    def _append(self, payload: dict) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._recent.append(payload)
            del self._recent[: max(0, len(self._recent) - self._keep)]

    def tail(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return list(reversed(self._recent[-limit:]))
