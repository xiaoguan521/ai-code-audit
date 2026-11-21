# 八性 AI 代码检查 · Pi 扫描镜像
# FROM 写成字面量，Dependabot 才能识别并提醒升级基础镜像。
FROM node:22-bookworm-slim

# 与 docker/PI_VERSION 保持一致；Pi 1.6.0 要求 Node.js >= 22.19。
ARG PI_VERSION=1.6.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep fd-find tini \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*' \
 && git config --system core.fsmonitor false \
 && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force \
 && pi --version

COPY skill/eight-quality-audit/SKILL.md /opt/audit/skills/eight-quality-code-audit/SKILL.md
COPY config/result.schema.json /opt/audit/result.schema.json
COPY docker/entrypoint.sh docker/pi_entrypoint.cjs docker/pi_events.cjs docker/pi_guard.mjs /opt/audit/
COPY docker/pi_rg.cjs /opt/audit/bin/rg
RUN chmod 0555 /opt/audit/entrypoint.sh /opt/audit/bin/rg && chmod -R a+rX /opt/audit

USER node
ENV HOME=/home/node PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PATH=/opt/audit/bin:$PATH
WORKDIR /workspace/repo
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/audit/entrypoint.sh"]
