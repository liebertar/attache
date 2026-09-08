"""Plain number comparisons. No model runs here, which is why 8,000 of these are affordable."""

from dataclasses import dataclass

BATTERY_LOW = 30.0
BATTERY_CRITICAL = 15.0
FAST_CHARGE_BELOW = 25.0  # 회전율 때문에 이 아래면 급속을 원합니다
VIBRATION_ALERT = 0.55
AUTONOMY_ALERT = 0.35
BATTERY_FULL = 60.0  # 기단은 만충까지 안 채웁니다. 회전이 중요합니다


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

    if state == "charging":
        if telemetry.get("battery", 0.0) >= BATTERY_FULL:
            return Concern("charged", "low", "충전이 끝났습니다")
        return None

    battery = telemetry.get("battery", 100.0)

    # 배달 주문이 있고 배터리가 되는데 아직 못 가고 있으면, 갈 수 있게 해달라는 신청.
    if (
        telemetry.get("job")
        and battery > BATTERY_LOW
        and state in ("cruising", "landed")
        and not telemetry.get("assigned_pad")
        and not telemetry.get("route")
    ):
        return Concern("needs_route", "normal",
                       f"배달지 {telemetry['job']}, 배터리 {battery:.0f}%")

    if state == "landed":
        return Concern("needs_charge", "high" if battery < FAST_CHARGE_BELOW else "normal",
                       f"패드 위, 배터리 {battery:.0f}%")
    if battery <= BATTERY_CRITICAL:
        return Concern("battery_critical", "high", f"배터리 {battery:.0f}%")
    if battery <= BATTERY_LOW and not telemetry.get("assigned_pad"):
        return Concern("battery_low", "normal", f"배터리 {battery:.0f}%")
    return None
