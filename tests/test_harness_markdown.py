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
import scoring  # noqa: E402
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
        self.assertEqual(scoring.schema_errors(parsed.result), [])
        self.assertEqual(scoring.validate(parsed.result, CK), [])

    def test_issue_id_is_separate_from_check_item_id(self):
        parsed = audit_markdown.parse(audit_markdown_text(with_issue=True), CK)
        issue = parsed.result["issues"][0]
        self.assertEqual(issue["issue_id"], "Q001")
        self.assertEqual(issue["item_id"], "6.1")
        self.assertEqual(scoring.validate(parsed.result, CK), [])

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

    def test_streams_redacted_step_and_tool_events_before_final(self):
        script = (
            "import json,time; "
            "print(json.dumps({'type':'status','phase':'step_start','turn':1,'step':40}),flush=True); "
            "print(json.dumps({'type':'tool_call','tool':'read','input':{'path':'SECRET_FILE'}}),flush=True); "
            "time.sleep(.1); "
            "print(json.dumps({'type':'status','phase':'step_end','turn':1,'step':40,'usage':{'inputTokens':100,'outputTokens':20}}),flush=True); "
            "print(json.dumps({'type':'final','reason':{'kind':'completed'},'text':'# Report'}),flush=True)"
        )
        seen = []
        out = harness.run("audit", Path("."), Path("."), {
            "agent": {"launcher": "local", "command": [sys.executable, "-c", script]},
            "_progress_callback": seen.append,
        }, 5)
        self.assertEqual(out["raw_text"], "# Report")
        self.assertTrue(any(e.get("phase") == "step_start" and e.get("step") == 40 for e in seen))
        self.assertTrue(any(e.get("tool") == "read" for e in seen))
        self.assertTrue(any(e.get("usage", {}).get("inputTokens") == 100 for e in seen))
        self.assertNotIn("SECRET_FILE", json.dumps(seen))
        self.assertNotIn("# Report", json.dumps(seen))
        self.assertTrue(any(e.get("phase") == "step_start" for e in out["event_log"]))

    def test_streaming_session_timeout_is_reported(self):
        with self.assertRaises(harness.HarnessError) as caught:
            harness.run("audit", Path("."), Path("."), {
                "agent": {"launcher": "local", "command": [sys.executable, "-c", "import time; time.sleep(2)"]},
                "_progress_callback": lambda event: None,
            }, .2)
        self.assertEqual(caught.exception.agent_status, "timeout")

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

    def test_counts_official_harness_tool_events_without_tool_output(self):
        events = [{"type": "tool_call", "tool": "read", "callId": "1", "input": {"file_path": "a.py"}},
                  {"type": "tool_result", "callId": "1", "status": "completed", "result": "source code"}]
        self.assertEqual(harness._stats(events)["tool_calls_by_name"], {"read": 1})
        self.assertEqual(harness._sanitize(events[0])["tool"], "read")
        self.assertNotIn("input", harness._sanitize(events[0]))
        self.assertNotIn("result", harness._sanitize(events[1]))

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

    def test_records_model_from_container_environment_without_recording_key(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "model.env"
            env_file.write_text("OPENAI_BASE_URL=http://model:8000/v1\n"
                                "OPENAI_MODEL=Qwen/Qwen3-8B\nOPENAI_API_KEY=private-value\n",
                                encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"type": "final", "reason": {"kind": "completed"}, "text": "ok"}),
                stderr="",
            )
            with mock.patch.object(harness.subprocess, "run", return_value=completed):
                result = harness.run("p", Path("/repo"), Path("/ctx"), {
                    "agent": {"launcher": "docker", "image": "harness:test", "network": "model-net",
                              "env_file": str(env_file)},
                }, 30)
            self.assertEqual(result["agent_model"], "Qwen/Qwen3-8B")
            self.assertNotIn("private-value", json.dumps(result))

    def test_container_environment_overrides_model_env_file(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "model.env"
            env_file.write_text("OPENAI_BASE_URL=http://model/v1\nOPENAI_MODEL=old\n", encoding="utf-8")
            self.assertEqual(harness._configured_model({"env_file": str(env_file), "env": {
                "OPENAI_MODEL": "new", "OPENAI_API_KEY": "secret"}}, "docker", {}), "new")
            self.assertIsNone(harness._configured_model({}, "docker", {
                "OPENAI_BASE_URL": "http://not-passed/v1", "OPENAI_MODEL": "not-passed"}))

    def test_unwraps_outer_markdown_fence_and_recovers_earlier_full_report(self):
        full_body = (
            "# indicatordisplay 八性代码质量审查报告\n\n"
            "## 评分总览\n\n"
            "| 维度 | 维度分 |\n|---|---|\n| 1 功能适合性 | 69 |\n| 6 安全性 | 25 |\n\n"
            "## 分维度检查结果\n\n"
            "| # | 检查项 | 结果 | 等级 | 结论 |\n|---|---|---|---|---|\n"
            + "\n".join(f"| 1.{i} | 检查项{i} | PASS | - | 正常通过检查说明文字示例 |" for i in range(1, 25))
            + "\n"
        )
        fenced = f"```markdown\n{full_body}```\n"
        self.assertEqual(harness.unwrap_outer_markdown_fence(fenced).strip(), full_body.strip())

        two_blocks = "```python\nx = 1\n```\n正文\n```python\ny = 2\n```"
        self.assertEqual(harness.unwrap_outer_markdown_fence(two_blocks), two_blocks)

        stdout = "\n".join([
            json.dumps({"type": "assistant_message", "role": "assistant", "text": fenced}),
            json.dumps({"type": "final", "reason": {"kind": "completed"},
                        "text": "审查已全部完成，报告已在上一条完整输出。总分 5.4。"}),
        ]) + "\n"
        completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(harness.subprocess, "run", return_value=completed):
            out = harness.run("audit", Path("."), Path("."), {
                "agent": {"launcher": "local", "command": ["dsh"]},
            }, 30)
        self.assertIn("## 评分总览", out["raw_text"])
        self.assertNotIn("```markdown", out["raw_text"])


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
            "c" * 40,
            {"agent": {"type": "deepseek-harness"}, "default_scope_out": []},
            "full",
        )
        self.assertIn("64 项", prompt)
        self.assertNotIn("front matter", prompt)
        self.assertIn("总分", prompt)
        self.assertNotIn("符合 JSON Schema 的结构化结果", prompt)
        self.assertNotIn("MODE: batch", prompt)
        self.assertNotIn("OUTPUT_FORMAT:", prompt)

    def test_harness_docker_uses_container_context_path(self):
        self.assertEqual(
            run_daily.ctx_view({"agent": {"type": "deepseek-harness", "launcher": "docker"}}, Path("/host/context")),
            "/workspace/context",
        )
        self.assertEqual(
            run_daily.ctx_view({"agent": {"type": "deepseek-harness", "launcher": "local"}}, Path("/host/context")),
            "/host/context",
        )



if __name__ == "__main__":
    unittest.main()
