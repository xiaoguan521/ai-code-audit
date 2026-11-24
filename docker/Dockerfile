# 八性 AI 代码检查 · DeepSeek Harness 扫描镜像
# FROM 写成字面量，Dependabot 才能识别并提醒升级基础镜像。
FROM node:24.22.3-bookworm-slim AS harness-build

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

# npm 会为 Harness 的多个插件重复安装相同文件；只合并内容、权限相同的文件，
# 保留原有路径和版本，避免改变 Node 的模块解析和 Harness 插件能力。
RUN hardlink --ignore-time --respect-name /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai

# 编译依赖只留在构建阶段，运行阶段保留 Harness、源码工具和原生库。
FROM node:24.22.3-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash git ca-certificates ripgrep fd-find tini \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*' \
 && git config --system core.fsmonitor false
COPY --from=harness-build /usr/local/lib/node_modules/@deepseek-ai/dsh/ /usr/local/lib/node_modules/@deepseek-ai/dsh/
RUN ln -s ../lib/node_modules/@deepseek-ai/dsh/lib/bin.js /usr/local/bin/dsh

COPY skill/eight-quality-audit/ /opt/audit/skills/eight-quality-code-audit/
COPY config/result.schema.json /opt/audit/result.schema.json
COPY docker/dsh.sh /opt/audit/bin/dsh
COPY docker/openai-model.patch.yml /opt/audit/openai-model.patch.yml
RUN chmod -R a+rX /opt/audit && chmod 0555 /opt/audit/bin/dsh

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
