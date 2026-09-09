"""Plain number comparisons. No model runs here, which is why 8,000 of these are affordable."""

from dataclasses import dataclass

BATTERY_LOW = 30.0
BATTERY_CRITICAL = 15.0
FAST_CHARGE_BELOW = 25.0  # 회전율 때문에 이 아래면 급속을 원합니다
VIBRATION_ALERT = 0.55
AUTONOMY_ALERT = 0.35
BATTERY_FULL = 60.0  # 기단은 만충까지 안 채웁니다. 회전이 중요합니다
CHARGE_BELOW = 40.0  # 마당에 돌아왔을 때 이 아래면 충전대로. 아니면 바로 다음 짐


@dataclass
class Concern:
    kind: str
    urgency: str
    detail: str


def detect(telemetry: dict) -> Concern | None:
    state = telemetry.get("state", "")
    if state in ("grounded", "stranded", "diverted"):
        return None

    # 고장은 충전 중이든 순항 중이든 똑같이 올라옵니다
    if telemetry.get("autonomy_health", 1.0) <= AUTONOMY_ALERT:
        return Concern(
            "autonomy_fault", "high",
            f"자율주행 상태 {telemetry.get('autonomy_health'):.2f}",
        )

    if telemetry.get("vibration", 0.0) >= VIBRATION_ALERT and not telemetry.get("assigned_pad"):
        return Concern(
            "motor_fault", "high", f"모터 진동 {telemetry.get('vibration'):.2f}"
        )

    battery = telemetry.get("battery", 100.0)
    idle = not telemetry.get("assigned_pad") and not telemetry.get("route")

    # 배달 주문(또는 창고 복귀)이 있는데 아직 승인된 경로가 없으면, 갈 수 있게 해달라는 신청.
    # 싣거나 내리는 중에도 냅니다 — 그래야 일이 끝난 자리에서 승인을 기다리며 서 있지 않습니다.
    # 배터리는 여기서 안 봅니다. 나간 기체는 어쨌든 돌아와야 하고(안 그러면 착륙장에 영영
    # 앉아 있었습니다), 나가기 전 잔량은 마당에서(아래 needs_pad) 봅니다.
    if (
        telemetry.get("job")
        and state in ("loading", "dropping", "picking", "ready", "cruising", "landed")
        and idle
    ):
        return Concern("needs_route", "normal",
                       f"배달지 {telemetry['job']}, 배터리 {battery:.0f}%")

    # 창고 마당의 제 자리(갈 곳 없음, 땅). 그 자리에서 바로 다음 짐을 싣습니다.
    # 충전대 순환은 뺐습니다 — 배터리 관리는 운영사 몫이고, 마당에서 이륙장으로 가는 짧은
    # 비행이 화면에서 "저 이상한 경로는 뭐냐"가 됐습니다.
    if not telemetry.get("job") and state == "ready" and idle:
        return Concern("needs_reload", "normal", f"배터리 {battery:.0f}%, 다음 짐을 싣습니다")

    # 착륙장에 내린 기체(landed)는 짐을 내리고 다음 경로를 신청합니다(위 needs_route). 충전 신청은
    # 없습니다.
    # 배달 도중 배터리 때문에 되돌아오는 규칙은 없습니다. 운영사는 한 바퀴를 항속 안에서
    # 짜고, 마당에 돌아왔을 때(위) 채웁니다. 가다가 돌아서는 기체는 이 데모가 보여줄 것이 아닙니다.
    return None
