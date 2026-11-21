#!/bin/sh
# 在能上网的构建机执行：构建镜像并导出为离线包，拷进内网后 docker load。
#   sh docker/build.sh 1.0                         # 构建机本机架构
#   PLATFORM=linux/arm64 sh docker/build.sh 1.0    # ARM64 服务器用（x86 构建机需先装 QEMU/binfmt）
#   PLATFORM=linux/amd64 sh docker/build.sh 1.0
# 离线包按架构命名：ai-code-audit_<版本>_<amd64|arm64>.tar.gz
set -eu
cd "$(dirname "$0")/.."
VER="${1:-latest}"
TAG="ai-code-audit:${VER}"
QWEN_CODE_VERSION="${QWEN_CODE_VERSION:-$(tr -d '[:space:]' < docker/QWEN_CODE_VERSION)}"
if [ -z "${PLATFORM:-}" ]; then
  case "$(uname -m)" in
    x86_64|amd64) PLATFORM=linux/amd64 ;;
    aarch64|arm64) PLATFORM=linux/arm64 ;;
    *) echo "不支持的构建机架构 $(uname -m)，请设置 PLATFORM=linux/amd64 或 linux/arm64" >&2; exit 1 ;;
  esac
fi
case "$PLATFORM" in
  linux/amd64|linux/arm64) ARCH="${PLATFORM#linux/}" ;;
  *) echo "PLATFORM 只支持 linux/amd64 或 linux/arm64，当前：$PLATFORM" >&2; exit 1 ;;
esac
# 基础镜像名要和 Dockerfile 的 FROM 一致；设置 BASE_IMAGE 时用命名上下文替换成内网地址（需要 BuildKit，Docker 23+ 默认开启）
# 注意：BASE_IMAGE 指向的内网镜像也必须包含目标架构（多架构镜像或对应架构的单架构镜像）
FROM_IMAGE="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]*\).*/\1/p' docker/Dockerfile | head -n 1)"
docker build --platform "$PLATFORM" -f docker/Dockerfile -t "$TAG" \
  ${QWEN_CODE_VERSION:+--build-arg QWEN_CODE_VERSION=$QWEN_CODE_VERSION} \
  ${BASE_IMAGE:+--build-context "$FROM_IMAGE=docker-image://$BASE_IMAGE"} .
GOT="$(docker image inspect -f '{{.Architecture}}' "$TAG")"
[ "$GOT" = "$ARCH" ] || { echo "构建结果架构为 $GOT，与目标 $ARCH 不符" >&2; exit 1; }
OUT="ai-code-audit_${VER}_${ARCH}.tar.gz"
docker save "$TAG" | gzip > "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
echo "已导出 $OUT（$PLATFORM）；内网执行：sha256sum -c $OUT.sha256 && docker load < $OUT"
