"""The agent package must not be able to reach the world, by construction."""

import ast
import pathlib
import unittest

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "attache" / "agent"
FORBIDDEN = ("attache.runtime", "attache.adapters")


class IsolationTest(unittest.TestCase):
    def test_agent_never_imports_runtime_or_adapters(self):
        offenders = []
        for path in AGENT_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.startswith(FORBIDDEN):
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], f"에이전트가 실행 경로를 import 했습니다: {offenders}")

    def test_agent_image_copies_neither(self):
        dockerfile = (AGENT_DIR.parent.parent / "docker" / "Dockerfile").read_text()
        stage = dockerfile.split("AS agent")[1].split("FROM base AS runtime")[0]
        copied = [line for line in stage.splitlines() if line.startswith("COPY ")]
        self.assertTrue(copied)
        for line in copied:
            self.assertNotIn("attache/adapters", line)
            self.assertNotIn("attache/runtime", line)
        # 빌드가 스스로도 확인하게 해둡니다
        self.assertIn("test ! -e /app/attache/adapters", stage)


if __name__ == "__main__":
    unittest.main()
