#!/usr/bin/env bash
# 진짜 PX4 자동조종 한 대(SIH)를 컨테이너로 띄우고, 호스트의 스택을 그 뒤에 붙입니다.
# 시연 영상에서 쓰는 길입니다 — 화면·시뮬레이터·기체 에이전트는 전부 호스트 프로세스이고,
# 컨테이너는 자동조종 하나뿐입니다.
#
#   ./scripts/sitl.sh            PX4 + 스택 (Ctrl-C 로 둘 다 내림)
#   ./scripts/sitl.sh px4        PX4 만 띄우고 어떻게 붙이는지 찍어 줍니다
#   ./scripts/sitl.sh check      링크·임무 규약 확인 (scripts/px4_check.py)
#   ./scripts/sitl.sh fly        확인 + 짧게 한 번 띄웠다 내림
#                                (check·fly 는 떠 있는 PX4 를 쓰고, 없으면 띄웠다가 끝날 때 내립니다)
#   ./scripts/sitl.sh stop       PX4 컨테이너 내림
#
# 기체 네 대는 그대로 시뮬레이션이고, 그중 MAVLINK_MIRROR(기본 drone-01) 한 대만 PX4 도 같이
# 납니다. 판정과 점수는 여전히 시뮬레이터의 것입니다 — PX4 는 명령 경로를 증명하는 거울입니다.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${PX4_IMAGE:-px4io/px4-sitl-gazebo:v1.18.0-beta2}"
NAME="${PX4_CONTAINER:-sky-net-px4}"
MIRROR="${MAVLINK_MIRROR:-drone-01}"
PORT="${MAVLINK_PORT:-14540}"
# PX4(SIH)는 1배속으로 돌립니다. 이 Mac 의 Docker 에서 SIH 락스텝이 버티는 것은 1배뿐입니다
# (잰 값: 1배 → 0.94배, 2배 → 들쭉날쭉, 4배 → 0.26배로 오히려 느려짐). 기본 틱(0.2초)의 시뮬은
# 4배속이라 PX4 는 지도의 기체보다 뒤처집니다 — 명령 경로는 그대로 보이고, 위치까지 나란히
# 보이려면 시뮬을 실시간으로: TICK_SECONDS=0.8 ./scripts/sitl.sh
SPEED="${PX4_SIM_SPEED_FACTOR:-1}"

seat_of() {
  # 거울 기체의 창고 옥상 자리. 세계가 아는 좌표를 그대로 씁니다 — 여기서 시작해야
  # PX4 의 궤적이 지도 위의 그 기체와 겹칩니다.
  PYTHONPATH=. python3 - "$1" <<'PY'
import sys
from sim.world import SEAT_ROOF_M, seat_of, to_latlon
asset = sys.argv[1]
index = int(asset.rsplit("-", 1)[-1]) - 1 if asset.rsplit("-", 1)[-1].isdigit() else 0
lat, lon = to_latlon(*seat_of(index))
print(f"{lat:.6f} {lon:.6f} {SEAT_ROOF_M:.0f}")
PY
}

start_px4() {
  read -r home_lat home_lon home_alt <<<"$(seat_of "$MIRROR")"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # Docker Desktop 이면 이미지의 entrypoint 가 MAVLink 를 host.docker.internal 로 돌려 줍니다.
  # 리눅스에는 그 이름이 없으니 호스트 망에 붙여 127.0.0.1 로 그대로 오게 합니다.
  local network=()
  docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qi "docker desktop" || \
    network=(--network host)
  docker run -d --name "$NAME" "${network[@]}" \
    -e PX4_SIM_MODEL=sihsim_quadx \
    -e PX4_HOME_LAT="$home_lat" -e PX4_HOME_LON="$home_lon" -e PX4_HOME_ALT="$home_alt" \
    -e PX4_SIM_SPEED_FACTOR="$SPEED" \
    -e PX4_PARAM_MPC_XY_CRUISE=20 -e PX4_PARAM_MPC_XY_VEL_MAX=20 \
    -e PX4_PARAM_MPC_TKO_SPEED=2 -e PX4_PARAM_MPC_Z_V_AUTO_UP=2 \
    -e PX4_PARAM_MPC_Z_V_AUTO_DN=1.75 -e PX4_PARAM_COM_RCL_EXCEPT=1 \
    "$IMAGE" -d >/dev/null
  echo "  px4     $NAME · SIH · $MIRROR 자리 $home_lat,$home_lon (옥상 ${home_alt}m) · ${SPEED}배속"
  echo "  link    MAVLink → 호스트 UDP $PORT"
}

# 런타임이 MAVLink 를 말하려면 pymavlink 가 있어야 합니다. 없으면 .run 에 작은 venv 를 만들고
# 거기 python 을 PATH 앞에 둡니다 — 시스템 python 에는 아무것도 설치하지 않습니다.
ensure_pymavlink() {
  if python3 -c 'import pymavlink' >/dev/null 2>&1; then
    return
  fi
  local venv=".run/sitl-venv"
  if [ ! -x "$venv/bin/python3" ]; then
    echo "  deps    pymavlink 가 없어 $venv 를 만듭니다 (한 번만)"
    python3 -m venv "$venv"
  fi
  "$venv/bin/python3" -c 'import pymavlink, yaml' >/dev/null 2>&1 || \
    "$venv/bin/pip" install --quiet pymavlink==2.4.49 pyyaml==6.0.2
  PATH="$PWD/$venv/bin:$PATH"
  export PATH
}

case "${1:-stack}" in
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "px4 내렸습니다."
    ;;
  px4)
    start_px4
    echo
    echo "  스택은 이렇게 붙입니다:"
    echo "    ADAPTER=composite MAVLINK_MIRROR=$MIRROR \\"
    echo "      MAVLINK_ENDPOINT=udpin:0.0.0.0:$PORT ./scripts/dev.sh"
    ;;
  check|fly)
    ensure_pymavlink
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      start_px4
      # 여기서 띄운 PX4 는 여기서 내립니다. 이미 떠 있던 것(스택이 쓰는 중)은 그대로 둡니다.
      trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
    fi
    extra=""
    [ "${1}" = "fly" ] && extra="--fly"
    PYTHONPATH=. python3 scripts/px4_check.py "udpin:0.0.0.0:$PORT" $extra
    ;;
  stack)
    ensure_pymavlink
    start_px4
    trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
    echo
    ADAPTER=composite MAVLINK_MIRROR="$MIRROR" MAVLINK_ENDPOINT="udpin:0.0.0.0:$PORT" \
      ./scripts/dev.sh
    ;;
  *)
    echo "쓰는 법: scripts/sitl.sh [stack|px4|check|fly|stop]"
    exit 2
    ;;
esac
