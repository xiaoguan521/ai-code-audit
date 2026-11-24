# 八性 AI 代码检查 · 部署包

支持三种模型后端（config/settings.json 的 agent.type）：**pi-docker**（兼容旧链路，Pi + 内网 OpenAI 兼容模型，在一次性 Docker 容器中运行）、**deepseek-harness**（完整 DeepSeek Harness Headless Agent，在一次性 Docker 容器中运行）和 **claude**（Claude Code 直连）。基于智能体无界面模式，对全公司在研项目每天做一次只读静态审查，按“八性”64 项检查项出逐项结论，由程序按固定规则出分，生成单项目报告、每日汇总、评分表和静态看板。

## 仓库维护（GitHub）

建议建为**私有仓库**。分支规则：`main` 受保护，改动走 Pull Request，CI 通过后合并。

| 想做的事 | 怎么做 | 自动发生什么 |
|---|---|---|
| 修改检查规则 | 改 `skill/eight-quality-audit/SKILL.md`；增删检查项后运行 `python3 tools/gen_checklist.py` | CI 校验 64 项与 checklist / schema 一致；合并后重新构建镜像 |
| 调整权重、评分档位 | 改 `config/checklist.json` | CI 校验权重合计 100 |
| 升级 DeepSeek Harness | 改 `docker/DSH_VERSION` | 构建 Harness 扫描镜像并冒烟测试（dsh、headless Profile、Skill、非 root） |
| 回滚 Pi | 使用 `docker/Dockerfile.pi` 和 `docker/PI_VERSION` 构建旧镜像，并将 `AUDIT_AGENT_TYPE` 改为 `pi-docker` | 保留旧 JSON 扫描链路 |
| 发布给内网 | 打 tag：`git tag v1.0.0 && git push origin v1.0.0` | 扫描镜像 `ghcr.io/<owner>/<repo>:1.0.0` 和调度镜像 `ghcr.io/<owner>/<repo>-runner:1.0.0` 均为 amd64 + arm64；GitHub Release 为两种镜像的每个架构分别附离线包及 sha256 |

工作流文件：`.github/workflows/ci.yml`（每次 PR / push 跑测试）、`.github/workflows/build-image.yml`（构建、测试、推送镜像，tag 时出离线包）。Dependabot 每月提醒升级 Actions 与基础镜像。

内网取镜像的三种方式（按安全规范选）：
1. 从 Release 下载与运行机架构对应的离线包（`uname -m` 为 x86_64 选 `_amd64`，aarch64 选 `_arm64`），校验 sha256 后 `docker load`（纯内网推荐）；
2. 由能出网的同步机把 `ghcr.io/<owner>/<repo>:<版本>` 同步到公司 Harbor（用 `skopeo copy --all` 或 `crane copy` 保留两个架构），运行机从 Harbor 拉；
3. 运行机经代理直接 `docker pull`（需要 GHCR 的读权限 token）。

运行机部署时，把仓库 clone（或拷贝）到运行机，`runner/` 与 `config/` 在宿主机运行，镜像只负责模型审查；`config/projects.csv` 中的真实 Git 地址属于内部资产，可以只保存在运行机上，仓库里保留示例行。

## 目录

```
skill/eight-quality-audit/SKILL.md   八性检查 Skill（规则本体，交互式审查和定时扫描共用）
config/checklist.json                64 项检查项与权重、评分参数（程序读取）
config/result.schema.json            模型输出的 JSON Schema（约束结构，保证可自动出分）
config/projects.csv                  项目 Git 地址清单（张玲维护，示例行上线前删除）
config/settings.json                 运行参数（目录、并发、预算、全量扫描日、模型后端 agent 等）
docker/                              DeepSeek Harness 扫描镜像：Dockerfile、版本、构建与冒烟测试脚本；Dockerfile.pi 为 Pi 回滚镜像
runner/run_daily.py                  每日扫描入口
runner/adapters/harness.py           完整 DeepSeek Harness Headless Docker Adapter
runner/adapters/summary.py           只读审查结果的独立 AI 总结 Adapter（含确定性降级）
runner/parsers/audit_markdown.py     版本化 Markdown 机器字段解析与覆盖校验
runner/evidence.py                   核验引用的仓库路径、行号及可选片段
runner/code_map.py                   生成有界的 Git 跟踪文件地图
runner/scoring.py                    校验与评分（确定性计算）
runner/render.py                     报告、CSV、看板渲染
runner/dashboard_template.html       看板模板（单文件，无外部依赖）
tests/make_samples.py                用模拟数据验证评分与渲染：python3 tests/make_samples.py /tmp/out
tests/check_consistency.py           SKILL / checklist / schema / 配置一致性检查（CI 必跑）
tests/test_runner.py                 单元测试：输出 Schema 校验、JSON 提取、重试、网络、git 同步（python3 -m unittest discover -s tests）
quality/                             固定提交的合成质量回归样例与评估脚本
tools/gen_checklist.py               从 SKILL.md 重新生成 checklist.json
.github/workflows/                   CI 与镜像构建流水线
docker/DSH_VERSION                   镜像内 DeepSeek Harness 版本（单一来源）
```

