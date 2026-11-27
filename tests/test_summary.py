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
            self.assertIn("# 代码检查分析报告汇总", out["text"])
            self.assertIn("## 一、总体概览", out["text"])
            self.assertIn("## 二、板块维度汇总", out["text"])
            self.assertIn("## 三、风险问题清单专项", out["text"])
            self.assertIn("## 六、各项目代码检查结果明细", out["text"])

    def test_structured_word_template_aggregation_with_departments_and_issues(self):
        import scoring
        from report_fixture import readable_report
        ck = scoring.load_checklist()
        results = {item["id"]: ("PASS", "none", [("src/a.py", 1)]) for item in ck["items"]}
        results["6.4"] = ("FAIL", "P0", [("src/a.py", 10)])
        results["3.1"] = ("WARN", "P1", [("src/a.py", 20)])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r1 = root / "p1.md"
            r1.write_text(readable_report(ck, results, report_mode="fast"), encoding="utf-8")
            records = [
                {
                    "project_id": "p1",
                    "project_name": "数据应用工具研发项目 / indicatordisplay",
                    "department": "技术研发",
                    "owner": "张三",
                    "report_mode": "fast",
                    "scan_type": "full",
                    "status": "ok",
                    "report_path": str(r1),
                },
                {
                    "project_id": "p2",
                    "project_name": "数据应用工具研发项目 / sxhx-pc",
                    "department": "技术研发",
                    "owner": "李四",
                    "status": "error",
                },
            ]
            out = summary.generate_batch(records, root, {"summary": {"enabled": False}}, "run-1")
            text = out["text"]
            self.assertEqual(out["status"], "disabled")
            self.assertIn("### 1.1 评分总览", text)
            self.assertIn("### 1.2 风险问题清单（等级分布）", text)
            self.assertIn("### 1.3 八个质量维度横向评分（均值）", text)
            self.assertIn("### 2.1 各板块质量八性维度评分对比", text)
            self.assertIn("### 3.1 全局共性问题 TOP 10", text)
            self.assertIn("### 3.2 P0 致命问题高发报告（前 15）", text)
            self.assertIn("## 四、八个质量维度深度分析", text)
            self.assertIn("## 五、总结与整改建议", text)
            self.assertIn("### 5.2 四级整改路线图", text)
            self.assertIn("### 【技术研发】", text)
            self.assertIn("#### ├─ 项目：数据应用工具研发项目-张三", text)
            self.assertIn("[indicatordisplay](p1.md)", text)

            # 验证零依赖 Word (.docx) 导出生成合法 OOXML 包且 17 列宽表自动启用横向 A4
            import io
            import xml.etree.ElementTree as ET
            import zipfile
            import docx_export
            docx_bytes = docx_export.markdown_to_docx_bytes(text)
            with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
                names = set(zf.namelist())
                self.assertIn("[Content_Types].xml", names)
                self.assertIn("word/document.xml", names)
                self.assertIn("word/styles.xml", names)
                doc_xml = zf.read("word/document.xml")
                styles_xml = zf.read("word/styles.xml")
                ET.fromstring(doc_xml)
                ET.fromstring(styles_xml)
                self.assertIn(b'orient="landscape"', doc_xml)
                self.assertIn("代码检查分析报告汇总".encode("utf-8"), doc_xml)
                self.assertIn("技术研发".encode("utf-8"), doc_xml)


if __name__ == "__main__":
    unittest.main()
