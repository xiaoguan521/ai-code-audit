# 八性代码质量审查平台

平台使用 **DeepSeek Harness** 和八性 Skill 审查 Git 项目，支持自部署的 OpenAI 兼容模型。每个项目由独立 Harness 会话自主规划和执行审查，直接输出包含 64 项、整改建议和分数的 Markdown 报告；标准模式另附详细证据。

每日批量任务可开启快速模式：仍输出 64 项、分数和整改建议，不生成详细证据、文件行号或代码片段；大型仓库可抽查非核心模块，无法判断的项标为 NEED_INFO。标准模式保留详细证据。快速模式减少证据整理和逐条复核，实际用时仍取决于仓库和模型服务。

Runner 负责项目选择、同步扫描副本、可选预扫描、并发调度、报告归档和启动多项目汇总。Runner 在落盘前只做最小机器校验（解析报告表格、重算分数、核验 WARN/FAIL 证据路径存在性），不改写报告、不判定结论、校验失败不重跑。八性审查及交付前的证据、覆盖和评分自检由 Harness 完成；不确定内容应在报告中标明。

### 旧流程残留文件（不参与运行）

- `runner/render.py`、`runner/dashboard_template.html`、`runner/parsers/audit_markdown.py`、`quality/`：只供单元测试与离线实验，不参与运行。注意 `runner/parsers/audit_markdown.py` 的 HTML 注释契约与现行 SKILL 格式不匹配（现行报告是人类可读 Markdown、无 audit-* 块），落盘校验解析的是人类可读表格，不用该解析器。`runner/scoring.py` 的算分函数与 `runner/evidence.py` 的引用核验则是落盘校验的底层构件，由 `runner/report_validate.py` 调用。
- `runner/facts.py` 的 `collect()` 未接进热路径：全仓库文本扫描慢，且其关键词信号只能作 inventory 参考、不能证明检查项达标；便宜信号由预扫描（gitleaks/semgrep/lizard/cloc）与代码地图的路径盘点覆盖。

全部项目结束后，独立的 Harness 会话只读取项目 Markdown，生成跨项目整体报告。汇总失败时保留项目报告，并生成报告索引。Harness 的 `--json` 仅表示运行事件流，最终报告仍是 Markdown。

## 部署与配置

- [新镜像配置与旧版迁移](docs/DeepSeek-Harness新镜像配置与迁移说明.md)：两个镜像、千问模型、项目清单、并发、预扫描和报告路径。
- [内网部署运维手册](docs/内网部署运维手册.md)：离线导入镜像及日常运行。
- [项目清单示例](config/projects.compose.example.csv)、[Compose 环境变量示例](.env.example)、[模型配置示例](docker/model.env.example)。

扫描镜像为 `ghcr.io/xiaoguan521/ai-code-audit`；Runner 镜像为 `ghcr.io/xiaoguan521/ai-code-audit-runner`。更新此版本时需要更新 **两个镜像及配套 Compose 配置**；同一提交构建的 `sha-<短提交号>` 标签可用于固定版本。仓库中的代码改动不会自动改变已有镜像。

## 运行

在已经配置 `.env`、`model.env`、`config/projects.local.csv` 和只读 Git 凭据的部署目录执行：

```bash
# 扫描多个项目；Harness 在独立会话里自行审查
docker compose run --rm --pull never -T audit --only project-a project-b

# 快速巡检全部在研项目，不生成详细证据
docker compose run --rm --pull never -T audit --fast

# 不带上次报告作为参考，重新审查指定项目
docker compose run --rm --pull never -T audit --only project-a --force-full

# 分别扫描过后，按各项目最近一次报告重新生成整体报告
docker compose run --rm --pull never -T audit --only project-a project-b --summary-only
```

要让每天的批量任务默认使用快速模式，可在 `.env` 设置 `AUDIT_FAST_MODE=true`；运行命令时加 `--no-fast` 可临时切回标准模式。默认值为 `false`，也可只在需要时添加 `--fast`。模式只影响新启动的项目扫描。

批量扫描可用 `--snapshot-at 2026-11-24T00:00:00+08:00` 固定 Git 代码时间点；Runner 会串行拉取各项目并选择目标分支该时间点之前最近的提交，全部项目的 Harness 审查仍按并发配置运行。未指定时使用本批次启动时间，也可以在 `.env` 设置 `AUDIT_SNAPSHOT_AT`。

不同项目可通过多条命令同时扫描；同一项目的并发任务会互斥。`AUDIT_MAX_PARALLEL` 控制每个 Runner 批次的项目数；共享数据目录的所有 Runner 使用 `AUDIT_MAX_MODEL_CONCURRENCY` 限制并发模型会话，请保持这些 Runner 的配置一致。模型服务承载能力不足时，应降低并发。

扫描时日志显示预扫描、等待模型名额、Harness 步骤以及每步耗时和可用的 Token 数；步骤数不是完成百分比。`AUDIT_PER_PROJECT_TIMEOUT_SEC` 默认 3600 秒，只计一次 Harness 进程运行，不包含预扫描或排队；可按实际需求调整，修改只对新任务生效。项目级会话并发上限不限制 Harness 自行创建的子代理请求，模型服务的实际并发能力也会影响耗时。

运行结束时日志会打印运行编号和报告路径。每次运行都有独立目录，不会覆盖其他批次：

```text
<data>/output/
├── reports/<日期>/<运行编号>/
│   ├── <项目ID>.md          # Harness 返回的原始单项目报告
│   └── _summary.md          # 本次整体报告或报告索引
├── results/<日期>/<运行编号>/
│   ├── _run.json            # 项目运行状态、耗时和报告路径
│   └── <项目ID>.events.jsonl # 脱敏 Harness 事件
└── state/<项目ID>.json       # 最近一次成功报告的位置及提交
```

项目报告来自模型，自检不能替代人工对重大风险的复核。进程完成表示 Harness 返回了报告，并不代表报告已由 Runner 独立验证。此前 Runner 生成的结构化评分、看板和历史趋势不属于新流程；旧文件不会被自动迁移或删除。`quality/` 中的旧结构化样例评估器仅用于离线实验，不参与运行链路。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 tests/check_consistency.py
```

镜像构建和多架构冒烟测试见 [GitHub Actions](https://github.com/xiaoguan521/ai-code-audit/actions/workflows/build-image.yml)。真实自部署模型、Git 凭据和现场网络仍需在部署环境中测试。

## 开源协议

本项目采用 [MIT 许可证](LICENSE)。
