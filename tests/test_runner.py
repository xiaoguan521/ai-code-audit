"""runner 的单元测试（不调用模型、不需要 Docker）：python3 -m unittest discover -s tests -v"""
import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import run_daily  # noqa: E402
import scoring  # noqa: E402

CK = scoring.load_checklist()


def valid_result() -> dict:
    """结构与语义都合格的最小结果：64 项全 PASS，没有问题条目。"""
    return {
        "inventory": {"languages": ["Python"], "frameworks": [], "build_tools": [], "file_count": 1, "loc_estimate": 10,
                      "modules": [], "checked_scope": "全部", "unchecked": []},
        "items": [{"id": it["id"], "result": "PASS", "evidence": [{"path": "a.py", "start_line": 1}], "risk": "none",
                   "conclusion": "ok", "carried_over": False} for it in CK["items"]],
        "issues": [],
        "need_info_list": [],
        "dimension_notes": [{"dimension": d["id"], "conclusion": "ok", "key_analysis": "-"} for d in CK["dimensions"]],
        "summary": {"key_findings": [], "top_risks": [], "undetermined": ""},
        "self_check": [{"check": "64 项齐全", "ok": True}],
    }


def with_semantic_problem() -> dict:
    """结构合格，但 1.1 为 WARN 却没有问题条目（语义问题）。"""
    r = valid_result()
    r["items"][0]["result"] = "WARN"
    return r


def with_structural_problem() -> dict:
    r = valid_result()
    r["items"][0]["result"] = "NEED_INFO"  # 少了后缀，枚举不接受
    return r


