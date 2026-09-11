"""compose.yaml's model settings: what a .env.local turns on, read as text (no Docker needed).

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
        self.services = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))["services"]

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
        text = Path(".env.example").read_text(encoding="utf-8")
        start = text.index("# ---- docker compose")
        lines = text[start:text.index("\n\n", start)].splitlines()
        for wanted in ("#LLM_BASE_URL=http://host.docker.internal:11439/v1",
                       "#LLM_URL_1=http://host.docker.internal:11435/v1",
                       "#LLM_URL_4=http://host.docker.internal:11438/v1",
                       "#MODEL_NANO=nemotron-3-nano:4b", "#MODEL_SUPER=nemotron-3-nano:4b",
                       "#MODEL_ULTRA="):
            with self.subTest(line=wanted):
                self.assertIn(wanted, lines)


if __name__ == "__main__":
    unittest.main()
