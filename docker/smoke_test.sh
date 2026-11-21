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
printf '%s\n' '请读取 a.py，并按照系统提示输出审计 JSON。' > "$T/ctx/prompt.md"
chmod -R a+rX "$T"
docker run --rm --network "$AUDIT_MODEL_NETWORK" --read-only \
  --tmpfs /tmp:rw,size=512m --tmpfs /home/node:rw,size=256m,uid=1000,gid=1000 \
  --env-file "$ENVF" -v "$T/repo:/workspace/repo:ro" -v "$T/ctx:/workspace/context:ro" \
  "$IMG" > "$T/result.json"
docker run --rm --network none -i --entrypoint node "$IMG" -e '
  let s=""; process.stdin.setEncoding("utf8"); process.stdin.on("data", c => s += c);
  process.stdin.on("end", () => { const r=JSON.parse(s); if (!Array.isArray(r) || !r.at(-1)?.result) process.exitCode=1; });
' < "$T/result.json"
echo "通过：Pi 成功调用模型并返回完整结果；请在正式试扫中检查 64 项输出。"
