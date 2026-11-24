#!/bin/sh
# 在与实际扫描容器一致的只读、noexec /tmp 环境中验证 Harness 启动。
set -eu

arch=${1:?usage: smoke_scanner_image.sh amd64\|arm64}
case "$arch" in
  amd64|arm64) ;;
  *) printf '不支持的架构：%s\n' "$arch" >&2; exit 1 ;;
esac

img="ai-code-audit:ci-$arch"
printf '== %s ==\n' "$arch"
[ "$(docker image inspect -f '{{.Architecture}}' "$img")" = "$arch" ] || {
  printf '%s 架构不是 %s\n' "$img" "$arch" >&2
  exit 1
}
docker run --rm --platform "linux/$arch" --entrypoint dsh "$img" --version
docker run --rm --platform "linux/$arch" --entrypoint sh "$img" -c '
  test -f /opt/audit/skills/eight-quality-code-audit/SKILL.md &&
  test -f /opt/audit/result.schema.json &&
  rg --version >/dev/null && fdfind --version >/dev/null && git --version >/dev/null &&
  test "$DSH_BUNDLED_SKILL_DIR" = /opt/audit/skills &&
  test "$DSH_PERMISSION_MODE" = read-only &&
  test "$NARB_DISABLE_NATIVE_CACHE" = 1 &&
  ! command -v pi >/dev/null && ! command -v claude >/dev/null &&
  test ! -d /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-coding-agent &&
  [ "$(id -u)" != "0" ] && echo "Harness 镜像内容检查通过：$(uname -m)"'

help_output=$(docker run --rm --init --platform "linux/$arch" --network none --read-only \
  --tmpfs /tmp:rw,size=128m --cap-drop ALL --security-opt no-new-privileges \
  --entrypoint dsh "$img" --profile headless --help)
printf '%s\n' "$help_output" | grep -q -- '--json'
printf 'Harness Headless 启动通过：%s\n' "$arch"