## 运行机制

| 情况 | 扫描类型 | 说明 |
|---|---|---|
| 首次扫描、到全量日或超过全量间隔、上次结果丢失、规则或范围变化 | full | 64 项全部重新判定 |
| 有新提交或补充材料变化 | incremental | runner 先按变更路径、旧证据和材料变化计算受影响检查项；只启动受影响维度并只让模型输出这些项，其余检查项由 runner 原样沿用；影响面超过 60% 自动升级 full |
| 文件内容、材料和规则没变，且未到全量日 | reuse | 核验证据后复用上次结果，不调用模型；仅改变提交而无文件差异时也适用 |

评分不由模型给出：模型只输出每项 PASS/WARN/FAIL/N/A/NEED_INFO 和证据，`scoring.py` 按 SKILL.md 第 6 节固定规则计算，同样的结论永远得同样的分。

证据使用结构化相对路径、起止行号及可选原文片段。runner 在评分前核验文件、行号和所提供的片段，并在 `meta.evidence_check` 和每次尝试的统计中记录引用数及无效数；该核验不能证明结论正确，也不能证明某类问题不存在。结构不合格时最多重试一次，第二次仍失败则不评分；无法安全自动修复的残余语义问题才会写入 `output/unverified/<日期>/<项目ID>.json`（文件权限 600）。Pi 达到轮次或输出长度硬上限时不会以相同配置原样重跑。

Harness 默认读取八性 Skill，并按 Markdown 契约输出审查事实；`config/result.schema.json` 仍保留给 Pi 回滚链路，不参与 Harness 的第一阶段输出格式约束。

结构错误（Schema、缺项、重复项）才会触发模型重试；无效证据、孤立问题和增量沿用抄写错误由 runner 自动清理或降级为 `NEED_INFO-证据不足`，并在 `meta.evidence_check` 记录修复数量。runner 同时生成 `context/facts.json`：inventory、代码地图和预扫描摘要由确定性工具提供，模型只需对需要语义判断的部分回源码核验。

升级注意：证据输出格式与旧版不同，**必须同时更新调度程序和 Harness 扫描镜像**。旧报告仍可查看；规则、证据格式、范围或模型配置指纹变化时，下一次扫描自动全量。若模型服务的同名模型替换了权重，请更新设置中的 `audit_revision` 触发全量复审。

默认后端是 `deepseek-harness`，`agent.strategy` 固定为 `single`：Runner 只发送一个顶层任务，DeepSeek Harness 自己规划审查步骤、工具调用、多轮推理和子任务；Runner 只负责进程、事件、Markdown 解析、证据校验、评分与归档。`agent.dimension_parallelism` 只对 Pi/其他兼容后端的旧分维度链路生效。Harness 输出不完整或证据不成立时整次审查标记为 `unverified`，不进入有效评分。

调度默认仍在周六全量；设置 `full_scan_strategy` 为 `staggered` 可按项目稳定错峰，`full_scan_interval_days` 控制最大间隔，错过计划会补扫。旧配置中的 `reuse_if_unchanged_days` 在没有新间隔配置时作为兼容默认值。每个项目完成后 `_run.json` 即更新，并记录同步、预扫描、模型调用、核验等耗时及可用的轮次、工具和 token 统计；`--dry-run` 的日志单独保存。同一工作目录或输出目录只允许一个批次运行。

项目清单中的 `project_id` 必须唯一，只能使用字母、数字、点、横线和下划线，不能以 `_` 开头或使用 `.`、`..`，以免与结果文件冲突或越出工作目录。

