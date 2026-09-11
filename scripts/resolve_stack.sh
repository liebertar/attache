#!/usr/bin/env bash
# 스택 설정을 스스로 고릅니다 — 플래그 없이. scripts/dev.sh 가 source 해서 resolve_stack 을 부르고,
# 혼자 돌리면 고른 결과를 표로 찍습니다(--env 면 KEY=VALUE 줄로).
#
# 모델 경로(먼저 맞는 것 하나):
#   1) LLM_BASE_URL 을 직접 줬으면 그것. 단 키 없는 Nebius 주소는 없는 것으로 봅니다 — 호출이 전부 401 이라
#      신청서는 규칙이 쓰면서 화면에는 모델 이름이 붙습니다(.env.example 을 그대로 복사하면 이렇게 됨).
#   2) NEBIUS_API_KEY 가 있으면 Nebius Token Factory + nvidia/... 모델 id.
#   3) Ollama 함대(11435..11438, scripts/ollama_fleet.sh)가 답하면 기체마다 4B. 런타임의 대역(stand-in)은
#      관제 서버(11439, 문맥 8k)가 답하면 거기, 아니면 11434 가 답할 때 거기(Ollama 앱은 문맥을 256k 로
#      잡아 같은 4B 에 KV 캐시를 5 GB 넘게 더 얹습니다).
#   4) 11434 만 답하면 Ollama 하나를 모두가.
#   5) 아무것도 없으면 규칙만.
# 정보 수집: Tavily 는 TAVILY_API_KEY 가 있을 때만, METAR 는 네트워크가 답할 때만 on 으로 표시합니다 —
# 런타임은 METAR 를 어차피 주기마다 다시 묻고 닿지 못하면 원장에 한 줄 남기므로, 여기서 끄지는
# 않습니다(나중에 네트워크가 돌아오면 저절로 켜짐). 시뮬레이터 공지는 언제나 on 입니다.
#
# 확인 주소는 바꿀 수 있습니다(시험이 가짜 서버로 돌림): OLLAMA_HOST_PROBE, OLLAMA_BASE_PORT,
# OLLAMA_FLEET_SIZE, OLLAMA_TOWER_PORT, METAR_PROBE_URL.

NEBIUS_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_NANO=nvidia/Nemotron-3_5-Lightning
NEBIUS_SUPER=nvidia/nemotron-3-super-120b-a12b
NEBIUS_ULTRA=nvidia/Nemotron-3-Ultra-550b-a55b
OLLAMA_HOST_PROBE="${OLLAMA_HOST_PROBE:-127.0.0.1}"
OLLAMA_BASE_PORT="${OLLAMA_BASE_PORT:-11434}"
OLLAMA_FLEET_SIZE="${OLLAMA_FLEET_SIZE:-4}"
OLLAMA_TOWER_PORT="${OLLAMA_TOWER_PORT:-11439}"
METAR_PROBE_URL="${METAR_PROBE_URL:-https://aviationweather.gov/api/data/metar?ids=KNYC&format=json}"
# 로컬 기본 모델. 30B 는 기본 문맥으로 26 GB 를 물어 4B 함대와 같이 못 올립니다(dev.sh 주석 참고).
LOCAL_NANO_DEFAULT="${LOCAL_NANO_DEFAULT:-nemotron-3-nano:4b}"
LOCAL_SUPER_STANDIN="${LOCAL_SUPER_STANDIN:-nemotron-3-nano:4b}"
# Ollama /v1 에서 생각을 끄는 인자(2026-09-09 확인). 켜 두면 답이 5~27초 늦습니다.
OLLAMA_REQUEST_EXTRA='{"reasoning_effort":"none"}'

ollama_up() { curl -sf --max-time 1 "http://$OLLAMA_HOST_PROBE:$1/api/version" >/dev/null 2>&1; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
is_nebius_url() { case "$(lower "$1")" in *nebius*) return 0 ;; esac; return 1; }
is_ollama_url() { case "$(lower "$1")" in *:1143[0-9]*|*ollama*) return 0 ;; esac; return 1; }

