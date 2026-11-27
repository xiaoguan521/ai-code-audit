"""External DeepSeek Harness Headless adapter.

The adapter owns only the process lifecycle and event transport. It does not
plan audit steps, inspect source files, or decide how the Agent Loop works.
The final Markdown answer is returned unchanged as the project report.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
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
IGNORED_EVENT_TYPES = {
    "tool_call", "tool_result", "tool_execution_start", "tool_execution_end",
    "tool_use", "tool_output", "user", "user_message", "system", "status",
    "heartbeat", "thinking", "reasoning", "thought", "turn_start", "turn_end",
}
DELTA_EVENT_TYPES = {
    "delta", "text_delta", "content_delta", "assistant_delta", "message_delta",
    "output_delta", "chunk",
}
OUTER_FENCE_OPEN_RE = re.compile(r"^```(?:markdown|md|txt)?[ \t]*$", re.IGNORECASE)


def unwrap_outer_markdown_fence(text: str) -> str:
    """若整份报告被最外层 ```markdown ... ``` 包裹，安全剥离外层围栏。"""
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 3:
        return text
    if not OUTER_FENCE_OPEN_RE.match(lines[0].strip()) or lines[-1].strip() != "```":
        return text
    inner_lines = lines[1:-1]
    # 确认开头的围栏没有在文末之前提前闭合（即内部围栏成对出现且不在中途降回 0 层）。
    depth = 0
    for line in inner_lines:
        s = line.strip()
        if s.startswith("```"):
            if depth == 0 and s == "```":
                # 说明首行围栏在此处就闭合了，正文包含多段代码块而非整篇被包在围栏里
                return text
            depth = 0 if (depth > 0 and s == "```") else depth + 1
    if depth != 0:
        return text
    inner = "\n".join(inner_lines)
    if re.search(r"^#{1,3}\s+", inner, re.M) or "|" in inner:
        return inner.strip() + "\n"
    return text


def _looks_like_full_report(text: str) -> bool:
    if len(text) < 800:
        return False
    has_heading = bool(re.search(r"^#{1,3}\s+", text, re.M))
    has_table = bool(re.search(r"^\|.+\|$", text, re.M))
    return has_heading and has_table


def _recover_report_from_events(events: list[dict], final_answer: str) -> str:
    """当 final 事件仅输出“报告已在上一条输出/报告已完成”等简短确认语时，从前序助手事件找回完整报告。"""
    cleaned_final = unwrap_outer_markdown_fence(final_answer)
    if _looks_like_full_report(cleaned_final):
        return cleaned_final

    candidates: list[str] = []
    delta_buf: list[str] = []

    def flush_delta() -> None:
        if delta_buf:
            joined = "".join(delta_buf).strip()
            if joined:
                candidates.append(joined)
            delta_buf.clear()

    for event in events:
        if _is_final(event):
            flush_delta()
            continue
        kind = str(event.get("type") or event.get("event") or "").lower()
        role = str(event.get("role") or (event.get("message") or {}).get("role") or "").lower() \
            if isinstance(event.get("message"), dict) else str(event.get("role") or "").lower()
        if kind in IGNORED_EVENT_TYPES or role in {"user", "system", "tool"}:
            flush_delta()
            continue
        if kind in DELTA_EVENT_TYPES:
            piece = event.get("delta") or event.get("text") or event.get("content")
            if isinstance(piece, str):
                delta_buf.append(piece)
            elif isinstance(piece, dict):
                sub = piece.get("text") or piece.get("content")
                if isinstance(sub, str):
                    delta_buf.append(sub)
            continue
        flush_delta()
        text = _answer(event)
        if text:
            candidates.append(text)
    flush_delta()

    for raw_candidate in reversed(candidates):
        unwrapped = unwrap_outer_markdown_fence(raw_candidate)
        if _looks_like_full_report(unwrapped) and len(unwrapped) >= max(1000, len(cleaned_final) * 2):
            return unwrapped
    return cleaned_final


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
    for key in ("type", "event", "phase", "turn", "step", "duration_ms", "durationMs", "tool_name", "toolName",
                "tool", "callId", "status", "session_id", "sessionId"):
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
        if not name and event.get("type") == "tool_call":
            name = event.get("tool")
        if isinstance(name, str):
            tools[name] = tools.get(name, 0) + 1
    return {
        "event_count": len(events),
        "tool_calls": sum(tools.values()),
        "tool_calls_by_name": tools,
        "final_events": sum(1 for event in events if _is_final(event)),
    }


def _configured_model(agent: dict, launcher: str, env: dict) -> str | None:
    """读取实际传给扫描容器的模型名；不读取、记录或散列 API Key。"""
    names = {"OPENAI_BASE_URL", "OPENAI_MODEL"}
    selected = {}
    if launcher == "local":
        selected.update({key: env[key] for key in names if key in env})
    elif agent.get("env_file"):
        try:
            with open(agent["env_file"], encoding="utf-8") as source:
                for line in source:
                    line = line.strip("\r\n").lstrip()
                    if not line or line.startswith("#"):
                        continue
                    key, separator, value = line.partition("=")
                    if key in names:
                        if separator:
                            selected[key] = value
                        elif key in env:
                            selected[key] = env[key]
        except (OSError, UnicodeError):
            pass  # Docker 会报告缺失或无法读取的 env 文件。
    configured = agent.get("env")
    if isinstance(configured, dict):
        selected.update({key: str(value) for key, value in configured.items() if key in names})
    if selected.get("OPENAI_BASE_URL") and selected.get("OPENAI_MODEL"):
        return selected["OPENAI_MODEL"]
    return agent.get("model") or None


def _run_with_progress(command: list[str], cwd, prompt_input: str | None, env: dict,
                       timeout: int, progress, diag: dict | None = None) -> subprocess.CompletedProcess:
    """Stream sanitized event boundaries while preserving the original final event stream."""
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if prompt_input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, shell=False,
                               encoding="utf-8", errors="replace")
    stdout, stderr = [], []
    start = time.monotonic()

    def emit(event):
        if diag is not None:
            if event.get("phase") == "step_start" or isinstance(event.get("step"), int):
                step_val = event.get("step")
                diag["steps"] = max(diag.get("steps", 0), step_val if isinstance(step_val, int) else diag.get("steps", 0) + 1)
            tname = event.get("tool") or event.get("tool_name") or event.get("toolName")
            if isinstance(tname, str) and tname:
                diag["tool_calls"] = diag.get("tool_calls", 0) + 1
                diag["last_tool"] = tname
        try:
            progress(event)
        except Exception:
            pass  # Progress reporting must never interrupt the Harness session.

    def pump(stream, output, events=False):
        try:
            for line in stream:
                output.append(line)
                if events:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(event, dict):
                        emit(_sanitize(event))
        finally:
            stream.close()

    readers = [threading.Thread(target=pump, args=(process.stdout, stdout, True), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, stderr), daemon=True)]
    for reader in readers:
        reader.start()
    if prompt_input is not None:
        def send_input():
            try:
                process.stdin.write(prompt_input)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=send_input, daemon=True).start()
    try:
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(remaining, 30))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - start >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                emit({"type": "heartbeat", "elapsed_seconds": int(time.monotonic() - start)})
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
    return subprocess.CompletedProcess(command, process.returncode, "".join(stdout), "".join(stderr))


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
    diag: dict[str, Any] = {}
    try:
        progress = settings.get("_progress_callback")
        if callable(progress):
            completed = _run_with_progress(command, repo, prompt_input, env, timeout, progress, diag=diag)
        else:
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
        detail_parts = []
        if diag.get("steps"):
            detail_parts.append(f"已运行 {diag['steps']} 步")
        if diag.get("tool_calls"):
            detail_parts.append(f"工具调用 {diag['tool_calls']} 次")
        if diag.get("last_tool"):
            detail_parts.append(f"最后工具 {diag['last_tool']}")
        suffix = f"（{'，'.join(detail_parts)}）" if detail_parts else ""
        raise HarnessError(f"Harness 运行超过 {timeout} 秒{suffix}", "timeout") from exc
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
    answer = _recover_report_from_events(events, answer)
    session_id = final.get("session_id") or final.get("sessionId")
    model = _configured_model(agent, launcher, env)
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
