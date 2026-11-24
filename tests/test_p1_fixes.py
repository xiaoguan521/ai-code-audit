"""P1 修复的回归测试（不调用模型、不需要 Docker）。"""
import fcntl
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import run_daily  # noqa: E402
import scoring  # noqa: E402
from adapters import summary  # noqa: E402

CK = scoring.load_checklist()


class ParseWeightsTest(unittest.TestCase):
    def test_unknown_dimension_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知维度"):
            scoring.parse_weights("9:10", CK, "demo")

    def test_non_numeric_rejected(self):
        with self.assertRaisesRegex(ValueError, "不是数字"):
            scoring.parse_weights("1:abc", CK, "demo")

    def test_negative_rejected(self):
        with self.assertRaisesRegex(ValueError, "负数"):
            scoring.parse_weights("1:-5", CK, "demo")

    def test_zero_total_rejected(self):
        with self.assertRaisesRegex(ValueError, "总和必须大于 0"):
            scoring.parse_weights("1:0;2:0;3:0;4:0;5:0;6:0;7:0;8:0", CK, "demo")

    def test_missing_colon_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少冒号"):
            scoring.parse_weights("1-15", CK, "demo")

    def test_blank_uses_default_weights(self):
        weights = scoring.parse_weights("", CK)
        self.assertEqual(weights, {d["id"]: float(d["weight"]) for d in CK["dimensions"]})
        self.assertEqual(sum(weights.values()), 100)

    def test_empty_segment_skipped(self):
        weights = scoring.parse_weights("1:20;;2:10;", CK)
        self.assertEqual(weights["1"], 20)
        self.assertEqual(weights["2"], 10)

    def test_prescan_timeouts_must_be_positive_integers(self):
        with self.assertRaisesRegex(ValueError, "prescan_timeouts"):
            run_daily.prescan_timeouts({"prescan_timeouts": {"cloc": 0}})
        with self.assertRaisesRegex(ValueError, "prescan_timeouts"):
            run_daily.prescan_timeouts({"prescan_timeouts": {"cloc": "900"}})
        self.assertEqual(run_daily.prescan_timeouts({})["cloc"], 900)
        self.assertEqual(run_daily.prescan_timeouts({"prescan_timeouts": {"cloc": 60}})["cloc"], 60)


class PrescanSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prescan = Path(self.tmp.name) / "prescan"
        self.prescan.mkdir()
        (self.prescan / "gitleaks.json").write_text(json.dumps([
            {"File": "src/app.py", "StartLine": 12, "RuleID": "generic-api-key",
             "Secret": "should-not-appear-in-summary"},
        ]), encoding="utf-8")
        (self.prescan / "semgrep.json").write_text(json.dumps({"results": [
            {"path": "src/x.py", "start": {"line": 3}, "check_id": "python.lang.security.audit"},
        ]}), encoding="utf-8")
        (self.prescan / "lizard_warnings.txt").write_text(
            "src/y.py:10: warning: function too long\nsrc/y.py:20: warning: complex\n",
            encoding="utf-8")
        (self.prescan / "cloc.json").write_text(json.dumps({"SUM": {"code": 1234}}), encoding="utf-8")

    def test_counts_and_top_entries(self):
        pre = {tool: {"status": "ok"} for tool in ("cloc", "gitleaks", "semgrep", "lizard")}
        out = run_daily.prescan_summary(pre, self.prescan)
        self.assertEqual(out["gitleaks"]["findings"], 1)
        self.assertEqual(out["semgrep"]["findings"], 1)
        self.assertEqual(out["lizard"]["findings"], 2)
        self.assertIsNone(out["cloc"]["findings"])
        self.assertEqual(out["cloc"]["loc"], 1234)
        self.assertIn("src/app.py:12 generic-api-key", out["gitleaks"]["top"])
        self.assertIn("src/x.py:3 python.lang.security.audit", out["semgrep"]["top"])
        self.assertEqual(len(out["lizard"]["top"]), 2)
        self.assertNotIn("should-not-appear-in-summary", json.dumps(out, ensure_ascii=False))

    def test_unreadable_output_records_reason(self):
        (self.prescan / "gitleaks.json").write_text("not json", encoding="utf-8")
        out = run_daily.prescan_summary({"gitleaks": {"status": "ok"}}, self.prescan)
        self.assertIsNone(out["gitleaks"]["findings"])
        self.assertIn("格式无效", out["gitleaks"]["reason"])

    def test_summary_text_marks_unconfigured_semgrep_and_empty_prescan(self):
        out = run_daily.prescan_summary_text({
            "semgrep": {"status": "skipped", "reason": "未配置离线规则", "findings": None, "top": []},
            "cloc": {"status": "skipped", "reason": "未安装", "findings": None, "top": []},
        })
        self.assertIn("semgrep 未配置离线规则，本次未扫描", out)
        self.assertIn("本次预扫描未产出可用线索", out)
        self.assertLessEqual(len(out.splitlines()), 30)


class SecretScanTest(unittest.TestCase):
    def test_aws_key_and_github_token_are_blocked(self):
        hits = run_daily.scan_report_for_secrets("密钥 AKIAIOSFODNN7EXAMPLE 已写入")
        self.assertIn("AWS Access Key ID", hits)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(hits, ensure_ascii=False))
        hits = run_daily.scan_report_for_secrets("token ghp_" + "a" * 36)
        self.assertIn("GitHub Token", hits)

    def test_private_key_and_slack_token_are_blocked(self):
        self.assertIn("Private Key", run_daily.scan_report_for_secrets("-----BEGIN PRIVATE KEY-----"))
        self.assertIn("Slack Token", run_daily.scan_report_for_secrets("xoxb-123456789012-abcdefghij"))

    def test_plain_password_text_is_not_blocked(self):
        self.assertEqual(run_daily.scan_report_for_secrets("password = hunter2\n请勿硬编码密码"), [])


