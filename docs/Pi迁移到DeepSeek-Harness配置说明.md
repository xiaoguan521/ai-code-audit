# Pi 扫描方式迁移到 DeepSeek Harness

本文用于把原来的 **Pi + JSON + Runner 分维度调度** 配置，迁移到现在的 **DeepSeek Harness Headless + Markdown + 程序校验** 配置。

最新的镜像标签、两种部署目录、模型选择限制和验收命令见[新镜像配置与迁移说明](DeepSeek-Harness新镜像配置与迁移说明.md)。

适用场景：本仓库的默认扫描镜像已经改为官方 DeepSeek Harness，内网使用 Docker Compose 批量扫描。

## 0. 先区分两个镜像

本项目实际有两个不同用途的镜像，不能只升级其中一个：

| 镜像 | 用途 | 当前处理方式 |
|---|---|---|
| Runner 镜像 | 拉取 Git、调度任务、启动扫描容器、解析 Markdown、校验证据、评分和生成报表 | 必须使用包含 Harness Adapter 的新版本 |
| Scanner 扫描镜像 | 真正运行 `dsh headless`，执行代码审查 | 必须是包含 `dsh`、八性 Skill 和 Profile 的 Harness 镜像 |

本仓库的 `docker/Dockerfile` 现在直接安装官方 `@deepseek-ai/dsh`，并把八性 Skill 放到 Harness 的 `DSH_BUNDLED_SKILL_DIR`。`docker/Dockerfile.pi` 仅作为回滚镜像保留。推送到 GitHub 后由 `.github/workflows/build-image.yml` 构建 amd64/arm64 镜像；内网需要同步扫描和调度两个新镜像到 Harbor。

在内网导入或同步后，先用下面的命令确认扫描镜像确实是 DeepSeek Harness：

```bash
HARNESS_IMAGE=<同步到内网的 Harness 扫描镜像>
docker run --rm --entrypoint dsh "$HARNESS_IMAGE" --version
```

命令能正常返回版本，并且镜像内已安装八性 Skill 和 headless Profile，才可以进入本文的配置迁移步骤。`AUDIT_RUNNER_IMAGE` 是调度镜像，`AUDIT_SCANNER_IMAGE` 是 Harness 扫描镜像，两者不要混填。

## 一、迁移后的运行方式

| 项目 | 原配置 | 新配置 |
|---|---|---|
| 扫描 Agent | Pi | DeepSeek Harness Headless |
| 任务拆分 | Runner 按八个维度拆分 | Harness 自己规划和拆分 |
| Runner 对 Agent 的干预 | 维度会话、维度重试 | 只传入一个顶层任务，采集最终事件 |
| Agent 最终结果 | JSON | Markdown + `audit-*` 机器字段块 |
| 评分 | Runner 校验后计算 | Runner 校验后计算，规则不变 |
| 原始审查文件 | 主要是 JSON | `audit.md` |
| 总结 | 无独立总结 | 可选独立 Summary Agent |

新模式下，`deepseek-harness` 必须使用 `strategy=single`。不要把 Harness 配置成 `dimensions`，否则 Runner 会拒绝启动。

## 二、先备份旧配置

在部署目录执行：

```bash
cd /opt/ai-code-audit
cp .env ".env.pi.$(date +%Y%m%d%H%M%S).bak"
cp model.env "model.env.pi.$(date +%Y%m%d%H%M%S).bak"
cp projects.csv "projects.csv.$(date +%Y%m%d%H%M%S).bak"
```

不要删除 `data/`。其中包含项目副本、历史结果、增量状态和评分趋势。

## 三、修改 `.env`

原来的关键配置通常类似：

```dotenv
AUDIT_AGENT_TYPE=pi-docker
AUDIT_SCANNER_IMAGE=<旧 Pi 扫描镜像>
AUDIT_RUNNER_IMAGE=<旧或当前 Runner 镜像>
AUDIT_MODEL_NETWORK=ai-audit-model-net
```

改成以下形式。镜像地址和版本替换为内网实际地址，建议 Runner 与 Harness 镜像使用同一批次发布版本：

