#!/usr/bin/env bash
# Docker 없이 같은 스택을 로컬 프로세스로 띄웁니다. 종료는 Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
mkdir -p .run

# .env 가 있으면 읽습니다(.env.example 참고). 이미 환경에 있는 값이 우선입니다 —
# 셸에서 LLM_BASE_URL=… ./scripts/dev.sh 로 한 번만 바꿔 띄울 수 있어야 합니다.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    [ -n "${!key:-}" ] && continue
    export "$line"
  done < .env
fi
# 모델 설정은 기체 에이전트와 런타임 양쪽에 그대로 갑니다. 여기서 export 해 두어야
# 아래 백그라운드 프로세스들이 같은 값을 봅니다(비어 있으면 규칙만으로 돕니다).
export LLM_BASE_URL="${LLM_BASE_URL:-}" NEBIUS_API_KEY="${NEBIUS_API_KEY:-}"
export MODEL_NANO="${MODEL_NANO:-}" MODEL_SUPER="${MODEL_SUPER:-}" MODEL_ULTRA="${MODEL_ULTRA:-}"
export LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-}" LLM_RECORD_DIR="${LLM_RECORD_DIR:-}"
[ -n "${LLM_TIMEOUT_S:-}" ] && export LLM_TIMEOUT_S

cleanup() { pkill -P $$ || true; }
trap cleanup EXIT INT TERM

PORT=8100 TICK_SECONDS="${TICK_SECONDS:-0.2}" FLEET_LIMIT_USD=720 \
  python3 -m sim.service & sleep 1
PORT=8000 CONFIG=configs/fleet.yaml SIM_URL=http://localhost:8100 \
  LEDGER_PATH=.run/ledger.jsonl python3 -m attache.runtime.service & sleep 1

# 직결 세계도 같은 신청서 작성기를 쓰지만, 로컬 Ollama 한 슬롯을 프로세스 8개가 나누면 런타임 쪽
# 경로 초안이 굶습니다(라이브에서 초안 0건). 기본은 직결 쪽만 규칙으로 쓰고, DIRECT_LLM=1 이면 같이 켭니다.
DIRECT_LLM_URL="${LLM_BASE_URL}"
[ "${DIRECT_LLM:-0}" = "1" ] || DIRECT_LLM_URL=""
for asset in drone-01 drone-02 drone-03 drone-04; do
  ASSET_ID=$asset RUNTIME_URL=http://localhost:8000 python3 -m attache.agent.loop &
  ASSET_ID=$asset TRANSPORT=http SIM_URL=http://localhost:8100 LLM_BASE_URL="$DIRECT_LLM_URL" \
    python3 -m direct_agent.loop &
done

python3 scripts/serve_ui.py 3100 ui >/dev/null 2>&1 &
echo
echo "  화면: http://localhost:3100"
echo "  런타임: http://localhost:8000/state   세계: http://localhost:8100/compare"
if [ -n "${LLM_BASE_URL}" ]; then
  echo "  모델: ${LLM_BASE_URL} (nano=${MODEL_NANO:-configs/fleet.yaml})"
else
  echo "  모델: 없음 — 규칙만으로 돕니다 (.env.example 참고)"
fi
echo
wait
