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

    def open_entry(self, proposal: Proposal, decision: Decision,
                   context: dict | None = None) -> LedgerEntry:
        entry = LedgerEntry(proposal=proposal.to_dict(), decision=decision.to_dict(),
                            context=dict(context or {}))
        self._append(entry.to_dict())
        return entry

    def close_entry(self, entry: LedgerEntry, outcome: str,
                    decision: Decision | None = None, context: dict | None = None) -> None:
        """실행이 끝난 뒤의 결정문을 다시 담습니다.

        열 때 찍은 사본은 아직 실행 전 상태입니다. 그대로 닫으면 원장이
        "결과는 done 인데 실행은 안 했다"고 적힙니다. 기록이 거짓이면 기록이 아닙니다.
        실행하면서 알게 된 맥락(만들어진 의도 id)은 닫는 줄에 더합니다.
        """
        if decision is not None:
            entry.decision = decision.to_dict()
        if context:
            entry.context = {**entry.context, **context}
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

    def read_all(self) -> list[dict]:
        """파일의 전부, 적힌 순서대로. 보고서는 기억이 아니라 파일에서 만듭니다 — 기억은 200줄뿐."""
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(encoding="utf-8") as handle:
                lines = [line for line in handle if line.strip()]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue    # 반쯤 적힌 마지막 줄. 보고서 하나 때문에 원장을 못 읽어서는 안 됩니다
        return out
