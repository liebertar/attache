"""Which model writes each aircraft's filings: a label on the screen, never a judgement.

Each operator process tells the runtime what it runs (model id, server) when it starts and every
30 s; the runtime keeps the latest per aircraft, gives it a human name, and drops the ones that
stopped talking. The direct world cannot reach the runtime (it is on the other network), so the
simulator carries its model per vehicle from the environment it was started with.
"""

import http.server
import json
import tempfile
import threading
import time
import unittest

from attache.agent.loop import ModelHealth, Registration, identity
from attache.core.config import model_display
from attache.llm.client import TieredLlm
from attache.runtime import service as service_module
from attache.runtime.service import Runtime
from sim.world import Simulation

CONFIG = "configs/fleet.yaml"
FLEET = ("drone-01", "drone-02", "drone-03", "drone-04")


def make_runtime() -> Runtime:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    runtime.tick = 100
    # 편대 명단은 세계(텔레메트리)가 줍니다. 등록은 명단 안의 기체만 받습니다.
    runtime.telemetry = {asset: {"lat": 40.70, "lon": -73.97, "alt_m": 0.0} for asset in FLEET}
    return runtime


def agent(asset: str = "drone-01", **fields) -> dict:
    return {"asset_id": asset, "world": "guarded", "model": "nemotron-3-nano:4b",
            "host": "ollama", "base_url_port": 11435, **fields}


def llm(base_url: str, nano: str, api_key: str = "") -> TieredLlm:
    """환경 변수와 무관한 클라이언트. 비운 값이 환경에서 채워지지 않게 만든 뒤에 덮습니다."""
    client = TieredLlm(base_url=base_url or "http://placeholder", models={"nano": nano})
    client.base_url, client.api_key = base_url, api_key
    return client


class DisplayNameTest(unittest.TestCase):
    def test_known_ids_read_as_names_unknown_ids_as_themselves_and_empty_as_rules(self):
        cases = {"nemotron-3-nano:4b": "Nemotron Nano 4B",
                 "nemotron-3-nano": "Nemotron Nano 30B",
                 "nemotron-3-nano:latest": "Nemotron Nano 30B",
                 "nvidia/Nemotron-3_5-Lightning": "Nemotron 3.5 Lightning",
                 "nvidia/nemotron-3-super-120b-a12b": "Nemotron Super 120B",
                 "gpt-oss-20b": "gpt-oss-20b", "": "rules", "   ": "rules", None: "rules"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(model_display(raw), expected)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.runtime = make_runtime()

    def test_a_registration_appears_in_state_with_its_display_name(self):
        status, body = self.runtime.register_agent(agent())
        self.assertEqual((status, body["ok"], body["display"]), (200, True, "Nemotron Nano 4B"))
        self.assertEqual(self.runtime.snapshot()["agents"], {"drone-01": {
            "model": "nemotron-3-nano:4b", "host": "ollama", "world": "guarded",
            "last_seen_tick": 100, "display": "Nemotron Nano 4B", "base_url_port": 11435,
            "model_ok": None}})

    def test_an_agent_without_a_model_is_rules(self):
        self.runtime.register_agent(agent("drone-02", model="", host="off", base_url_port=None))
        row = self.runtime.snapshot()["agents"]["drone-02"]
        self.assertEqual((row["model"], row["host"], row["display"]), ("", "off", "rules"))

    def test_bad_forms_are_refused_at_the_door_and_the_direct_world_is_not_ours(self):
        for body in ({}, {"asset_id": "  "}, agent(host="mars"), agent(world="moon"),
                     agent(base_url_port="eleven")):
            with self.subTest(body=body):
                self.assertEqual(self.runtime.register_agent(body)[0], 400)
        status, body = self.runtime.register_agent(agent(world="direct"))
        self.assertEqual(status, 400)
        self.assertIn("DIRECT_MODEL", body["error"])
        self.assertEqual(self.runtime.snapshot()["agents"], {})

    def test_a_silent_agent_drops_after_the_stale_window_and_a_fresh_one_stays(self):
        self.runtime.register_agent(agent("drone-01"))
        self.runtime.register_agent(agent("drone-02"))
        self.runtime.tick = 100 + service_module.AGENT_STALE_TICKS
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-01", "drone-02"})
        self.runtime.register_agent(agent("drone-02"))
        self.runtime.tick += 1
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-02"},
                         "죽은 프로세스의 모델 이름을 화면이 계속 달면 거짓말입니다")
        self.runtime.register_agent(agent("drone-01"))
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-01", "drone-02"})

    def test_a_new_round_keeps_the_registry_on_the_new_clock(self):
        self.runtime._round = 0
        self.runtime.tick = 4900
        self.runtime.register_agent(agent())
        self.runtime.tick = 3
        self.runtime._follow_round(1)
        self.assertEqual(self.runtime.snapshot()["agents"]["drone-01"]["last_seen_tick"], 3,
                         "프로세스는 그대로 — 판이 바뀌었다고 등록이 사라지지 않습니다")
        self.runtime.tick = 3 + service_module.AGENT_STALE_TICKS + 1
        self.assertEqual(self.runtime.snapshot()["agents"], {})


    def test_only_the_fleets_aircraft_register_and_only_once_the_world_is_known(self):
        status, body = self.runtime.register_agent(agent("drone-09"))
        self.assertEqual(status, 404, body)
        self.runtime.telemetry = {}
        status, body = self.runtime.register_agent(agent("drone-01"))
        self.assertEqual((status, body.get("retry")), (503, True),
                         "세계를 받기 전에는 명단을 모릅니다 — 기체는 몇 초 뒤에 다시 알립니다")
        self.assertEqual(self.runtime.snapshot()["agents"], {})

    def test_a_model_that_stopped_answering_is_labelled_rules_and_keeps_its_id(self):
        self.runtime.register_agent(agent(model_ok=False))
        row = self.runtime.snapshot()["agents"]["drone-01"]
        self.assertEqual((row["model"], row["model_ok"], row["display"]),
                         ("nemotron-3-nano:4b", False, "rules"))


