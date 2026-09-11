#!/usr/bin/env bash
# 드론마다 Ollama 서버 하나 — 11435, 11436, … 에 N 개 (기본 4).
#
# 왜: Ollama 는 nemotron-3-nano(Mamba 혼합) 계열에 동시 처리 슬롯 1개를 강제합니다
# (OLLAMA_NUM_PARALLEL 은 무시됨). 서버 하나에 기체 4대가 초안을 물으면 줄을 서서 뒤의 셋이
# 타임아웃을 맞습니다(실주행: 초안 9건 중 7건 잘림, nano 승인 0). 서버를 기체 수만큼 띄우면
# 슬롯도 기체 수만큼입니다. 모델 폴더(~/.ollama/models, OLLAMA_MODELS)는 그대로 공유하고,
# 서버마다 작은 4B(nemotron-3-nano:4b, 2.8 GB)를 따로 올립니다.
#
# 관제 서버(11439) 하나를 더 띄웁니다 — 런타임의 Super 대역(공지 읽기·권고)이 씁니다. 전에는 기본
# 서버(11434, Ollama 앱)를 썼는데, 앱은 문맥 창을 256k 로 잡아 같은 4B 에 KV 캐시를 5 GB 넘게 더
# 얹었습니다(Ollama 표시 8.4 GB, 8k 에서는 3.0 GB). 공지 한 편은 2천 자 안팎이라 8k 면 넉넉합니다.
# 실제 메모리는 Ollama 표시보다 큽니다: 서버마다 가중치를 제 힙에 따로 올려(footprint 의 MALLOC_LARGE)
# 8k 서버 하나가 약 7.5 GB, 넷이면 30 GB 입니다(2026-09-11 footprint 로 잼).
# 관제 서버를 안 띄우려면 OLLAMA_FLEET_TOWER=0 — 그러면 런타임은 전처럼 11434 를 씁니다.
#
#   scripts/ollama_fleet.sh start  [N]   # 서버를 띄우고 4B 를 한 번씩 데워 둡니다(첫 초안이 적재를 기다리지 않게)
#   scripts/ollama_fleet.sh stop   [N]
#   scripts/ollama_fleet.sh status [N]
#
# 로그는 .run/ollama-<port>.log, pid 는 .run/ollama-<port>.pid. 띄운 뒤 찍히는
# LLM_PER_ASSET_URLS 를 export 하고 scripts/dev.sh 를 돌리면 기체 i 가 i 번째 서버를 씁니다
# (.env.example 의 "로컬 함대" 블록).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .run

COMMAND="${1:-status}"
SIZE="${2:-${OLLAMA_FLEET_SIZE:-4}}"
MODEL="${OLLAMA_FLEET_MODEL:-nemotron-3-nano:4b}"
BASE_PORT="${OLLAMA_FLEET_BASE_PORT:-11434}"     # 이 다음 포트부터 씁니다
# 문맥 창. Ollama 기본은 VRAM 을 보고 256k 까지 잡아 4B 하나가 8.4 GB 를 먹습니다. 초안은 지도 읽기
# 약 1.5k 토큰 + 답 700 토큰이라 8k 면 넉넉하고, 서버 4개가 함께 메모리에 있어야 합니다.
CONTEXT="${OLLAMA_FLEET_CONTEXT:-8192}"
# 데워 둔 모델을 얼마나 오래 들고 있을지. 데모 한 판(13분)보다 넉넉히.
KEEP_ALIVE="${OLLAMA_FLEET_KEEP_ALIVE:-2h}"
# 관제 서버. scripts/resolve_stack.sh 가 런타임 자리로 이 포트를 먼저 봅니다.
TOWER="${OLLAMA_FLEET_TOWER:-1}"
TOWER_PORT="${OLLAMA_TOWER_PORT:-11439}"
if [ "$TOWER" = 1 ] && [ "$((BASE_PORT + SIZE))" -ge "$TOWER_PORT" ]; then
  echo "함대(:$((BASE_PORT + 1))..:$((BASE_PORT + SIZE)))가 관제 서버 :$TOWER_PORT 와 겹칩니다 — OLLAMA_TOWER_PORT 를 옮기세요" >&2
  exit 2
fi

port_of() { echo $((BASE_PORT + $1)); }
url_of() { echo "http://127.0.0.1:$(port_of "$1")/v1"; }
is_up() { curl -sf --max-time 1 "http://127.0.0.1:$1/api/version" >/dev/null 2>&1; }

# 이 스크립트가 다루는 포트 전부: 드론 서버 N 개, 그리고 관제 서버.
ports() {
  for i in $(seq 1 "$SIZE"); do port_of "$i"; done
  if [ "$TOWER" = 1 ]; then echo "$TOWER_PORT"; fi
}

