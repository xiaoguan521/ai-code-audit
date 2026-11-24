"""P0 修复的回归测试：报告校验、分数口径、强制全量 TTL。"""
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
sys.path.insert(0, str(ROOT / "tests"))
import report_validate  # noqa: E402
import run_daily  # noqa: E402
import scoring  # noqa: E402
from report_fixture import build_structured, readable_report  # noqa: E402


def _sample_text() -> str:
    return (ROOT / "samples" / "report_sample_crm.md").read_text(encoding="utf-8")


def _structured_result(items, issues):
    return {"items": items, "issues": issues,
            "dimension_notes": [{"dimension": str(n), "conclusion": ""} for n in range(1, 9)],
            "need_info_list": []}


def _result_with(items_by_dim):
    """items_by_dim: {维度编号: {检查项编号: (result, risk)}}，其余项 NEED_INFO。"""
    ck = scoring.load_checklist()
    results = {}
    for item in ck["items"]:
        dim = item["id"].split(".")[0]
        results[item["id"]] = items_by_dim.get(dim, {}).get(item["id"], ("NEED_INFO-证据不足", "none", []))
    data, _weights = build_structured(ck, results)
    return data


class SampleReportTest(unittest.TestCase):
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

    def test_sample_report_validates_clean(self):
        result = report_validate.validate_report(
            _sample_text(), self.repo, self.checklist, self.weights, "standard")
        self.assertEqual(result["problems"], [])
        scores = result["scores"]
        self.assertEqual(scores["total"], 6.6)
        self.assertEqual(scores["scored_dimensions"], 5)
        self.assertAlmostEqual(scores["coverage"], 0.683, places=2)
        self.assertEqual(scores["security_score"], 7.2)

    def test_missing_item_is_reported(self):
        text = _sample_text().replace("| 1.7 | 测试有效性 | PASS·沿用 | src/crm/module1.py:17 | 示例结论 |\n", "")
        result = report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "standard")
        self.assertTrue(any("缺少检查项" in p and "1.7" in p for p in result["problems"]))

    def test_wrong_total_is_reported(self):
        text = _sample_text().replace("总评分 6.6", "总评分 9.9")
        result = report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "standard")
        self.assertTrue(any("总分不一致" in p for p in result["problems"]))

    def test_missing_evidence_file_is_reported_in_standard_mode(self):
        results = {"2.2": ("WARN", "P2", [("src/nope.py", 1)])}
        text = readable_report(self.checklist, results, report_mode="standard")
        result = report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "standard")
        self.assertTrue(any("文件不存在" in p for p in result["problems"]))

    def test_fast_mode_skips_evidence_verification(self):
        results = {"2.2": ("WARN", "P2", [("src/nope.py", 1)])}
        text = readable_report(self.checklist, results, report_mode="fast")
        result = report_validate.validate_report(
            text, self.repo, self.checklist, self.weights, "fast")
        self.assertFalse(any("文件不存在" in p or "但无证据" in p for p in result["problems"]))


class ScoringThresholdTest(unittest.TestCase):
    def setUp(self):
        self.checklist = scoring.load_checklist()
        self.weights = scoring.parse_weights("", self.checklist)

    def test_single_scored_dimension_cannot_produce_total(self):
        # 只安全性 9 项 PASS、其余 55 项全 NEED_INFO：旧口径总分 10.0，新口径暂不评分。
        data = _result_with({"6": {f"6.{n}": ("PASS", "none", []) for n in range(1, 10)}})
        scores = scoring.score_result(data, self.checklist, self.weights)
        self.assertEqual(scores["security_score"], 10.0)
        self.assertEqual(scores["scored_dimensions"], 1)
        self.assertIsNone(scores["total"])
        self.assertEqual(scores["grade"], "暂不评分")

    def test_dimension_with_too_many_na_is_not_scored(self):
        # 维度 1：3 项 N/A（占比 3/7 > 40%）+ 4 项 PASS，其余维度全 PASS。
        dim1 = {"1.1": ("N/A", "none", []), "1.2": ("N/A", "none", []), "1.3": ("N/A", "none", []),
                "1.4": ("PASS", "none", []), "1.5": ("PASS", "none", []),
                "1.6": ("PASS", "none", []), "1.7": ("PASS", "none", [])}
        other = {}
        for item in self.checklist["items"]:
            dim = item["id"].split(".")[0]
            if dim != "1":
                other.setdefault(dim, {})[item["id"]] = ("PASS", "none", [])
        data = _result_with({**other, "1": dim1})
        scores = scoring.score_result(data, self.checklist, self.weights)
        self.assertIsNone(scores["dimensions"]["1"]["score"])
        self.assertIsNotNone(scores["total"])

    def test_five_scored_dimensions_but_low_overall_coverage_has_no_total(self):
        # 5 个维度全判、其余 3 个维度全 NEED_INFO：总体覆盖率 < 60%，总分暂不评分。
        scored = {}
        for item in self.checklist["items"]:
            dim = item["id"].split(".")[0]
            if dim in ("1", "2", "3", "4", "5"):
                scored.setdefault(dim, {})[item["id"]] = ("PASS", "none", [])
        data = _result_with(scored)
        scores = scoring.score_result(data, self.checklist, self.weights)
        self.assertEqual(scores["scored_dimensions"], 5)
        self.assertLess(scores["coverage"], 0.6)
        self.assertIsNone(scores["total"])

    def test_sample_equivalent_result_still_scores_6_6(self):
        items, _ = report_validate._parse_items(_sample_text())
        issues = report_validate._parse_issues(_sample_text())
        issue_level = {issue["item_id"]: issue["level"] for issue in issues}
        for item in items:
            if item["risk"] == "none" and item["id"] in issue_level:
                item["risk"] = issue_level[item["id"]]
        scores = scoring.score_result(
            _structured_result(items, issues), self.checklist, self.weights)
        self.assertEqual(scores["total"], 6.6)