class IdentityTest(unittest.TestCase):
    def test_what_an_agent_says_about_itself(self):
        ollama = llm("http://127.0.0.1:11435/v1", "nemotron-3-nano:4b")
        self.assertEqual(identity("drone-01", ollama),
                         {"asset_id": "drone-01", "world": "guarded",
                          "model": "nemotron-3-nano:4b", "host": "ollama", "base_url_port": 11435,
                          "model_ok": None})
        nebius = identity("drone-02", llm("https://api.tokenfactory.nebius.com/v1",
                                          "nvidia/Nemotron-3_5-Lightning", api_key="k"))
        self.assertEqual((nebius["model"], nebius["host"], nebius["base_url_port"]),
                         ("nvidia/Nemotron-3_5-Lightning", "nebius", 443))

    def test_a_model_it_cannot_call_is_not_claimed(self):
        # 키 없는 Nebius: 모든 호출이 401 이고 신청서는 규칙이 씁니다.
        keyless = identity("drone-01", llm("https://api.tokenfactory.nebius.com/v1",
                                           "nvidia/Nemotron-3_5-Lightning"))
        no_server = identity("drone-01", llm("", "nemotron-3-nano:4b"))
        no_nano = identity("drone-01", llm("http://127.0.0.1:11435/v1", ""))
        for said in (keyless, no_server, no_nano):
            with self.subTest(said=said):
                self.assertEqual((said["model"], said["host"], said["base_url_port"]),
                                 ("", "off", None))


    def test_whether_the_model_answers_rides_along_only_when_there_is_a_model(self):
        ollama = llm("http://127.0.0.1:11435/v1", "nemotron-3-nano:4b")
        self.assertIs(identity("drone-01", ollama, model_ok=False)["model_ok"], False)
        self.assertIsNone(identity("drone-01", llm("", "nemotron-3-nano:4b"),
                                   model_ok=True)["model_ok"])


