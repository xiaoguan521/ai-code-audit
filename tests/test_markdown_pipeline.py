"""Production Markdown workflow: autonomous sessions, isolation and batch summaries."""
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
import run_daily
import scoring
from adapters import summary
from report_fixture import readable_report


def _all_pass_results(checklist, overrides=None):
    """全部 PASS（证据指向 src/a.py:1），再按 overrides 改个别项。"""
    results = {item["id"]: ("PASS", "none", [("src/a.py", 1)]) for item in checklist["items"]}
    if overrides:
        results.update(overrides)
    return results


class MarkdownPipelineTest(unittest.TestCase):
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

    def test_invalid_report_is_blocked_without_rescanning(self):
        text = "# 项目报告\n\n总分：7.2\n"
        response = {"raw_text": text, "event_log": [{"type": "final"}], "session_id": "session-1"}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", return_value=response) as agent:
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-24", self.args)
        self.assertEqual(rec["status"], "error")
        self.assertEqual(agent.call_count, 1)
        self.assertNotIn("report_path", rec)
        self.assertTrue(rec["report_validation"]["problems"])
        self.assertFalse((self.root / "out" / "state" / "p1.json").exists())
        self.assertFalse((self.root / "out" / "reports" / "2026-11-24" / "run-1" / "p1.md").exists())

    def test_valid_report_lands_with_scores(self):
        text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        response = {"raw_text": text, "event_log": [{"type": "final"}], "session_id": "session-1"}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", return_value=response) as agent:
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-24", self.args)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(Path(rec["report_path"]).read_text(), text)
        self.assertTrue(rec["report_validation"]["ok"])
        self.assertIn("total", rec["report_scores"])
        state = json.loads((self.root / "out" / "state" / "p1.json").read_text())
        self.assertEqual(state["scores"], rec["report_scores"])
        prompt = agent.call_args.args[0]
        for word in ("64 项", "安全分", "评分依据", "当前会话", "只补查"):
            self.assertIn(word, prompt)

    def test_failed_session_does_not_start_another_review(self):
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", side_effect=run_daily.AgentCallError("timeout", agent_status="timeout")) as agent:
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-24", self.args)
        self.assertEqual((rec["status"], rec["agent_status"], agent.call_count), ("error", "timeout", 1))
        self.assertNotIn("report_path", rec)

    def test_fast_mode_delivers_markdown_without_evidence_contract(self):
        text = readable_report(self.checklist, _all_pass_results(self.checklist),
                               report_mode="fast")
        response = {"raw_text": text, "event_log": []}
        settings = dict(self.settings, fast_mode=True)
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "call_agent", return_value=response) as agent:
            rec = run_daily.scan_project(self.project, settings, "2026-11-24", self.args)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["report_mode"], "fast")
        self.assertEqual(Path(rec["report_path"]).read_text(), text)
        prompt = agent.call_args.args[0]
        self.assertIn("report_mode: fast", prompt)
        self.assertIn("不生成逐项证据", prompt)
        self.assertNotIn("本次是标准审查", prompt)
        self.assertIn("64 项", prompt)
        self.assertIn("安全分", prompt)

    def test_standard_mode_can_use_previous_fast_report_without_assuming_evidence(self):
        fast_text = readable_report(self.checklist, _all_pass_results(self.checklist),
                                    report_mode="fast")
        standard_text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        args = types.SimpleNamespace(force_full=False, dry_run=False)
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "changed_files", return_value=[]), \
             mock.patch.object(run_daily, "call_agent", side_effect=[
                 {"raw_text": fast_text, "event_log": []},
                 {"raw_text": standard_text, "event_log": []}]) as agent:
            first = run_daily.scan_project(self.project, dict(self.settings, fast_mode=True), "2026-11-23", args)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-24", args)
        self.assertEqual((first["report_mode"], second["report_mode"]), ("fast", "standard"))
        self.assertEqual(second["scan_type"], "incremental")
        self.assertIn("上次是快速报告", agent.call_args.args[0])
        self.assertIn("本次标准审查须", agent.call_args.args[0])

    def test_previous_report_is_context_not_a_runner_decision(self):
        fast_text = readable_report(self.checklist, _all_pass_results(self.checklist),
                                    report_mode="fast")
        standard_text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        args = types.SimpleNamespace(force_full=False, dry_run=False)
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "changed_files", return_value=["src/a.py"]), \
             mock.patch.object(run_daily, "call_agent", side_effect=[
                 {"raw_text": fast_text, "event_log": []},
                 {"raw_text": standard_text, "event_log": []}]) as agent:
            first = run_daily.scan_project(self.project, self.settings, "2026-11-23", args)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-24", args)
        self.assertEqual([first["status"], second["status"]], ["ok", "ok"])
        self.assertEqual(agent.call_count, 2)
        prompt = agent.call_args.args[0]
        self.assertIn("previous_report.md", prompt)
        self.assertIn("changed_files.json", prompt)
        self.assertIn("自行判断", prompt)
        self.assertNotIn("预计算的本次重判", prompt)

    def test_missing_git_diff_never_masquerades_as_no_changes(self):
        fast_text = readable_report(self.checklist, _all_pass_results(self.checklist),
                                    report_mode="fast")
        standard_text = readable_report(self.checklist, _all_pass_results(self.checklist, {
            "2.2": ("WARN", "P2", [("src/a.py", 5)]),
            "6.4": ("FAIL", "P0", [("src/a.py", 10, 12)]),
        }), report_mode="standard")
        args = types.SimpleNamespace(force_full=False, dry_run=False)
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "changed_files", return_value=None), \
             mock.patch.object(run_daily, "call_agent", side_effect=[
                 {"raw_text": fast_text, "event_log": []},
                 {"raw_text": standard_text, "event_log": []}]) as agent:
            first = run_daily.scan_project(self.project, self.settings, "2026-11-23", args)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-24", args)
        self.assertEqual([first["status"], second["status"]], ["ok", "ok"])
        self.assertEqual(second["scan_type"], "full")
        self.assertIn("scan_type: full", agent.call_args.args[0])
        self.assertNotIn("changed_files.json", agent.call_args.args[0])

    def test_project_locks_allow_distinct_projects(self):
        with run_daily.project_lock(self.settings, "p1"):
            with run_daily.project_lock(self.settings, "p2"):
                with self.assertRaisesRegex(RuntimeError, "已有扫描任务"):
                    with run_daily.project_lock(self.settings, "p1"):
                        self.fail("same project must be locked")

    def test_model_slots_limit_separate_sessions(self):
        settings = dict(self.settings, max_model_concurrency=1)
        started = threading.Event()
        released = threading.Event()
        def second():
            started.set()
            with run_daily.model_slot(settings):
                released.set()
        with run_daily.model_slot(settings):
            thread = threading.Thread(target=second)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(released.wait(0.25))
        self.assertTrue(released.wait(2))
        thread.join()

    def test_summary_reads_only_reports_and_failure_keeps_index(self):
        report = self.root / "p1.md"
        report.write_text("# p1\n总分 7.2\n")
        records = [{"project_id": "p1", "project_name": "项目一", "status": "ok", "report_path": str(report)},
                   {"project_id": "p2", "project_name": "项目二", "status": "error"}]
        settings = dict(self.settings, summary={"enabled": True, "timeout_sec": 10})
        def fake_run(prompt, repo, ctx, agent_settings, timeout):
            self.assertEqual(repo, ctx)
            self.assertEqual([p.name for p in repo.iterdir()], ["p1.md"])
            self.assertIn("不要重新审查源码", prompt)
            self.assertEqual((repo / "p1.md").read_text(), report.read_text())
            return {"raw_text": "# 整体报告\n", "event_log": []}
        with mock.patch.object(summary.harness, "run", side_effect=fake_run):
            result = summary.generate_batch(records, self.root, settings, "run")
        self.assertEqual(result["status"], "completed")
        self.assertIn("(p1.md)", result["text"])
        self.assertIn("项目二", result["text"])
        with mock.patch.object(summary.harness, "run", side_effect=RuntimeError("offline")):
            result = summary.generate_batch(records, self.root, settings, "run")
        self.assertEqual(result["status"], "fallback")
        self.assertIn("(p1.md)", result["text"])
        self.assertEqual(report.read_text(), "# p1\n总分 7.2\n")

    def test_separate_runner_processes_scan_different_projects(self):
        # setUp 在 work/p1/repo 里放了证据文件；子进程要从空目录 clone，先清掉。
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
            for pid in ("p1", "p2"):
                writer.writerow({"project_id": pid, "project_name": pid, "git_url": source.as_uri(),
                                 "branch": "main", "status": "在研"})
        # 假模型输出一份有效报告：证据引用被扫仓库内的 app.py:1。
        valid_text = readable_report(self.checklist, {
            "1.1": ("PASS", "none", [("app.py", 1)]),
            "2.2": ("WARN", "P2", [("app.py", 1)]),
            "6.4": ("FAIL", "P0", [("app.py", 1)]),
        }, report_mode="standard")
        (self.root / "valid_report.md").write_text(valid_text, encoding="utf-8")
        script = self.root / "fake_dsh.py"
        script.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "text = Path(__file__).with_name('valid_report.md').read_text(encoding='utf-8')\n"
            "print(json.dumps({'type':'final','reason':{'kind':'completed'},'text': text}))\n")
        settings = dict(self.settings, max_parallel=2, max_model_concurrency=2,
                        agent={"type": "deepseek-harness", "launcher": "local", "command": [sys.executable, str(script)]})
        config = self.root / "settings.json"
        config.write_text(json.dumps(settings))
        common = [sys.executable, str(ROOT / "runner/run_daily.py"), "--settings", str(config),
                  "--projects", str(projects), "--date", "2026-11-24", "--only"]
        processes = [subprocess.Popen([*common, pid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for pid in ("p1", "p2")]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        runs = list((self.root / "out/results/2026-11-24").glob("*/_run.json"))
        self.assertEqual(len(runs), 2)
        self.assertEqual({json.loads(p.read_text())["projects"][0]["project_id"] for p in runs}, {"p1", "p2"})
        self.assertEqual(len(list((self.root / "out/reports/2026-11-24").glob("*/*.md"))), 4)

        summary_process = subprocess.run([*common, "p1", "p2", "--summary-only"],
                                         capture_output=True, text=True, timeout=20)
        self.assertEqual(summary_process.returncode, 0, summary_process.stdout + summary_process.stderr)
        all_runs = [json.loads(p.read_text()) for p in (self.root / "out/results/2026-11-24").glob("*/_run.json")]
        self.assertEqual(len(all_runs), 3)
        combined = next(run for run in all_runs if len(run["projects"]) == 2)
        self.assertTrue(all(item["status"] == "ok" for item in combined["projects"]))
        self.assertEqual({item["source_date"] for item in combined["projects"]}, {"2026-11-24"})
        text = Path(combined["summary_path"]).read_text()
        self.assertIn("(p1.md)", text)
        self.assertIn("(p2.md)", text)


if __name__ == "__main__":
    unittest.main()