```dotenv
AUDIT_DATA_DIR=/opt/ai-code-audit/data
AUDIT_DOCKER_SOCKET=/var/run/docker.sock
AUDIT_MODEL_NETWORK=ai-audit-model-net

AUDIT_AGENT_TYPE=deepseek-harness
AUDIT_RUNNER_IMAGE=<已升级的 Runner 镜像>
AUDIT_SCANNER_IMAGE=<已升级的 DeepSeek Harness 镜像>

AUDIT_HARNESS_LAUNCHER=docker
AUDIT_HARNESS_BIN=dsh
AUDIT_HARNESS_ARGS=["headless","--json","--permission-mode","read-only","--approval-policy","never"]

# 第一轮可以先关闭，确认审查链路稳定后再开启。
AUDIT_SUMMARY_ENABLED=false
```

注意：`AUDIT_HARNESS_ARGS` 必须是 JSON 字符串数组，不能写成下面这种 shell 字符串：

```dotenv
# 错误示例
AUDIT_HARNESS_ARGS=dsh headless --json
```

如果启用 Summary Agent：

```dotenv
AUDIT_SUMMARY_ENABLED=true
```

Harness 模式下，Summary Agent 默认复用扫描镜像、网络和模型环境，但会以独立进程和独立会话运行，只读取 `audit.json` 和 `audit.md`，不会读取源码。

## 四、`model.env` 是否需要修改

接入自部署 OpenAI 兼容模型时，原 Pi 环境文件中的地址、模型名和密钥可以沿用，确认模型 ID 与服务实际暴露的 ID 一致，并按服务限制设置令牌数：

```dotenv
OPENAI_BASE_URL=http://模型服务地址:8000/v1
OPENAI_MODEL=服务实际提供的模型ID
OPENAI_API_KEY=<实际密钥或EMPTY>
OPENAI_CONTEXT_WINDOW=131072
OPENAI_MAX_OUTPUT_TOKENS=8192
```

扫描镜像会自动启用 OpenAI 兼容模型路由；模型服务必须支持流式输出和工具调用。若改用 DeepSeek 官方 Messages API，则注释 `OPENAI_*`，改填 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_API_KEY`。当前官方默认模型为 `deepseek-flash`，仅设置 `DEEPSEEK_MODEL` 不能保证切换模型。不要把密钥写入 `.env`、`projects.csv` 或 Git URL；`model.env` 保持 `600` 权限。

```bash
chmod 600 model.env
```

## 五、确认 Compose 文件已升级

新版 `compose.yaml` 必须把以下变量传给调度容器：

```text
AUDIT_AGENT_TYPE
AUDIT_SCANNER_IMAGE
AUDIT_HARNESS_LAUNCHER
AUDIT_HARNESS_BIN
AUDIT_HARNESS_ARGS
AUDIT_SUMMARY_ENABLED
```

如果部署目录中的 `compose.yaml` 是旧文件，即使 `.env` 已改成 Harness，调度器仍可能强制使用 Pi。应使用升级后的 Compose 文件，不要只替换镜像标签。

检查最终展开配置：

```bash
docker compose config --quiet
docker compose config | grep -E 'AUDIT_AGENT_TYPE|AUDIT_SCANNER_IMAGE|AUDIT_HARNESS_ARGS|AUDIT_SUMMARY_ENABLED'
```

输出中应能看到：

```text
AUDIT_AGENT_TYPE: deepseek-harness
AUDIT_HARNESS_ARGS: '["headless", "--json", "--permission-mode", "read-only", "--approval-policy", "never"]'
```

## 六、迁移后的验收顺序

### 1. 拉取并检查镜像

```bash
docker compose pull scanner-image audit
docker image inspect <Harness 镜像>
```

确认 Harness 镜像至少包含：

- `dsh` 命令；
- 八性 Skill；
- Headless 使用的 Profile；
- 能访问模型服务的网络配置。

### 2. 先做 dry-run

```bash
docker compose run --rm --pull never audit --dry-run
```

dry-run 会检查项目和 Git 配置，但不会调用模型。确认项目数正确、Git 分支可访问、网络配置没有被改成默认网桥。

### 3. 只扫描一个项目

```bash
docker compose run --rm --pull never audit \
  --only <项目ID> --force-full
```

第一轮建议使用 `--force-full`，避免旧 Pi 结果被误认为新 Harness 结果。正常情况下，模型审查过程由 Harness 自己规划，Runner 不会按维度启动多个 Harness 会话。

### 4. 检查运行状态

```bash
RUN_DATE=$(date +%F)
PROJECT_ID=<项目ID>