代码地图仅根据 `git ls-files` 记录有界路径清单，不读取源码，候选路径不能代替审查；锁文件保留名称供依赖审查。预扫描状态区分成功、跳过、失败和无报告；默认并行运行 2 个工具（可按机器负载调小或调大），增量模式只扫描未排除的变更文件，确定性事实也只读取变更文件，inventory 的总行数沿用上次全量基线，实际并发还会叠加项目并发。发布的 Compose 调度镜像固定预装 cloc 2.02、gitleaks 8.30.1、semgrep 1.186.0 和 lizard 1.26.1，构建冒烟测试会检查四个命令；semgrep 仍需要 `semgrep_rules_dir` 指向随部署提供的离线规则目录，否则会明确跳过（不会联网拉规则）。如自建调度镜像，请保留同样的版本和命令检查。固定合成样例的用法见 [`quality/README.md`](quality/README.md)。

## 纯内网部署：DeepSeek Harness + Docker（默认）

```
运行机（宿主机）                                   一次性容器 ai-code-audit（每个项目一个）
run_daily.py ──git fetch（只读凭据，只在宿主机）     DeepSeek Harness + 八性 Skill + 只读工具
     │  写入 work/<项目>/context/prompt.md            │  /workspace/repo     ← 代码，只读挂载
     └─ docker run ─────────────────────────────────▶ │  /workspace/context  ← 提示词/预扫描/上次结果，只读挂载
                                                      └─ 仅能访问模型服务，输出 JSON 事件流中的 Markdown 到 stdout
     ◀── 解析 Markdown → 结构、证据与沿用校验 → 程序评分 → 报告 / 看板
```

安全要点：容器里没有 Git 凭据；代码以 `:ro` 挂载；容器以非 root 运行，`--read-only`、`--cap-drop ALL`、`no-new-privileges` 并限制资源。Harness 使用 `read-only` 权限预设，Skill 从镜像内置目录发现；Runner 不介入 Harness 的内部规划。其他文件仍可能含敏感信息，模型服务的网络范围仍需通过防火墙限制。

1. **构建镜像（推荐由 GitHub Actions 完成）**：推送到 `main` 或版本 tag 后，工作流会构建并发布内置官方 `@deepseek-ai/dsh` 的 amd64/arm64 Harness 扫描镜像和 Runner 镜像。若必须手工构建，在能上网的构建机执行 `sh docker/build.sh 1.0`；覆盖 Harness 版本可设置 `DSH_VERSION`。给 ARM 服务器构建加 `PLATFORM=linux/arm64`，反之加 `PLATFORM=linux/amd64`。如需从内网镜像仓库拉基础镜像，加 `BASE_IMAGE=harbor.xxx/library/node:24.22.3-bookworm-slim`（Node.js 须满足 Harness 版本要求，镜像须包含目标架构）。纯内网优先导入 GitHub Release 的离线镜像包。
2. **导入内网**：`sha256sum -c ai-code-audit_1.0_amd64.tar.gz.sha256 && docker load < ai-code-audit_1.0_amd64.tar.gz`（ARM 运行机换成 `_arm64`）。
3. **配置模型地址**：`cp docker/model.env.example /etc/ai-audit/model.env && chmod 600 /etc/ai-audit/model.env`，填写自部署模型的 `OPENAI_BASE_URL`、`OPENAI_MODEL` 和 `OPENAI_API_KEY`。
4. **网络隔离（必填）**：建一个专用 Docker 网络（如 `docker network create --subnet 172.30.0.0/24 ai-audit-net`），在宿主机防火墙上只放行该网段到千问服务的地址和端口，填到 settings.json 的 `agent.network`。被审查的代码是不可信输入，`agent.network` 留空时 runner 拒绝启动（`--dry-run` 只告警）；确需使用 Docker 默认网桥（可访问任意外网），须显式填写 `"bridge"`。
5. **冒烟测试**：按[新镜像配置与迁移说明](docs/DeepSeek-Harness新镜像配置与迁移说明.md)中的模型连接验证命令，确认 DeepSeek Harness headless 能连接模型并返回 `final.reason.kind=completed` 的事件流。宿主机运行时将命令中的 `model.env` 换成 `/etc/ai-audit/model.env`。
6. **试扫一个项目**：`python3 runner/run_daily.py --only <项目ID> --force-full`，人工核对报告证据；再按下面第 8 步配置定时任务。

