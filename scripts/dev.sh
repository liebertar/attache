#!/usr/bin/env bash
# Docker 없이 같은 스택을 로컬 프로세스로 띄웁니다. 종료는 Ctrl-C.
# 플래그가 없습니다 — 모델·정보 수집 경로는 scripts/resolve_stack.sh 가 떠 있는 것을 보고 고릅니다
# (Nebius 키 → Ollama 함대 → Ollama 하나 → 규칙). 고른 결과는 시작할 때 표 한 장으로 찍습니다.
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
    # 값을 감싼 따옴표 한 쌍은 벗깁니다. .env 는 셸이 아니라 따옴표가 값에 그대로 들어가고,
    # LLM_PER_ASSET_URLS="a b" 로 적으면 첫 기체가 "a 를, 마지막 기체가 b" 를 받아 모델 없이 돕니다.
    value="${line#*=}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    export "$key=$value"
  done < .env
fi

# 모델·정보 수집 경로를 고릅니다. 결과는 STACK_MODEL, LLM_BASE_URL(런타임), PER_ASSET_URLS(기체마다),
# RUNTIME_SUPER, MODEL_*, STACK_METAR/STACK_TAVILY, INTAKE_DB, DIRECT_* 입니다.
# shellcheck source=scripts/resolve_stack.sh
. scripts/resolve_stack.sh
resolve_stack

# 모델 설정은 기체 에이전트와 런타임 양쪽에 그대로 갑니다. 여기서 export 해 두어야
# 아래 백그라운드 프로세스들이 같은 값을 봅니다(비어 있으면 규칙만으로 돕니다).
export LLM_BASE_URL NEBIUS_API_KEY MODEL_NANO MODEL_SUPER MODEL_ULTRA
export LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-}" LLM_RECORD_DIR="${LLM_RECORD_DIR:-}"
[ -n "${LLM_TIMEOUT_S:-}" ] && export LLM_TIMEOUT_S
# 정보 수집. Tavily 는 키가 있을 때만 돌고, METAR 는 런타임이 주기마다 묻습니다(닿지 못하면 원장에 한 줄).
export TAVILY_API_KEY="${TAVILY_API_KEY:-}" INTAKE_DB
[ -n "${METAR:-}" ] && export METAR
[ -n "${METAR_STATIONS:-}" ] && export METAR_STATIONS
[ -n "${METAR_PERIOD_S:-}" ] && export METAR_PERIOD_S

cleanup() { pkill -P $$ || true; }
trap cleanup EXIT INT TERM

# 프로세스끼리는 127.0.0.1 로 부릅니다. 이 Mac 에서 localhost 는 ::1 이 먼저이고, Docker 컨테이너가
# 같은 포트를 [::1] 에 열면(dynamodb-local 8100, carter-agent 8000) 런타임이 시뮬레이터 대신 그것을
# 폴링해 틱이 멈춥니다 — 실제로 틱 2618 에 얼어 있었습니다.
LOOPBACK=127.0.0.1
# 포트는 바꿀 수 있습니다(둘째 스택, 또는 기본 포트를 다른 것이 쥐고 있을 때): RT_PORT SIM_PORT UI_PORT.
RT_PORT="${RT_PORT:-8000}" SIM_PORT="${SIM_PORT:-8100}" UI_PORT="${UI_PORT:-3100}"
# 직결 세계의 모델은 시뮬레이터가 기체마다 싣습니다(DIRECT_MODEL) — 그 에이전트들은 런타임에 등록할
# 길이 없는 배선이라, 띄우는 여기서 알려 줍니다.
PORT=$SIM_PORT TICK_SECONDS="${TICK_SECONDS:-0.2}" FLEET_LIMIT_USD=720 DIRECT_MODEL="$DIRECT_MODEL" \
  python3 -m sim.service & sleep 1
# 둘째 스택은 원장도 따로 둡니다(LEDGER_PATH, INTAKE_DB) — 같은 파일에 두 런타임이 쓰면 보고서가 섞입니다.
PORT=$RT_PORT CONFIG=configs/fleet.yaml SIM_URL=http://$LOOPBACK:$SIM_PORT MODEL_SUPER="$RUNTIME_SUPER" \
  LEDGER_PATH="${LEDGER_PATH:-.run/ledger.jsonl}" python3 -m holdshort.runtime.service & sleep 1

# 기체 i 는 i 번째 서버(PER_ASSET_URLS, Ollama 함대)를, 없으면 LLM_BASE_URL 을 씁니다. Ollama 는 이
# 모델 계열에 동시 처리 1을 강제해 서버 하나를 넷이 나누면 초안이 줄을 서서 잘립니다. 대역(Super)은
# 런타임 줄에만 줍니다 — 기체에 가면 급한 신청서를 자기 4B 서버에 30B 로 묻습니다.
# 직결 세계도 같은 신청서 작성기를 쓰지만, 로컬 Ollama 한 슬롯을 프로세스 8개가 나누면 런타임 쪽
# 경로 초안이 굶습니다(라이브에서 초안 0건). 기본은 직결 쪽만 규칙으로 쓰고, DIRECT_LLM=1 이면 같이 켭니다.
index=0
for asset in drone-01 drone-02 drone-03 drone-04; do
  agent_url="${PER_ASSET_URLS[$index]:-$LLM_BASE_URL}"
  agent_nano="$(agent_nano_for "$agent_url")"
  ASSET_ID=$asset RUNTIME_URL=http://$LOOPBACK:$RT_PORT LLM_BASE_URL="$agent_url" MODEL_NANO="$agent_nano" \
    MODEL_SUPER="$MODEL_SUPER" python3 -m holdshort.agent.loop &
  ASSET_ID=$asset TRANSPORT=http SIM_URL=http://$LOOPBACK:$SIM_PORT LLM_BASE_URL="$DIRECT_LLM_URL" \
    MODEL_NANO="$DIRECT_NANO" python3 -m direct_agent.loop &
  index=$((index + 1))
done

python3 scripts/serve_ui.py "$UI_PORT" ui >/dev/null 2>&1 &
echo
print_stack_table
UI_QUERY=""
[ "$RT_PORT$SIM_PORT" = "80008100" ] || UI_QUERY="?rt=$RT_PORT&sim=$SIM_PORT"
echo "  screen  http://$LOOPBACK:$UI_PORT/map.html$UI_QUERY   (localhost 는 ::1 이 먼저라 Docker 가 같은 포트를 열면 엉뚱한 곳)"
echo "  state   http://$LOOPBACK:$RT_PORT/state   world http://$LOOPBACK:$SIM_PORT/compare"
echo
wait
