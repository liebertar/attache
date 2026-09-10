"""OpenAI-compatible client with three Nemotron tiers.

The model never decides anything. It fills in a form (nano/super), drafts a route that a
judge will read (nano), or returns one index from a list the runtime built (ultra).
Anything else is discarded and the caller falls back to rules. That is what keeps this
side of the system non-authoritative.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from attache.core.http import post_json_status


class LlmTier(str, Enum):
    NANO = "nano"      # 이상하다 → 신청서를 씁니다 · 길을 그려야 한다 → 초안을 냅니다
    SUPER = "super"    # 원인을 알아내야 한다 → 근거를 붙입니다
    ULTRA = "ultra"    # 신청이 겹친다 → 통과한 것 중 하나를 고릅니다


# 답을 못 받으면 규칙이 대신합니다. 그러니 오래 기다릴 이유가 없습니다. 런타임은 20초,
# 기체 에이전트는 6초(loop.build_llm) — 거절 표시가 5.6초라 그보다 길면 화면이 멈춘 듯 보입니다.
DEFAULT_TIMEOUT_S = 20.0

# 생각하는 모델은 답 앞에 <think>…</think> 를 붙이기도 합니다. 그 안의 JSON 은 답이 아니라
# 생각이라 버립니다. 닫는 태그가 없으면 생각만 하다 끊긴 것이고, 그건 전부 생각입니다.
THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


@dataclass
class LlmReply:
    text: str
    model: str
    latency_ms: int = 0
    # 답이 어느 필드에서 왔나: content | think-stripped | reasoning_content | reasoning
    via: str = "content"


@dataclass
class TierStats:
    ok: int = 0            # 답을 받아 그대로 쓴 횟수
    fallback: int = 0      # 답이 없거나 버려서 규칙이 대신한 횟수
    last_ms: int | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "fallback": self.fallback, "last_ms": self.last_ms}


def strip_think(text: str) -> str:
    """앞머리의 <think>…</think> 를 떼고 답만 남깁니다. 열기만 하고 안 닫혔으면 전부 생각입니다."""
    if not text:
        return ""
    stripped = THINK_BLOCK.sub("", text, count=1)
    if stripped == text and text.lstrip().startswith("<think>"):
        return ""
    return stripped


def host_of(base_url: str) -> str:
    """어느 서버에 묻고 있나. 화면 헤더 한 줄과 기록용입니다."""
    url = (base_url or "").lower()
    if not url:
        return "none"
    # 11434 는 기본 서버, 11435.. 는 기체마다 하나씩 띄운 함대(scripts/ollama_fleet.sh)입니다.
    if re.search(r":1143\d(?!\d)", url) or "ollama" in url:
        return "ollama"
    if "nebius" in url:
        return "nebius"
    return "other"


def _extra_from_env() -> dict:
    """서버마다 다른 인자(예: Ollama 는 reasoning_effort=none 이라야 생각을 안 함).

    코드에 서버 이름으로 분기해 두면 서버가 바뀔 때마다 코드를 고쳐야 합니다. JSON 한 줄을
    환경에서 받아 요청에 그대로 섞습니다. 잘못된 JSON 은 조용히 무시하지 않고 알립니다.
    """
    raw = os.getenv("LLM_REQUEST_EXTRA", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"LLM_REQUEST_EXTRA 가 JSON 이 아닙니다: {raw!r}", flush=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


class TieredLlm:
    def __init__(self, base_url: str = "", api_key: str = "", models: dict | None = None,
                 timeout_s: float | None = None, request_extra: dict | None = None,
                 record_dir: str | None = None):
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("NEBIUS_API_KEY", "")
        self.models = models or {}
        # compose 는 값이 없어도 빈 문자열을 넘깁니다. 빈 값은 없는 값입니다.
        self.timeout_s = float(os.getenv("LLM_TIMEOUT_S") or str(DEFAULT_TIMEOUT_S)
                               if timeout_s is None else timeout_s)
        self.request_extra = _extra_from_env() if request_extra is None else dict(request_extra)
        self.record_dir = os.getenv("LLM_RECORD_DIR", "") if record_dir is None else record_dir
        self.stats: dict[str, TierStats] = {tier.value: TierStats() for tier in LlmTier}
        # response_format 을 모르는 서버는 400 으로 답합니다. 한 번 그러면 다시 안 보냅니다.
        self._json_mode_ok = True
        self._recorded = 0
        # 마지막으로 서버에 닿지 못한(타임아웃·연결 실패) 시각(monotonic). 답을 받으면 지웁니다.
        # 부르는 쪽이 "방금 못 받은 서버에 또 긴 질문을 걸 것인가" 를 정하는 근거입니다.
        self.unreachable_at: float | None = None
        # 장부(stats·기록 번호)만 잠급니다. HTTP 호출은 잠그지 않습니다 — 기체 에이전트는 경로
        # 초안을 작업 스레드에서 묻는 동안 본 스레드가 신청서를 물을 수 있고, 둘이 같은
        # 클라이언트를 씁니다. 잠그지 않으면 `ok += 1` 이 서로를 덮고, 기록 파일 번호가 겹쳐
        # 한 호출이 다른 호출을 지웁니다.
        self._books = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.models)

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def model_for(self, tier: LlmTier) -> str:
        return self.models.get(tier.value, "")

    def ask(self, tier: LlmTier, system: str, user: str, max_tokens: int = 400,
            json_object: bool = False, timeout_s: float | None = None) -> LlmReply | None:
        """한 번 묻습니다. timeout_s 는 이 질문만의 예산 (초안은 신청서보다 오래 걸립니다)."""
        model = self.model_for(tier)
        if not self.enabled or not model:
            return None
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
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
        wants_json = json_object and self._json_mode_ok
        if wants_json:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.request_extra)

        started = time.monotonic()
        url = f"{self.base_url}/chat/completions"
        status, response = post_json_status(url, payload, timeout=budget, headers=headers)
        if wants_json and status == 400:
            # JSON 강제를 모르는 서버입니다. 인자를 빼고 한 번만 더 냅니다. 타임아웃(0)은 다시
            # 내봐야 또 기다리기만 하니 그대로 포기합니다.
            self._json_mode_ok = False
            retry = {k: v for k, v in payload.items() if k != "response_format"}
            status, response = post_json_status(url, retry, timeout=budget, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        reply = reply_from(response, model, latency_ms)
        self.unreachable_at = time.monotonic() if status == 0 else None
        self.account(tier, reply, latency_ms)
        self.record(tier, model, system, user, reply, latency_ms)
        return reply

    def unreachable_within(self, seconds: float) -> bool:
        """최근 seconds 초 안에 서버에 닿지 못했나. 그 사이에 답을 받았으면 False."""
        return (self.unreachable_at is not None
                and time.monotonic() - self.unreachable_at < seconds)

    def discard(self, tier: LlmTier) -> None:
        """받긴 했는데 양식이 아니라 버렸습니다. 규칙이 대신한 것으로 셉니다."""
        with self._books:
            stats = self.stats[tier.value]
            stats.ok = max(0, stats.ok - 1)
            stats.fallback += 1

    def account(self, tier: LlmTier, reply: LlmReply | None, latency_ms: int) -> None:
        with self._books:
            stats = self.stats[tier.value]
            stats.last_ms = latency_ms
            if reply is None:
                stats.fallback += 1
            else:
                stats.ok += 1

    def record(self, tier: LlmTier, model: str, system: str, user: str,
               reply: LlmReply | None, latency_ms: int) -> None:
        """LLM_RECORD_DIR 이 있으면 호출을 하나씩 파일로 남깁니다. 시험 fixture 의 원료입니다."""
        if not self.record_dir:
            return
        with self._books:
            self._recorded += 1
            serial = self._recorded
        payload = {
            "tier": tier.value, "model": model, "system": system, "user": user,
            "text": reply.text if reply else None, "via": reply.via if reply else None,
            "latency_ms": latency_ms, "at": time.time(),
        }
        try:
            folder = Path(self.record_dir)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{os.getpid()}-{serial:04d}-{tier.value}.json"
            (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except OSError as error:
            print(f"LLM_RECORD_DIR 에 못 씁니다: {error}", flush=True)

    def stats_dict(self) -> dict:
        return {tier: stats.to_dict() for tier, stats in self.stats.items()}


def reply_from(response: dict | None, model: str, latency_ms: int = 0) -> LlmReply | None:
    """답을 꺼냅니다. content 가 먼저, 비어 있으면 reasoning_content, 그다음 reasoning.

    생각하는 모델은 생각이 max_tokens 를 다 먹어 content 가 비기도 합니다. 그때 생각 필드에
    답이 들어 있으면 그거라도 씁니다 — 어차피 양식 검사와 판정을 다시 거칩니다.
    """
    if not response:
        return None
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    text, via = strip_think(content).strip(), "content"
    if text and text != content.strip():
        via = "think-stripped"
    if not text:
        for name in ("reasoning_content", "reasoning"):
            candidate = message.get(name)
            if isinstance(candidate, str) and candidate.strip():
                text, via = candidate.strip(), name
                break
    if not text:
        return None
    return LlmReply(text=text, model=model, latency_ms=latency_ms, via=via)


def parse_json_object(text: str) -> dict | None:
    """모델이 문장을 섞어 보내도 객체 하나만 건집니다. 못 건지면 버립니다.

    <think> 안의 JSON 은 답이 아닙니다. 생각 속에서 {"legs": …} 를 쓰고 답은 안 쓴 경우가
    있어서, 먼저 생각을 떼고 남은 것에서만 찾습니다.
    """
    text = strip_think(text or "")
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
    """목록에 있는 번호 하나만 받습니다. 번호만 있어야 하고, 범위를 벗어나면 버립니다.

    문장 속의 숫자를 건지던 때는 "7번은 안 되고 2번으로" 같은 답에서 7을 집었습니다.
    번호 하나(앞에 #, 뒤에 마침표 정도)만 답으로 칩니다. 문장은 규칙이 대신합니다.
    """
    if not text:
        return None
    match = re.fullmatch(r"\s*#?\s*(\d+)\s*[.)]?\s*", strip_think(text))
    if not match:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < option_count else None
