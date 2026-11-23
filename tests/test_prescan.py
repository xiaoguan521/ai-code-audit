"""预扫描报告要区分跳过、失败、无输出与真实告警。"""
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import run_daily  # noqa: E402


class PrescanStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.out = Path(self.tmp.name) / "context/prescan"

    def test_missing_tools_and_rules_are_skipped(self):
        with mock.patch.object(run_daily.shutil, "which", side_effect=lambda name: None if name == "cloc" else "/bin/tool"):
            status = run_daily.prescan(self.repo, self.out, ["cloc", "semgrep", "unknown"], {})
        self.assertEqual(status["cloc"], {"status": "skipped", "reason": "未安装"})
        self.assertEqual(status["semgrep"], {"status": "skipped", "reason": "未配置离线规则"})
        self.assertEqual(status["unknown"]["status"], "skipped")

    def test_lizard_warning_exit_is_success_but_bad_json_is_not(self):
        def fake_run(cmd, **_kwargs):
            if cmd[0] == "lizard":
                return subprocess.CompletedProcess(cmd, 1, stdout="app.py:1: warning: complex\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")
        with mock.patch.object(run_daily.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(run_daily, "run", side_effect=fake_run):
            status = run_daily.prescan(self.repo, self.out, ["lizard", "cloc"], {})
        self.assertEqual(status["lizard"]["status"], "ok")
        self.assertEqual(status["lizard"]["exit_code"], 1)
        self.assertEqual(status["cloc"]["status"], "failed")
        self.assertTrue((self.out / "lizard_warnings.txt").exists())
        self.assertFalse((self.out / "cloc.json").exists())

    def test_missing_or_failed_report_is_not_claimed_as_success(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(run_daily.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(run_daily, "run", return_value=done):
            status = run_daily.prescan(self.repo, self.out, ["gitleaks"], {})
        self.assertEqual(status["gitleaks"]["status"], "no_output")
        self.assertFalse((self.out / "gitleaks.json").exists())

        def failure(cmd, **_kwargs):
            Path(cmd[cmd.index("--report-path") + 1]).write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="failed")
        with mock.patch.object(run_daily.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(run_daily, "run", side_effect=failure):
            status = run_daily.prescan(self.repo, self.out, ["gitleaks"], {})
        self.assertEqual(status["gitleaks"]["status"], "failed")
        self.assertFalse((self.out / "gitleaks.json").exists())

    def test_optional_parallelism_runs_independent_tools_concurrently(self):
        barrier = threading.Barrier(2, timeout=3)
        def fake_run(cmd, **_kwargs):
            barrier.wait()
            stdout = '{}' if cmd[0] == 'cloc' else ''
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr='')
        with mock.patch.object(run_daily.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(run_daily, "run", side_effect=fake_run):
            status = run_daily.prescan(self.repo, self.out, ["cloc", "lizard"], {"prescan_parallelism": 2})
        self.assertEqual({tool: data["status"] for tool, data in status.items()}, {"cloc": "ok", "lizard": "ok"})

    def test_incremental_prescan_passes_only_changed_files_and_excludes_scope(self):
        (self.repo / "changed.py").write_text("print(1)\n", encoding="utf-8")
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "bundle.js").write_text("minified\n", encoding="utf-8")
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(cmd)
            if cmd[0] == "cloc":
                return subprocess.CompletedProcess(cmd, 0, stdout='{"SUM": {"code": 1}}', stderr="")
            if cmd[0] == "lizard":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(run_daily.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(run_daily, "run", side_effect=fake_run):
            status = run_daily.prescan(self.repo, self.out, ["cloc", "lizard"],
                                       {"default_scope_out": ["dist/"]}, ["changed.py", "dist/bundle.js"])
        self.assertEqual({tool: info["status"] for tool, info in status.items()}, {"cloc": "ok", "lizard": "ok"})
        self.assertTrue(any("changed.py" in cmd and "dist/bundle.js" not in cmd for cmd in commands))


if __name__ == "__main__":
    unittest.main()