模型说明：自部署千问等模型可通过 OpenAI Chat Completions 兼容接口接入，填写 `OPENAI_BASE_URL`、`OPENAI_MODEL` 和 `OPENAI_API_KEY`，服务还须支持流式输出和工具调用。Harness 自动启用内置的模型路由配置。未配置 OpenAI 模型时，可使用 DeepSeek Messages 接口和 `DEEPSEEK_API_KEY`；官方默认模型是 `deepseek-flash`，`DEEPSEEK_MODEL` 不能保证切换模型。详见 [新镜像配置与迁移说明](docs/DeepSeek-Harness新镜像配置与迁移说明.md)。

## DeepSeek Harness Headless + Docker

deepseek-harness 后端只启动一个完整的 Headless Agent 任务。Runner 不按八性维度拆分任务，也不介入 Harness 的规划、工具调用、多轮推理或子 Agent；它只负责启动容器、传入顶层任务、检查进程退出码与最终事件、保存脱敏事件，并解析最终 Markdown。该后端必须使用 strategy: single。

本仓库的 `docker/Dockerfile` 已直接安装官方 `@deepseek-ai/dsh`，预装八性 Skill，并通过 `DSH_BUNDLED_SKILL_DIR` 接入 headless Profile；源码与扫描上下文以只读方式挂载。GitHub Actions 构建完成后，默认 `ghcr.io/<owner>/<repo>:main` 就是 Harness 扫描镜像。示例配置：

~~~json
{
  "agent": {
    "type": "deepseek-harness",
    "strategy": "single",
    "launcher": "docker",
    "image": "deepseek-harness:固定版本",
    "network": "ai-audit-net",
    "bin": "dsh",
    "args": ["headless", "--json", "--permission-mode", "read-only", "--approval-policy", "never"],
    "env_file": "/etc/ai-audit/deepseek.env"
  }
}
~~~

Harness 的 `--json` 是运行事件流，不是要求模型输出 JSON。Adapter 默认把顶层任务作为 argv 传给 `dsh`，并强制使用只读权限和 `--approval-policy never`；如某个固定版本只从 stdin 读取，可配置 `prompt_mode: "stdin"`。Adapter 只从最终事件提取 Markdown；退出码为 0 但缺少 `final.reason.kind`、最终状态失败或 Markdown 不完整时，审查仍标记为 unverified，不进入评分。若需要调整事件成功状态，可配置 `success_reason_kinds`，但不要修改 Harness 的内部任务流程。

从旧 Pi 配置迁移到 Harness 的逐步操作见[新镜像配置与迁移说明](docs/DeepSeek-Harness新镜像配置与迁移说明.md)。

### 独立 AI 综合总结

`summary.enabled` 默认关闭。Compose 部署可用 `AUDIT_SUMMARY_ENABLED=true` 开启；Harness 模式默认复用扫描镜像、网络和模型环境，但会启动独立的 Summary Agent 进程/会话。该会话只读 `audit.json` 与 `audit.md`，不挂载源码，也不能修改逐项结论、证据或评分。它只负责管理层摘要、重复问题聚合和整改优先级，并通过 `summary-theme` 的 `source_issue_ids` 引用已存在的问题。未配置凭证、启动失败或输出引用无效时，自动生成确定性摘要，审查结果仍保持有效。

## Docker Compose 批量扫描

内网部署、离线镜像导入与运维排障详见[内网部署运维手册](docs/内网部署运维手册.md)。

Compose 默认指向本仓库发布的 DeepSeek Harness 扫描镜像，`AUDIT_AGENT_TYPE=deepseek-harness`。如需回滚，才切换到 `docker/Dockerfile.pi` 构建的 Pi 镜像并设置 `AUDIT_AGENT_TYPE=pi-docker`。`audit` 使用调度镜像，负责拉取仓库、管理扫描状态和生成报告；调度器通过本机 Docker 为每个项目启动一个扫描镜像的独立容器。扫描完成后调度容器退出；再次运行会使用保存的状态，按原有规则选择全量、增量或复用。

