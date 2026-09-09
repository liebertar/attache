#!/usr/bin/env bash
# Docker 없이 같은 스택을 로컬 프로세스로 띄웁니다. 종료는 Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
mkdir -p .run

cleanup() { pkill -P $$ || true; }
trap cleanup EXIT INT TERM

PORT=8100 TICK_SECONDS="${TICK_SECONDS:-0.2}" FLEET_LIMIT_USD=720 \
  python3 -m sim.service & sleep 1
PORT=8000 CONFIG=configs/fleet.yaml SIM_URL=http://localhost:8100 \
  LEDGER_PATH=.run/ledger.jsonl python3 -m attache.runtime.service & sleep 1

for asset in drone-01 drone-02 drone-03; do
  ASSET_ID=$asset RUNTIME_URL=http://localhost:8000 python3 -m attache.agent.loop &
  ASSET_ID=$asset TRANSPORT=http SIM_URL=http://localhost:8100 python3 -m direct_agent.loop &
done

( cd ui && python3 -m http.server 3100 >/dev/null 2>&1 ) &
echo
echo "  화면: http://localhost:3100"
echo "  런타임: http://localhost:8000/state   세계: http://localhost:8100/compare"
echo
wait
