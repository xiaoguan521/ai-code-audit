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

import run_daily  # noqa: E402
from adapters import harness  # noqa: E402
from parsers import audit_markdown  # noqa: E402
from test_runner import CK  # noqa: E402


def audit_markdown_text(with_issue: bool = False) -> str:
    blocks = [
        "<!-- audit-meta\nschema_version: 1\ncommit_sha: cccccccccccccccccccccccccccccccccccccccc\nskill_sha256: test\n-->",
    ]
    for item in CK["items"]:
        result = "WARN" if with_issue and item["id"] == "6.1" else "PASS"
        risk = "P1" if result == "WARN" else "none"
        blocks.append(
            f"<!-- audit-item\nschema_version: 1\nid: {item['id']}\nresult: {result}\n"
            f"risk: {risk}\nevidence:\n  - path: a.py\n    start_line: 1\n    end_line: 1\nconclusion: checked\n-->"
        )
    if with_issue:
        blocks.append(
            "<!-- audit-issue\nschema_version: 1\nissue_id: Q001\nitem_id: 6.1\nlevel: P1\n"
            "title: test issue\nevidence:\n  - path: a.py\n    start_line: 1\n    end_line: 1\n"
            "impact: test impact\nsuggestion: test suggestion\neffort: S\n-->"
        )
    blocks.append("<!-- audit-complete\nschema_version: 1\nitem_count: 64\n-->")
    return "# Harness audit\n\n" + "\n\n".join(blocks) + "\n"


class MarkdownParserTest(unittest.TestCase):
    def test_complete_markdown_projects_to_existing_result(self):
        parsed = audit_markdown.parse(audit_markdown_text(), CK)
        self.assertEqual(len(parsed.result["items"]), 64)
        self.assertEqual(parsed.metadata["item_count"], 64)
        self.assertTrue(parsed.metadata["complete"])
        self.assertEqual(len(parsed.result["dimension_notes"]), 8)
        self.assertEqual(run_daily.scoring.schema_errors(parsed.result), [])
        self.assertEqual(run_daily.scoring.validate(parsed.result, CK), [])

    def test_issue_id_is_separate_from_check_item_id(self):
        parsed = audit_markdown.parse(audit_markdown_text(with_issue=True), CK)
        issue = parsed.result["issues"][0]
        self.assertEqual(issue["issue_id"], "Q001")
        self.assertEqual(issue["item_id"], "6.1")
        self.assertEqual(run_daily.scoring.validate(parsed.result, CK), [])

    def test_missing_item_is_unverified(self):
        text = audit_markdown_text().replace("id: 8.10", "id: 8.9", 1)
        with self.assertRaisesRegex(audit_markdown.AuditMarkdownError, "覆盖不完整|重复"):
            audit_markdown.parse(text, CK)

    def test_missing_complete_marker_is_unverified(self):
        text = audit_markdown_text().replace("<!-- audit-complete\nschema_version: 1\nitem_count: 64\n-->", "")
        with self.assertRaisesRegex(audit_markdown.AuditMarkdownError, "audit-complete"):
            audit_markdown.parse(text, CK)