1. 在有 Docker Engine 和 Compose 的运行机上，从项目目录准备配置：
   ```sh
   cp .env.example .env
   cp config/projects.compose.example.csv config/projects.local.csv
   cp docker/model.env.example model.env
   mkdir -p secrets/ssh audit-data
   cp docker/git-credentials.example secrets/git-credentials
   chmod 700 secrets/ssh
   chmod 600 model.env secrets/git-credentials
   ```
2. 编辑 `config/projects.local.csv`：每行填写一个 `git_url` 和 `branch`。示例包含两个仓库，其中一个扫描 `main` 和 `develop` 两个分支；每一行的 `project_id` 必须唯一，`status` 为 `在研` 才会被扫描。不要把令牌或密码写进仓库 URL，因为 URL 会进入扫描结果。更换项目的仓库地址时请使用新的 `project_id`，以免复用旧仓库的本地副本。
3. 编辑 `model.env`，填入能从扫描容器访问到的自部署模型地址、模型名和密钥：
   ```text
   OPENAI_BASE_URL=http://模型服务地址:8000/v1
   OPENAI_MODEL=服务实际提供的模型ID
   OPENAI_API_KEY=替换为实际密钥
   OPENAI_CONTEXT_WINDOW=131072
   OPENAI_MAX_OUTPUT_TOKENS=8192
   ```
   地址只填写到 `/v1`；模型名要与服务暴露的 ID 完全一致，两个令牌限制按服务实际能力设置。无需鉴权时可填 `OPENAI_API_KEY=EMPTY`。此文件按 Docker `--env-file` 格式填写，值直接写在等号后，不要额外加引号。容器中的 `127.0.0.1` 指向容器自身；模型部署在其他机器或宿主机时应填写容器能够访问的实际地址。
4. 配置私有 Git 仓库的只读凭据，任选一种方式：
   - **HTTPS 用户名与密码 / 访问令牌**：编辑 `secrets/git-credentials`，每个仓库一行，如 `https://用户名:令牌@git.example.com/trade/order-service.git`。地址路径须与项目清单里的 HTTPS 仓库地址一致；同一仓库的不同分支共用一行。用户名或令牌中的 `@`、`:`、`/`、`%` 等特殊字符须先做 URL 编码。Git 服务要求访问令牌时填写只读令牌。
   - **SSH deploy key**：将私钥及经过验证的 `known_hosts` 放到 `secrets/ssh/`，私钥权限设为 `600`，并将对应项目的 `git_url` 改为 SSH 地址。多个 Git 服务器可在该目录放 `config`，为各主机指定不同的 `IdentityFile`。

   Git 凭据只挂载给调度容器；不使用 HTTPS 凭据时可以把 `secrets/git-credentials` 清空并保留文件。该文件和 `model.env` 都是明文，应保留 `600` 权限并使用只读凭据。`.env`、`model.env`、本地项目清单及 `secrets/` 都被 Git 忽略，也不会发给 Docker 构建器。
5. 检查 `.env`。`AUDIT_DATA_DIR` 必须是运行机上的绝对路径，调度容器和宿主机使用同一路径，以便 Docker 挂载项目副本；默认是当前项目目录下的 `audit-data/`，更换路径时先创建该目录。`AUDIT_AGENT_TYPE=deepseek-harness` 时，`AUDIT_SCANNER_IMAGE` 必须是预装 dsh、Skill 和 Profile 的 Harness 镜像；`AUDIT_HARNESS_ARGS` 必须保持为 `["headless","--json","--permission-mode","read-only","--approval-policy","never"]` 这样的 JSON 数组。若暂时回滚 Pi，设置 `AUDIT_AGENT_TYPE=pi-docker` 并使用 Pi 扫描镜像。`AUDIT_RUNNER_IMAGE` 配置调度镜像；Docker socket 不在 `/var/run/docker.sock` 时，修改 `AUDIT_DOCKER_SOCKET`。
6. 拉取两个镜像并试跑：
   ```sh
   docker compose --profile images pull scanner-image audit
   docker compose run --rm audit --dry-run
   docker compose run --rm audit --only order-main --force-full
   docker compose run --rm audit
   ```
   `scanner-image` 服务只用来显式拉取镜像，不单独启动；实际扫描时由 `audit` 启动它。如需修改调度器，可执行 `docker compose build audit` 在本机构建；构建需要 `docker:27-cli` 基础镜像和 Alpine 软件包。`--dry-run` 仍会连接 Git 并克隆或更新扫描专用副本，但不调用模型；全部项目正常后再正式运行。报告和看板位于 `AUDIT_DATA_DIR/output/`，项目副本位于 `AUDIT_DATA_DIR/work/`。