python3 - "$AUDIT_DATA_DIR/output/results/$RUN_DATE/$PROJECT_ID.json" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
meta = data["meta"]
assert len(data["items"]) == 64, len(data["items"])
assert meta.get("agent") == "deepseek-harness", meta.get("agent")
print({
    "agent_status": meta.get("agent_status"),
    "audit_status": meta.get("audit_status"),
    "summary_status": meta.get("summary", {}).get("status"),
    "items": len(data["items"]),
})
PY
```

有效结果应满足：

- `agent_status=completed`：Harness 进程正常结束；
- `audit_status=verified`：Markdown、64 项、证据和语义校验通过；
- `summary_status=disabled`、`verified` 或 `fallback`：三者都不影响审查评分；
- `items` 数量为 64。

如果 Harness 进程退出码为 0，但缺少 `final.reason.kind`、最终 Markdown 不完整或证据不成立，`audit_status` 必须是 `unverified`，不能把该结果当作有效评分。

### 5. 检查归档文件

```text
data/output/artifacts/<日期>/<项目ID>/
├── audit.md              # Harness 原始 Markdown
├── audit.json            # 程序解析、证据校验和评分结果
├── metrics.json          # 耗时、调用和校验指标
├── run-events.jsonl      # 脱敏后的 Harness 运行事件
├── summary.md            # 独立总结或程序降级摘要
└── summary.meta.json     # 总结状态和主题引用
```

`summary.md` 即使在 Summary Agent 关闭或失败时也会生成；`fallback` 不代表第一阶段审查失败。

## 七、与旧结果和增量扫描的关系

1. 不要手工删除旧的 `data/output/results` 或 `data/state`。
2. Agent 类型、镜像、参数、Skill、解析器和评分口径都会进入审查指纹；切换到 Harness 后，必要时会自动触发全量扫描。
3. 第一轮 Harness 结果通过校验后，后续仍可按原有规则进行 `full`、`incremental` 和 `reuse`。
4. `reuse` 只会复用通过当前源码证据核验的旧结果；不会因为旧结果存在就跳过证据验证。
5. 不要把旧 Pi 的 JSON 输出直接放入 `audit.md`。Harness 必须返回带 `audit-*` 机器字段块的完整 Markdown。

## 八、回滚到 Pi

如果 Harness 镜像、Profile 或模型接口暂时不可用，可以只回滚 `.env` 中的 Agent 配置：

```dotenv
AUDIT_AGENT_TYPE=pi-docker
AUDIT_SCANNER_IMAGE=<旧 Pi 扫描镜像>
AUDIT_SUMMARY_ENABLED=false
```

Runner 仍支持 Pi 的兼容链路。回滚后先验证：

```bash
docker compose config --quiet
docker compose run --rm --pull never audit \
  --only <项目ID> --force-full
```

不要通过删除历史目录来“解决”切换问题；先查看 `data/output/results/<日期>/<项目ID>.error.txt` 和 `_run.json` 中的错误信息。

## 九、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `dsh` 启动失败 | Scanner 镜像不是 Harness 镜像，或命令名不同 | 检查 `AUDIT_SCANNER_IMAGE`、`AUDIT_HARNESS_BIN` 和镜像内容 |
| 提示 `strategy=single` | Harness 不允许 Runner 分维度调度 | 保持 `AUDIT_AGENT_TYPE=deepseek-harness`，不要配置 `dimensions` |
| 退出码为 0 但 `unverified` | 缺少最终事件、`final.reason.kind` 或 64 项 Markdown | 查看 `audit.md`、`run-events.jsonl` 和 `metrics.json` |
| `summary_status=fallback` | Summary Agent 不可用或引用了不存在的问题 ID | 不影响审查评分；修正 Summary 镜像/凭证后再开启 |
| 网络连接失败 | Docker 网络为空、网络名错误或防火墙未放行模型服务 | 明确设置 `AUDIT_MODEL_NETWORK`，确认该网络只允许访问模型服务 |
| 旧结果没有 `audit.md` | 旧 Pi 结果本来是 JSON | 新 Harness 扫描成功后会生成；不要手工伪造原始 Markdown |

## 十、迁移完成标准

满足以下条件后再切换全部项目：

- 一个项目的 Harness 全量扫描成功；
- `audit_status=verified` 且 64 项齐全；
- `audit.md`、`audit.json`、`metrics.json` 和 `run-events.jsonl` 均已生成；
- 报告、CSV 和看板能正常渲染；
- 明确记录 `agent_status`、`audit_status` 和 `summary_status`；
- 已保留 Pi 配置备份，并完成一次回滚演练；
- 确认 Harness 容器没有源码写权限、没有 Git 凭据，也没有不受限制的外网访问权限。
