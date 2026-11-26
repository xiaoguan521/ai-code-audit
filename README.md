# 八性代码质量审查平台

平台基于 **DeepSeek Harness** 与八性检查 Skill 对 Git 仓库执行静态只读审查，支持自部署的 OpenAI 兼容模型。每个项目由独立的 Harness 会话自主规划并执行审查，输出包含 8 个维度、64 项检查、评分与整改建议的中文 Markdown 报告；全部项目完成后由独立汇总会话生成跨项目整体报告，并提供配套 **Web 看板与报告阅读平台**。

## 核心特性

- **双扫描模式**
  - **标准模式（默认）**：输出完整 64 项检查结果、评分、整改建议及文件行号/代码证据。
  - **快速模式（`--fast` / `AUDIT_FAST_MODE=true`）**：适合每日批量巡检，输出 64 项结论、评分与整改建议，跳过详细证据整理；大型仓库可抽查非核心模块，无法判定的项标为 `NEED_INFO`。
- **增量与快照调度**：支持全量 / 增量 / 断点复用（`--resume`），支持 `--snapshot-at` 固定 Git 提交时间点，同项目互斥、跨项目并发。
- **落盘前最小机器校验**：Runner 在落盘前自动校验报告表格结构、重算 8 维度得分与总分、核验 `WARN/FAIL` 证据路径存在性，并拦截高置信密钥泄露；不改写模型结论，校验失败不落盘。
- **一体化 Web 看板（内置于 Runner 镜像）**：支持质量总览（KPI、历史趋势、8 维度均分、项目排行）、历史批次查看、Markdown 报告在线阅读（含风险问题筛选与 XSS 防护）、扫描触发与实时日志，以及可选的令牌访问鉴权（`AUDIT_WEB_TOKEN`）。

## 仓库目录结构

```text
.
├── runner/                    # 调度器、自检工具与 Web API（零外部 Python 依赖）
│   ├── run_daily.py           # 每日批量扫描主入口
│   ├── web_server.py          # Web API 与前端静态资源托管服务
│   ├── doctor.py              # 一键初始化（--init）与环境自检工具
│   ├── report_validate.py     # 落盘前最小机器校验（表格解析、算分、证据核验）
│   ├── scoring.py / evidence.py / code_map.py / incremental.py
│   └── adapters/              # DeepSeek Harness 与跨项目汇总适配器
├── frontend/                  # 独立前端工程（Vite + React + TypeScript）
├── skill/eight-quality-audit/ # 八性 64 项无人值守审查 Skill（SKILL.md）
├── config/                    # 检查清单、配置模板与项目清单示例
├── docker/                    # 扫描镜像、Runner 镜像、Compose 入口与离线导出脚本
├── docs/                      # 迁移说明与内网部署运维手册
├── tests/                     # 单元测试与配置一致性校验
└── compose.yaml               # Docker Compose 编排（audit 扫描 + web 看板）
```

> **旧流程残留文件（不参与当前运行链路）**
> - `runner/render.py`、`runner/dashboard_template.html`、`runner/parsers/audit_markdown.py`、`quality/`：仅保留用于历史单测与离线实验。其中 `runner/scoring.py` 与 `runner/evidence.py` 的底层函数仍被 `runner/report_validate.py` 复用。
> - `runner/facts.py` 的 `collect()` 未接入热路径；低成本静态信号由预扫描（`cloc`/`gitleaks`/`semgrep`/`lizard`）与代码地图覆盖。

## 部署与配置

首次部署可一键初始化所需配置模板与占位目录（不覆盖已有文件），并执行环境自检：

```bash
# 生成 .env（含随机 AUDIT_WEB_TOKEN）、model.env、projects.local.csv 及 secrets 占位项
python3 runner/doctor.py --init

# 校验配置、权重、挂载项与工具链（加 --check-model 可探测模型连通性）
python3 runner/doctor.py
```

详细文档与配置模板：

- [新镜像配置与旧版迁移](docs/DeepSeek-Harness新镜像配置与迁移说明.md)：双镜像结构、模型接入、项目清单、并发与预扫描配置。
- [内网部署运维手册](docs/内网部署运维手册.md)：离线镜像包导入与日常运维。
- 配置模板：[项目清单示例](config/projects.compose.example.csv)、[Compose 环境变量示例](.env.example)、[模型配置示例](docker/model.env.example)。

### 镜像说明

平台采用**双镜像架构**（前端静态产物在构建 Runner 镜像时通过多阶段构建打包进镜像，不新增第三个镜像）：

- **扫描镜像**：`ghcr.io/xiaoguan521/ai-code-audit`（DeepSeek Harness + 八性 Skill）
- **调度与 Web 镜像**：`ghcr.io/xiaoguan521/ai-code-audit-runner`（Runner 调度器 + 预扫描工具链 + Web API 与前端产物）

更新版本时请同步更新**两个镜像及配套 `compose.yaml`**；同一提交构建的 `sha-<短提交号>` 标签可用于版本固定。仓库代码改动不会自动影响本地已有镜像。

## 日常使用

在已配置 `.env`、`model.env`、`config/projects.local.csv` 及只读 Git 凭据的部署目录中执行：

### 1. 启动 Web 看板（推荐常驻运行）

在 `.env` 中配置端口与访问令牌（生产/共享内网环境强烈建议设置强随机 `AUDIT_WEB_TOKEN`）：

```env
AUDIT_WEB_PORT=8787
AUDIT_WEB_TOKEN=请替换为强随机访问令牌
```

启动服务：