class ModelSlotTimeoutTest(unittest.TestCase):
    def test_queue_timeout_raises_quickly_when_all_slots_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            slots = workdir / ".model-slots"
            slots.mkdir()
            held = (slots / "0.lock").open("a")
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                settings = {"workdir": str(workdir), "max_model_concurrency": 1,
                            "model_queue_timeout_sec": 0.5}
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, "0.5"):
                    with run_daily.model_slot(settings):
                        pass
                self.assertLess(time.monotonic() - started, 2.0)
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()


class CoverageGapsTest(unittest.TestCase):
    def test_submodules_and_counts_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
            gaps = run_daily.detect_coverage_gaps(repo, {"counts": {
                "large": 2, "binary_by_extension": 3, "protected": 1,
                "non_file": 0, "unreadable_or_outside": 1,
            }})
        self.assertIn("含子模块，子模块内容未扫描", gaps)
        self.assertTrue(any("2 个 >2MB" in gap for gap in gaps))
        self.assertTrue(any("3 个二进制" in gap for gap in gaps))
        self.assertTrue(any("1 个受保护" in gap for gap in gaps))
        self.assertTrue(any("不可读" in gap for gap in gaps))
        self.assertFalse(any("非文件" in gap for gap in gaps))

    def test_lfs_pointer_files_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", encoding="utf-8")
            gaps = run_daily.detect_coverage_gaps(repo, {"counts": {}})
        self.assertIn("含 Git LFS 文件，扫描到的是指针文本", gaps)


class BuildPromptTest(unittest.TestCase):
    def test_prompt_contains_recheck_gaps_and_injection_sections(self):
        prompt = run_daily.build_prompt(
            {"project_id": "p", "project_name": "测试"}, "c" * 40, {}, "incremental",
            prescan_summary_text="预扫描摘要（工具结果只作线索，须回源码核实）：\n- gitleaks：发现 1 处",
            coverage_gaps=["含子模块，子模块内容未扫描"],
            injection_suspects=["README.md:1"],
            recheck_items=[("6.4", "源码变化：src/a.py")],
        )
        self.assertIn("预扫描摘要", prompt)
        self.assertIn("本次须重判的检查项", prompt)
        self.assertIn("6.4：源码变化：src/a.py", prompt)
        self.assertIn("未检查部分（须写入报告的检查范围章节，不得声称已覆盖）：", prompt)
        self.assertIn("含子模块，子模块内容未扫描", prompt)
        self.assertIn("疑似提示注入文件", prompt)
        self.assertIn("README.md:1", prompt)

    def test_prompt_omits_optional_sections_when_empty(self):
        prompt = run_daily.build_prompt({"project_id": "p"}, "c" * 40, {}, "full")
        self.assertNotIn("本次须重判的检查项", prompt)
        self.assertNotIn("未检查部分", prompt)
        self.assertNotIn("疑似提示注入", prompt)


class SummaryMetadataTest(unittest.TestCase):
    def test_disabled_summary_still_carries_runner_metadata(self):
        records = [
            {"project_id": "p1", "project_name": "项目一", "department": "研发部", "owner": "张三",
             "report_mode": "fast", "scan_type": "incremental", "commit": "abc123",
             "source_date": "2026-11-24", "status": "ok", "report_path": "/not/read"},
            {"project_id": "p2", "project_name": "项目二", "department": "测试部", "owner": "李四",
             "report_mode": "standard", "scan_type": "full", "commit": "def456", "status": "error"},
        ]
        with mock.patch.object(summary.harness, "run") as model:
            out = summary.generate_batch(records, Path("/tmp"), {"summary": {"enabled": False}}, "test")
        model.assert_not_called()
        self.assertEqual(out["status"], "disabled")
        self.assertIn("## 汇总口径说明（Runner 生成，非模型输出）", out["text"])
        self.assertIn("本次汇总混合了 fast 与 standard 报告，总分不可直接比较", out["text"])
        self.assertIn("研发部", out["text"])
        self.assertIn("张三", out["text"])
        self.assertIn("模式 fast", out["text"])
        self.assertIn("扫描 incremental", out["text"])

    def test_single_mode_has_no_mixed_warning(self):
        records = [{"project_id": "p1", "report_mode": "standard", "status": "ok",
                    "report_path": "/not/read"}]
        out = summary.generate_batch(records, Path("/tmp"), {"summary": {"enabled": False}}, "test")
        self.assertNotIn("混合了 fast 与 standard", out["text"])


class RunStatusTest(unittest.TestCase):
    def test_three_states(self):
        self.assertEqual(run_daily.run_status([]), "complete")
        self.assertEqual(run_daily.run_status([{"status": "ok"}, {"status": "dry-run"}]), "complete")
        self.assertEqual(run_daily.run_status([{"status": "ok"}, {"status": "error"}]), "partial_failed")
        self.assertEqual(run_daily.run_status([{"status": "error"}, {"status": "error"}]), "failed")


if __name__ == "__main__":
    unittest.main()
