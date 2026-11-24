"""审查发现的 5 个必须修问题的回归测试：批次超时、问题清单关联、总分声明、汇总分数、diff 失败。"""
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))
import report_validate  # noqa: E402
import run_daily  # noqa: E402
import scoring  # noqa: E402
from adapters import summary  # noqa: E402
from report_fixture import readable_report  # noqa: E402


def _all_pass_results(checklist, overrides=None):
    """全部 PASS（证据指向 src/a.py:1），再按 overrides 改个别项。"""
    results = {item["id"]: ("PASS", "none", [("src/a.py", 1)]) for item in checklist["items"]}
    if overrides:
        results.update(overrides)
    return results


def _sample_text() -> str:
    return (ROOT / "samples" / "report_sample_crm.md").read_text(encoding="utf-8")


class LinkageTest(unittest.TestCase):
    """#2：WARN/FAIL 必须与风险问题清单关联。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
        self.checklist = scoring.load_checklist()
        self.weights = scoring.parse_weights("", self.checklist)

    def _report(self):
        return readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")

    def _validate(self, text):
        return report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "standard")

    def test_normal_report_passes_linkage(self):
        self.assertEqual(self._validate(self._report())["problems"], [])

    def test_missing_issue_list_is_blocked(self):
        text = self._report()
        start = text.index("## 风险问题清单")
        end = text.index("## 分维度检查结果")
        result = self._validate(text[:start] + text[end:])
        self.assertTrue(any("未列入风险问题清单" in p for p in result["problems"]))
        self.assertTrue(any("2.2" in p for p in result["problems"]))
        self.assertTrue(any("6.4" in p for p in result["problems"]))

    def test_issue_pointing_at_pass_item_is_blocked(self):
        text = self._report()
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if line.startswith("| ID | 检查项"):
                lines.insert(index + 2, "| I-999 | 1.1 功能适合性 | P2 | 假问题 | src/a.py:1 | 影响 | 建议 | M |")
                break
        result = self._validate("\n".join(lines))
        self.assertTrue(any("不是 WARN/FAIL" in p and "1.1" in p for p in result["problems"]))

    def test_issue_pointing_at_missing_item_is_blocked(self):
        text = self._report()
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if line.startswith("| ID | 检查项"):
                lines.insert(index + 2, "| I-999 | 9.9 不存在项 | P2 | 假问题 | src/a.py:1 | 影响 | 建议 | M |")
                break
        result = self._validate("\n".join(lines))
        self.assertTrue(any("不存在" in p and "9.9" in p for p in result["problems"]))


class DeclarationTest(unittest.TestCase):
    """#3：总分/安全分声明取最后一次出现，执行摘要里回顾上次分数不误拦。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "src" / "crm").mkdir(parents=True)
        (self.repo / "src" / "crm" / "module1.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 80)) + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.checklist = scoring.load_checklist()
        self.weights = scoring.parse_weights("", self.checklist)

    def _validate(self, text):
        return report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "standard")

    def test_sample_report_still_validates_clean(self):
        self.assertEqual(self._validate(_sample_text())["problems"], [])

    def test_previous_total_in_summary_is_ignored(self):
        text = _sample_text().replace("总评分 6.6", "上次总评分 7.0，本次总评分 6.6")
        result = self._validate(text)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["scores"]["total"], 6.6)

    def test_previous_security_score_in_summary_is_ignored(self):
        text = _sample_text().replace("安全评分 7.2", "上次安全评分 8.0，本次安全评分 7.2")
        result = self._validate(text)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["scores"]["security_score"], 7.2)


