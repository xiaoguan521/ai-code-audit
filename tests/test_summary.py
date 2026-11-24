"""The batch summary has only Markdown inputs and never blocks project reports."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
from adapters import summary


class SummaryTest(unittest.TestCase):
    def test_disabled_produces_a_report_index_without_model_call(self):
        records = [{"project_id": "p1", "project_name": "项目", "status": "ok", "report_path": "/not/read"}]
        with mock.patch.object(summary.harness, "run") as model:
            out = summary.generate_batch(records, Path("/tmp"), {"summary": {"enabled": False}}, "test")
        self.assertEqual(out["status"], "disabled")
        self.assertIn("(p1.md)", out["text"])
        model.assert_not_called()

    def test_isolated_summary_inherits_configured_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "p1.md"
            report.write_text("# 报告\n总分：8.0\n", encoding="utf-8")
            settings = {"workdir": str(root / "work"), "agent": {"type": "deepseek-harness", "launcher": "local"}}
            records = [{"project_id": "p1", "status": "ok", "report_path": str(report), "source_date": "2026-11-23"}]
            def invoke(prompt, repo, ctx, model_settings, timeout):
                self.assertEqual(repo, ctx)
                self.assertEqual([p.name for p in repo.iterdir()], ["p1.md"])
                self.assertTrue(repo.stat().st_mode & 0o005)
                # 报告副本只允许属主读写（0o600），不向同组/其他用户暴露。
                self.assertEqual((repo / "p1.md").stat().st_mode & 0o777, 0o600)
                self.assertEqual(model_settings["agent"]["type"], "deepseek-harness")
                self.assertIn("仅使用报告已有事实和分数", prompt)
                return {"raw_text": "# 汇总", "event_log": []}
            with mock.patch.object(summary.harness, "run", side_effect=invoke):
                out = summary.generate_batch(records, root, settings, "run")
            self.assertEqual(out["status"], "completed")
            self.assertIn("报告日期 2026-11-23", out["text"])


if __name__ == "__main__":
    unittest.main()