# 결과: STACK_MODEL(nebius|ollama-fleet|ollama|other|rules), LLM_BASE_URL(런타임과 함대 밖 기체),
# PER_ASSET_URLS(배열, 기체 순서), RUNTIME_SUPER, MODEL_NANO/SUPER/ULTRA, NEBIUS_API_KEY,
# LLM_REQUEST_EXTRA.
resolve_models() {
  local key="${NEBIUS_API_KEY:-}" given="${LLM_BASE_URL:-}" fleet=() index port
  # "ollama" 는 예전 Ollama 설정에서 쓰던 자리표시입니다. 키가 아닙니다.
  [ "$key" = "ollama" ] && key=""
  if [ -n "$given" ] && is_nebius_url "$given" && [ -z "$key" ]; then
    given=""
  fi
  read -r -a PER_ASSET_URLS <<< "${LLM_PER_ASSET_URLS:-}"
  STACK_MODEL=rules
  if [ -n "$given" ]; then
    LLM_BASE_URL="$given"
    if is_nebius_url "$given"; then
      STACK_MODEL=nebius
    elif is_ollama_url "$given"; then
      STACK_MODEL=ollama
      [ "${#PER_ASSET_URLS[@]}" -gt 0 ] && STACK_MODEL=ollama-fleet
    else
      STACK_MODEL=other
    fi
  elif [ -n "$key" ]; then
    STACK_MODEL=nebius
    LLM_BASE_URL="$NEBIUS_URL"
  else
    for index in $(seq 1 "$OLLAMA_FLEET_SIZE"); do
      port=$((OLLAMA_BASE_PORT + index))
      if ollama_up "$port"; then fleet+=("http://$OLLAMA_HOST_PROBE:$port/v1"); fi
    done
    if [ "${#PER_ASSET_URLS[@]}" -eq 0 ] && [ "${#fleet[@]}" -gt 0 ]; then
      PER_ASSET_URLS=("${fleet[@]}")
    fi
    # 런타임 자리. 관제 서버가 먼저 — 11434 는 Ollama 앱이라 문맥을 256k 로 잡습니다.
    LLM_BASE_URL=""
    if ollama_up "$OLLAMA_TOWER_PORT"; then
      LLM_BASE_URL="http://$OLLAMA_HOST_PROBE:$OLLAMA_TOWER_PORT/v1"
    elif ollama_up "$OLLAMA_BASE_PORT"; then
      LLM_BASE_URL="http://$OLLAMA_HOST_PROBE:$OLLAMA_BASE_PORT/v1"
    fi
    if [ "${#PER_ASSET_URLS[@]}" -gt 0 ]; then
      STACK_MODEL=ollama-fleet
    elif [ -n "$LLM_BASE_URL" ]; then
      STACK_MODEL=ollama
    fi
  fi
  NEBIUS_API_KEY="$key"
  RUNTIME_SUPER="${MODEL_SUPER:-}"
  case "$STACK_MODEL" in
    nebius)
      MODEL_NANO="${MODEL_NANO:-$NEBIUS_NANO}"
      MODEL_SUPER="${MODEL_SUPER:-$NEBIUS_SUPER}"
      MODEL_ULTRA="${MODEL_ULTRA:-$NEBIUS_ULTRA}"
      RUNTIME_SUPER="$MODEL_SUPER" ;;
    ollama|ollama-fleet)
      # 대역은 런타임 줄에만 줍니다. 기체에 가면 급한 신청서를 자기 4B 서버에 30B 로 묻습니다.
      [ -n "${LLM_BASE_URL:-}" ] && RUNTIME_SUPER="${MODEL_SUPER:-$LOCAL_SUPER_STANDIN}"
      LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-$OLLAMA_REQUEST_EXTRA}" ;;
  esac
  MODEL_NANO="${MODEL_NANO:-}" MODEL_SUPER="${MODEL_SUPER:-}" MODEL_ULTRA="${MODEL_ULTRA:-}"
}

# 기체 하나의 nano. 따로 주지 않았고 Ollama 서버면 4B 가 기본입니다. 확인해서 찾은 Ollama 는 포트가
# 1143x 가 아니어도(OLLAMA_BASE_PORT) Ollama 입니다 — 주소 모양이 아니라 고른 경로로 봅니다.
agent_nano_for() {
  if [ -n "${MODEL_NANO:-}" ]; then
    printf '%s' "$MODEL_NANO"
  elif [ -n "$1" ]; then
    case "${STACK_MODEL:-}" in
      ollama|ollama-fleet) printf '%s' "$LOCAL_NANO_DEFAULT" ;;
      *) if is_ollama_url "$1"; then printf '%s' "$LOCAL_NANO_DEFAULT"; fi ;;
    esac
  fi
}

