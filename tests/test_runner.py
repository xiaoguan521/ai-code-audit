"""runner 的单元测试（不调用模型、不需要 Docker）：python3 -m unittest discover -s tests -v"""
import copy
import datetime as dt
import json
import os
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
        prompt = run_daily.build_prompt(project, "c" * 40, {}, "incremental",
                                        prev_file="/workspace/context/previous_result.json",
                                        changes_file="/workspace/context/changed_files.txt")
        self.assertIn("/workspace/context/changed_files.txt", prompt)
        self.assertIn("仅作参考", prompt)


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

    def test_snapshot_selects_branch_commit_at_cutoff(self):
        """A fixed cutoff selects an older first-parent commit, not current HEAD."""
        base = Path(self.tmp.name)
        up = base / "snapshot-upstream"
        up.mkdir()

        def git(*args, env=None):
            merged_env = dict(os.environ)
            if env:
                merged_env.update(env)
            return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  cwd=up, check=True, capture_output=True, text=True, env=merged_env).stdout.strip()

        git("init", "-q", "-b", "release")
        (up / "a.txt").write_text("before\n")
        git("add", ".")
        git("commit", "-qm", "before", env={
            "GIT_AUTHOR_DATE": "2026-11-23T01:00:00+08:00",
            "GIT_COMMITTER_DATE": "2026-11-23T01:00:00+08:00",
        })
        before = git("rev-parse", "HEAD")
        (up / "a.txt").write_text("after\n")
        git("commit", "-qam", "after", env={
            "GIT_AUTHOR_DATE": "2026-11-24T01:00:00+08:00",
            "GIT_COMMITTER_DATE": "2026-11-24T01:00:00+08:00",
        })

        repo = base / "snapshot-copy"
        snapshot_at = dt.datetime.fromisoformat("2026-11-23T23:00:00+08:00")
        selected = run_daily.sync_repo({"git_url": up.as_uri(), "branch": "release"}, repo, 1, snapshot_at)
        self.assertEqual(selected, before)
        self.assertEqual((repo / "a.txt").read_text(), "before\n")

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