Compose 会创建 `AUDIT_MODEL_NETWORK` 指定的 Docker 网络。该网络本身不会限制出站访问，需要按现有网络隔离要求在宿主机防火墙上限制扫描容器只能访问模型服务。调度容器挂载 Docker socket 以启动扫描容器，拥有宿主机 Docker 的控制权限，应只在可信运行机上使用。扫描容器本身不挂载该 socket，也不接收 Git 凭据。

如需每天运行，可在运行机的 crontab 中进入项目目录后执行 `docker compose run --rm audit`；Compose 本身不会自动定时。

## 部署步骤（Claude Code 后端，兴华 + 晓晨）

1. **运行机**：内网 Linux 服务器或容器，Python ≥ 3.10、git、Node.js 18+；安装 Claude Code（`npm i -g @anthropic-ai/claude-code` 或官方安装脚本），固定版本，升级前先用测试项目回归。
2. **模型凭据**：设置 `ANTHROPIC_API_KEY`（Claude Console 创建，单独一个工作区便于计费与限额），或按公司要求改用 Amazon Bedrock / Google Vertex / 公司模型网关（Claude Code 支持对应环境变量）。凭据放在运行账号的环境文件中，权限 600。
3. **代码访问**：为扫描账号在 GitLab/GitHub 开只读权限（Reporter / read_repository 级别的 deploy token 或 deploy key），预先配置在运行机的 git 凭据中；脚本只做 clone/fetch，不推送。
4. **网络**：运行机出站只放行 Git 服务器与模型 API 域名。
5. **预扫描工具**：发布的调度镜像已预装 cloc、gitleaks、semgrep、lizard；semgrep 需在 settings.json 配置离线规则目录 `semgrep_rules_dir`，否则跳过（避免联网拉规则）。如需完全关闭预扫描，可将 `prescan_tools` 设为空数组。
6. **配置**：编辑 `config/settings.json` 的 `workdir`、`output_dir`，导入 `config/projects.csv`。
7. **试跑**：`python3 runner/run_daily.py --dry-run` 看计划，再 `python3 runner/run_daily.py --only <一个项目ID> --force-full` 试扫一个项目，人工核对报告证据。
8. **定时**：crontab（运行账号）：
   ```
   CRON_TZ=Asia/Shanghai
   30 1 * * * cd /opt/ai-code-audit && /usr/bin/python3 runner/run_daily.py >> /data/ai-audit/logs/$(date +\%F).log 2>&1
   ```
9. **看板发布**：把 `output_dir/dashboard/` 用内网 Nginx 静态托管（加公司 SSO 或 IP 白名单），`output_dir/reports/` 同目录发布供下载。

## 输出

```
output_dir/results/<日期>/<项目ID>.json   原始结构化结果（含逐项、问题、评分、元数据）
output_dir/results/<日期>/_run.json       批次进度、耗时、每次模型尝试及预扫描状态
output_dir/artifacts/<日期>/<项目ID>/audit.md          Harness 原始 Markdown
output_dir/artifacts/<日期>/<项目ID>/audit.json        程序解析、核验后的结果
output_dir/artifacts/<日期>/<项目ID>/run-events.jsonl  脱敏后的运行事件
output_dir/artifacts/<日期>/<项目ID>/summary.md        独立 AI 总结或确定性降级摘要
output_dir/artifacts/<日期>/<项目ID>/summary.meta.json  总结校验元数据与状态
output_dir/unverified/<日期>/<项目ID>.json 待核实结果（不参与评分）
output_dir/reports/<日期>/<项目ID>.md     单项目完整报告（按模板第六节格式）
output_dir/reports/<日期>/_summary.md     每日汇总报告
output_dir/scores_latest.csv               最新评分（Excel 直接打开）
output_dir/scores_history.csv              近 90 天历史
output_dir/dashboard/index.html            看板（每日覆盖，另存 <日期>.html）
```

## 常用命令

```
python3 runner/run_daily.py --only order-service --force-full   # 某项目立即全量复审
python3 runner/run_daily.py --date 2026-11-20                   # 补跑某天
python3 runner/render.py --date 2026-11-21                      # 只重渲染报告和看板
```
