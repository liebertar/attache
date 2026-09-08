# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.2

# ---------------------------------------------------------------------------
# agent: adapters 와 runtime 을 아예 복사하지 않습니다.
# 에이전트 컨테이너 안에는 모터를 끄는 코드가 물리적으로 존재하지 않습니다.
# ---------------------------------------------------------------------------
FROM base AS agent
COPY attache/__init__.py /app/attache/__init__.py
COPY attache/core /app/attache/core
COPY attache/llm /app/attache/llm
COPY attache/agent /app/attache/agent
RUN test ! -e /app/attache/adapters \
 && test ! -e /app/attache/runtime \
 && test ! -e /app/direct_agent
CMD ["python", "-m", "attache.agent.loop"]

# ---------------------------------------------------------------------------
# agent-direct: 오늘 대부분의 배선. 조종장치 클라이언트를 스스로 들고 있습니다.
# 위 이미지와 다른 이미지라는 게 요점입니다.
# ---------------------------------------------------------------------------
FROM base AS agent-direct
RUN pip install --no-cache-dir pymavlink==2.4.49
COPY attache/__init__.py /app/attache/__init__.py
COPY attache/core /app/attache/core
COPY attache/llm /app/attache/llm
COPY attache/agent /app/attache/agent
COPY direct_agent /app/direct_agent
CMD ["python", "-m", "direct_agent.loop"]

# ---------------------------------------------------------------------------
# runtime: 유일하게 adapters 를 가진 이미지
# ---------------------------------------------------------------------------
FROM base AS runtime
RUN pip install --no-cache-dir pymavlink==2.4.49
COPY attache /app/attache
COPY configs /app/configs
CMD ["python", "-m", "attache.runtime.service"]

# ---------------------------------------------------------------------------
# sim: 세계. 시키는 대로만 합니다
# ---------------------------------------------------------------------------
FROM base AS sim
COPY attache/__init__.py /app/attache/__init__.py
COPY attache/core /app/attache/core
COPY sim /app/sim
CMD ["python", "-m", "sim.service"]

# ---------------------------------------------------------------------------
# ui: 승인 화면. 정적 파일 하나
# ---------------------------------------------------------------------------
FROM base AS ui
COPY ui /app/ui
WORKDIR /app/ui
CMD ["python", "-m", "http.server", "3000"]
