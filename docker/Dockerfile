# 八性 AI 代码检查 · Qwen Code 扫描镜像
# 在能上网的构建机上构建，再 docker save 导入内网（见 build.sh）。
# FROM 写成字面量，Dependabot 才能识别并提醒升级基础镜像。
# 要从公司内网镜像仓库拉基础镜像，不改本文件，用 BuildKit 的命名上下文替换：
#   docker build --build-context node:22-bookworm-slim=docker-image://harbor.example.com/library/node:22-bookworm-slim ...
# （build.sh 里设置 BASE_IMAGE 环境变量即可，会自动加上这个参数）
FROM node:22-bookworm-slim

# 固定 Qwen Code 版本：选 GitHub Releases 上最新的稳定版（不要用 preview / nightly），升级前先回归
ARG QWEN_CODE_VERSION=0.26.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep tini \
 && rm -rf /var/lib/apt/lists/* \
 # 挂载进来的仓库属主是宿主机用户，放行只读 git 命令（git log / diff / show）
 && git config --system safe.directory '*' \
 && git config --system core.fsmonitor false

# 有离线包时优先用离线包：把 npm pack 得到的 qwen-code-qwen-code-<版本>.tgz 放在 docker/offline/ 下
COPY docker/offline/ /tmp/offline/
RUN if ls /tmp/offline/*.tgz >/dev/null 2>&1; then npm install -g /tmp/offline/*.tgz; \
    else npm install -g @qwen-code/qwen-code@${QWEN_CODE_VERSION}; fi \
 && npm cache clean --force && rm -rf /tmp/offline \
 && qwen --version

# 系统级（运维级）配置：只读工具、关闭遥测/统计/自动更新、技能目录
COPY docker/qwen-settings.json /etc/qwen-code/settings.json
# Skill 与输出 Schema
COPY skill/eight-quality-audit/SKILL.md /opt/audit/skills/eight-quality-code-audit/SKILL.md
COPY config/result.schema.json /opt/audit/result.schema.json
COPY docker/entrypoint.sh /opt/audit/entrypoint.sh
RUN chmod 0555 /opt/audit/entrypoint.sh && chmod -R a+rX /opt/audit /etc/qwen-code

USER node
ENV HOME=/home/node NO_UPDATE_NOTIFIER=1 NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /workspace/repo
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/audit/entrypoint.sh"]
