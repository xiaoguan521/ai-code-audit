"""项目完成后立即落盘，dry-run 不覆盖正式运行日志。"""
import csv
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import render  # noqa: E402
import run_daily  # noqa: E402


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                                             "max_parallel": 2, "agent": {"type": "claude"}}), encoding="utf-8")
        self.projects = self.root / "projects.csv"
        with self.projects.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["project_id", "project_name", "status"])
            writer.writeheader()
            writer.writerows([{"project_id": "fast", "project_name": "fast", "status": "在研"},
                              {"project_id": "slow", "project_name": "slow", "status": "在研"}])
        self.log = self.root / "out/results/2026-11-23/_run.json"

    def test_progress_is_written_before_other_project_finishes(self):
        release = threading.Event()
        started = threading.Event()
        def scan(p, *_args):
            if p["project_id"] == "slow":
                started.set()
                if not release.wait(5):
                    raise RuntimeError("测试未及时释放")
            return {"project_id": p["project_id"], "status": "ok"}
        args = ["run_daily.py", "--settings", str(self.settings), "--projects", str(self.projects), "--date", "2026-11-23"]
        results = []
        with mock.patch.object(sys, "argv", args), mock.patch.object(run_daily, "scan_project", side_effect=scan), \
                mock.patch.object(render, "render_all"):
            thread = threading.Thread(target=lambda: results.append(run_daily.main()))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                deadline = time.monotonic() + 3
                progress = {}
                while time.monotonic() < deadline:
                    if self.log.exists():
                        progress = json.loads(self.log.read_text(encoding="utf-8"))
                        if len(progress.get("projects", [])) == 1:
                            break
                    time.sleep(0.01)
                self.assertEqual(progress["status"], "running")
                self.assertEqual(progress["projects"][0]["project_id"], "fast")
                self.assertEqual(progress["pending_projects"], ["slow"])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [0])
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8"))["status"], "complete")

    def test_dry_run_does_not_overwrite_formal_run_log(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text('{"previous": true}', encoding="utf-8")
        args = ["run_daily.py", "--settings", str(self.settings), "--projects", str(self.projects),
                "--date", "2026-11-23", "--dry-run"]
        def scan(p, *_args):
            return {"project_id": p["project_id"], "status": "dry-run"}
        with mock.patch.object(sys, "argv", args), mock.patch.object(run_daily, "scan_project", side_effect=scan):
            self.assertEqual(run_daily.main(), 0)
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8")), {"previous": True})
        self.assertTrue((self.log.parent / "_dry_run.json").exists())

    def test_project_id_cannot_escape_workspace(self):
        self.projects.write_text("project_id,project_name,status\n../outside,unsafe,在研\n", encoding="utf-8")
        args = ["run_daily.py", "--settings", str(self.settings), "--projects", str(self.projects)]
        with mock.patch.object(sys, "argv", args), mock.patch.object(run_daily, "scan_project") as scan:
            self.assertEqual(run_daily.main(), 2)
        scan.assert_not_called()
        self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