class HarnessAdapterTest(unittest.TestCase):
    def test_local_process_round_trip_uses_top_level_prompt_argument(self):
        script = (
            "import json,sys; "
            "print(json.dumps({'type':'final','reason':{'kind':'completed'},'answer':sys.argv[-1]}))"
        )
        out = harness.run("top-level task", Path("."), Path("."), {
            "agent": {"launcher": "local", "command": [sys.executable, "-c", script]},
        }, 30)
        self.assertEqual(out["raw_text"], "top-level task")

    def test_runs_one_complete_headless_task_and_extracts_final_answer(self):
        report = audit_markdown_text()
        stdout = "\n".join([
            json.dumps({"type": "turn_start", "turn": 1}),
            json.dumps({"type": "tool_execution_start", "toolName": "read"}),
            json.dumps({"type": "final", "reason": {"kind": "completed"}, "answer": report,
                        "session_id": "s1"}),
        ]) + "\n"
        completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(harness.subprocess, "run", return_value=completed) as process:
            out = harness.run("审查整个仓库", Path("."), Path("."), {
                "agent": {"launcher": "local", "command": ["dsh", "headless", "--json"],
                          "model": "deepseek-coder"},
            }, 30)
        self.assertEqual(out["agent_status"], "completed")
        self.assertEqual(out["final_reason_kind"], "completed")
        self.assertEqual(out["raw_text"], report)
        self.assertEqual(out["stats"]["tool_calls"], 1)
        self.assertNotIn("answer", json.dumps(out["event_log"], ensure_ascii=False))
        self.assertIsNone(process.call_args.kwargs["input"])
        self.assertEqual(process.call_args.args[0][-1], "审查整个仓库")
        self.assertFalse(process.call_args.kwargs["shell"])

    def test_requires_final_reason_kind(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"type": "final", "answer": "x"}), stderr=""
        )
        with mock.patch.object(harness.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(harness.HarnessError, "reason.kind"):
                harness.run("p", Path("."), Path("."), {
                    "agent": {"launcher": "local", "command": ["dsh"]},
                }, 30)

    def test_accepts_pretty_printed_json_event(self):
        completed = subprocess.CompletedProcess(
            [], 0,
            stdout=json.dumps({"type": "final", "reason": {"kind": "completed"}, "answer": "ok"}, indent=2),
            stderr="",
        )
        with mock.patch.object(harness.subprocess, "run", return_value=completed):
            result = harness.run("p", Path("."), Path("."), {
                "agent": {"launcher": "local", "command": ["dsh"]},
            }, 30)
        self.assertEqual(result["raw_text"], "ok")

    def test_supports_stdin_prompt_for_older_cli_variants(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"type": "final", "reason": {"kind": "completed"}, "answer": "ok"}), stderr=""
        )
        with mock.patch.object(harness.subprocess, "run", return_value=completed) as process:
            harness.run("p", Path("."), Path("."), {
                "agent": {"launcher": "local", "command": ["dsh"], "prompt_mode": "stdin"},
            }, 30)
        self.assertEqual(process.call_args.kwargs["input"], "p")
        self.assertEqual(process.call_args.args[0], ["dsh"])

    def test_nonzero_exit_is_execution_failure(self):
        completed = subprocess.CompletedProcess([], 7, stdout="", stderr="container failed")
        with mock.patch.object(harness.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(harness.HarnessError, "exit=7"):
                harness.run("p", Path("."), Path("."), {
                    "agent": {"launcher": "local", "command": ["dsh"]},
                }, 30)

    def test_docker_launcher_keeps_repo_read_only_and_runs_full_command(self):
        completed = subprocess.CompletedProcess(
            [], 0,
            stdout=json.dumps({"type": "final", "reason": {"kind": "completed"}, "answer": "ok"}),
            stderr="",
        )
        settings = {
            "agent": {
                "launcher": "docker", "image": "deepseek-harness:test", "network": "audit-net",
                "args": ["headless", "--json", "--permission-mode", "read-only"],
            },
        }
        with mock.patch.object(harness.subprocess, "run", return_value=completed) as process:
            harness.run("p", Path("/repo"), Path("/context"), settings, 30)
        command = process.call_args.args[0]
        self.assertEqual(command[:2], ["docker", "run"])
        self.assertIn("--read-only", command)
        self.assertIn("/repo:/workspace/repo:ro", command)
        self.assertIn("/context:/workspace/context:ro", command)
        self.assertIn("--network", command)
        self.assertIn("audit-net", command)
        image_index = command.index("deepseek-harness:test")
        self.assertEqual(command[image_index + 1:-1], ["dsh", "headless", "--json",
                                                        "--permission-mode", "read-only"])
        self.assertEqual(command[-1], "p")


class IntegrationContractTest(unittest.TestCase):
    def test_harness_cannot_enable_runner_dimension_split(self):
        problem = run_daily.backend_problem({
            "agent": {"type": "deepseek-harness", "strategy": "dimensions"},
        })
        self.assertIn("Harness 自己负责任务拆分", problem)

    def test_harness_docker_requires_image_and_network(self):
        self.assertIn("agent.image", run_daily.backend_problem({
            "agent": {"type": "deepseek-harness", "strategy": "single"},
        }))
        self.assertIn("agent.network", run_daily.network_problem({
            "agent": {"type": "deepseek-harness", "strategy": "single", "image": "dsh:test"},
        }))

    def test_harness_prompt_requests_markdown_not_json(self):
        prompt = run_daily.build_prompt(
            {"project_id": "p", "project_name": "x"},
            "full",
            "c" * 40,
            {"agent": {"type": "deepseek-harness"}, "default_scope_out": []},
            None, None, None, False,
        )
        self.assertIn("audit-complete", prompt)
        self.assertIn("OUTPUT_FORMAT: markdown", prompt)
        self.assertNotIn("符合 JSON Schema 的结构化结果", prompt)

    def test_harness_docker_uses_container_context_path(self):
        self.assertEqual(
            run_daily.ctx_view({"agent": {"type": "deepseek-harness", "launcher": "docker"}}, Path("/host/context")),
            "/workspace/context",
        )
        self.assertEqual(
            run_daily.ctx_view({"agent": {"type": "deepseek-harness", "launcher": "local"}}, Path("/host/context")),
            "/host/context",
        )

    def test_scan_projects_verified_markdown_into_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "work" / "p1" / "repo"
            repo.mkdir(parents=True)
            (repo / "a.py").write_text("pass\n", encoding="utf-8")
            settings = {
                "workdir": str(root / "work"),
                "output_dir": str(root / "out"),
                "prescan_tools": [],
                "code_map_enabled": False,
                "agent": {"type": "deepseek-harness", "strategy": "single", "launcher": "local"},
            }
            output = {
                "raw_text": audit_markdown_text(),
                "event_log": [{"type": "final", "reason": {"kind": "completed"}}],
                "stats": {"event_count": 1},
                "cost_usd": None,
                "session_id": "s1",
                "agent_status": "completed",
                "final_reason_kind": "completed",
            }
            args = types.SimpleNamespace(force_full=True, dry_run=False)
            with mock.patch.object(run_daily, "sync_repo", return_value="c" * 40), \
                    mock.patch.object(run_daily, "call_agent", return_value=output):
                rec = run_daily.scan_project(
                    {"project_id": "p1", "project_name": "测试项目"},
                    settings, "2026-11-23", args, CK,
                )
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["agent_status"], "completed")
            self.assertEqual(rec["audit_status"], "verified")
            artifacts = root / "out" / "artifacts" / "2026-11-23" / "p1"
            self.assertEqual((artifacts / "audit.md").read_text(encoding="utf-8"), output["raw_text"])
            self.assertTrue((artifacts / "audit.json").exists())
            self.assertTrue((artifacts / "metrics.json").exists())
            self.assertTrue((artifacts / "run-events.jsonl").exists())
            self.assertTrue((artifacts / "summary.md").exists())
            self.assertTrue((artifacts / "summary.meta.json").exists())
            stored = json.loads((artifacts / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["meta"]["summary"]["status"], "disabled")
            metrics = json.loads((artifacts / "metrics.json").read_text(encoding="utf-8"))
            self.assertIn("summary", metrics)
            self.assertGreaterEqual(metrics["total_seconds"], metrics["stages_seconds"]["summary"])


if __name__ == "__main__":
    unittest.main()
