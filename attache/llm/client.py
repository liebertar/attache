"""OpenAI-compatible client with three Nemotron tiers.

The model never decides anything. It fills in a form (nano/super) or returns one index
from a list the runtime built (ultra). Anything else is discarded and the caller falls
back to rules. That is what keeps this side of the system non-authoritative.
"""

import json
import os
import re
from dataclasses import dataclass
from enum import Enum

from attache.core.http import post_json


class LlmTier(str, Enum):
    NANO = "nano"      # 이상하다 → 신청서를 씁니다
    SUPER = "super"    # 원인을 알아내야 한다 → 근거를 붙입니다
    ULTRA = "ultra"    # 신청이 겹친다 → 통과한 것 중 하나를 고릅니다


@dataclass
class LlmReply:
    text: str
    model: str


class TieredLlm:
    def __init__(self, base_url: str = "", api_key: str = "", models: dict | None = None):
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("NEBIUS_API_KEY", "")
        self.models = models or {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.models)

    def model_for(self, tier: LlmTier) -> str:
        return self.models.get(tier.value, "")

    def ask(self, tier: LlmTier, system: str, user: str, max_tokens: int = 400) -> LlmReply | None:
        model = self.model_for(tier)
        if not self.enabled or not model:
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        response = post_json(f"{self.base_url}/chat/completions", payload, headers=headers)
        if not response:
            return None
        try:
            return LlmReply(text=response["choices"][0]["message"]["content"], model=model)
        except (KeyError, IndexError, TypeError):
            return None


def parse_json_object(text: str) -> dict | None:
    """모델이 문장을 섞어 보내도 객체 하나만 건집니다. 못 건지면 버립니다."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        candidate = text[start : end + 1] if 0 <= start < end else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_choice(text: str, option_count: int) -> int | None:
    """목록에 있는 번호 하나만 받습니다. 범위를 벗어나면 버립니다."""
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    index = int(match.group()) - 1
    return index if 0 <= index < option_count else None
