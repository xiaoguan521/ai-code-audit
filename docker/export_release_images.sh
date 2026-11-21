#!/usr/bin/env bash
# 将已经通过冒烟测试的两种镜像按架构导出，并保留 GHCR 和本地兼容标签。
set -euo pipefail
: "${GITHUB_REF_NAME:?需要版本 tag}"
: "${SCANNER_IMAGE:?需要扫描镜像名}"
: "${RUNNER_IMAGE:?需要调度镜像名}"

mkdir -p offline
version="${GITHUB_REF_NAME#v}"
for arch in amd64 arm64; do
  scanner_name="ai-code-audit_${GITHUB_REF_NAME}_${arch}.tar.gz"
  runner_name="ai-code-audit-runner_${GITHUB_REF_NAME}_${arch}.tar.gz"
  docker tag "ai-code-audit:ci-$arch" "ai-code-audit:$version"
  docker tag "ai-code-audit:ci-$arch" "$SCANNER_IMAGE:$version"
  docker save "ai-code-audit:$version" "$SCANNER_IMAGE:$version" | gzip > "offline/$scanner_name"
  docker tag "ai-code-audit-runner:ci-$arch" "ai-code-audit-runner:$version"
  docker tag "ai-code-audit-runner:ci-$arch" "$RUNNER_IMAGE:$version"
  docker save "ai-code-audit-runner:$version" "$RUNNER_IMAGE:$version" | gzip > "offline/$runner_name"
  (cd offline && sha256sum "$scanner_name" > "$scanner_name.sha256" && sha256sum "$runner_name" > "$runner_name.sha256")
done
ls -l offline