tower_note() {
  if [ "$TOWER" = 1 ]; then echo " + 관제 서버 :$TOWER_PORT"; fi
}

urls_line() {
  local urls=""
  for i in $(seq 1 "$SIZE"); do urls="${urls:+$urls }$(url_of "$i")"; done
  echo "$urls"
}

start_one() {
  local port=$1
  if is_up "$port"; then
    echo "  :$port 이미 떠 있음"
    return
  fi
  OLLAMA_HOST="127.0.0.1:$port" OLLAMA_KEEP_ALIVE="$KEEP_ALIVE" OLLAMA_CONTEXT_LENGTH="$CONTEXT" \
    nohup ollama serve >".run/ollama-$port.log" 2>&1 &
  echo $! >".run/ollama-$port.pid"
  echo "  :$port 시작 (pid $!, 로그 .run/ollama-$port.log)"
}

wait_up() {
  local port=$1
  for _ in $(seq 1 100); do
    is_up "$port" && return 0
    sleep 0.2
  done
  echo "  :$port 가 20초 안에 안 떴습니다 — .run/ollama-$port.log 를 보세요" >&2
  return 1
}

warm_one() {
  # 작은 질문 하나로 모델을 올려 둡니다. 첫 진짜 초안이 적재(수 초)를 기다리지 않게.
  local port=$1 started ended
  started=$(date +%s)
  curl -s --max-time 180 "http://127.0.0.1:$port/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word ok.\"}],\"max_tokens\":4,\"reasoning_effort\":\"none\"}" \
    >".run/ollama-$port.warm.json" 2>&1 || true
  ended=$(date +%s)
  if grep -q '"choices"' ".run/ollama-$port.warm.json"; then
    echo "  :$port $MODEL 데움 ($((ended - started))초)"
  else
    echo "  :$port 데우기 실패 — $(head -c 200 ".run/ollama-$port.warm.json")" >&2
  fi
}

stop_one() {
  local port=$1 pid=""
  [ -f ".run/ollama-$port.pid" ] && pid=$(cat ".run/ollama-$port.pid")
  # pid 파일이 없거나 낡았으면 포트를 쥔 프로세스를 찾습니다. 11434(기본 서버)는 여기 안 옵니다.
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    pid=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  fi
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    echo "  :$port 내림 (pid $pid)"
  else
    echo "  :$port 떠 있지 않음"
  fi
  rm -f ".run/ollama-$port.pid"
}

# /api/ps 답을 한 줄로: 모델 이름 (메모리, 문맥 창)
PS_SUMMARY=$(cat <<'PY'
import json, sys
loaded = json.load(sys.stdin).get("models") or []
print(", ".join("%s (%.1f GB, ctx %s)" % (m["name"], m.get("size_vram", 0) / 1e9, m.get("context_length"))
               for m in loaded) or "(올라간 모델 없음)")
PY
)

status_one() {
  local port=$1 loaded role=""
  if [ "$port" = "$TOWER_PORT" ]; then role=" (관제)"; fi
  if is_up "$port"; then
    loaded=$(curl -s --max-time 2 "http://127.0.0.1:$port/api/ps" | python3 -c "$PS_SUMMARY" 2>/dev/null || echo "?")
    echo "  :$port$role 떠 있음 — $loaded"
  else
    echo "  :$port$role 꺼짐"
  fi
}

case "$COMMAND" in
  start)
    echo "Ollama 함대 $SIZE 대$(tower_note) ($MODEL, ctx $CONTEXT, keep-alive $KEEP_ALIVE)"
    for port in $(ports); do start_one "$port"; done
    for port in $(ports); do wait_up "$port"; done
    # 같이 데웁니다. 같은 blob 을 mmap 하므로 따로 하는 것보다 빠릅니다. 서버 자체도 이
    # 셸의 자식이라 wait 는 데우기 pid 만 — 아니면 서버가 내려갈 때까지 안 돌아옵니다.
    warmers=""
    for port in $(ports); do
      warm_one "$port" &
      warmers="$warmers $!"
    done
    # shellcheck disable=SC2086
    wait $warmers
    echo
    echo "export LLM_PER_ASSET_URLS=\"$(urls_line)\""
    if [ "$TOWER" = 1 ]; then
      echo "# 관제(런타임 Super 대역) http://127.0.0.1:$TOWER_PORT/v1 — scripts/dev.sh 가 알아서 씁니다"
    fi
    ;;
  stop)
    for port in $(ports); do stop_one "$port"; done
    ;;
  status)
    for port in $(ports); do status_one "$port"; done
    echo "LLM_PER_ASSET_URLS=\"$(urls_line)\""
    ;;
  *)
    echo "사용법: $0 start|stop|status [N]" >&2
    exit 2
    ;;
esac
