"""The model fills in the form; the operator's own sense of the aircraft filters what it writes.

A form is discarded when it names an action outside the list (charging does not exist in this cycle)
or one the aircraft cannot perform right now (departing while airborne). The rule form takes its
place. This is not a judgement — the runtime still judges whatever is filed — it is the operator
not filing what its own aircraft cannot do.
"""

import unittest

from attache.agent.detect import Concern, detect
from attache.agent.propose import ALLOWED_ACTIONS, Proposer, possible_now
from attache.llm.client import LlmReply, TieredLlm


class ScriptedLlm(TieredLlm):
    def __init__(self, text: str):
        super().__init__(base_url="http://scripted", models={"nano": "scripted-nano"},
                         timeout_s=1.0, request_extra={}, record_dir="")
        self.text = text

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        reply = LlmReply(text=self.text, model="scripted-nano")
        self.account(tier, reply, 0)
        return reply


READY = {"id": "drone-02", "model": "dv-x500", "state": "ready", "battery": 56.0, "alt_m": 0.8,
         "vibration": 0.1, "autonomy_health": 1.0, "passengers": 0, "cargo": 0}
RELOAD = Concern(kind="needs_reload", urgency="normal", detail="배터리 56%, 다음 짐을 싣습니다")


class ImpossibleFormTest(unittest.TestCase):
    def test_charging_is_not_an_action_the_agent_can_ask_for(self):
        self.assertNotIn("charge", ALLOWED_ACTIONS)
        self.assertNotIn("fast_charge", ALLOWED_ACTIONS)
        llm = ScriptedLlm('{"action": "charge", "pad": null, "rationale": "Battery is low."}')
        written = Proposer(llm).write(RELOAD, READY, "pad:launch", frozenset(), ("pad:launch",))
        self.assertEqual((written.action, written.author), ("depart", "rules"))
        self.assertEqual(llm.stats["nano"].fallback, 1, "버린 답은 규칙이 대신한 것으로 셉니다")

    def test_an_aircraft_on_a_landing_area_asks_for_its_next_route_not_a_charge(self):
        landed = {**READY, "state": "landed", "alt_m": 0.0, "battery": 18.0, "job": "Union Square",
                  "route": [], "assigned_pad": None}
        concern = detect(landed)
        self.assertEqual(concern.kind, "needs_route")
        no_job = {**landed, "job": None}
        self.assertIsNone(detect(no_job), "갈 곳이 없으면 착륙장에서는 아무것도 신청하지 않습니다")

    def test_a_bay_reservation_is_only_for_a_fault(self):
        llm = ScriptedLlm('{"action": "reserve_pad", "pad": "pad:launch", '
                          '"rationale": "Park now."}')
        written = Proposer(llm).write(RELOAD, READY, "pad:launch", frozenset(), ("pad:launch",))
        self.assertEqual((written.action, written.author), ("depart", "rules"))
        fault = Concern(kind="motor_fault", urgency="high", detail="모터 진동 0.9")
        written = Proposer(llm).write(fault, READY, "pad:launch", frozenset(), ("pad:launch",))
        self.assertEqual((written.action, written.author), ("reserve_pad", "scripted-nano"))

    def test_what_is_possible_now(self):
        self.assertFalse(possible_now("depart", {"state": "delivering", "alt_m": 80.0}))
        self.assertTrue(possible_now("depart", READY))
        self.assertTrue(possible_now("fly_route", {"state": "delivering", "alt_m": 80.0}))


if __name__ == "__main__":
    unittest.main()