```bash
# 如本地修改了前端或 Runner 代码，先重新构建镜像
docker compose build web

# 后台常驻启动 Web 看板（复用 Runner 镜像）
docker compose up -d web
```

访问 `http://<宿主机地址>:8787`，输入 `AUDIT_WEB_TOKEN` 登录后可使用：

- **质量看板**：KPI 指标卡片、全公司平均总分/安全分趋势折线、8 维度项目均分、可排序筛选的项目排行表（含较上次分差 `▲/▼`）。
- **批次记录**：按日期与 `run_id` 浏览每次扫描状态、项目耗时/报错，一键打开单项目报告或 `_summary.md`。
- **报告阅读**：侧边目录大纲（TOC）快速跳转、风险问题 `P0–P3` 筛选、直达链接复制分享、下载 `.md` / 导出问题 CSV / 打印另存 PDF。
- **扫描与配置**：实时扫描进度条（同时感知 Web、定时与 CLI 触发的批次，支持点击已完成项目直达报告）、可视化定时扫描配置（持久化保存于 `output/state/schedule.json`）、一键环境自检（Doctor）、支持 `--fast` / `--force-full` / `--resume` / `--snapshot-at` 触发扫描、实时日志与一键中止任务。

> 脚本调用 API 可在请求头携带 `Authorization: Bearer <AUDIT_WEB_TOKEN>`；`AUDIT_WEB_TOKEN` 留空时为免密模式（仅建议本机开发使用）。

### 2. 命令行批量扫描

```bash
# 扫描指定项目（每个项目在独立 Harness 会话中审查）
docker compose run --rm --pull never -T audit --only project-a project-b

# 快速巡检全部在研项目（不生成详细证据）
docker compose run --rm --pull never -T audit --fast

# 忽略上次报告参考，强制全量重审指定项目
docker compose run --rm --pull never -T audit --only project-a --force-full

# 提交/配置/材料未变化时直接复用上次报告，跳过模型调用
docker compose run --rm --pull never -T audit --resume

# 固定 Git 快照时间点批量扫描
docker compose run --rm --pull never -T audit --snapshot-at 2026-11-24T00:00:00+08:00

# 按各项目最近一次成功报告重新生成跨项目整体汇总
docker compose run --rm --pull never -T audit --only project-a project-b --summary-only
```

### 3. 常用运行参数与并发控制

- **默认快速模式**：在 `.env` 中设置 `AUDIT_FAST_MODE=true`；临时切回标准模式可传 `--no-fast`。
- **快照时间**：未传 `--snapshot-at` 且未配置 `AUDIT_SNAPSHOT_AT` 时，默认以本批次启动时间选取目标分支最近提交。
- **并发与互斥**：`AUDIT_MAX_PARALLEL` 控制单批次并发项目数；`AUDIT_MAX_MODEL_CONCURRENCY` 控制共享数据目录下跨 Runner 的模型会话并发上限（多实例请保持一致）；同一项目的并发扫描自动互斥。
- **超时设置**：`AUDIT_PER_PROJECT_TIMEOUT_SEC` 默认 `3600` 秒（仅计单次 Harness 进程耗时，不含预扫描与排队）；开启 `AUDIT_ADAPTIVE_TIMEOUT=true` 后可按大/中/小规模自动匹配超时。

## 输出目录结构

每次运行写入独立目录，不覆盖历史批次：

```text
<AUDIT_DATA_DIR>/output/
├── reports/<日期>/<运行编号>/
│   ├── <项目ID>.md             # Harness 生成的原始单项目报告
│   └── _summary.md             # 本次跨项目整体汇总报告（或失败兜底索引）
├── results/<日期>/<运行编号>/
│   ├── _run.json               # 批次状态、项目耗时、重算评分与报告路径
│   └── <项目ID>.events.jsonl   # 脱敏后的 Harness 事件流
└── state/<项目ID>.json          # 项目最近一次成功报告位置、提交哈希与评分
```

> **结果复核提示**：项目报告由模型生成，机器校验与自检不能替代人工对重大风险（尤其是 `P0`/`P1` 安全与稳定性问题）的复核。

## 本地开发与维护

### 后端与配置校验

```bash
# 运行 Python 单元测试（CI 使用 Python 3.12）
python3 -m unittest discover -s tests -v

# 校验 SKILL.md、checklist.json、result.schema.json 与 Dockerfile 一致性
python3 tests/check_consistency.py

# 如修改了 SKILL.md 中的 64 项检查项，重新生成 config/checklist.json
python3 tools/gen_checklist.py
```

### 前端本地开发与联调

需要 Node.js 22+（CI 与 `Dockerfile.runner` 使用 `node:22-alpine`）：

```bash
# 终端 1：启动后端 API（--data-dir 指定包含 reports/ results/ state/ 的目录）
python3 runner/web_server.py --data-dir ./audit-data/output --port 8787

# 终端 2：启动 Vite 前端热更新（已配置 /api 代理至 127.0.0.1:8787）
cd frontend
npm ci
npm run dev
```

生产构建（构建后 `runner/web_server.py` 会自动托管 `frontend/dist`）：

```bash
cd frontend && npm run build
python3 runner/web_server.py --data-dir ./audit-data/output --port 8787 --token your-token
```

### CI 与发布

镜像多架构构建（`linux/amd64`、`linux/arm64`）、单元测试与无模型冒烟测试由 [GitHub Actions](.github/workflows/build-image.yml) 自动执行；打 `v*` 标签时同步导出内网离线镜像包。真实自部署模型、Git 凭据和现场网络仍需在目标部署环境中实测验证。

## 开源协议

本项目采用 [MIT 许可证](LICENSE)。