class SummaryScoresTest(unittest.TestCase):
    """#4：汇总元数据只对 status 为 ok 的项目印分数。"""

    def test_failed_record_scores_are_not_printed(self):
        scores = {"total": 6.6, "grade": "存在明显风险", "scored_dimensions": 5,
                  "coverage": 0.683, "not_evaluated": 19,
                  "security_score": 7.2, "security_p0": 1, "security_p1": 0}
        report = Path(tempfile.mkdtemp()) / "p1.md"
        report.write_text("# p1\n", encoding="utf-8")
        records = [
            {"project_id": "p1", "project_name": "项目一", "status": "ok",
             "report_path": str(report), "report_scores": dict(scores)},
            {"project_id": "p2", "project_name": "项目二", "status": "error",
             "report_scores": dict(scores)},
        ]
        result = summary.generate_batch(records, report.parent, {"summary": {"enabled": False}}, "run")
        lines = result["text"].split("\n")
        ok_line = next(line for line in lines if "project_id p1" in line)
        error_line = next(line for line in lines if "project_id p2" in line)
        self.assertIn("总分 6.6", ok_line)
        self.assertNotIn("总分", error_line)


class DiffFailureTest(unittest.TestCase):
    """#5：Git diff 失败走全量时，不把上次报告当上下文传给模型。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                         "prescan_tools": [], "summary": {"enabled": False},
                         "agent": {"type": "deepseek-harness", "launcher": "local"}}
        self.repo = Path(self.settings["workdir"]) / "p1" / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "a.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
        self.project = {"project_id": "p1", "project_name": "项目一"}
        self.args = types.SimpleNamespace(force_full=False, dry_run=False)
        self.checklist = scoring.load_checklist()

    def test_diff_failure_drops_previous_report(self):
        fast_text = readable_report(self.checklist, _all_pass_results(self.checklist),
                                    report_mode="fast")
        standard_text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "changed_files", return_value=None), \
             mock.patch.object(run_daily, "call_agent", side_effect=[
                 {"raw_text": fast_text, "event_log": []},
                 {"raw_text": standard_text, "event_log": []}]) as agent:
            first = run_daily.scan_project(self.project, dict(self.settings, fast_mode=True), "2026-11-23", self.args)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-24", self.args)
        self.assertEqual([first["status"], second["status"]], ["ok", "ok"])
        self.assertEqual(second["scan_type"], "full")
        self.assertFalse((Path(self.settings["workdir"]) / "p1" / "context" / "previous_report.md").exists())
        self.assertNotIn("previous_report.md", agent.call_args.args[0])


class BatchAbortTest(unittest.TestCase):
    """#1：批次超时后孤儿线程不落盘、不写 state，进程不被拖住。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                         "prescan_tools": [], "summary": {"enabled": False},
                         "agent": {"type": "deepseek-harness", "launcher": "local"}}
        self.repo = Path(self.settings["workdir"]) / "p1" / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "a.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
        self.project = {"project_id": "p1", "project_name": "项目一"}
        self.args = types.SimpleNamespace(force_full=True, dry_run=False, run_id="run-1")
        self.checklist = scoring.load_checklist()
        self.out = self.root / "out"

    def _valid_response(self):
        text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        return {"raw_text": text, "event_log": [], "session_id": "s1"}

    def test_abort_set_during_model_call_blocks_landing(self):
        event = threading.Event()
        settings = dict(self.settings, _abort_event=event)

        def fake_agent(prompt, repo, ctx, agent_settings, timeout):
            event.set()
            return self._valid_response()

        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", side_effect=fake_agent):
            rec = run_daily.scan_project(self.project, settings, "2026-11-24", self.args)
        self.assertEqual(rec["status"], "error")
        self.assertIn("批次已超时", rec["error"])
        self.assertNotIn("report_path", rec)
        self.assertFalse((self.out / "state" / "p1.json").exists())
        self.assertFalse((self.out / "reports" / "2026-11-24" / "run-1" / "p1.md").exists())

    def test_abort_between_landing_and_state_write_keeps_old_state(self):
        old_state = {"last_commit": "o" * 40, "last_report": "old.md",
                     "last_scan_date": "2026-11-23", "report_mode": "standard"}
        state_file = self.out / "state" / "p1.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps(old_state), encoding="utf-8")
        event = threading.Event()
        settings = dict(self.settings, _abort_event=event)
        real_write = run_daily.write_text_atomic

        def write_spy(path, value, mode=0o644):
            if str(path).endswith("p1.md"):
                event.set()
            return real_write(path, value, mode=mode)

        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", return_value=self._valid_response()), \
             mock.patch.object(run_daily, "write_text_atomic", side_effect=write_spy):
            rec = run_daily.scan_project(self.project, settings, "2026-11-24", self.args)
        self.assertEqual(rec["status"], "error")
        self.assertIn("批次已超时", rec["error"])
        # 报告已落地（作废点在落盘之后），但 state 仍指向旧的好报告。
        self.assertTrue((self.out / "reports" / "2026-11-24" / "run-1" / "p1.md").exists())
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), old_state)

    def test_daemon_executor_exits_without_waiting_for_orphan(self):
        script = self.root / "probe_daemon.py"
        marker = self.root / "orphan_marker.txt"
        script.write_text(
            "import sys, threading, time\n"
            f"sys.path.insert(0, {str(ROOT / 'runner')!r})\n"
            "from run_daily import DaemonThreadPoolExecutor\n"
            "started = threading.Event()\n"
            "def slow():\n"
            "    started.set()\n"
            "    time.sleep(10)\n"
            f"    open({str(marker)!r}, 'w').write('done')\n"
            "ex = DaemonThreadPoolExecutor(max_workers=1)\n"
            "ex.submit(slow)\n"
            "assert started.wait(5)\n"
            "ex.shutdown(wait=False, cancel_futures=True)\n")
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=15)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLess(elapsed, 8)
        self.assertFalse(marker.exists())

    def test_batch_timeout_end_to_end(self):
        # 假模型睡 5 秒才返回；批次总时限 0.5 秒；汇总只等模型名额 1 秒。
        shutil.rmtree(self.repo)
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "app.py").write_text("pass\n")
        subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "-qm", "test"], check=True)
        projects = self.root / "projects.csv"
        with projects.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["project_id", "project_name", "git_url", "branch", "status"])
            writer.writeheader()
            writer.writerow({"project_id": "p1", "project_name": "p1", "git_url": source.as_uri(),
                             "branch": "main", "status": "在研"})
        valid_text = readable_report(self.checklist, {
            "1.1": ("PASS", "none", [("app.py", 1)]),
            "2.2": ("WARN", "P2", [("app.py", 1)]),
            "6.4": ("FAIL", "P0", [("app.py", 1)]),
        }, report_mode="standard")
        (self.root / "valid_report.md").write_text(valid_text, encoding="utf-8")
        script = self.root / "fake_dsh.py"
        script.write_text(
            "import json, time\n"
            "from pathlib import Path\n"
            "time.sleep(5)\n"
            "text = Path(__file__).with_name('valid_report.md').read_text(encoding='utf-8')\n"
            "print(json.dumps({'type':'final','reason':{'kind':'completed'},'text': text}))\n")
        settings = dict(self.settings, max_parallel=1, max_model_concurrency=1, batch_timeout_sec=0.5,
                        agent={"type": "deepseek-harness", "launcher": "local",
                               "command": [sys.executable, str(script)]})
        config = self.root / "settings.json"
        config.write_text(json.dumps(settings))
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(ROOT / "runner/run_daily.py"),
                               "--settings", str(config), "--projects", str(projects),
                               "--date", "2026-11-24", "--only", "p1"],
                              capture_output=True, text=True, timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertLess(elapsed, 15)
        runs = list((self.root / "out/results/2026-11-24").glob("*/_run.json"))
        self.assertEqual(len(runs), 1)
        run = json.loads(runs[0].read_text())
        self.assertEqual(run["projects"][0]["status"], "error")
        self.assertIn("批次总时限到", run["projects"][0]["error"])
        self.assertFalse(list((self.root / "out/reports/2026-11-24").glob("*/p1.md")))
        self.assertFalse((self.root / "out/state/p1.json").exists())


if __name__ == "__main__":
    unittest.main()
