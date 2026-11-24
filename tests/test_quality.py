"""质量样例评估器只能通过有实际文件证据且符合标注的结果。"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "quality"))
import evaluate  # noqa: E402
import prepare  # noqa: E402
from dataset import fixture_digest  # noqa: E402
from test_runner import valid_result  # noqa: E402
from audit_fixture import markdown_from_result  # noqa: E402


class QualitySuiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dataset = json.loads((ROOT / "quality/cases.json").read_text(encoding="utf-8"))
        self.manifest = {"dataset_version": self.dataset["version"],
                         "dataset_sha256": hashlib.sha256((ROOT / "quality/cases.json").read_bytes()).hexdigest(), "cases": {
            case["id"]: {"commit": "a" * 40, "fixture_sha256": fixture_digest(ROOT / "quality/fixtures" / case["id"])}
            for case in self.dataset["cases"]}}

    def write_results(self):
        results = self.root / "results"
        results.mkdir()
        for case in self.dataset["cases"]:
            result = valid_result()
            result["meta"] = {"commit": self.manifest["cases"][case["id"]]["commit"]}
            for item in result["items"]:
                item.update(result="NEED_INFO-补充材料", evidence=[], risk="none")
            for expected in case["expectations"]:
                item = next(i for i in result["items"] if i["id"] == expected["item_id"])
                item["result"] = expected["results"][0]
                if "evidence" in expected:
                    item["evidence"] = [copy.deepcopy(expected["evidence"])]
                if item["result"] in ("WARN", "FAIL"):
                    item["risk"] = "P1"
                    result["issues"].append({"item_id": item["id"], "level": "P1", "title": "示例风险",
                                             "evidence": copy.deepcopy(item["evidence"]), "impact": "风险",
                                             "suggestion": "修复", "effort": "S"})
            result["need_info_list"] = [{"items": [i["id"] for i in result["items"] if i["result"].startswith("NEED_INFO")],
                                         "missing": "对应信息", "why": "无法判定", "form": "补充材料",
                                         "unlocks": "相关检查项", "priority": "中"}]
            (results / f"{case['id']}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return results

    def test_labeled_findings_and_evidence_are_checked(self):
        results = self.write_results()
        report = evaluate.evaluate(results, self.manifest)
        self.assertEqual((report["passed_checks"], report["total_checks"]), (5, 5))

        path = results / "sql_unsafe.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        item = next(i for i in result["items"] if i["id"] == "6.1")
        item["result"] = "PASS"
        result["issues"] = []
        path.write_text(json.dumps(result), encoding="utf-8")
        report = evaluate.evaluate(results, self.manifest)
        self.assertEqual(report["passed_checks"], 4)

        item["result"] = "WARN"
        item["evidence"] = [{"path": "not-real.py", "start_line": 1}]
        path.write_text(json.dumps(result), encoding="utf-8")
        report = evaluate.evaluate(results, self.manifest)
        self.assertEqual(report["cases"][0]["status"], "invalid")

    def test_missing_case_never_counts_as_pass(self):
        report = evaluate.evaluate(self.root, self.manifest)
        self.assertEqual(report["passed_checks"], 0)
        self.assertTrue(all(case["status"] == "missing" for case in report["cases"]))

    def test_prepare_creates_separate_committed_repositories(self):
        settings = self.root / "source-settings.json"
        settings.write_text(json.dumps({"agent": {"type": "deepseek-harness", "env_file": "/tmp/model.env"}}), encoding="utf-8")
        dest = self.root / "suite"
        prepare.prepare(dest, settings)
        generated = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(generated["agent"]["env_file"], "/tmp/model.env")
        self.assertEqual(generated["output_dir"], str(dest.resolve() / "output"))
        self.assertEqual((dest / "settings.json").stat().st_mode & 0o777, 0o600)
        for case in self.dataset["cases"]:
            repo = dest / "fixtures" / case["id"]
            commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(commit, manifest["cases"][case["id"]]["commit"])
            self.assertEqual(fixture_digest(repo), manifest["cases"][case["id"]]["fixture_sha256"])
        with self.assertRaises(FileExistsError):
            prepare.prepare(dest, settings)

    def test_fixture_suite_through_runner_with_mock_model(self):
        fake_results = self.write_results()
        config = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))
        config.update(max_parallel=2, prescan_tools=[])
        config["summary"]["enabled"] = False
        config["agent"].update(type="deepseek-harness", network="test-network", env_file="/tmp/test-model.env")
        for path in fake_results.glob("*.json"):
            path.with_suffix(".md").write_text(markdown_from_result(json.loads(path.read_text(encoding="utf-8"))),
                                               encoding="utf-8")
        config_path = self.root / "source-settings.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        dest = self.root / "suite"
        prepare.prepare(dest, config_path)

        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "mount = next(a for a in sys.argv if a.endswith(':/workspace/repo:ro'))\n"
            "pid = Path(mount[:-len(':/workspace/repo:ro')]).parent.name\n"
            "text = (Path(os.environ['FIXTURE_RESULTS']) / (pid + '.md')).read_text(encoding='utf-8')\n"
            "print(json.dumps({'type': 'final', 'reason': {'kind': 'completed'}, 'text': text}))\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FIXTURE_RESULTS=str(fake_results))
        process = subprocess.run([sys.executable, str(ROOT / "runner/run_daily.py"),
                                  "--settings", str(dest / "settings.json"), "--projects", str(dest / "projects.csv"),
                                  "--date", "2026-11-23", "--force-full"],
                                 env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result_dir = dest / "output/results/2026-11-23"
        progress = json.loads(next(result_dir.glob("*/_run.json")).read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "complete")
        self.assertTrue(all(p["status"] == "ok" for p in progress["projects"]))
        for record in progress["projects"]:
            self.assertEqual(Path(record["report_path"]).read_text(encoding="utf-8"),
                             (fake_results / (record["project_id"] + ".md")).read_text(encoding="utf-8"))
        self.assertFalse(list(result_dir.rglob("sql_unsafe.json")))



if __name__ == "__main__":
    unittest.main()