class TtlTest(unittest.TestCase):
    def test_streak_five_forces_full(self):
        self.assertIsNotNone(run_daily.ttl_force_full(
            {"incremental_streak": 5, "last_full_date": "2026-11-24"}, "2026-11-24"))
        self.assertIsNotNone(run_daily.ttl_force_full(
            {"incremental_streak": 6, "last_full_date": "2026-11-24"}, "2026-11-24"))

    def test_thirty_days_since_full_forces_full(self):
        self.assertIsNotNone(run_daily.ttl_force_full(
            {"incremental_streak": 0, "last_full_date": "2026-10-25"}, "2026-11-24"))

    def test_below_thresholds_does_not_force_full(self):
        self.assertIsNone(run_daily.ttl_force_full(
            {"incremental_streak": 4, "last_full_date": "2026-10-26"}, "2026-11-24"))
        self.assertIsNone(run_daily.ttl_force_full(
            {"incremental_streak": 0, "last_full_date": "2026-11-24"}, "2026-11-24"))

    def test_missing_or_broken_state_does_not_force_full(self):
        self.assertIsNone(run_daily.ttl_force_full(None, "2026-11-24"))
        self.assertIsNone(run_daily.ttl_force_full("not-a-dict", "2026-11-24"))
        self.assertIsNone(run_daily.ttl_force_full(
            {"incremental_streak": 0, "last_full_date": "not-a-date"}, "2026-11-24"))


class TtlWiringTest(unittest.TestCase):
    """TTL 在 scan_project 里的接线：增量成功 streak+1，满 5 次强制全量并重置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {"workdir": str(self.root / "work"), "output_dir": str(self.root / "out"),
                         "prescan_tools": [], "summary": {"enabled": False},
                         "agent": {"type": "deepseek-harness", "launcher": "local"}}
        self.repo = Path(self.settings["workdir"]) / "p1" / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
        self.project = {"project_id": "p1", "project_name": "项目一"}
        self.checklist = scoring.load_checklist()
        results = {item["id"]: ("PASS", "none", [("src/a.py", 1)]) for item in self.checklist["items"]}
        self.text = readable_report(self.checklist, results, report_mode="standard")

    def _scan(self, args):
        with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
             mock.patch.object(run_daily, "changed_files", return_value=[]), \
             mock.patch.object(run_daily, "call_agent",
                               return_value={"raw_text": self.text, "event_log": []}):
            return run_daily.scan_project(self.project, self.settings, "2026-11-24", args)

    def test_incremental_streak_increments_and_fifth_forces_full(self):
        args = types.SimpleNamespace(force_full=True, dry_run=False, run_id="run-1")
        first = self._scan(args)
        self.assertEqual(first["scan_type"], "full")
        self.assertEqual(first["incremental_streak"], 0)
        args = types.SimpleNamespace(force_full=False, dry_run=False, run_id="run-2")
        second = self._scan(args)
        self.assertEqual(second["scan_type"], "incremental")
        self.assertEqual(second["incremental_streak"], 1)
        state_file = self.root / "out" / "state" / "p1.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["incremental_streak"] = 5
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        third = self._scan(types.SimpleNamespace(force_full=False, dry_run=False, run_id="run-3"))
        self.assertEqual(third["scan_type"], "full")
        self.assertIn("forced_full_reason", third)
        self.assertEqual(third["incremental_streak"], 0)
        self.assertEqual(third["last_full_date"], "2026-11-24")


if __name__ == "__main__":
    unittest.main()
