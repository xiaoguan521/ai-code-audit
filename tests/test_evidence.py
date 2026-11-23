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
        self.assertEqual(run_daily.check_output(r, CK, self.repo), [])

    def test_known_repo_prefix_is_normalized_but_traversal_is_rejected(self):
        r = self.result({"path": "/workspace/repo/./a.py", "start_line": 1})
        self.assertEqual(run_daily.check_output(r, CK, self.repo), [])
        self.assertEqual(r["items"][0]["evidence"][0]["path"], "a.py")
        r = self.result({"path": "/workspace/repo/../outside.txt", "start_line": 1})
        self.assertTrue(run_daily.check_output(r, CK, self.repo))

    def test_nonexistent_path_bad_line_and_fabricated_snippet_fail(self):
        for ref, expected in [
            ({"path": "missing.py", "start_line": 1}, "文件不存在"),
            ({"path": "a.py", "start_line": 4}, "超出文件范围"),
            ({"path": "a.py", "start_line": 2, "snippet": "invented"}, "片段与所引用行不一致"),
            ({"path": "a.py", "start_line": 3, "end_line": 2}, "行号无效"),
        ]:
            with self.subTest(ref=ref):
                self.assertTrue(any(expected in e for e in run_daily.check_output(self.result(ref), CK, self.repo)))

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
        self.assertTrue(any("问题 1.1" in e for e in run_daily.check_output(r, CK, self.repo)))

    def test_pass_requires_evidence(self):
        r = valid_result()
        r["items"][0]["evidence"] = []
        self.assertTrue(any("1.1" in e and "无证据" in e for e in run_daily.check_output(r, CK, self.repo)))

    def test_issue_fingerprint_ignores_model_title_but_distinguishes_location(self):
        first = {"item_id": "6.4", "title": "旧措辞", "evidence": [{"path": "a.py", "start_line": 2}]}
        second = {"item_id": "6.4", "title": "完全不同的措辞", "evidence": [{"path": "a.py", "start_line": 2}]}
        moved = {"item_id": "6.4", "title": "完全不同的措辞", "evidence": [{"path": "a.py", "start_line": 3}]}
        self.assertEqual(run_daily.scoring.issue_fingerprint("p1", first),
                         run_daily.scoring.issue_fingerprint("p1", second))
        self.assertNotEqual(run_daily.scoring.issue_fingerprint("p1", first),
                            run_daily.scoring.issue_fingerprint("p1", moved))


class TrustBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                         "prescan_tools": [], "agent": {"type": "claude"}}
        self.project = {"project_id": "p1", "project_name": "示例"}
        self.args = types.SimpleNamespace(force_full=False, dry_run=False)
        repo = Path(self.settings["workdir"]) / "p1" / "repo"
        repo.mkdir(parents=True)
        (repo / "a.py").write_text("pass\n", encoding="utf-8")

    def test_invalid_evidence_is_downgraded_without_model_retry(self):
        result = valid_result()
        result["items"][0]["evidence"] = [{"path": "missing.py", "start_line": 999}]
        output = {"result": result, "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", return_value=output):
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["metrics"]["model_attempts"][0]["evidence_check"]["invalid_references"], 1)
        self.assertEqual(rec["metrics"]["model_attempts"][0]["evidence_check"]["invalid_references_before_repair"], 1)
        self.assertTrue((self.root / "out/results/2026-11-22/p1.json").exists())
        self.assertFalse((self.root / "out/unverified/2026-11-22/p1.json").exists())
        stored = json.loads((self.root / "out/results/2026-11-22/p1.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["items"][0]["result"], "NEED_INFO-证据不足")
        self.assertEqual(stored["items"][0]["evidence"], [])

    def test_unchanged_result_reuses_but_scope_change_forces_full(self):
        def fake_agent(*_args):
            return {"result": copy.deepcopy(valid_result()), "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", side_effect=fake_agent) as agent:
            first = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-23", self.args, CK)
            updated = dict(self.project, scope_out="generated/")
            third = run_daily.scan_project(updated, self.settings, "2026-11-24", self.args, CK)
        self.assertEqual([first["mode"], second["mode"], third["mode"]], ["full", "reuse", "full"])
        self.assertEqual([first["status"], second["status"], third["status"]], ["ok", "ok", "ok"])
        self.assertEqual(agent.call_count, 2)

    def test_model_change_invalidates_cache_but_key_rotation_does_not(self):
        env = self.root / "model.env"
        settings = copy.deepcopy(self.settings)
        settings["agent"]["env_file"] = str(env)
        env.write_text("OPENAI_MODEL=first\nOPENAI_API_KEY=old\n", encoding="utf-8")
        first = run_daily.audit_fingerprint(self.project, settings)
        env.write_text("OPENAI_MODEL=first\nOPENAI_API_KEY=new\n", encoding="utf-8")
        self.assertEqual(first, run_daily.audit_fingerprint(self.project, settings))
        env.write_text("OPENAI_MODEL=second\nOPENAI_API_KEY=new\n", encoding="utf-8")
        self.assertNotEqual(first, run_daily.audit_fingerprint(self.project, settings))

    def test_new_commit_with_identical_files_reuses_and_records_new_commit(self):
        output = {"result": valid_result(), "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "sync_repo", side_effect=["a" * 40, "b" * 40]), \
                mock.patch.object(run_daily, "changed_files", return_value=[]), \
                mock.patch.object(run_daily, "call_agent", return_value=output) as agent:
            first = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
            second = run_daily.scan_project(self.project, self.settings, "2026-11-23", self.args, CK)
        self.assertEqual((first["mode"], second["mode"], second["status"]), ("full", "reuse", "ok"))
        self.assertEqual(agent.call_count, 1)
        data = json.loads((self.root / "out/results/2026-11-23/p1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["meta"]["commit"], "b" * 40)

    def test_excluded_only_change_reuses_without_model(self):
        settings = copy.deepcopy(self.settings)
        settings["default_scope_out"] = ["dist/"]
        output = {"result": valid_result(), "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "sync_repo", side_effect=["a" * 40, "b" * 40]), \
                mock.patch.object(run_daily, "changed_files", return_value=["dist/bundle.js"]), \
                mock.patch.object(run_daily, "call_agent", return_value=output) as agent:
            first = run_daily.scan_project(self.project, settings, "2026-11-22", self.args, CK)
            second = run_daily.scan_project(self.project, settings, "2026-11-23", self.args, CK)
        self.assertEqual((first["mode"], second["mode"]), ("full", "reuse"))
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(second["ignored_changes"], ["dist/bundle.js"])

    def test_same_workspace_cannot_run_two_batches(self):
        with run_daily.batch_lock(self.settings):
            with self.assertRaisesRegex(RuntimeError, "另一扫描任务"):
                with run_daily.batch_lock(self.settings):
                    self.fail("重复调度不应获得锁")
        with run_daily.batch_lock(self.settings):
            pass

    def test_misspelled_backend_never_falls_back_to_claude(self):
        settings = {"agent": {"type": "pi-dockerr"}}
        with mock.patch.object(run_daily, "call_claude") as claude:
            with self.assertRaisesRegex(RuntimeError, "agent.type"):
                run_daily.call_agent("", self.root, self.root, settings, 1)
            claude.assert_not_called()

    def test_metrics_keep_failed_and_successful_attempts(self):
        first = run_daily.AgentOutputError("输出截断", stats={"turns": 3, "tool_calls": 2})
        second = {"result": valid_result(), "cost_usd": None, "session_id": "s",
                  "stats": {"turns": 2, "tool_calls": 1, "usage_total": {"input": 20, "output": 10}}}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", side_effect=[first, second]):
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
        self.assertEqual(rec["status"], "ok")
        metrics = rec["metrics"]
        self.assertIn("git_sync", metrics["stages_seconds"])
        self.assertIn("prescan", metrics["stages_seconds"])
        self.assertGreaterEqual(metrics["total_seconds"], 0)
        self.assertEqual([a["status"] for a in metrics["model_attempts"]], ["invalid", "ok"])
        self.assertEqual([a["stats"]["turns"] for a in metrics["model_attempts"]], [3, 2])
        stored = json.loads((self.root / "out/results/2026-11-22/p1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["meta"]["metrics"]["model_attempts"]), 2)
        self.assertEqual(stored["meta"]["evidence_check"], {"references": 64, "invalid_references": 0})

    def test_known_hard_limit_does_not_repeat_unchanged_session(self):
        error = run_daily.AgentOutputError("模型达到长度上限", stats={"turns": 2}, retryable=False)
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", side_effect=error) as agent:
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
        self.assertEqual(rec["status"], "error")
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(rec["metrics"]["model_attempts"][0]["stats"]["turns"], 2)

    def test_semantic_problem_is_repaired_without_retry(self):
        first = valid_result()
        first["items"][0]["result"] = "WARN"
        def agent(_prompt, _repo, _ctx, _settings, _timeout):
            return {"result": copy.deepcopy(first), "cost_usd": None, "session_id": "s1"}
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", side_effect=agent) as mocked:
            rec = run_daily.scan_project(self.project, self.settings, "2026-11-22", self.args, CK)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(mocked.call_count, 1)
        stored = json.loads((self.root / "out/results/2026-11-22/p1.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["items"][0]["result"], "NEED_INFO-证据不足")

    def test_pi_failure_keeps_statistics_without_log_marker(self):
        stats, clean = run_daily.pi_stderr_stats('error\nAI_AUDIT_STATS:{"turns":3,"tool_calls":2}\n')
        self.assertEqual(stats, {"turns": 3, "tool_calls": 2})
        self.assertEqual(clean, "error")

    def test_failed_rerun_does_not_render_old_same_day_result(self):
        day = self.root / "out/results/2026-11-22"
        day.mkdir(parents=True)
        (day / "p1.json").write_text(json.dumps({"old": True}), encoding="utf-8")
        (day / "p1.error.txt").write_text("failed", encoding="utf-8")
        self.assertEqual(render.load_day(self.root / "out", "2026-11-22"), {})

    def test_staggered_full_scan_is_stable_and_recovers_overdue_projects(self):
        settings = {"full_scan_strategy": "staggered", "full_scan_interval_days": 7}
        start = dt.date(2026, 11, 23)
        slots = []
        for pid in ("p1", "p2", "p3", "p4", "p5"):
            due = [offset for offset in range(7)
                   if run_daily.full_scan_due(pid, start + dt.timedelta(days=offset),
                                              start + dt.timedelta(days=offset - 1), settings)]
            self.assertEqual(len(due), 1)
            slots.extend(due)
        self.assertGreater(len(set(slots)), 1)
        self.assertTrue(run_daily.full_scan_due("p1", start, start - dt.timedelta(days=7), settings))
        self.assertFalse(run_daily.full_scan_due("p1", start, start, settings))

    def test_legacy_weekday_schedule_backfills_missed_full_scan(self):
        settings = {"full_scan_weekday": 6, "reuse_if_unchanged_days": 7}
        monday = dt.date(2026, 11, 23)
        self.assertTrue(run_daily.full_scan_due("p1", monday, monday - dt.timedelta(days=7), settings))
        self.assertFalse(run_daily.full_scan_due("p1", monday, monday - dt.timedelta(days=2), settings))

    def test_incremental_carry_is_checked_against_previous_and_changed_files(self):
        previous = valid_result()
        result = copy.deepcopy(previous)
        for item in result["items"]:
            item["carried_over"] = True
        problems = run_daily.incremental_problems(result, previous, ["a.py"], False)
        self.assertTrue(any("1.1" in p and "旧证据文件已变更" in p for p in problems))
        self.assertTrue(any("6.4" in p and "不得标记为沿用" in p for p in problems))
        result["items"][0]["conclusion"] = "改变了结论"
        problems = run_daily.incremental_problems(result, previous, ["new.py"], False)
        self.assertTrue(any("1.1" in p and "结论或证据已改变" in p for p in problems))

    def test_incremental_config_and_material_changes_invalidate_relevant_carry(self):
        previous = valid_result()
        result = copy.deepcopy(previous)
        for item in result["items"]:
            item["carried_over"] = True
        config_problems = run_daily.incremental_problems(result, previous, ["package.json"], False)
        self.assertTrue(any("4.6" in p and "不得标记为沿用" in p for p in config_problems))
        material_problems = run_daily.incremental_problems(result, previous, [], True)
        self.assertTrue(any("1.1" in p and "不得标记为沿用" in p for p in material_problems))
        changed = result["items"][0]
        changed.update(carried_over=False, result="NEED_INFO-补充材料")
        self.assertTrue(any("必须原样沿用" in p for p in run_daily.incremental_problems(result, previous, [], False)))

    def test_changed_symlink_target_invalidates_carried_evidence(self):
        repo = Path(self.settings["workdir"]) / "p1" / "repo"
        (repo / "alias.py").symlink_to(repo / "a.py")
        previous = valid_result()
        previous["items"][0]["evidence"] = [{"path": "alias.py", "start_line": 1}]
        result = copy.deepcopy(previous)
        result["items"][0]["carried_over"] = True
        problems = run_daily.incremental_problems(result, previous, ["a.py"], False, repo)
        self.assertTrue(any("1.1" in p and "旧证据文件已变更" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
