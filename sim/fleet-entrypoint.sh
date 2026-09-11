#!/bin/sh
# PX4 자동조종 한 대를 띄우고, 오프보드 MAVLink 를 런타임 컨테이너로 보냅니다.
#
# SIH(simulation-in-hardware)를 씁니다. 기체 물리를 자동조종 안에서 풀기 때문에 Gazebo 가
# 필요 없습니다 — CPU 반 코어, 메모리 10 MiB. Gazebo 로 여섯 대를 띄우던 예전 판은 Mac 에서
# 렌더링만으로 기계를 다 먹었고, 우리가 증명해야 하는 것은 그림이 아니라 명령 경로입니다.
#
# 이미지의 entrypoint 를 그대로 쓰지 않는 이유: 그 스크립트는 MAVLink 를 전부
# host.docker.internal 로 돌립니다. 런타임이 호스트에서 돌 때(scripts/sitl.sh)는 그게 맞지만,
# compose 안의 런타임은 호스트가 아니라 같은 망의 컨테이너입니다. PX4 의 mavlink -t 는 IPv4
# 주소만 받으므로 이름(runtime)을 여기서 주소로 바꿉니다.
#
# -d 로 띄우는 이유: 대화형 셸(pxh>)은 stdin 이 없으면 빈 줄을 끝없이 읽어 CPU 한 코어를
# 먹고 로그를 몇십 MB 씩 남깁니다(실제로 35초에 44 MB).
set -eu

PX4_PREFIX=/opt/px4-gazebo
[ -d "$PX4_PREFIX" ] || PX4_PREFIX=/opt/px4
RC="$PX4_PREFIX/etc/init.d-posix/px4-rc.mavlink"
PARTNER="${MAVLINK_PARTNER:-runtime}"

resolve() {
  getent ahostsv4 "$1" 2>/dev/null | awk '/STREAM/ {print $1; exit}'
}

# 런타임 컨테이너가 아직 안 떴으면 이름이 안 풀립니다. 조금 기다립니다.
partner_ip=""
attempt=0
while [ "$attempt" -lt "${PARTNER_WAIT_S:-60}" ]; do
  partner_ip="$(resolve "$PARTNER")"
  [ -n "$partner_ip" ] && break
  attempt=$((attempt + 1))
  sleep 1
done
if [ -z "$partner_ip" ]; then
  echo "fleet: $PARTNER 를 찾지 못했습니다 — MAVLINK_PARTNER 를 확인하세요"
  exit 1
fi
echo "fleet: 오프보드 MAVLink 를 $PARTNER($partner_ip):14540 으로 보냅니다"

# 오프보드 링크(원격 14540) 한 줄만 런타임으로 돌립니다.
sed -i '/-o \$udp_offboard_port_remote/ s/mavlink start -x -u/mavlink start -x -t '"$partner_ip"' -u/' "$RC"

# 지상국 링크(원격 14550)는 호스트로 둡니다 — QGroundControl 이 이 기체를 그대로 봅니다.
host_ip="$(resolve host.docker.internal)"
if [ -n "$host_ip" ]; then
  sed -i '/^mavlink start -x -u \$udp_gcs_port_local/ s/mavlink start -x -u/mavlink start -x -t '"$host_ip"' -u/' "$RC"
  echo "fleet: 지상국 링크는 호스트($host_ip):14550 — QGroundControl 용"
fi

exec "$PX4_PREFIX/bin/px4" -d "$@"
