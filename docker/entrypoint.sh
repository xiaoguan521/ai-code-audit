#!/bin/sh
# 容器入口：读取宿主机写好的提示词，带上八性 Skill 与输出格式要求，非交互运行 Qwen Code。
set -eu
PROMPT_FILE=/workspace/context/prompt.md
[ -f "$PROMPT_FILE" ] || { echo "缺少 $PROMPT_FILE" >&2; exit 2; }
: "${OPENAI_BASE_URL:?需要 OPENAI_BASE_URL（内网千问 OpenAI 兼容地址）}"
: "${OPENAI_MODEL:?需要 OPENAI_MODEL}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-none}"

SKILL=$(cat /opt/audit/skills/eight-quality-code-audit/SKILL.md)
SCHEMA=$(cat /opt/audit/result.schema.json)

# Qwen 0.26.1 会把以 - 开头的参数值重新识别为选项；前导空格保留原文并避开该问题。
SYSTEM_EXTRA=" ${SKILL}

## 输出格式（批量模式强制）
完成审查后，最终回复只输出一个 JSON 对象，不要输出任何解释文字或 Markdown，不要用代码块包裹。
JSON 必须符合下面的 JSON Schema（items 必须恰好 64 项，dimension_notes 恰好 8 项）：
${SCHEMA}"

exec qwen \
  --prompt " $(cat "$PROMPT_FILE")" \
  --output-format json \
  --approval-mode default \
  --append-system-prompt "$SYSTEM_EXTRA" \
  --include-directories /workspace/context \
  --max-session-turns "${MAX_SESSION_TURNS:-300}" \
  --model "$OPENAI_MODEL"
