#!/usr/bin/env bash
# 시연 녹화용 한 줄. 떠 있는 스택을 내리고, 깨끗한 스택(씨앗 7)을 올리고, 판을 처음부터 돌린 뒤
# 연출 화면(map.html?demo=1)을 엽니다. 몇 번을 돌려도 같은 결과입니다 — 쓸 포트를 쥔 것은 먼저 내립니다.
# 종료는 Ctrl-C(dev.sh 와 같음). 모델은 dev.sh 가 스스로 고릅니다: Ollama 함대가 답하면 함대, 아니면 규칙.
#
# 바꿀 수 있는 것: RT_PORT SIM_PORT UI_PORT(둘째 스택), DEMO_OPEN=0(브라우저를 열지 않음).
set -euo pipefail
cd "$(dirname "$0")/.."

RT_PORT="${RT_PORT:-8000}" SIM_PORT="${SIM_PORT:-8100}" UI_PORT="${UI_PORT:-3100}"
# 씨앗은 7 로 못박습니다. 장면(거절·기상 대기·화재·링크 두절)이 어느 기체에 걸리는지가 씨앗에 달려 있고,
# 문서와 자막 설명이 씨앗 7 기준입니다.
export RT_PORT SIM_PORT UI_PORT SEED=7
# 기본 포트가 아니면 둘째 스택입니다. 원장·수집 기록도 따로 둡니다 — 두 런타임이 같은 파일에 쓰면 보고서가 섞입니다.
if [ "$RT_PORT" != "8000" ]; then
  export LEDGER_PATH="${LEDGER_PATH:-.run/ledger-$RT_PORT.jsonl}" INTAKE_DB="${INTAKE_DB:-.run/intake-$RT_PORT.sqlite}"
fi
# 프로세스끼리는 127.0.0.1 로 부릅니다(dev.sh 참고: localhost 는 ::1 이 먼저라 Docker 가 같은 포트를 열면 엉뚱한 곳).
LOOPBACK=127.0.0.1
READY_S=60

listeners() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }

# 한 포트를 쥔 것을 내립니다. dev.sh 가 띄운 프로세스면 dev.sh 를 내려 기체 에이전트까지 같이 거두게 하고
# (dev.sh 의 trap 이 자식을 전부 정리합니다), Docker 컨테이너면 docker stop 합니다 — 다른 프로젝트의
# carter-agent·dynamodb-local 이 8000/8100 을 가로챈 적이 있습니다.
stop_port() {
  local port=$1 pid parent command ids
  for pid in $(listeners "$port"); do
    command=$(ps -o command= -p "$pid" 2>/dev/null || true)
    case "$command" in
      *com.docker*|*vpnkit*|*docker-proxy*)
        ids=$(docker ps -q --filter "publish=$port" 2>/dev/null || true)
        # shellcheck disable=SC2086 — 컨테이너 id 여럿을 그대로 넘깁니다
        [ -n "$ids" ] && docker stop $ids >/dev/null 2>&1 || true
        continue ;;
    esac
    parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$parent" ] && [ "$parent" != 1 ] \
       && ps -o command= -p "$parent" 2>/dev/null | grep -q "scripts/dev.sh"; then
      kill "$parent" 2>/dev/null || true
    fi
    kill "$pid" 2>/dev/null || true
  done
}

# dev.sh 없이 남은 기체 에이전트(창을 닫았거나 dev.sh 가 강제로 죽은 경우). 이 스택의 포트를 보고 있는 것만
# 내립니다 — 남겨 두면 새 런타임에 같은 기체가 둘씩 신청을 냅니다. 환경 변수는 ps -E 로 봅니다(macOS).
stop_orphan_agents() {
  local pid
  for pid in $(ps -Eww -ax -o pid=,command= 2>/dev/null \
      | grep -E "drone[.]agent[.]loop|drone[.]direct[.]loop" \
      | grep -E "(RUNTIME_URL|SIM_URL)=http://$LOOPBACK:($RT_PORT|$SIM_PORT)( |$)" \
      | awk '{print $1}'); do
    kill "$pid" 2>/dev/null || true
  done
}

wait_free() {
  local waited=0
  while [ -n "$(listeners "$RT_PORT")$(listeners "$SIM_PORT")$(listeners "$UI_PORT")" ]; do
    [ "$waited" -ge 20 ] && { echo "  demo    ports $RT_PORT/$SIM_PORT/$UI_PORT are still held" >&2; return 1; }
    sleep 0.5; waited=$((waited + 1))
  done
}

answers() { curl -sf --max-time 2 "$1" >/dev/null 2>&1; }

# 시뮬레이터·런타임·화면이 답하고, 기체 에이전트 넷이 런타임에 등록할 때까지. 에이전트는 기다리되
# (판이 에이전트 없이 먼저 흐르면 첫 장면을 놓칩니다) 끝내 넷이 안 되면 있는 대로 갑니다.
wait_ready() {
  local waited=0 agents=0
  until answers "http://$LOOPBACK:$SIM_PORT/health" && answers "http://$LOOPBACK:$RT_PORT/state" \
        && answers "http://$LOOPBACK:$UI_PORT/map.html"; do
    [ "$waited" -ge $((READY_S * 2)) ] && return 1
    sleep 0.5; waited=$((waited + 1))
  done
  waited=0
  while [ "$waited" -lt 40 ]; do
    agents=$(curl -sf --max-time 2 "http://$LOOPBACK:$RT_PORT/state" \
      | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("agents") or {}))' 2>/dev/null || echo 0)
    [ "$agents" -ge 4 ] && break
    sleep 0.5; waited=$((waited + 1))
  done
  echo "  demo    $agents drone agent(s) registered"
}

for port in "$RT_PORT" "$SIM_PORT" "$UI_PORT"; do stop_port "$port"; done
stop_orphan_agents
wait_free

./scripts/dev.sh &
stack=$!
# Ctrl-C 는 dev.sh 로 넘깁니다. dev.sh 가 자기 자식(시뮬레이터·런타임·에이전트·화면)을 거둡니다.
trap 'kill "$stack" 2>/dev/null || true; wait "$stack" 2>/dev/null || true; exit 130' INT TERM

if ! wait_ready; then
  echo "  demo    the stack did not come up within ${READY_S} s — see the output above" >&2
  kill "$stack" 2>/dev/null || true
  exit 1
fi
# 판을 처음부터. 에이전트가 붙기 전에 흐른 틱을 버리고 틱 0 에서 시작합니다.
curl -sf --max-time 5 -X POST "http://$LOOPBACK:$SIM_PORT/reset" >/dev/null
QUERY="demo=1"
[ "$RT_PORT$SIM_PORT" = "80008100" ] || QUERY="$QUERY&rt=$RT_PORT&sim=$SIM_PORT"
URL="http://$LOOPBACK:$UI_PORT/map.html?$QUERY"
echo "  demo    round reset to tick 0 (seed 7)"
echo "  demo    $URL"
if [ "${DEMO_OPEN:-1}" = "1" ] && [ "$(uname)" = "Darwin" ]; then open "$URL"; fi
wait "$stack"
