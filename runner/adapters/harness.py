"""External DeepSeek Harness Headless adapter.

The adapter owns only the process lifecycle and event transport. It does not
plan audit steps, inspect source files, or decide how the Agent Loop works.
The final Markdown answer is returned unchanged for the parser layer.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from typing import Any


DEFAULT_COMMAND = [
    "dsh", "headless", "--json",
    "--permission-mode", "read-only",
    "--approval-policy", "never",
]
DEFAULT_SUCCESS_REASONS = {"completed", "success", "done", "stop", "end_turn", "finish"}
DEFAULT_FAILURE_REASONS = {"error", "failed", "failure", "timeout", "cancelled", "aborted"}
FINAL_TYPES = {"final", "final_answer", "result", "done", "complete"}


class HarnessError(RuntimeError):
    def __init__(self, message: str, agent_status: str = "failed", stats: dict | None = None):
        super().__init__(message)
        self.agent_status = agent_status
        self.stats = stats


def _command(agent: dict) -> list[str]:
    raw = agent.get("command")
    if isinstance(raw, str):
        raise HarnessError("agent.command 必须是参数列表，不允许 shell 字符串")
    if raw is not None:
        command = [str(value) for value in raw]
    else:
        command = [str(agent.get("bin", DEFAULT_COMMAND[0]))]
        command.extend(str(value) for value in agent.get("args", DEFAULT_COMMAND[1:]))
    if not command or not command[0]:
        raise HarnessError("Harness command 不能为空")
    return command


def _docker_command(agent: dict, repo, ctx) -> tuple[list[str], str]:
    image = str(agent.get("image", "")).strip()
    if not image:
        raise HarnessError("deepseek-harness 的 agent.image 必须指定 Harness Docker 镜像")
    network = str(agent.get("network", "")).strip()
    if not network:
        raise HarnessError("deepseek-harness 的 agent.network 必须显式指定")
    name = f"ai-audit-harness-{uuid.uuid4().hex[:12]}"
    command = _command(agent)
    docker = [
        "docker", "run", "--rm", "--init", "--name", name,
        "--label", "ai-code-audit=1",
        "--read-only", "--tmpfs", "/tmp:rw,size=512m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(agent.get("pids_limit", 512)),
        "--memory", str(agent.get("memory", "6g")),
        "--cpus", str(agent.get("cpus", 2)),
        "-v", f"{repo}:/workspace/repo:ro",
        "-v", f"{ctx}:/workspace/context:ro",
        "-w", "/workspace/repo",
        "--network", network,
    ]
    if agent.get("env_file"):
        docker += ["--env-file", str(agent["env_file"])]
    configured_env = agent.get("env")
    if isinstance(configured_env, dict):
        for key, value in configured_env.items():
            docker += ["-e", f"{key}={value}"]
    return [*docker, image, *command], name


def _events(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        raise HarnessError("Harness 没有输出事件")
    try:
        parsed = json.loads(text)
        events = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise HarnessError(f"Harness 输出不是合法 JSON 事件流：{exc}") from exc
    if not events or not all(isinstance(event, dict) for event in events):
        raise HarnessError("Harness 事件流必须由 JSON 对象组成")
    return events


def _is_final(event: dict) -> bool:
    kind = event.get("type") or event.get("event")
    if kind in FINAL_TYPES:
        return True
    return isinstance(event.get("final"), dict) or event.get("final") is True


def _reason_kind(event: dict) -> str | None:
    candidates: list[Any] = [event.get("reason")]
    for key in ("final", "data", "payload"):
        value = event.get(key)
        if isinstance(value, dict):
            candidates.append(value.get("reason"))
    for reason in candidates:
        if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
            return reason["kind"]
        if isinstance(reason, str):
            return reason
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"text", "output", "markdown"}:
                candidate = _text(item.get("text") or item.get("content") or item.get("value"))
                if candidate:
                    parts.append(candidate)
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("answer", "markdown", "text", "output", "result", "content", "message"):
            candidate = _text(value.get(key))
            if candidate:
                return candidate
    return None


def _answer(event: dict) -> str | None:
    candidates: list[Any] = [event]
    for key in ("final", "data", "payload", "result", "message"):
        if key in event:
            candidates.append(event[key])
    for candidate in candidates:
        answer = _text(candidate)
        if answer:
            return answer
    return None


def _reason_from_event(event: dict) -> dict:
    reason = _reason_kind(event)
    return {"kind": reason} if reason is not None else {}


def _sanitize(event: dict) -> dict:
    """Keep event telemetry while excluding prompts, source, and tool output."""
    result = {}
    for key in ("type", "event", "turn", "duration_ms", "durationMs", "tool_name", "toolName",
                "session_id", "sessionId"):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    reason = _reason_from_event(event)
    if reason:
        result["reason"] = reason
    usage = event.get("usage")
    if isinstance(usage, dict):
        safe_usage = {key: value for key, value in usage.items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0}
        if safe_usage:
            result["usage"] = safe_usage
    return result


def _stats(events: list[dict]) -> dict:
    tools = {}
    for event in events:
        name = event.get("tool_name") or event.get("toolName")
        if isinstance(name, str):
            tools[name] = tools.get(name, 0) + 1
    return {
        "event_count": len(events),
        "tool_calls": sum(tools.values()),
        "tool_calls_by_name": tools,
        "final_events": sum(1 for event in events if _is_final(event)),
    }


def run(prompt: str, repo, ctx, settings: dict, timeout: int) -> dict:
    """Run one complete Harness task and return its final Markdown answer."""
    agent = settings.get("agent", {})
    launcher = agent.get("launcher", "docker")
    container_name = None
    if launcher == "docker":
        command, container_name = _docker_command(agent, repo, ctx)
    elif launcher == "local":
        command = _command(agent)
    else:
        raise HarnessError(f"不支持的 Harness launcher：{launcher}")
    prompt_mode = agent.get("prompt_mode", "arg")
    if prompt_mode not in {"arg", "stdin"}:
        raise HarnessError("Harness 的 agent.prompt_mode 必须为 arg 或 stdin")
    if prompt_mode == "arg":
        command.append(prompt)
        prompt_input = None
    else:
        prompt_input = prompt
    env = dict(os.environ)
    configured_env = agent.get("env")
    if launcher == "local" and isinstance(configured_env, dict):
        env.update({str(key): str(value) for key, value in configured_env.items()})
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            input=prompt_input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        if container_name:
            try:
                subprocess.run(["docker", "kill", container_name], capture_output=True,
                               text=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise HarnessError(f"Harness 运行超过 {timeout} 秒", "timeout") from exc
    except OSError as exc:
        raise HarnessError(f"Harness 启动失败：{exc}", "failed") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip()[-800:]
        suffix = f"：{detail}" if detail else ""
        raise HarnessError(f"Harness 进程失败（exit={completed.returncode}）{suffix}", "failed")
    events = _events(completed.stdout)
    stats = _stats(events)
    final = next((event for event in reversed(events) if _is_final(event)), None)
    if final is None:
        raise HarnessError("Harness 事件流缺少最终事件", "failed", stats)
    reason = _reason_kind(final)
    configured_reasons = agent.get("success_reason_kinds", DEFAULT_SUCCESS_REASONS)
    if isinstance(configured_reasons, str):
        raise HarnessError("Harness 的 agent.success_reason_kinds 必须是字符串数组", "failed", stats)
    success_reasons = set(configured_reasons)
    if not reason:
        raise HarnessError("Harness 最终事件缺少 final.reason.kind", "failed", stats)
    if reason in DEFAULT_FAILURE_REASONS or reason not in success_reasons:
        raise HarnessError(f"Harness 最终事件未成功结束：{reason}", "failed", stats)
    answer = _answer(final)
    if not answer:
        raise HarnessError("Harness 最终事件没有 Markdown 答案", "failed", stats)
    session_id = final.get("session_id") or final.get("sessionId")
    model = agent.get("model") or env.get("DEEPSEEK_MODEL") or env.get("OPENAI_MODEL")
    return {
        "raw_text": answer,
        "event_log": [_sanitize(event) for event in events],
        "stats": stats,
        "session_id": session_id,
        "usage": final.get("usage") if isinstance(final.get("usage"), dict) else None,
        "cost_usd": final.get("cost_usd") if isinstance(final.get("cost_usd"), (int, float)) else None,
        "agent_status": "completed",
        "final_reason_kind": reason,
        "agent_model": model,
    }
