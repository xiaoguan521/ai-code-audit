#!/bin/sh
# 在能上网的构建机执行：构建镜像并导出为离线包，拷进内网后 docker load。
#   sh docker/build.sh 1.0
set -eu
cd "$(dirname "$0")/.."
TAG="ai-code-audit:${1:-latest}"
QWEN_CODE_VERSION="${QWEN_CODE_VERSION:-$(tr -d '[:space:]' < docker/QWEN_CODE_VERSION)}"
# 基础镜像名要和 Dockerfile 的 FROM 一致；设置 BASE_IMAGE 时用命名上下文替换成内网地址（需要 BuildKit，Docker 23+ 默认开启）
FROM_IMAGE="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]*\).*/\1/p' docker/Dockerfile | head -n 1)"
docker build -f docker/Dockerfile -t "$TAG" \
  ${QWEN_CODE_VERSION:+--build-arg QWEN_CODE_VERSION=$QWEN_CODE_VERSION} \
  ${BASE_IMAGE:+--build-context "$FROM_IMAGE=docker-image://$BASE_IMAGE"} .
docker save "$TAG" | gzip > "ai-code-audit_${1:-latest}.tar.gz"
echo "已导出 ai-code-audit_${1:-latest}.tar.gz；内网执行：docker load < ai-code-audit_${1:-latest}.tar.gz"
