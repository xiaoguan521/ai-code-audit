#!/bin/sh
# 仅为配置了 OpenAI 兼容模型的 Headless 会话选择模型路由。
# Agent、工具和任务规划仍由官方 dsh 执行；密钥只通过环境变量传递。
set -eu

case "${1:-}" in
  headless) profile=headless ;;
  --profile) profile=${2:-} ;;
  *) profile= ;;
esac

if [ "$profile" = headless ] && { [ -n "${OPENAI_BASE_URL:-}" ] || [ -n "${OPENAI_MODEL:-}" ]; }; then
  for key in OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY; do
    case "$key" in
      OPENAI_BASE_URL) value=${OPENAI_BASE_URL:-} ;;
      OPENAI_MODEL) value=${OPENAI_MODEL:-} ;;
      OPENAI_API_KEY) value=${OPENAI_API_KEY:-} ;;
    esac
    if [ -z "$value" ]; then
      printf 'Harness 模型配置缺少 %s；无鉴权的模型服务也请设置 OPENAI_API_KEY=EMPTY\n' "$key" >&2
      exit 2
    fi
  done
  for value in "${OPENAI_CONTEXT_WINDOW:-131072}" "${OPENAI_MAX_OUTPUT_TOKENS:-8192}"; do
    case "$value" in
      ''|*[!0-9]*|0) printf 'OPENAI_CONTEXT_WINDOW 和 OPENAI_MAX_OUTPUT_TOKENS 必须是正整数\n' >&2; exit 2 ;;
    esac
  done
  case "$1" in
    headless) shift; set -- headless --patch /opt/audit/openai-model.patch.yml "$@" ;;
    --profile) shift 2; set -- --profile headless --patch /opt/audit/openai-model.patch.yml "$@" ;;
  esac
fi

exec /usr/local/bin/dsh "$@"