class SchemaTest(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(scoring.schema_errors(valid_result()), [])
        self.assertEqual(scoring.validate(valid_result(), CK), [])

    def test_missing_result_field(self):
        r = valid_result()
        del r["items"][3]["result"]
        errs = scoring.schema_errors(r)
        self.assertTrue(any("$.items[3]" in e and "result" in e for e in errs), errs)

    def test_bad_enum(self):
        errs = scoring.schema_errors(with_structural_problem())
        self.assertTrue(any("NEED_INFO" in e and "允许范围" in e for e in errs), errs)

    def test_issue_missing_title(self):
        r = valid_result()
        r["items"][0].update(result="WARN", risk="P2")
        r["issues"].append({"item_id": "1.1", "level": "P2", "evidence": [{"path": "a.py", "start_line": 1}], "impact": "-", "suggestion": "-", "effort": "S"})
        errs = scoring.schema_errors(r)
        self.assertTrue(any("$.issues[0]" in e and "title" in e for e in errs), errs)

    def test_wrong_types_and_counts(self):
        r = valid_result()
        r["items"].pop()
        r["inventory"]["file_count"] = "很多"
        r["self_check"][0]["ok"] = 1
        errs = scoring.schema_errors(r)
        self.assertTrue(any("至少需要 64 项" in e for e in errs), errs)
        self.assertTrue(any("file_count" in e and "integer" in e for e in errs), errs)
        self.assertTrue(any("ok" in e and "boolean" in e for e in errs), errs)

    def test_bool_is_not_integer(self):
        r = valid_result()
        r["inventory"]["file_count"] = True
        self.assertTrue(scoring.schema_errors(r))


class CheckOutputTest(unittest.TestCase):
    def test_structural_raises_with_full_problem_list(self):
        with self.assertRaises(run_daily.AgentOutputError) as cm:
            run_daily.check_output(with_structural_problem(), CK)
        self.assertTrue(cm.exception.problems)

    def test_semantic_returns_problems(self):
        problems = run_daily.check_output(with_semantic_problem(), CK)
        self.assertTrue(any("1.1" in p for p in problems), problems)

    def test_undetermined_items_and_dimensions_must_be_represented(self):
        result = valid_result()
        result["items"][0].update(result="NEED_INFO-证据不足", evidence=[])
        result["dimension_notes"][0]["dimension"] = "2"
        problems = run_daily.check_output(result, CK)
        self.assertTrue(any("1.1" in p and "待补充" in p for p in problems))
        self.assertTrue(any("维度结论" in p for p in problems))

    def test_need_info_cannot_reference_unknown_item(self):
        result = valid_result()
        result["need_info_list"] = [{"items": ["9.9"], "missing": "材料", "why": "原因",
                                     "form": "文档", "unlocks": "结论", "priority": "中"}]
        problems = run_daily.check_output(result, CK)
        self.assertTrue(any("不存在的检查项" in p for p in problems))
        self.assertEqual(run_daily.repair_semantics(result, CK), [])
        self.assertEqual(result["need_info_list"], [])


class RetryTest(unittest.TestCase):
    """scan_project 的重试逻辑：结构不合格的输出绝不进入评分。"""

    def run_scan(self, outputs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = {"workdir": f"{tmp.name}/work", "output_dir": f"{tmp.name}/out", "prescan_tools": [],
                    "agent": {"type": "deepseek-harness", "launcher": "local"}}
        repo = Path(settings["workdir"]) / "p1" / "repo"
        repo.mkdir(parents=True)
        (repo / "a.py").write_text("pass\n", encoding="utf-8")
        project = {"project_id": "p1", "project_name": "测试项目"}
        args = types.SimpleNamespace(force_full=True, dry_run=False)
        calls = iter(outputs)

        def fake_agent(*_a, **_k):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return {"result": copy.deepcopy(r), "cost_usd": None, "session_id": "s"}

        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                mock.patch.object(run_daily, "call_agent", side_effect=fake_agent) as agent:
            rec = run_daily.scan_project(project, settings, "2026-11-21", args, CK)
        return rec, agent.call_count

    def test_structural_then_good(self):
        rec, n = self.run_scan([with_structural_problem(), valid_result()])
        self.assertEqual((rec["status"], n, rec["validation_problems"]), ("ok", 2, 0))

    def test_retry_prompt_lists_schema_problems(self):
        with mock.patch.object(run_daily, "call_agent") as agent:
            agent.side_effect = [{"result": with_structural_problem(), "cost_usd": None, "session_id": "s"},
                                 {"result": valid_result(), "cost_usd": None, "session_id": "s"}]
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            repo = Path(tmp.name) / "w" / "p1" / "repo"
            repo.mkdir(parents=True)
            (repo / "a.py").write_text("pass\n", encoding="utf-8")
            with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40):
                run_daily.scan_project({"project_id": "p1", "project_name": "x"},
                                       {"workdir": f"{tmp.name}/w", "output_dir": f"{tmp.name}/o", "agent": {"type": "deepseek-harness", "launcher": "local"}},
                                       "2026-11-21", types.SimpleNamespace(force_full=True, dry_run=False), CK)
        second_prompt = agent.call_args_list[1].args[0]
        self.assertIn("上次输出校验未通过", second_prompt)
        self.assertIn("NEED_INFO", second_prompt)

    def test_structural_twice_fails_cleanly(self):
        rec, n = self.run_scan([with_structural_problem(), with_structural_problem()])
        self.assertEqual((rec["status"], n), ("error", 2))
        self.assertIn("JSON Schema", rec["error"])

    def test_semantic_then_structural_does_not_score(self):
        rec, n = self.run_scan([with_semantic_problem(), with_structural_problem()])
        self.assertEqual((rec["status"], n), ("ok", 1))

    def test_unparseable_then_good(self):
        rec, n = self.run_scan([run_daily.AgentOutputError("最终回复中没有 JSON 对象"), valid_result()])
        self.assertEqual((rec["status"], n), ("ok", 2))


class NetworkTest(unittest.TestCase):
    def test_docker_harness_requires_explicit_network(self):
        agent = {"type": "deepseek-harness", "image": "harness:test", "launcher": "docker"}
        self.assertIsNotNone(run_daily.network_problem({"agent": agent}))
        agent["network"] = "audit-net"
        self.assertIsNone(run_daily.network_problem({"agent": agent}))
        agent.update(launcher="local", network="")
        self.assertIsNone(run_daily.network_problem({"agent": agent}))

    def test_main_refuses_missing_network(self):
        with tempfile.TemporaryDirectory() as t:
            s = Path(t) / "settings.json"
            s.write_text(json.dumps({"workdir": f"{t}/w", "output_dir": f"{t}/o",
                                     "agent": {"type": "deepseek-harness", "image": "harness:test"}}))
            with mock.patch.object(sys, "argv", ["run_daily.py", "--settings", str(s)]), \
                    mock.patch.object(run_daily, "scan_project") as scan:
                self.assertEqual(run_daily.main(), 2)
            scan.assert_not_called()
            self.assertFalse((Path(t) / "o").exists())


class PromptTest(unittest.TestCase):
    def test_large_change_list_points_to_complete_manifest(self):
        project = {"project_id": "p", "project_name": "测试项目"}
        changes = [f"file-{i}.py" for i in range(801)]
        prompt = run_daily.build_prompt(project, "incremental", "c" * 40, {}, None,
                                        "/workspace/context/previous_result.json", changes, False,
                                        "/workspace/context/changed_files.txt")
        self.assertIn("/workspace/context/changed_files.txt", prompt)
        self.assertIn("file-799.py", prompt)
        self.assertNotIn("file-800.py", prompt)


class SyncRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def fake_run(self, fail: str):
        def run(cmd, **_k):
            rc = 1 if cmd[1] == fail else 0
            return subprocess.CompletedProcess(cmd, rc, stdout="d" * 40 if cmd[1] == "rev-parse" else "", stderr=f"{fail} 出错")
        return run

    def test_reset_failure_raises(self):
        with mock.patch.object(run_daily, "run", side_effect=self.fake_run("reset")):
            with self.assertRaisesRegex(RuntimeError, "git reset 失败"):
                run_daily.sync_repo({"git_url": "x"}, self.repo, 10)

    def test_clean_failure_raises(self):
        with mock.patch.object(run_daily, "run", side_effect=self.fake_run("clean")):
            with self.assertRaisesRegex(RuntimeError, "git clean 失败"):
                run_daily.sync_repo({"git_url": "x"}, self.repo, 10)

    def test_rev_parse_failure_raises(self):
        with mock.patch.object(run_daily, "run", side_effect=self.fake_run("rev-parse")):
            with self.assertRaisesRegex(RuntimeError, "rev-parse"):
                run_daily.sync_repo({"git_url": "x"}, self.repo, 10)

    def test_success(self):
        with mock.patch.object(run_daily, "run", side_effect=self.fake_run("none")):
            self.assertEqual(run_daily.sync_repo({"git_url": "x"}, self.repo, 10), "d" * 40)

    def test_real_git_fetch_updates_head(self):
        """用真实 git：上游有新提交时，副本 HEAD 必须前进到新提交。"""
        base = Path(self.tmp.name)
        up = base / "upstream"
        git = lambda *a, cwd=up: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd,
                                                check=True, capture_output=True, text=True).stdout.strip()
        up.mkdir()
        git("init", "-q", "-b", "main")
        (up / "a.txt").write_text("1")
        git("add", ".")
        git("commit", "-qm", "1")
        repo = base / "copy"
        p = {"git_url": str(up), "branch": "main"}
        self.assertEqual(run_daily.sync_repo(p, repo, 10), git("rev-parse", "HEAD"))
        (up / "a.txt").write_text("2")
        git("commit", "-qam", "2")
        self.assertEqual(run_daily.sync_repo(p, repo, 10), git("rev-parse", "HEAD"))

    def test_changed_files_preserves_non_ascii_paths(self):
        base = Path(self.tmp.name) / "unicode-repo"
        base.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(base), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        (base / "中文文件.py").write_text("first\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", "first")
        old = git("rev-parse", "HEAD")
        (base / "中文文件.py").write_text("second\n", encoding="utf-8")
        git("commit", "-qam", "second")
        self.assertEqual(run_daily.changed_files(base, old, git("rev-parse", "HEAD")), ["中文文件.py"])


if __name__ == "__main__":
    unittest.main()
