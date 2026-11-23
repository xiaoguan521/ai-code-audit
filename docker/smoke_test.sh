#!/bin/sh
# 在运行机测试真实模型连接：AUDIT_MODEL_NETWORK=网络名 sh docker/smoke_test.sh 镜像 model.env
set -eu
IMG="${1:-ai-code-audit:latest}"
ENVF="${2:-/etc/ai-audit/model.env}"
: "${AUDIT_MODEL_NETWORK:?请设置能访问内网模型的 Docker 网络名}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/repo" "$T/ctx"
printf '%s\n' 'print("hello")' > "$T/repo/a.py"
printf '%s\n' '请读取 a.py，并使用 eight-quality-code-audit Skill 完成一次最小八性审查；按系统提示输出完整 Markdown 结果。' > "$T/ctx/prompt.md"
chmod -R a+rX "$T"
docker run --rm --network "$AUDIT_MODEL_NETWORK" --read-only \
  --tmpfs /tmp:rw,size=512m \
  --env-file "$ENVF" -v "$T/repo:/workspace/repo:ro" -v "$T/ctx:/workspace/context:ro" \
  "$IMG" dsh headless --permission-mode read-only --approval-policy never --json - < "$T/ctx/prompt.md" > "$T/events.jsonl"
docker run --rm --network none -i --entrypoint node "$IMG" -e '
  let s=""; process.stdin.setEncoding("utf8"); process.stdin.on("data", c => s += c);
  process.stdin.on("end", () => {
    const events = s.trim().split(/\n+/).filter(Boolean).map(line => JSON.parse(line));
    const final = events.at(-1);
    if (events[0]?.type !== "session" || final?.type !== "final" ||
        final?.reason?.kind !== "completed" || typeof final?.text !== "string" || !final.text.trim()) {
      console.error(JSON.stringify({ first: events[0], final }));
      process.exitCode = 1;
    }
  });
' < "$T/events.jsonl"
echo "通过：DeepSeek Harness headless Agent 已完成真实模型调用并返回 final 事件；请在正式试扫中检查 64 项输出。"
