import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))

from adapters import summary  # noqa: E402
from test_runner import valid_result  # noqa: E402


def summary_data() -> dict:
    data = valid_result()
    data["meta"] = {"audit_sha256": "a" * 64}
    data["score"] = {
        "total": 92.5,
        "grade": "A",
        "security_score": 88.0,
        "issue_counts": {"P0": 0, "P1": 1, "P2": 0, "P3": 0},
    }
    data["items"][0].update(result="WARN", risk="P1")
    data["issues"] = [{
        "issue_id": "Q001",
        "item_id": "1.1",
        "level": "P1",
        "title": "测试问题",
        "evidence": [{"path": "a.py", "start_line": 1}],
        "impact": "影响",
        "suggestion": "修复",
        "effort": "S",
    }]
    return data


def ai_summary_text(source_hash: str) -> str:
    return f'''<!-- summary-meta
schema_version: 1
source_audit_sha256: "{source_hash}"
-->
# 管理层摘要

<!-- summary-theme
schema_version: 1
theme_id: T001
priority: P1
source_issue_ids: ["Q001"]
-->

<!-- summary-complete
schema_version: 1
-->
'''


class SummaryValidationTest(unittest.TestCase):
    def test_fallback_is_self_consistent(self):
        data = summary_data()
        text = summary.fallback(data)
        result = summary.validate(text, data)
        self.assertEqual(result["source_issue_ids"], ["Q001"])
        self.assertEqual(result["themes"][0]["priority"], "P1")

    def test_unknown_issue_reference_is_rejected(self):
        data = summary_data()
        text = ai_summary_text(data["meta"]["audit_sha256"]).replace("Q001", "Q999")
        with self.assertRaisesRegex(summary.SummaryError, "不存在的问题"):
            summary.validate(text, data)

    def test_summary_hash_mismatch_is_rejected(self):
        data = summary_data()
        text = ai_summary_text("b" * 64)
        with self.assertRaisesRegex(summary.SummaryError, "audit_sha256"):
            summary.validate(text, data)


class SummaryAdapterTest(unittest.TestCase):
    def test_disabled_summary_uses_deterministic_fallback(self):
        result = summary.generate(summary_data(), Path("/tmp/not-used"), {"summary": {"enabled": False}}, 30)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["agent_status"], "not_run")
        self.assertEqual(summary.validate(result["text"], summary_data())["source_issue_ids"], ["Q001"])

    def test_enabled_summary_mounts_only_audit_artifacts(self):
        data = summary_data()
        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            (artifact_dir / "audit.md").write_text("audit", encoding="utf-8")
            captured = {}

            def fake_run(prompt, repo, context, settings, timeout):
                captured.update(prompt=prompt, repo=repo, context=context, settings=settings, timeout=timeout)
                self.assertTrue((context / "audit.json").is_file())
                self.assertTrue((context / "audit.md").is_file())
                self.assertFalse((context / "a.py").exists())
                return {
                    "raw_text": ai_summary_text(data["meta"]["audit_sha256"]),
                    "agent_status": "completed",
                    "event_log": [],
                    "stats": {"event_count": 1},
                }

            settings = {
                "summary": {
                    "enabled": True,
                    "timeout_sec": 17,
                    "agent": {"launcher": "local", "command": ["dsh"]},
                },
            }
            with mock.patch.object(summary.harness, "run", side_effect=fake_run):
                result = summary.generate(data, artifact_dir, settings, 30)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metadata"]["source_issue_ids"], ["Q001"])
        self.assertEqual(captured["timeout"], 17)
        self.assertEqual(captured["settings"]["agent"]["strategy"], "single")
        self.assertIn("不能读取任何源码", captured["prompt"])

    def test_summary_failure_falls_back_without_changing_audit(self):
        data = summary_data()
        original = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            summary.harness, "run", side_effect=summary.harness.HarnessError("not ready", "failed")
        ):
            result = summary.generate(
                data, Path(temp),
                {"summary": {"enabled": True, "agent": {"launcher": "local", "command": ["dsh"]}}},
                30,
            )
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["agent_status"], "failed")
        self.assertEqual(data, original)
        self.assertEqual(summary.validate(result["text"], data)["source_issue_ids"], ["Q001"])


if __name__ == "__main__":
    unittest.main()
