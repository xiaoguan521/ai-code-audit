#!/usr/bin/env bash
# 从 GHCR 拉取已合并的多架构镜像，按架构导出离线包，并保留 GHCR 和本地兼容标签。
# 须在 manifest 合并之后运行；每个架构单独拉取、打标签、导出，避免后拉取的
# 架构覆盖前一个的本地标签。
set -euo pipefail
: "${GITHUB_REF_NAME:?需要版本 tag}"
: "${SCANNER_IMAGE:?需要扫描镜像名}"
: "${RUNNER_IMAGE:?需要调度镜像名}"

mkdir -p offline
version="${GITHUB_REF_NAME#v}"
for arch in amd64 arm64; do
  scanner_name="ai-code-audit_${GITHUB_REF_NAME}_${arch}.tar.gz"
  runner_name="ai-code-audit-runner_${GITHUB_REF_NAME}_${arch}.tar.gz"
  docker pull --platform "linux/$arch" "$SCANNER_IMAGE:$version"
  docker tag "$SCANNER_IMAGE:$version" "ai-code-audit:$version"
  docker save "ai-code-audit:$version" "$SCANNER_IMAGE:$version" | gzip > "offline/$scanner_name"
  docker pull --platform "linux/$arch" "$RUNNER_IMAGE:$version"
  docker tag "$RUNNER_IMAGE:$version" "ai-code-audit-runner:$version"
  docker save "ai-code-audit-runner:$version" "$RUNNER_IMAGE:$version" | gzip > "offline/$runner_name"
  (cd offline && sha256sum "$scanner_name" > "$scanner_name.sha256" && sha256sum "$runner_name" > "$runner_name.sha256")
done
ls -l offline
