"""源码证据、失败关闭、结果复用与调度互斥的回归测试。"""
import copy
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "quality"))
import offline_validation
import scoring
import evidence  # noqa: E402
import render
import run_daily
from test_runner import CK, valid_result


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "a.py").write_text("first\nsecond\nthird\n", encoding="utf-8")

    def result(self, ref):
        r = valid_result()
        r["items"][0]["evidence"] = [ref]
        return r

    def test_real_lines_and_snippet_pass(self):
        r = self.result({"path": "a.py", "start_line": 2, "end_line": 3, "snippet": "second\nthird"})
        self.assertEqual(offline_validation.check_output(r, CK, self.repo), [])

    def test_known_repo_prefix_is_normalized_but_traversal_is_rejected(self):
        r = self.result({"path": "/workspace/repo/./a.py", "start_line": 1})
        self.assertEqual(offline_validation.check_output(r, CK, self.repo), [])
        self.assertEqual(r["items"][0]["evidence"][0]["path"], "a.py")
        r = self.result({"path": "/workspace/repo/../outside.txt", "start_line": 1})
        self.assertTrue(offline_validation.check_output(r, CK, self.repo))

    def test_nonexistent_path_bad_line_and_fabricated_snippet_fail(self):
        for ref, expected in [
            ({"path": "missing.py", "start_line": 1}, "文件不存在"),
            ({"path": "a.py", "start_line": 4}, "超出文件范围"),
            ({"path": "a.py", "start_line": 2, "snippet": "invented"}, "片段与所引用行不一致"),
            ({"path": "a.py", "start_line": 3, "end_line": 2}, "行号无效"),
        ]:
            with self.subTest(ref=ref):
                self.assertTrue(any(expected in e for e in offline_validation.check_output(self.result(ref), CK, self.repo)))

    def test_traversal_symlink_and_env_are_not_read(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repo / "link.txt").symlink_to(outside)
        (self.repo / ".env").write_text("private\n", encoding="utf-8")
        for path in ("../outside.txt", str(outside), "link.txt", ".env"):
            with self.subTest(path=path):
                self.assertTrue(evidence.verify(self.result({"path": path, "start_line": 1}), self.repo))

    def test_issue_evidence_also_requires_verification(self):
        r = valid_result()
        r["items"][0].update(result="FAIL", risk="P1")
        r["issues"] = [{"item_id": "1.1", "level": "P1", "title": "示例", "impact": "风险",
                        "suggestion": "修复", "effort": "S",
                        "evidence": [{"path": "missing.py", "start_line": 1}]}]
        self.assertTrue(any("问题 1.1" in e for e in offline_validation.check_output(r, CK, self.repo)))

    def test_pass_requires_evidence(self):
        r = valid_result()
        r["items"][0]["evidence"] = []
        self.assertTrue(any("1.1" in e and "无证据" in e for e in offline_validation.check_output(r, CK, self.repo)))

    def test_issue_fingerprint_ignores_model_title_but_distinguishes_location(self):
        first = {"item_id": "6.4", "title": "旧措辞", "evidence": [{"path": "a.py", "start_line": 2}]}
        second = {"item_id": "6.4", "title": "完全不同的措辞", "evidence": [{"path": "a.py", "start_line": 2}]}
        moved = {"item_id": "6.4", "title": "完全不同的措辞", "evidence": [{"path": "a.py", "start_line": 3}]}
        self.assertEqual(scoring.issue_fingerprint("p1", first),
                         scoring.issue_fingerprint("p1", second))
        self.assertNotEqual(scoring.issue_fingerprint("p1", first),
                            scoring.issue_fingerprint("p1", moved))


class TrustBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                         "prescan_tools": [], "agent": {"type": "deepseek-harness", "launcher": "local"}}
        self.project = {"project_id": "p1", "project_name": "示例"}
        self.args = types.SimpleNamespace(force_full=False, dry_run=False)
        repo = Path(self.settings["workdir"]) / "p1" / "repo"
        repo.mkdir(parents=True)
        (repo / "a.py").write_text("pass\n", encoding="utf-8")







    def test_other_backends_are_rejected(self):
        for kind in ("pi-docker", "qwen-docker", "claude", "unknown"):
            settings = {"agent": {"type": kind}}
            with mock.patch.object(run_daily, "call_deepseek_harness") as harness:
                with self.assertRaisesRegex(RuntimeError, "agent.type"):
                    run_daily.call_agent("", self.root, self.root, settings, 1)
                harness.assert_not_called()











if __name__ == "__main__":
    unittest.main()
