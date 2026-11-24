# DeepSeek Harness 新镜像：配置与迁移说明

本文面向原来使用 Pi 扫描镜像的部署，说明如何切换到 DeepSeek Harness，并接入自部署的 OpenAI 兼容千问服务。以下命令以 Linux、Docker Compose 和 `/opt/ai-code-audit` 为例；路径、仓库和模型地址以现场实际配置为准。

新版本包含 Harness `0.2.3-alpha.2`，由 [GitHub Actions](https://github.com/xiaoguan521/ai-code-audit/actions/workflows/build-image.yml) 构建两种架构的镜像，并通过模拟 OpenAI 兼容接口检查 Harness 的多轮工具调用。模拟测试不代表已经连接现场模型；内网服务和真实项目仍需按本文验证。

旧的 `sha-79826f2` 镜像不包含本文的 OpenAI 兼容模型接入；请使用包含本次改造的两个新镜像。

## 1. 两个镜像都要升级

| 镜像 | 用途 | 镜像地址 |
| --- | --- | --- |
| 扫描镜像 | 运行 DeepSeek Harness、加载八性 Skill、自主规划并执行审查 | `ghcr.io/xiaoguan521/ai-code-audit:main` |
| 调度镜像 | 拉取 Git、启动扫描容器、校验结果、评分和生成报告 | `ghcr.io/xiaoguan521/ai-code-audit-runner:main` |

两个镜像均提供 `linux/amd64` 和 `linux/arm64`。`main` 标签会随构建更新，生产环境可改用本次构建对应的 `sha-<提交短号>` 固定标签。内部仓库使用其他名称时，须同步两个新镜像，不能只更换标签文字。

新扫描流程是：Runner 为项目启动一次 Harness 顶层任务，Harness 自己规划审查和工具调用；Runner 接收结果后进行解析、证据校验、评分和归档。`--json` 是 Harness 的事件流格式，审查正文仍是 Markdown。

## 2. 备份原配置

在原部署目录执行；不存在的文件会跳过。备份可能包含凭据，须限制访问，不要提交到 Git。

```bash
cd /opt/ai-code-audit
umask 077
for f in compose.yaml .env model.env projects.csv git-credentials \
         config/projects.local.csv secrets/git-credentials; do
  [ ! -f "$f" ] || cp -p "$f" "$f.bak.$(date +%Y%m%d%H%M%S)"
done
```

保留原有数据目录、项目清单和 Git 凭据；不要删除历史结果或扫描状态。扫描专用的 `work/` 内部会由程序更新，不要把人工修改保存在那里。

## 3. 准备新镜像

### 服务器可以访问 GHCR

```bash
docker pull ghcr.io/xiaoguan521/ai-code-audit:main
docker pull ghcr.io/xiaoguan521/ai-code-audit-runner:main
```

### 服务器不能访问 GHCR

可以先在**与内网服务器架构相同**、能访问 GHCR 的机器执行上面的两条 `docker pull`，然后打包：

```bash
docker save -o ai-code-audit-main.tar \
  ghcr.io/xiaoguan521/ai-code-audit:main \
  ghcr.io/xiaoguan521/ai-code-audit-runner:main
```

按公司的文件传输流程把 tar 包送到内网服务器，再执行：

```bash
docker load -i ai-code-audit-main.tar
```

也可以由运维先同步到内网 Harbor，再将下面 `.env` 中的镜像地址改为 Harbor 的实际地址和标签。**是否已同步到 Harbor，需要以内网仓库实际镜像为准；本文不假定同步已经完成。**

## 4. 配置部署目录

已有部署优先修改原文件，不要用示例覆盖项目清单或凭据。两种部署布局的区别如下：

| 配置 | 使用本仓库的 `compose.yaml` | 按原内网手册独立部署 |
| --- | --- | --- |
| 项目清单 | `config/projects.local.csv` | `projects.csv` |
| Git HTTPS 凭据 | `secrets/git-credentials` | `git-credentials` |
| Git SSH 配置 | `secrets/ssh/` | `ssh/` |
| 模型配置 | `model.env` | `model.env` |

### 4.1 修改 `.env`

在部署目录的 `.env` 中调整以下项目。`AUDIT_DATA_DIR` 改成原来保存工作副本和报告的**宿主机绝对路径**；如果使用 Harbor，也要替换两个镜像地址。

```dotenv
AUDIT_DATA_DIR=/opt/ai-code-audit/data
AUDIT_DOCKER_SOCKET=/var/run/docker.sock
AUDIT_MODEL_NETWORK=ai-audit-model-net

AUDIT_AGENT_TYPE=deepseek-harness
AUDIT_SCANNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit:main
AUDIT_RUNNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit-runner:main

AUDIT_HARNESS_LAUNCHER=docker
AUDIT_HARNESS_BIN=dsh
AUDIT_HARNESS_ARGS=["headless","--json","--permission-mode","read-only","--approval-policy","never"]
AUDIT_MAX_PARALLEL=2
AUDIT_MAX_MODEL_CONCURRENCY=2
AUDIT_SUMMARY_ENABLED=false
```

`AUDIT_HARNESS_ARGS` 必须是 JSON 字符串数组。Compose 启动时会使用该变量覆盖 Runner 设置中的 `agent.args`；只修改另一个位置的 `settings.json`，不一定会生效。Harness 在 Compose 模式下使用 `single` 策略，不由 Runner 按八个维度拆分任务。

### 4.2 确认 `compose.yaml`

使用本仓库部署时，使用与新 Runner 配套的 [compose.yaml](../compose.yaml)，并保留原本的本地配置文件。独立部署时，原内网手册使用的是另一套文件路径。以下是与上面 `.env` 配套的独立部署示例；如果现场路径不同，按原路径修改卷挂载：

```yaml
services:
  audit:
    image: ${AUDIT_RUNNER_IMAGE}
    init: true
    environment:
      AUDIT_DATA_DIR: ${AUDIT_DATA_DIR}
      AUDIT_MODEL_NETWORK: ${AUDIT_MODEL_NETWORK}
      AUDIT_AGENT_TYPE: ${AUDIT_AGENT_TYPE:-deepseek-harness}
      AUDIT_SCANNER_IMAGE: ${AUDIT_SCANNER_IMAGE}
      AUDIT_MODEL_ENV_PATH: /run/secrets/model.env
      AUDIT_HARNESS_LAUNCHER: ${AUDIT_HARNESS_LAUNCHER:-docker}
      AUDIT_HARNESS_BIN: ${AUDIT_HARNESS_BIN:-dsh}
      AUDIT_HARNESS_ARGS: '${AUDIT_HARNESS_ARGS:-["headless","--json","--permission-mode","read-only","--approval-policy","never"]}'
      AUDIT_MAX_PARALLEL: ${AUDIT_MAX_PARALLEL:-2}
      AUDIT_MAX_MODEL_CONCURRENCY: ${AUDIT_MAX_MODEL_CONCURRENCY:-2}
      AUDIT_SUMMARY_ENABLED: ${AUDIT_SUMMARY_ENABLED:-false}
      TZ: Asia/Shanghai
    volumes:
      - "${AUDIT_DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock"
      - "${AUDIT_DATA_DIR}:${AUDIT_DATA_DIR}"
      - "./projects.csv:/opt/ai-code-audit/config/projects.csv:ro"
      - "./model.env:/run/secrets/model.env:ro"
      - "./git-credentials:/run/secrets/git-credentials:ro"
      - "./ssh:/root/.ssh:ro"
    networks:
      - model

networks:
  model:
    name: ${AUDIT_MODEL_NETWORK}
```

项目清单中每个 `project_id` 必须唯一，`status` 为 `在研` 的项目才会被扫描；原有清单和只读 Git 凭据可以沿用。网络名称必须与实际扫描容器使用的网络一致；Compose 创建的网络不会自动限制出站范围，仍需按内网要求配置访问策略。

### 4.3 修改 `model.env`

使用自部署的千问 OpenAI 兼容接口时，填写：

```dotenv
OPENAI_BASE_URL=http://模型服务地址:8000/v1
OPENAI_MODEL=服务实际提供的模型ID
OPENAI_API_KEY=替换为实际密钥
OPENAI_CONTEXT_WINDOW=131072
OPENAI_MAX_OUTPUT_TOKENS=8192
```

`OPENAI_BASE_URL` 写到 `/v1`，不要加 `/chat/completions`；模型 ID 必须与服务实际暴露的名称一致。无需鉴权时填 `OPENAI_API_KEY=EMPTY`。令牌数应按现场模型的上下文和输出上限调整，不能假定示例值适用。地址必须能从扫描容器访问，`127.0.0.1` 在容器内指容器自身。`model.env` 是 Docker 环境文件，值不要额外加引号，并限制权限：

```bash
chmod 600 model.env
```

扫描镜像会自动加载内置模型配置，让 DeepSeek Harness 使用上述地址和模型；无需手工创建或挂载 patch。模型服务还必须支持 Chat Completions 的流式输出和工具调用。以 vLLM 为例，需为实际模型启用对应的工具调用解析器；参数取决于 vLLM 和模型版本，参见 [千问工具调用说明](https://qwen.readthedocs.io/en/stable/framework/function_call.html)。

如改用 DeepSeek 官方 Messages API，注释 `OPENAI_*` 配置并填写 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_API_KEY`。官方默认模型为 `deepseek-flash`，`DEEPSEEK_MODEL` 不能保证切换模型。

## 5. 验证镜像和配置

先在服务器上按 `.env` 中的实际值设置这两个**当前 shell 变量**。Docker Compose 读取 `.env` 并不意味着它们会自动成为当前 shell 变量。

```bash
SCANNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit:main
RUNNER_IMAGE=ghcr.io/xiaoguan521/ai-code-audit-runner:main
MODEL_NETWORK=ai-audit-model-net
```

如果使用 Harbor，在这里同样改成实际的两个镜像地址；网络名与 `.env` 保持一致。先核对镜像记录的源码提交，两条输出应相同，并与本次构建的提交一致：

```bash
docker image inspect "$SCANNER_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
docker image inspect "$RUNNER_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

如果内部镜像同步过程移除了该标签，应由镜像管理员核对来源，不能仅凭相同标签名判断镜像内容。

### 5.1 验证 Harness 本身能启动

以下检查不连接模型：

```bash
docker run --rm --entrypoint dsh "$SCANNER_IMAGE" --version
docker run --rm --read-only --network none --tmpfs /tmp:rw,size=128m \
  --entrypoint dsh "$SCANNER_IMAGE" --profile headless --help
docker run --rm --entrypoint sh "$SCANNER_IMAGE" -c \
  'test -f /opt/audit/skills/eight-quality-code-audit/SKILL.md && echo "八性 Skill 已安装"'
```

版本应为 `0.2.3-alpha.2`，Headless 帮助应正常显示。新镜像已经处理 `/tmp` 不可执行时的原生库加载问题；若仍出现 `No usable native binding`，先核对服务器上是否导入了本次的新扫描镜像，而不是只核对标签文字。

### 5.2 验证 Compose 和 Git

```bash
cd /opt/ai-code-audit
docker compose config --quiet
docker compose run --rm --pull never audit --dry-run
```

`--dry-run` 不调用模型，但会连接 Git，并克隆或更新扫描专用副本。确认项目数、仓库地址和分支正确。Compose 会按配置创建网络；如已有同名网络但不归当前 Compose 项目管理，需要按实际环境处理。

### 同时扫描多个项目

一个批次内可以并行扫描多个项目，默认最多同时处理 2 个项目、运行 2 个模型会话：

```bash
docker compose run --rm --pull never -T audit --only 项目A 项目B --force-full
```

在 `.env` 设置 `AUDIT_MAX_PARALLEL` 控制项目并发，`AUDIT_MAX_MODEL_CONCURRENCY` 控制实际模型会话并发；应按千问服务的吞吐能力设置。独立部署的 `compose.yaml` 也必须把这两个变量传进 `audit`，如上面的示例。两个独立批次不能同时使用相同的数据目录；出现“另一扫描任务正在使用”时，可用 `docker ps` 检查当前任务，等待其结束后重试。锁由进程持有，不要删除锁文件。

### 5.3 验证真实模型连接

以下命令会发起一次简短模型调用；先确保上一步已创建网络，且 `model.env`、镜像名和网络名均已配置。输出是 Harness 事件流，不是正式的 64 项审查报告：

```bash
docker run --rm --init --network "$MODEL_NETWORK" --read-only \
  --tmpfs /tmp:rw,size=512m --cap-drop ALL --security-opt no-new-privileges \
  --env-file model.env --entrypoint dsh "$SCANNER_IMAGE" \
  headless --json --permission-mode read-only --approval-policy never \
  '请仅回复 OK。'
```

检查命令是否正常退出、事件流是否包含 `final` 且 `reason.kind` 为 `completed`。这只能说明短请求和模型连接可用，不能代替项目扫描和证据验证。排障时分享日志前先检查是否含内部地址、源码或其他敏感信息。

接着检查模型是否能驱动 Harness 工具。以下命令只创建临时示例文件，并向模型发起一次短任务：

```bash
TEST_DIR=$(mktemp -d)
printf '%s\n' 'print("HARNESS_SMOKE_READ_OK")' > "$TEST_DIR/a.py"
chmod -R a+rX "$TEST_DIR"
docker run --rm --init --network "$MODEL_NETWORK" --read-only \
  --tmpfs /tmp:rw,size=512m --cap-drop ALL --security-opt no-new-privileges \
  --env-file model.env -v "$TEST_DIR:/workspace/repo:ro" -w /workspace/repo \
  --entrypoint dsh "$SCANNER_IMAGE" \
  headless --json --permission-mode read-only --approval-policy never \
  '请调用 read 工具读取 a.py，并根据文件内容给出简短回答。'
```

检查事件流中有 `tool_call`（`tool` 为 `read`）、对应的成功 `tool_result` 和 `final.reason.kind=completed`。若只有文本回答、没有工具调用，请检查模型服务是否开启了该模型对应的工具调用解析；完成后可删除上面的临时目录。如果已下载仓库，也可用 `AUDIT_MODEL_NETWORK=网络名 sh docker/smoke_test.sh 镜像 model.env` 自动检查这些事件。

### 5.4 试扫一个项目

把 `order-main` 换成实际 `project_id`：

```bash
docker compose run --rm --pull never audit --only order-main --force-full
```

首轮使用 `--force-full`，避免直接复用旧结果。检查当次批次状态、64 项和校验结果；以下命令使用调度镜像内的 Python，无需服务器额外安装 Python。日期应与实际扫描日期一致：

```bash
SCAN_DATE=$(TZ=Asia/Shanghai date +%F)
PROJECT_ID=order-main
docker compose run --rm --pull never --no-deps -T --entrypoint python3 audit \
  - "$SCAN_DATE" "$PROJECT_ID" <<'PY'
import json
import os
import sys
from pathlib import Path

date, project = sys.argv[1:]
root = Path(os.environ["AUDIT_DATA_DIR"]) / "output" / "results" / date
run = json.loads((root / "_run.json").read_text(encoding="utf-8"))
record = next(row for row in run["projects"] if row["project_id"] == project)
print("本次批次状态:", record.get("status"), record.get("audit_status"))
assert record.get("status") == "ok", record.get("error")
data = json.loads((root / f"{project}.json").read_text(encoding="utf-8"))
meta = data["meta"]
print("Agent:", meta.get("agent"), "执行:", meta.get("agent_status"),
      "审查:", meta.get("audit_status"), "检查项:", len(data["items"]))
assert meta.get("agent") == "deepseek-harness"
assert meta.get("agent_status") == "completed"
assert meta.get("audit_status") == "verified"
assert len(data["items"]) == 64
PY
```

若失败，查看 `AUDIT_DATA_DIR/output/results/<日期>/_run.json` 和 `<项目ID>.error.txt`；如果有候选结果，也可查看 `output/unverified/<日期>/`。同一天可能留有之前成功的结果文件，因此应先核对本次 `_run.json`，不要只看旧 JSON 是否存在。

正常产物在 `AUDIT_DATA_DIR/output/` 下，包括 `artifacts/<日期>/<项目ID>/audit.md`、`audit.json`、`metrics.json`、`run-events.jsonl`，以及 `reports/` 和 `dashboard/`。Summary 默认关闭；需要时可设 `AUDIT_SUMMARY_ENABLED=true`，总结失败不会改变已经验证的审查评分。

## 6. 模型配置如何生效

调度器把 `model.env` 交给新启动的扫描容器；扫描镜像根据 `OPENAI_BASE_URL` 和 `OPENAI_MODEL` 自动加载镜像内的 Harness 模型配置，密钥仍从环境变量读取。无需额外挂载截图中的 patch 文件，也不需要修改 `AUDIT_HARNESS_ARGS`。不要沿用截图中仅在调度容器挂载 patch 的做法：扫描容器是另行启动的。

模型路由使用官方 Harness 内置的 OpenAI 兼容提供器；完整 Agent Loop 和文件工具仍由 DeepSeek Harness 运行。使用自定义 Harness 参数额外传入其他 patch 时，后加载的配置可能覆盖模型选择，应自行检查最终配置。

## 7. 回滚

本版调度器只支持 DeepSeek Harness。若需恢复旧系统，必须同时恢复旧版调度镜像、扫描镜像和配置；不能在新调度镜像中切换旧 Agent。历史数据不需要为升级而删除。