# 결과: STACK_TAVILY, STACK_METAR(on|off), STACK_METAR_WHY, INTAKE_DB.
resolve_intake() {
  STACK_TAVILY=off
  [ -n "${TAVILY_API_KEY:-}" ] && STACK_TAVILY=on
  STACK_METAR=off STACK_METAR_WHY=""
  if [ "$(lower "${METAR:-on}")" = off ]; then
    STACK_METAR_WHY="METAR=off"
  elif curl -sf --max-time 3 "$METAR_PROBE_URL" >/dev/null 2>&1; then
    STACK_METAR=on
  else
    STACK_METAR_WHY="aviationweather.gov did not answer — the runtime retries every ${METAR_PERIOD_S:-300} s"
  fi
  INTAKE_DB="${INTAKE_DB:-.run/intake.sqlite}"
}

# 결과: DIRECT_LLM_URL, DIRECT_NANO, DIRECT_MODEL(시뮬레이터가 직결 기체마다 싣는 모델 id).
# 직결 쪽은 기본이 규칙입니다 — 로컬 한 슬롯을 여덟 프로세스가 나누면 런타임 쪽 초안이 굶습니다.
resolve_direct() {
  DIRECT_LLM_URL="" DIRECT_NANO=""
  if [ "${DIRECT_LLM:-0}" = "1" ] && [ -n "${LLM_BASE_URL:-}" ]; then
    DIRECT_LLM_URL="$LLM_BASE_URL"
    DIRECT_NANO="$(agent_nano_for "$LLM_BASE_URL")"
  fi
  DIRECT_MODEL="$DIRECT_NANO"
}

resolve_stack() {
  resolve_models
  resolve_intake
  resolve_direct
}

print_stack_env() {
  printf '%s\n' "STACK_MODEL=$STACK_MODEL" "LLM_BASE_URL=${LLM_BASE_URL:-}" \
    "PER_ASSET_URLS=${PER_ASSET_URLS[*]:-}" "MODEL_NANO=${MODEL_NANO:-}" \
    "MODEL_SUPER=${MODEL_SUPER:-}" "MODEL_ULTRA=${MODEL_ULTRA:-}" "RUNTIME_SUPER=${RUNTIME_SUPER:-}" \
    "LLM_REQUEST_EXTRA=${LLM_REQUEST_EXTRA:-}" "STACK_TAVILY=$STACK_TAVILY" \
    "STACK_METAR=$STACK_METAR" "INTAKE_DB=$INTAKE_DB" "DIRECT_MODEL=${DIRECT_MODEL:-}"
}

print_stack_table() {
  local drones="" index url
  for index in "${!PER_ASSET_URLS[@]}"; do
    url="${PER_ASSET_URLS[$index]##*//}"
    drones="${drones} ${url%%/*}"
  done
  echo "  ── stack ─────────────────────────────────────────────────────────────"
  case "$STACK_MODEL" in
    rules) echo "  model   rules — no model server answered; rules write every filing" ;;
    ollama-fleet)
      echo "  model   ollama-fleet · drones $(agent_nano_for "${PER_ASSET_URLS[0]}") @${drones}"
      if [ -n "${LLM_BASE_URL:-}" ]; then
        url="${LLM_BASE_URL##*//}"
        echo "          runtime ${RUNTIME_SUPER:-no super} @ ${url%%/*} (Super stand-in)"
      else
        echo "          runtime rules (neither the tower server nor 11434 answered)"
      fi ;;
    *) echo "  model   $STACK_MODEL · nano ${MODEL_NANO:-$(agent_nano_for "$LLM_BASE_URL")}" \
            "· super ${RUNTIME_SUPER:-none} @ ${LLM_BASE_URL##*//}" ;;
  esac
  echo "  intake  metar $STACK_METAR${STACK_METAR_WHY:+ ($STACK_METAR_WHY)}" \
       "· tavily $STACK_TAVILY · sim on"
  echo "  store   $INTAKE_DB"
  echo "  direct  ${DIRECT_MODEL:-rules}${DIRECT_MODEL:+ (DIRECT_LLM=1)}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  resolve_stack
  if [ "${1:-}" = "--env" ]; then print_stack_env; else print_stack_table; fi
fi
