"""Git 变更影响映射的回归测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import incremental  # noqa: E402
import scoring  # noqa: E402


class IncrementalPlanTest(unittest.TestCase):
    def setUp(self):
        self.checklist = scoring.load_checklist()

    def test_document_change_is_narrow_and_security_injection_is_checked(self):
        plan = incremental.plan(self.checklist, None, ["README.md"])
        self.assertFalse(plan.full_scan)
        self.assertEqual(plan.affected_items, frozenset({"5.3", "5.4", "6.8", "8.9"}))
        self.assertEqual(plan.affected_dimensions, frozenset({"5", "6", "8"}))

    def test_dependency_and_ci_changes_cover_build_contract(self):
        plan = incremental.plan(self.checklist, None, ["package.json", ".github/workflows/ci.yml"])
        for item_id in ("4.6", "6.7", "7.2", "7.8", "8.8"):
            self.assertIn(item_id, plan.affected_items)

    def test_large_impact_upgrades_to_full_scan(self):
        paths = ["generated/opaque.artifact"]
        plan = incremental.plan(self.checklist, None, paths)
        self.assertTrue(plan.full_scan)
        self.assertEqual(len(plan.affected_items), 64)

    def test_old_evidence_path_expands_plan(self):
        previous = {"items": [{"id": "1.1", "evidence": [{"path": "src/api.py", "start_line": 1}]}], "issues": []}
        plan = incremental.plan(self.checklist, previous, ["src/api.py"])
        self.assertIn("1.1", plan.affected_items)
        self.assertTrue(any("旧证据" in reason for reason in plan.reasons["1.1"]))

    def test_hidden_dot_paths_are_not_stripped(self):
        self.assertEqual(incremental.normalize_path("./.github/workflows/ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(incremental.normalize_path(".env.example"), ".env.example")
        self.assertEqual(incremental.normalize_path("/workspace/repo/src/app.py"), "src/app.py")


if __name__ == "__main__":
    unittest.main()
