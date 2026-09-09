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
# 모델 설정은 기체 에이전트와 런타임 양쪽에 그대로 갑니다. 여기서 export 해 두어야
# 아래 백그라운드 프로세스들이 같은 값을 봅니다(비어 있으면 규칙만으로 돕니다).
export LLM_BASE_URL="${LLM_BASE_URL:-}" NEBIUS_API_KEY="${NEBIUS_API_KEY:-}"
export MODEL_NANO="${MODEL_NANO:-}" MODEL_SUPER="${MODEL_SUPER:-}" MODEL_ULTRA="${MODEL_ULTRA:-}"
export LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-}" LLM_RECORD_DIR="${LLM_RECORD_DIR:-}"
[ -n "${LLM_TIMEOUT_S:-}" ] && export LLM_TIMEOUT_S

# 기체마다 자기 모델 서버: LLM_PER_ASSET_URLS 에 base URL 을 공백으로 나눠 기체 순서대로
# (scripts/ollama_fleet.sh 가 띄운 로컬 4B 들). Ollama 는 이 모델 계열에 동시 처리 1을 강제해
# 서버 하나를 넷이 나누면 초안이 줄을 서서 잘립니다. 런타임(중재·공지)은 그대로 LLM_BASE_URL 을
# 씁니다 — 거기가 Ollama 이고 MODEL_SUPER 가 비어 있으면 LOCAL_SUPER_STANDIN(기본 4B)이
# Nemotron 3 Super 의 로컬 대역입니다. 진짜 Super 는 Nebius 의 nvidia/nemotron-3-super-120b-a12b.
is_ollama_url() { case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in *:1143[0-9]*|*ollama*) return 0 ;; esac; return 1; }
read -r -a PER_ASSET_URLS <<< "${LLM_PER_ASSET_URLS:-}"
# 30B 는 기본 문맥(262k)으로 올리면 26 GB 를 물어, 4B 함대 넷과 시험·브라우저를 같이 띄운 64 GB Mac 에서
# 메모리가 바닥났습니다(대기 셸이 죽음). 기본 대역은 4B 로 두고, 여유가 있으면
# LOCAL_SUPER_STANDIN=nemotron-3-nano:latest 로 바꿉니다(11434 서버는 OLLAMA_CONTEXT_LENGTH=8192 권장).
LOCAL_SUPER_STANDIN="${LOCAL_SUPER_STANDIN:-nemotron-3-nano:4b}"
LOCAL_NANO_DEFAULT=nemotron-3-nano:4b
# 대역은 런타임 줄에만 줍니다. 전역으로 export 했더니 기체 프로세스도 받아, 급한 신청서
# (배터리 25% 미만 등)를 자기 4B 함대 서버에 30B 로 물었습니다 — 그 서버는 30B 를 새로
# 올리느라(18~24 GB) 슬롯을 잡고, 6초 안에 답도 못 합니다.
RUNTIME_SUPER="$MODEL_SUPER"
if [ "${#PER_ASSET_URLS[@]}" -gt 0 ] && [ -z "$MODEL_SUPER" ] && is_ollama_url "$LLM_BASE_URL"; then
  RUNTIME_SUPER="$LOCAL_SUPER_STANDIN"
fi

cleanup() { pkill -P $$ || true; }
trap cleanup EXIT INT TERM

PORT=8100 TICK_SECONDS="${TICK_SECONDS:-0.2}" FLEET_LIMIT_USD=720 \
  python3 -m sim.service & sleep 1
PORT=8000 CONFIG=configs/fleet.yaml SIM_URL=http://localhost:8100 MODEL_SUPER="$RUNTIME_SUPER" \
  LEDGER_PATH=.run/ledger.jsonl python3 -m attache.runtime.service & sleep 1

# 직결 세계도 같은 신청서 작성기를 쓰지만, 로컬 Ollama 한 슬롯을 프로세스 8개가 나누면 런타임 쪽
# 경로 초안이 굶습니다(라이브에서 초안 0건). 기본은 직결 쪽만 규칙으로 쓰고, DIRECT_LLM=1 이면 같이 켭니다.
DIRECT_LLM_URL="${LLM_BASE_URL}"
[ "${DIRECT_LLM:-0}" = "1" ] || DIRECT_LLM_URL=""
index=0
AGENT_LINES=""
for asset in drone-01 drone-02 drone-03 drone-04; do
  agent_url="${PER_ASSET_URLS[$index]:-$LLM_BASE_URL}"
  agent_nano="$MODEL_NANO"
  if [ -n "${PER_ASSET_URLS[$index]:-}" ] && [ -z "$agent_nano" ] && is_ollama_url "$agent_url"; then
    agent_nano="$LOCAL_NANO_DEFAULT"
  fi
  ASSET_ID=$asset RUNTIME_URL=http://localhost:8000 LLM_BASE_URL="$agent_url" MODEL_NANO="$agent_nano" \
    MODEL_SUPER="$MODEL_SUPER" python3 -m attache.agent.loop &
  ASSET_ID=$asset TRANSPORT=http SIM_URL=http://localhost:8100 LLM_BASE_URL="$DIRECT_LLM_URL" \
    python3 -m direct_agent.loop &
  [ -n "${PER_ASSET_URLS[$index]:-}" ] && AGENT_LINES="${AGENT_LINES}    ${asset}: ${agent_url} (nano=${agent_nano} super=${MODEL_SUPER:-없음})
"
  index=$((index + 1))
done

python3 scripts/serve_ui.py 3100 ui >/dev/null 2>&1 &
echo
echo "  화면: http://localhost:3100"
echo "  런타임: http://localhost:8000/state   세계: http://localhost:8100/compare"
if [ -n "${LLM_BASE_URL}" ]; then
  echo "  모델: ${LLM_BASE_URL} (nano=${MODEL_NANO:-configs/fleet.yaml} super=${RUNTIME_SUPER:-configs/fleet.yaml}, 런타임)"
  [ -n "$AGENT_LINES" ] && printf '  기체별 모델 서버:\n%s' "$AGENT_LINES"
else
  echo "  모델: 없음 — 규칙만으로 돕니다 (.env.example 참고)"
fi
echo
wait
