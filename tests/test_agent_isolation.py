"""The agent package must not be able to reach the world, by construction."""

import ast
import pathlib
import unittest

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "drone" / "agent"
FORBIDDEN = ("backend", "drone.direct", "pymavlink")


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

    def test_guarded_image_carries_no_way_to_act(self):
        dockerfile = (AGENT_DIR.parent / "Dockerfile").read_text()
        stage = dockerfile.split("FROM base AS guarded\n")[1].split("FROM base AS")[0]
        copied = [line for line in stage.splitlines() if line.startswith("COPY ")]
        self.assertTrue(copied)
        for line in copied:
            for forbidden in ("backend", "drone/direct", "COPY drone /"):
                self.assertNotIn(forbidden, line)
        self.assertNotIn("pymavlink", stage)
        # 빌드가 스스로도 확인하게 해둡니다
        self.assertIn("test ! -e /app/backend", stage)
        self.assertIn("test ! -e /app/drone/direct", stage)

    def test_both_wirings_share_the_same_brain(self):
        """직결 쪽을 못나게 만들지 않았다는 걸 코드로 못박아 둡니다."""
        direct = (AGENT_DIR.parent / "direct" / "loop.py").read_text()
        for shared in ("from drone.agent.detect import detect",
                       "from drone.agent.propose import COSTS, Proposer"):
            self.assertIn(shared, direct)


if __name__ == "__main__":
    unittest.main()
