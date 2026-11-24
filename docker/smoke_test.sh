#!/bin/sh
# 在运行机测试真实模型连接：AUDIT_MODEL_NETWORK=网络名 sh docker/smoke_test.sh 镜像 model.env
set -eu
IMG="${1:-ai-code-audit:latest}"
ENVF="${2:-/etc/ai-audit/model.env}"
: "${AUDIT_MODEL_NETWORK:?请设置能访问内网模型的 Docker 网络名}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/repo" "$T/ctx"
printf '%s\n' 'print("HARNESS_SMOKE_READ_OK")' > "$T/repo/a.py"
chmod -R a+rX "$T"
docker run --rm --network "$AUDIT_MODEL_NETWORK" --read-only \
  --tmpfs /tmp:rw,size=512m \
  --env-file "$ENVF" -v "$T/repo:/workspace/repo:ro" -v "$T/ctx:/workspace/context:ro" \
  -w /workspace/repo "$IMG" dsh headless --permission-mode read-only --approval-policy never --json \
  '请调用 read 工具读取 a.py，并根据文件内容给出简短回答。' > "$T/events.jsonl"
docker run --rm --network none -i --entrypoint node "$IMG" -e '
  let s=""; process.stdin.setEncoding("utf8"); process.stdin.on("data", c => s += c);
  process.stdin.on("end", () => {
    const events = s.trim().split(/\n+/).filter(Boolean).map(line => JSON.parse(line));
    const final = events.at(-1);
    const reads = events.filter(e => e.type === "tool_call" && e.tool === "read");
    const readSucceeded = reads.some(call => events.some(e => e.type === "tool_result" &&
      e.callId === call.callId && e.status === "completed" && e.result?.includes("HARNESS_SMOKE_READ_OK")));
    if (events[0]?.type !== "session" || final?.type !== "final" || !readSucceeded ||
        final?.reason?.kind !== "completed" || typeof final?.text !== "string" || !final.text.trim()) {
      console.error("Harness 未完成读取工具调用或未正常结束，请检查模型服务和工具调用配置。");
      process.exitCode = 1;
    }
  });
' < "$T/events.jsonl"
echo "通过：DeepSeek Harness 已使用模型调用 read 工具并正常结束；请在正式试扫中检查 64 项输出。"
