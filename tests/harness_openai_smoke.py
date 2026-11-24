#!/usr/bin/env python3
"""在两个镜像之间验证 Harness → OpenAI SSE → read 工具 → 第二轮请求。

不使用真实模型或源码。构建工作流用每个架构的扫描镜像和调度镜像各跑一次。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "Qwen/test-model"
KEY = "mock-key-not-a-secret"
MARKER = "HARNESS_READ_SMOKE_SENTINEL"
ANSWER = "HARNESS_OPENAI_TOOL_LOOP_OK"


class MockHandler(BaseHTTPRequestHandler):
    requests = 0
    tool_results = 0

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"requests": type(self).requests,
                                     "tool_results": type(self).tool_results}).encode())

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions", self.path
            assert self.headers.get("Authorization") == f"Bearer {KEY}"
            assert data["model"] == MODEL, data["model"]
            assert data.get("stream") is True
            assert data.get("max_tokens") == 2048, "max_tokens 未使用配置值"
            messages = data["messages"]
            tool_result = any(message.get("role") == "tool" and MARKER in json.dumps(message)
                              for message in messages)
            if tool_result:
                type(self).tool_results += 1
                deltas = [{"role": "assistant", "content": ANSWER}]
                finish_reason = "stop"
            else:
                names = [tool.get("function", {}).get("name") for tool in data.get("tools", [])]
                assert "read" in names, f"Harness 未提供 read 工具: {names}"
                assert type(self).requests == 0, "没有收到 read 工具结果"
                deltas = [
                    {"role": "assistant", "reasoning_content": "Inspect the file first."},
                    {"tool_calls": [{"index": 0, "id": "call_smoke", "type": "function",
                                     "function": {"name": "read", "arguments": '{"file_path":'}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
                ]
                finish_reason = "tool_calls"
            type(self).requests += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [(delta, None) for delta in deltas] + [({}, finish_reason)]:
                chunk = {"id": "chatcmpl-smoke", "object": "chat.completion.chunk", "created": 1,
                         "model": MODEL, "choices": [{"index": 0, "delta": delta,
                                                       "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as exc:
            self.send_error(400, str(exc))


def client():
    from runner.adapters import harness

    root = Path(sys.argv[2])
    image, network = sys.argv[3:5]
    url = "http://audit-model:8000"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=2).close()
            break
        except OSError:
            time.sleep(0.5)
    else:
        raise RuntimeError("模拟模型服务未启动")
    result = harness.run("请使用 read 工具读取 a.py，然后回答。", root / "repo", root / "ctx", {
        "agent": {"launcher": "docker", "image": image, "network": network,
                  "env_file": str(root / "model.env")},
    }, 180)
    assert result["raw_text"] == ANSWER, result["raw_text"]
    assert result["agent_model"] == MODEL, result["agent_model"]
    assert result["final_reason_kind"] == "completed"
    assert result["stats"]["tool_calls_by_name"].get("read", 0) >= 1, result["stats"]
    assert KEY not in json.dumps(result, ensure_ascii=False)
    with urllib.request.urlopen(url, timeout=5) as response:
        status = json.load(response)
    assert status["requests"] >= 2 and status["tool_results"] >= 1, status
    print(f"通过：调度镜像 → 扫描镜像 → {MODEL} 兼容接口，Harness 读取文件并完成多轮调用")


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: harness_openai_smoke.py SCANNER_IMAGE RUNNER_IMAGE")
    scanner, runner = sys.argv[1:]
    suffix = uuid.uuid4().hex[:12]
    network, server = f"audit-smoke-{suffix}", f"audit-model-{suffix}"
    script = str(Path(__file__).resolve())
    subprocess.run(["docker", "network", "create", network], check=True, stdout=subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory(prefix="audit-harness-smoke-") as temp:
            root = Path(temp)
            root.chmod(0o755)
            (root / "repo").mkdir(mode=0o755)
            (root / "ctx").mkdir(mode=0o755)
            (root / "repo" / "a.py").write_text(f"# {MARKER}\n", encoding="utf-8")
            (root / "model.env").write_text(
                f"OPENAI_BASE_URL=http://audit-model:8000/v1\nOPENAI_MODEL={MODEL}\n"
                f"OPENAI_API_KEY={KEY}\nOPENAI_CONTEXT_WINDOW=16384\nOPENAI_MAX_OUTPUT_TOKENS=2048\n",
                encoding="utf-8")
            subprocess.run(["docker", "run", "-d", "--rm", "--name", server, "--network", network,
                            "--network-alias", "audit-model", "-v", f"{script}:/test/smoke.py:ro",
                            "--entrypoint", "python3", runner, "/test/smoke.py", "--serve"],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "run", "--rm", "--network", network,
                            "-v", "/var/run/docker.sock:/var/run/docker.sock",
                            "-v", f"{temp}:{temp}", "-v", f"{script}:/test/smoke.py:ro",
                            "-e", "PYTHONPATH=/opt/ai-code-audit", "--entrypoint", "python3", runner,
                            "/test/smoke.py", "--client", temp, scanner, network], check=True)
    finally:
        subprocess.run(["docker", "rm", "-f", server], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        ThreadingHTTPServer(("0.0.0.0", 8000), MockHandler).serve_forever()
    elif len(sys.argv) > 1 and sys.argv[1] == "--client":
        client()
    else:
        main()
