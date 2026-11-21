#!/bin/sh
# 内网运行机上的冒烟测试：确认镜像能连上内网千问、只读挂载生效、Skill 可见。
#   sh docker/smoke_test.sh ai-code-audit:1.0 /etc/ai-audit/model.env
set -eu
IMG="${1:-ai-code-audit:latest}"; ENVF="${2:-/etc/ai-audit/model.env}"
T=$(mktemp -d); mkdir -p "$T/repo" "$T/ctx"
( cd "$T/repo" && git init -q && echo 'print("hi")' > a.py && git add . && git -c user.email=t@t -c user.name=t commit -qm init )
echo "只用一句话回答：当前目录有哪些文件？然后尝试创建文件 x.txt，报告是否成功。" > "$T/ctx/prompt.md"
trap 'rm -rf "$T"' EXIT
# 测试目录对容器内用户可写、并且故意不加 :ro：这样 x.txt 写不出来只能是 Qwen Code 的 deny 规则在起作用，
# 而不是只读挂载挡住的（正式扫描时 runner 仍然用 :ro 挂载）
chmod -R a+rwX "$T/repo"
docker run --rm --entrypoint sh --env-file "$ENVF" -v "$T/repo:/workspace/repo" -v "$T/ctx:/workspace/context:ro" "$IMG" \
  -c 'qwen --prompt "$(cat /workspace/context/prompt.md)" --output-format json --approval-mode default --model "$OPENAI_MODEL"' \
  | tail -c 1500
echo
if [ -e "$T/repo/x.txt" ]; then
  echo "失败：模型成功创建了 x.txt，写文件权限没有被禁用" >&2; exit 1
fi
echo "通过：能调用模型，写文件被权限规则拒绝"
