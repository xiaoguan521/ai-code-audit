# 八性 AI 代码检查 · DeepSeek Harness 扫描镜像
# FROM 写成字面量，Dependabot 才能识别并提醒升级基础镜像。
FROM node:24.22.3-bookworm-slim

# DeepSeek Harness 官方 CLI。版本必须和 Node.js 主版本要求匹配。
ARG DSH_VERSION=0.2.3-alpha.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash git ca-certificates ripgrep fd-find tini build-essential python3 \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*' \
 && git config --system core.fsmonitor false \
 && npm install -g --omit=dev --include=optional \
      --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
      "@deepseek-ai/dsh@${DSH_VERSION}" \
 && npm cache clean --force \
 && dsh --version

COPY skill/eight-quality-audit/ /opt/audit/skills/eight-quality-code-audit/
COPY config/result.schema.json /opt/audit/result.schema.json
RUN chmod -R a+rX /opt/audit

USER node
# 扫描容器的 /tmp 为 noexec；原生加载器须直接从镜像加载预编译库。
ENV HOME=/tmp/home \
    DSH_HOME=/tmp/dsh \
    DSH_BUNDLED_SKILL_DIR=/opt/audit/skills \
    DSH_PERMISSION_MODE=read-only \
    NARB_DISABLE_NATIVE_CACHE=1 \
    DSH_TELEMETRY_MODE=DISABLED \
    DSH_TELEMETRY_DISABLED=1 \
    PATH=/opt/audit/bin:$PATH
WORKDIR /workspace/repo
CMD ["dsh", "--profile", "headless", "--help"]
