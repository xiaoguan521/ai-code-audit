import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "docker"))

import code_map
import compose_entrypoint
import run_daily


class TimeoutOptimizationTest(unittest.TestCase):
    def test_code_map_summarize_includes_key_sections(self):
        data = {
            "tracked_files": 120,
            "counts": {"indexed": 85},
            "language_file_counts": {"Python": 60, "TypeScript": 25},
            "top_level_file_counts": {"src": 70, "tests": 15},
            "manifests": ["pyproject.toml", "package.json"],
            "entrypoints": ["src/main.py"],
            "ci": [".github/workflows/ci.yml"],
        }
        summary = code_map.summarize(data)
        self.assertIn("tracked=120，indexed=85", summary)
        self.assertIn("Python(60)", summary)
        self.assertIn("src(70)", summary)
        self.assertIn("src/main.py", summary)
        self.assertIn("pyproject.toml", summary)

    def test_classify_project_scale_and_resolve_timeout(self):
        self.assertEqual(run_daily.classify_project_scale({"indexed": 25}, {"cloc": {"status": "ok", "loc": 2000}})[0], "small")
        self.assertEqual(run_daily.classify_project_scale({"indexed": 25}, {"cloc": {"status": "failed"}})[0], "medium")
        self.assertEqual(run_daily.classify_project_scale({"indexed": 120}, {"cloc": {"status": "ok", "loc": 15000}})[0], "medium")
        self.assertEqual(run_daily.classify_project_scale({"indexed": 350}, {"cloc": {"status": "ok", "loc": 10000}})[0], "large")
        self.assertEqual(run_daily.classify_project_scale({"indexed": 80}, {"cloc": {"status": "ok", "loc": 60000}})[0], "large")

        settings = {"per_project_timeout_sec": 3600, "adaptive_timeout": True}
        self.assertEqual(run_daily.resolve_project_timeout(settings, "small"), 1800)
        self.assertEqual(run_daily.resolve_project_timeout(settings, "medium"), 3600)
        self.assertEqual(run_daily.resolve_project_timeout(settings, "large"), 5400)

        # 显式 scale_timeouts 配置优先，且 adaptive_timeout=false 时回退统一基准值
        custom = {"per_project_timeout_sec": 3600, "adaptive_timeout": True,
                  "scale_timeouts": {"small": 1200, "medium": 3000, "large": 7200}}
        self.assertEqual(run_daily.resolve_project_timeout(custom, "small"), 1200)
        self.assertEqual(run_daily.resolve_project_timeout(custom, "medium"), 3000)
        self.assertEqual(run_daily.resolve_project_timeout(custom, "large"), 7200)

        # 测试场景的极短超时或显式关闭 adaptive_timeout 时保持原值
        self.assertEqual(run_daily.resolve_project_timeout({"per_project_timeout_sec": 10, "adaptive_timeout": True,
                                                            "scale_timeouts": {"large": 7200}}, "large"), 10)
        self.assertEqual(run_daily.resolve_project_timeout({"per_project_timeout_sec": 3600, "adaptive_timeout": False,
                                                            "scale_timeouts": {"large": 7200}}, "large"), 3600)

    def test_build_prompt_scale_concurrency_and_code_map(self):
        proj = {"project_id": "demo", "project_name": "Demo"}
        large_prompt = run_daily.build_prompt(
            proj, "abc123", {"fast_mode": True}, "full",
            code_map_summary_text="代码地图摘要：tracked=500，indexed=400",
            project_scale="large", timeout_sec=5400,
        )
        self.assertIn("project_scale=large，timeout=90 分钟", large_prompt)
        self.assertIn("并发派生 2-3 个子代理", large_prompt)
        self.assertIn("代码地图摘要：tracked=500，indexed=400", large_prompt)

        small_prompt = run_daily.build_prompt(
            proj, "abc123", {"fast_mode": False}, "full",
            project_scale="small", timeout_sec=1800,
        )
        self.assertIn("project_scale=small，timeout=30 分钟", small_prompt)
        self.assertIn("在当前会话直接完成", small_prompt)

    def test_try_resume_from_state_reuses_unchanged_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            old_report = output / "reports" / "2026-11-24" / "run1" / "p1.md"
            old_report.parent.mkdir(parents=True)
            old_report.write_text("# report", encoding="utf-8")

            state_file = output / "state" / "p1.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps({
                "last_commit": "c1",
                "last_report": str(old_report),
                "last_scan_date": "2026-11-24",
                "last_full_date": "2026-11-24",
                "incremental_streak": 0,
                "report_mode": "fast",
                "branch": "main",
                "git_url": "https://example.com/p1.git",
                "weights": "",
                "materials_hash": None,
                "scores": {"total": 85},
            }), encoding="utf-8")

            proj = {"project_id": "p1", "branch": "main", "git_url": "https://example.com/p1.git"}
            new_report_dir = output / "reports" / "2026-11-25" / "run2"

            resumed = run_daily._try_resume_from_state(
                proj, "c1", None, "fast", "2026-11-25", state_file, new_report_dir, output, {}
            )
            self.assertIsNotNone(resumed)
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["scan_type"], "resumed")
            self.assertTrue((new_report_dir / "p1.md").is_file())

            # commit 变化或模式变化时不可复用
            self.assertIsNone(run_daily._try_resume_from_state(
                proj, "c2", None, "fast", "2026-11-25", state_file, new_report_dir, output, {}
            ))
            self.assertIsNone(run_daily._try_resume_from_state(
                proj, "c1", None, "standard", "2026-11-25", state_file, new_report_dir, output, {}
            ))

    def test_compose_entrypoint_forwards_batch_timeout_and_adaptive_timeout(self):
        settings = compose_entrypoint.configure_settings(
            {"agent": {}, "scale_timeouts": {"small": 1800, "medium": 3600, "large": 5400}},
            {
                "AUDIT_DATA_DIR": "/tmp/audit-data",
                "AUDIT_MODEL_NETWORK": "net",
                "AUDIT_BATCH_TIMEOUT_SEC": "43200",
                "AUDIT_ADAPTIVE_TIMEOUT": "false",
                "AUDIT_TIMEOUT_SMALL_SEC": "1200",
                "AUDIT_TIMEOUT_LARGE_SEC": "7200",
            },
        )
        self.assertEqual(settings["batch_timeout_sec"], 43200)
        self.assertFalse(settings["adaptive_timeout"])
        self.assertEqual(settings["scale_timeouts"], {"small": 1200, "medium": 3600, "large": 7200})


if __name__ == "__main__":
    unittest.main()
