"""代码地图只盘点跟踪路径，不泄露文件内容，也不遗漏锁文件元信息。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import code_map  # noqa: E402


class CodeMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for path, content in {
            "src/main.py": "SYNTHETIC_PRIVATE_CONTENT\n",
            "tests/test_main.py": "assert True\n",
            ".github/workflows/check.yml": "name: test\n",
            "package.json": "{}\n",
            "yarn.lock": "large dependency contents\n",
            "vendor/lib.py": "third party\n",
            ".env": "TEST_ONLY_SECRET\n",
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (self.repo / "alias.txt").symlink_to(self.repo / ".env")
        subprocess.run(["git", "-C", str(self.repo), "add", "-f", "."], check=True, capture_output=True)

    def test_map_is_bounded_and_preserves_lock_metadata(self):
        result = code_map.build(self.repo, "abc123", ["vendor/", "*.lock"], limit=2)
        self.assertIn("yarn.lock", result["lockfiles"])
        self.assertNotIn("yarn.lock", result["files"])
        self.assertIn("package.json", result["manifests"])
        self.assertIn("src/main.py", result["entrypoints"])
        self.assertIn("tests/test_main.py", result["tests"])
        self.assertIn(".github/workflows/check.yml", result["ci"])
        serialized = json.dumps(result)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT", serialized)
        self.assertNotIn("TEST_ONLY_SECRET", serialized)
        self.assertNotIn("vendor/lib.py", serialized)
        self.assertNotIn("alias.txt", serialized)
        self.assertEqual(result["counts"]["protected"], 2)
        self.assertGreater(result["omitted_per_list"]["files"], 0)

    def test_non_git_repository_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "Git"):
                code_map.build(Path(d), "c", [])


if __name__ == "__main__":
    unittest.main()
