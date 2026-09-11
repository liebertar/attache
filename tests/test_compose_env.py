"""docker-compose.local.yml's model settings: what .env.local turns on, read as text (no Docker).

A Nebius key alone must reach Token Factory wherever the runtime side calls a model, and an
explicitly empty MODEL_ULTRA must turn the arbiter model off — compose's default is a Nebius id that
a local Ollama does not have. The direct world stays on rules unless DIRECT_LLM_URL is given.
"""

import unittest
from pathlib import Path

import yaml

NEBIUS = "${NEBIUS_API_KEY:+https://api.tokenfactory.nebius.com/v1}"


class ComposeEnvTest(unittest.TestCase):
    def setUp(self):
        text = Path("docker-compose.local.yml").read_text(encoding="utf-8")
        self.services = yaml.safe_load(text)["services"]

    def test_a_nebius_key_alone_reaches_token_factory_on_the_runtime_side_only(self):
        self.assertIn(NEBIUS, self.services["runtime"]["environment"]["LLM_BASE_URL"])
        for number in range(1, 5):
            guarded = self.services[f"guarded-drone-0{number}"]["environment"]["LLM_BASE_URL"]
            self.assertIn(NEBIUS, guarded)
            direct = self.services[f"direct-drone-0{number}"]["environment"]["LLM_BASE_URL"]
            self.assertEqual(direct, "${DIRECT_LLM_URL:-}", "직결 세계는 규칙이 기본입니다")

    def test_an_empty_model_ultra_turns_the_arbiter_off(self):
        ultra = self.services["runtime"]["environment"]["MODEL_ULTRA"]
        self.assertTrue(ultra.startswith("${MODEL_ULTRA-"), ultra)

    def test_the_env_example_compose_block_has_every_line_the_ollama_case_needs(self):
        text = Path(".env.local.example").read_text(encoding="utf-8")
        start = text.index("# ---- docker compose")
        lines = text[start:text.index("\n\n", start)].splitlines()
        for wanted in ("#LLM_BASE_URL=http://host.docker.internal:11439/v1",
                       "#LLM_URL_1=http://host.docker.internal:11435/v1",
                       "#LLM_URL_4=http://host.docker.internal:11438/v1",
                       "#MODEL_NANO=nemotron-3-nano:4b", "#MODEL_SUPER=nemotron-3-nano:4b",
                       "#MODEL_ULTRA="):
            with self.subTest(line=wanted):
                self.assertIn(wanted, lines)


    def test_the_dev_file_takes_every_service_from_the_local_file(self):
        dev = yaml.safe_load(Path("docker-compose.dev.yml").read_text(encoding="utf-8"))["services"]
        self.assertEqual(set(dev), set(self.services))
        for name, service in dev.items():
            with self.subTest(service=name):
                self.assertEqual(service["extends"],
                                 {"file": "docker-compose.local.yml", "service": name})
                self.assertEqual(service["restart"], "always")
        self.assertEqual(dev["runtime"]["volumes"], ["ledger-dev:/data"],
                         "dev 의 원장·접수 기록은 로컬과 다른 볼륨")

    def test_both_files_group_the_stack_under_sky_net(self):
        for path in ("docker-compose.local.yml", "docker-compose.dev.yml"):
            compose = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(compose["name"], "sky-net", path)


if __name__ == "__main__":
    unittest.main()
