#!/usr/bin/env bash
# 한 컨테이너 안에서 PX4 여섯 대를 띄웁니다.
#
# 컨테이너를 여섯 개로 나누지 않는 이유: Gazebo 는 gz-transport 멀티캐스트로 서로를
# 찾습니다. 컨테이너가 갈라지면 같은 세계에 모이질 못하고 여섯 개의 따로 노는 세계가
# 됩니다. 우리가 보여줘야 하는 건 "같은 하늘"이라서 한 컨테이너에 모읍니다.
#
# 1번이 Gazebo 서버를 띄우고 나머지는 거기에 붙습니다 (PX4_GZ_STANDALONE=1).
set -euo pipefail

MODEL="${PX4_SIM_MODEL:-gz_x500}"
AUTOSTART="${PX4_SYS_AUTOSTART:-4001}"
COUNT="${FLEET_SIZE:-6}"
PX4_BIN="${PX4_BIN:-/px4_sitl_default/bin/px4}"

# 여섯 대의 출발 위치. 앞의 셋은 런타임을 거치는 기체, 뒤의 셋은 직접 날리는 기체입니다.
POSES=("0,0" "0,6" "0,12" "14,0" "14,6" "14,12")

start_one() {
  local index="$1" standalone="$2"
  local dir="/tmp/px4_${index}"
  mkdir -p "$dir"
  (
    cd "$dir"
    env PX4_SYS_AUTOSTART="$AUTOSTART" \
        PX4_SIM_MODEL="$MODEL" \
        PX4_GZ_MODEL_POSE="${POSES[$((index - 1))]}" \
        ${standalone:+PX4_GZ_STANDALONE=1} \
        "$PX4_BIN" -i "$index" -d
  ) &
  echo "  px4 instance $index up (pose ${POSES[$((index - 1))]}, MAVLink udp $((14539 + index)))"
}

echo "fleet: PX4 x${COUNT}, model=${MODEL}, world 공유"
start_one 1 ""            # 1번이 Gazebo 서버를 띄웁니다
sleep "${SERVER_WARMUP_S:-12}"
for i in $(seq 2 "$COUNT"); do
  start_one "$i" 1
  sleep "${SPAWN_GAP_S:-3}"
done

wait -n
