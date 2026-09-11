"""A model that answers from recorded replies. Offline, deterministic, and real.

`tests/fixtures/llm/*.json` holds replies the local Ollama Nemotron actually gave, promoted
from LLM_RECORD_DIR dumps with a `needle` added: a substring of the user prompt that says
which question the reply belongs to. Matching is by tier and needle, so a small change in
prompt wording does not orphan a fixture, and a fixture never answers a question it was
not recorded for — then the caller gets None and the rules take over, exactly as when the
server is down.
"""

import json
import pathlib

from shared.llm.client import LlmReply, LlmTier, TieredLlm

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "llm"


def load_fixtures(folder: pathlib.Path = FIXTURE_DIR) -> list[dict]:
    records = []
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if item.get("needle") and item.get("text") is not None:
                records.append({**item, "_file": path.name})
    return records


class FixtureLlm(TieredLlm):
    """티어와 바늘(needle)로 녹음된 답을 돌려줍니다. 맞는 것이 없으면 None — 규칙 차례입니다."""

    def __init__(self, records: list[dict] | None = None, model: str = "nemotron-3-nano",
                 tiers: tuple[str, ...] = ("nano", "super", "ultra")):
        super().__init__(base_url="http://fixture", api_key="fixture",
                         models={tier: model for tier in tiers}, timeout_s=0.0,
                         request_extra={}, record_dir="")
        self.records = load_fixtures() if records is None else list(records)
        self.asked: list[tuple[str, str]] = []     # (tier, user) — 무엇을 물었는지 시험이 봅니다
        self.served: list[str] = []                # 어느 fixture 가 답했는지

    def ask(self, tier: LlmTier, system: str, user: str, max_tokens: int = 400,
            json_object: bool = False, timeout_s: float | None = None) -> LlmReply | None:
        self.asked.append((tier.value, user))
        haystack = system + "\n" + user
        for record in self.records:
            if record.get("tier", tier.value) != tier.value:
                continue
            if record["needle"] in haystack:
                self.served.append(record.get("_file", record["needle"]))
                reply = LlmReply(text=record["text"],
                                 model=record.get("model") or self.model_for(tier),
                                 latency_ms=int(record.get("latency_ms") or 0),
                                 via=record.get("via") or "content")
                self.account(tier, reply, reply.latency_ms)
                return reply
        self.account(tier, None, 0)
        return None
