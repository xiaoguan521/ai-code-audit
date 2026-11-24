# DeepSeek Harness 新镜像配置与旧版迁移

本文适用于从旧版 Runner 解析、校验和评分的流程，迁移到 Harness 直接交付 Markdown 的流程。代码更新后必须重新构建并升级**扫描镜像和 Runner 镜像**，并使用与镜像同版本的 `compose.yaml`；仅修改旧镜像的环境变量不会改变运行逻辑。以下 `main` 标签会随构建更新，生产环境建议使用两个镜像同一次构建对应的 `sha-<短提交号>`。

| 镜像 | 职责 |
| --- | --- |
| `ghcr.io/xiaoguan521/ai-code-audit` | DeepSeek Harness、八性 Skill 和模型接入；一个项目一个独立自主审查会话 |
| `ghcr.io/xiaoguan521/ai-code-audit-runner` | 同步项目、预扫描、会话并发、报告归档、启动整体汇总 |

Harness 在每个项目的同一会话中自检和局部补正、计算得分，返回完整 Markdown。Runner 不解析项目 Markdown，不进行证据校验和评分，不按维度拆任务，也不因机器字段格式整项目重跑。另一个独立会话只读取本批项目 Markdown，生成整体报告；失败时保留项目报告与索引。

## 1. 保留原配置并更新两个镜像

备份原 `.env`、`model.env`、项目清单和 Git 凭据，保留原数据目录。使用含本次改造的代码构建后：

```bash
docker pull ghcr.io/xiaoguan521/ai-code-audit:<相同版本>
docker pull ghcr.io/xiaoguan521/ai-code-audit-runner:<相同版本>
```

内网无法访问 GHCR 时，在外网机器拉取后执行 `docker save`，传入内网再执行 `docker load`；也可同步到 Harbor。两台机器的目标架构须匹配镜像架构。镜像是否已发布或同步到内网，以构建结果和现场仓库为准。

用同版本的仓库 `compose.yaml` 替换旧编排文件；不要覆盖现场的 `.env`、`model.env`、`config/projects.local.csv` 和 `secrets/`。如下路径适用于仓库自带 Compose，独立编排需保持与现场目录一致：

```text
部署目录/
├── compose.yaml
├── .env
├── model.env
├── config/projects.local.csv
└── secrets/
    ├── git-credentials
    └── ssh/
```

## 2. 调整 `.env`

```dotenv
AUDIT_DATA_DIR=/home/atwa/ai-code-audit/data
AUDIT_DOCKER_SOCKET=/var/run/docker.sock
AUDIT_MODEL_NETWORK=ai-audit-model-net
AUDIT_SCANNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit:<相同版本>
AUDIT_RUNNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit-runner:<相同版本>
AUDIT_AGENT_TYPE=deepseek-harness
AUDIT_HARNESS_BIN=dsh
AUDIT_HARNESS_ARGS=["headless","--json","--permission-mode","read-only","--approval-policy","never"]
AUDIT_MAX_PARALLEL=2
AUDIT_MAX_MODEL_CONCURRENCY=2
AUDIT_SUMMARY_ENABLED=true
AUDIT_SEMGREP_RULES_DIR=
```

`AUDIT_DATA_DIR` 是宿主机绝对路径，Runner 容器与宿主机必须使用相同路径。模型网络必须可访问模型服务。`AUDIT_MAX_PARALLEL` 控制每个批次最多同时处理的项目数；`AUDIT_MAX_MODEL_CONCURRENCY` 控制同一数据目录下多个 Runner 合计可用的模型会话数。请在共享目录的不同 Runner 中使用相同的并发配置。

已有 `.env` 如果包含 `AUDIT_SUMMARY_ENABLED=false`，升级后要改为 `true` 才会启动 AI 汇总；关闭时只生成项目报告索引。汇总会话默认复用扫描镜像、网络和模型凭据，仍为独立会话。

`AUDIT_HARNESS_ARGS` 是 JSON 字符串数组，`--json` 指事件流。扫描镜像会根据 `model.env` 自动加载 OpenAI 兼容模型配置，通常无需手动增加 `--patch`。

## 3. 配置自部署千问

将 `docker/model.env.example` 复制为部署目录的 `model.env` 后修改，已有凭据不要覆盖：

```dotenv
OPENAI_BASE_URL=http://模型服务地址:8000/v1
OPENAI_MODEL=模型服务实际暴露的模型ID
OPENAI_API_KEY=EMPTY
OPENAI_CONTEXT_WINDOW=131072
OPENAI_MAX_OUTPUT_TOKENS=8192
```

模型地址是从 **Harness 容器**可访问的 OpenAI 兼容接口根路径，不要附加 `/chat/completions`。需鉴权时填写实际 Key；无鉴权可用 `EMPTY`。服务需要支持 Harness 所需的流式响应和工具调用。上下文及最大输出令牌数需与实际模型能力一致；完整 64 项报告较长，应按服务能力调整，避免输出上限截断。

只读 Git 凭据按原部署方式保存到 `secrets/git-credentials` 或 `secrets/ssh/`，不要提交真实凭据。`config/projects.local.csv` 参考 `config/projects.compose.example.csv`，项目 ID 在同一清单中必须唯一。

## 4. 可选 Semgrep 离线规则

Runner 自带 Semgrep，但默认不下载规则。未配置规则时会显示 `semgrep: 未配置离线规则` 并跳过，其他工具和 Harness 审查继续运行。

如需启用，将准备好的规则放在已经挂载进 Runner 的 `AUDIT_DATA_DIR` 下，例如 `/home/atwa/ai-code-audit/data/rules/semgrep/`，并设置：

```dotenv
AUDIT_SEMGREP_RULES_DIR=/home/atwa/ai-code-audit/data/rules/semgrep
```

路径须为 Runner 容器可读的绝对路径。规则必须事先准备好；平台不会自动联网下载。预扫描结果只是供 Harness 回查源码的线索。

## 5. 开始扫描和汇总

```bash
# 一批并发扫描两个项目
docker compose run --rm --pull never -T audit --only project-a project-b

# 单项目：不使用历史 Markdown 作为参考
docker compose run --rm --pull never -T audit --only project-a --force-full

# 将已分别扫描过的项目按最近一次报告汇总
docker compose run --rm --pull never -T audit --only project-a project-b --summary-only
```

正常扫描会向 Harness 提供可用的上次 Markdown 和 Git 变更作为参考，由 Harness 自主决定需要复核的内容；仍须交付覆盖 64 项的完整新报告。`--force-full` 不提供历史报告。旧版本仅有 JSON 结果时不会被当成新 Markdown 自动沿用，第一次扫描会直接审查。

日志显示运行编号和结果路径：

```text
<data>/output/reports/<日期>/<运行编号>/<项目ID>.md
<data>/output/reports/<日期>/<运行编号>/_summary.md
<data>/output/results/<日期>/<运行编号>/_run.json
```

不同项目的多条扫描命令可以同时运行；同一项目会互斥。每次运行的输出目录独立。项目失败不会删除其他项目结果；AI 汇总失败时 `_summary.md` 是项目报告索引。旧版 JSON、看板和历史趋势不会自动迁移，原文件也不会被删除。

Runner 在扫描专用工作副本中执行 Git 同步及清理，不要在 `<data>/work/` 存放需要保留的人工修改。模型报告应由负责人结合范围和证据复核；完成状态表示 Harness 已返回，不表示 Runner 已验证结论。