class ModelHealthTest(unittest.TestCase):
    def test_answered_since_the_last_registration_is_true_and_all_missed_is_false(self):
        client = llm("http://127.0.0.1:11435/v1", "nemotron-3-nano:4b")
        health = ModelHealth(client)
        nano = client.stats["nano"]
        self.assertIsNone(health.check(), "아직 부른 적이 없으면 모릅니다")
        nano.fallback += 2
        self.assertIs(health.check(), False, "부른 것이 전부 규칙으로 넘어갔습니다")
        self.assertIs(health.check(), False, "그 사이에 부른 적이 없으면 지난 판단 그대로")
        nano.fallback += 3
        nano.ok += 1
        self.assertIs(health.check(), True, "하나라도 답을 받아 썼으면 모델이 쓴 것입니다")


class FakeRuntime(http.server.BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append({"path": self.path, **json.loads(self.rfile.read(length))})
        payload = b'{"ok": true, "display": "Nemotron Nano 4B"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        FakeRuntime.bodies = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def test_it_posts_once_per_period_once_accepted_and_says_what_it_is_each_time(self):
        said = iter([agent(model_ok=None), agent(model_ok=True)])
        registration = Registration(self.url, lambda: next(said), period_s=30.0, retry_s=3.0)
        self.assertTrue(registration.maybe_send(now=0.0))
        self.assertTrue(registration.accepted)
        self.assertFalse(registration.maybe_send(now=10.0))
        self.assertTrue(registration.maybe_send(now=31.0))
        self.assertEqual([b["path"] for b in FakeRuntime.bodies], ["/agents/register"] * 2)
        self.assertEqual([b["model_ok"] for b in FakeRuntime.bodies], [None, True],
                         "보낼 때마다 새로 묻습니다 — 모델이 요즘 답하는지는 바뀝니다")

    def test_a_missing_runtime_is_tried_again_within_seconds(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        registration = Registration(self.url, agent, period_s=30.0, retry_s=3.0)
        self.assertTrue(registration.maybe_send(now=0.0))
        self.assertFalse(registration.accepted,
                         "런타임이 없으면 등록은 실패하고, 기체는 계속 납니다")
        self.assertFalse(registration.maybe_send(now=2.0))
        self.assertTrue(registration.maybe_send(now=3.0), "반 분이 아니라 몇 초 뒤에 다시")

    def test_it_runs_on_its_own_thread_and_stops(self):
        registration = Registration(self.url, agent, period_s=30.0, retry_s=0.05)
        thread = registration.start()
        deadline = time.monotonic() + 3.0
        while not FakeRuntime.bodies and time.monotonic() < deadline:
            time.sleep(0.01)
        registration.stop()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(FakeRuntime.bodies), 1, "받아들여지면 다음은 30초 뒤")


class DirectModelTest(unittest.TestCase):
    def test_the_simulator_carries_the_direct_worlds_model_on_each_vehicle_and_only_there(self):
        simulation = Simulation(seed=7, direct_model="nemotron-3-nano:4b")
        direct = simulation.worlds["direct"].snapshot(0)["assets"]
        guarded = simulation.worlds["guarded"].snapshot(0)["assets"]
        self.assertEqual({v["agent_model"] for v in direct.values()}, {"nemotron-3-nano:4b"})
        self.assertTrue(all("agent_model" not in v for v in guarded.values()),
                        "런타임 세계의 모델은 런타임의 /state.agents 가 압니다")
        simulation.reset()
        self.assertEqual({v["agent_model"] for v in
                          simulation.worlds["direct"].snapshot(0)["assets"].values()},
                         {"nemotron-3-nano:4b"}, "판이 바뀌어도 같은 에이전트입니다")

    def test_rules_is_an_empty_id_and_unknown_is_no_field(self):
        rules = Simulation(seed=7, direct_model="").worlds["direct"].snapshot(0)["assets"]
        self.assertEqual({v["agent_model"] for v in rules.values()}, {""})
        unknown = Simulation(seed=7).worlds["direct"].snapshot(0)["assets"]
        self.assertTrue(all("agent_model" not in v for v in unknown.values()))


if __name__ == "__main__":
    unittest.main()
