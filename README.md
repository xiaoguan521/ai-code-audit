# 八性 AI 代码检查 · 部署包

支持两种模型后端（config/settings.json 的 agent.type）：**qwen-docker**（推荐，内网千问 + Qwen Code，在一次性 Docker 容器中运行）和 **claude**（Claude Code 直连）。基于智能体无界面模式，对全公司在研项目每天做一次只读静态审查，按“八性”64 项检查项出逐项结论，由程序按固定规则出分，生成单项目报告、每日汇总、评分表和静态看板。

## 仓库维护（GitHub）

建议建为**私有仓库**。分支规则：`main` 受保护，改动走 Pull Request，CI 通过后合并。

| 想做的事 | 怎么做 | 自动发生什么 |
|---|---|---|
| 修改检查规则 | 改 `skill/eight-quality-audit/SKILL.md`；增删检查项后运行 `python3 tools/gen_checklist.py` | CI 校验 64 项与 checklist / schema 一致；合并后重新构建镜像 |
| 调整权重、评分档位 | 改 `config/checklist.json` | CI 校验权重合计 100 |
| 升级 Qwen Code | 改 `docker/QWEN_CODE_VERSION` | 构建镜像并冒烟测试（版本号、Skill、配置、非 root、入口校验） |
| 发布给内网 | 打 tag：`git tag v1.0.0 && git push origin v1.0.0` | 镜像推送到 `ghcr.io/<owner>/<repo>:1.0.0`，镜像为多架构（amd64 + arm64）；GitHub Release 附带两个离线镜像包 `ai-code-audit_v1.0.0_amd64.tar.gz`、`ai-code-audit_v1.0.0_arm64.tar.gz` 及各自 sha256 |

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
docker/                              Qwen Code 扫描镜像：Dockerfile、入口脚本、系统级配置、构建与冒烟测试脚本
runner/run_daily.py                  每日扫描入口
runner/scoring.py                    校验与评分（确定性计算）
runner/render.py                     报告、CSV、看板渲染
runner/dashboard_template.html       看板模板（单文件，无外部依赖）
tests/make_samples.py                用模拟数据验证评分与渲染：python3 tests/make_samples.py /tmp/out
tests/check_consistency.py           SKILL / checklist / schema / 配置一致性检查（CI 必跑）
tests/test_runner.py                 单元测试：输出 Schema 校验、JSON 提取、重试、网络、git 同步（python3 -m unittest discover -s tests）
tools/gen_checklist.py               从 SKILL.md 重新生成 checklist.json
.github/workflows/                   CI 与镜像构建流水线
docker/QWEN_CODE_VERSION             镜像内 Qwen Code 版本（单一来源）
```

## 运行机制

| 情况 | 扫描类型 | 说明 |
|---|---|---|
| 首次扫描、每周全量日（默认周六）、上次结果丢失 | full | 64 项全部重新判定 |
| 有新提交或补充材料变化 | incremental | 只重判受变更影响的检查项，其余沿用上次结论并标注“沿用” |
| 无新提交、材料没变、距上次全量不满 7 天 | reuse | 直接复用上次结果，不调用模型 |

评分不由模型给出：模型只输出每项 PASS/WARN/FAIL/N/A/NEED_INFO 和证据，`scoring.py` 按 SKILL.md 第 6 节固定规则计算，同样的结论永远得同样的分。

## 纯内网部署：Qwen Code + Docker（推荐）

```
运行机（宿主机）                                   一次性容器 ai-code-audit（每个项目一个）
run_daily.py ──git fetch（只读凭据，只在宿主机）     Qwen Code + 八性 Skill + 只读权限配置
     │  写入 work/<项目>/context/prompt.md            │  /workspace/repo     ← 代码，只读挂载
     └─ docker run ─────────────────────────────────▶ │  /workspace/context  ← 提示词/预扫描/上次结果，只读挂载
                                                      └─ 仅能访问内网千问 OPENAI_BASE_URL，输出 JSON 到 stdout
     ◀── 解析 JSON → 校验（不合格重试一次）→ 程序评分 → 报告 / 看板
```

安全要点：容器里没有 Git 凭据；代码以 `:ro` 挂载，即使模型尝试写文件也会失败；容器以非 root 运行，`--read-only`、`--cap-drop ALL`、`no-new-privileges`、限制 CPU/内存/进程数；Qwen Code 的遥测、使用统计、自动更新在系统级配置中关闭，写文件、联网抓取等工具被禁用。

1. **构建镜像（在能上网的构建机）**：`QWEN_CODE_VERSION=<稳定版本号> sh docker/build.sh 1.0`，得到本机架构的 `ai-code-audit_1.0_<amd64|arm64>.tar.gz` 及 sha256。给 ARM 服务器（鲲鹏、飞腾等）构建加 `PLATFORM=linux/arm64`（x86 构建机需先装 QEMU：`docker run --privileged --rm tonistiigi/binfmt --install arm64`），反之加 `PLATFORM=linux/amd64`。如需从内网镜像仓库拉基础镜像，加 `BASE_IMAGE=harbor.xxx/library/node:22-bookworm-slim`（该镜像须包含目标架构）。完全离线构建：先 `npm pack @qwen-code/qwen-code@<版本>`，把 tgz 放进 `docker/offline/`。
2. **导入内网**：`sha256sum -c ai-code-audit_1.0_amd64.tar.gz.sha256 && docker load < ai-code-audit_1.0_amd64.tar.gz`（ARM 运行机换成 `_arm64`）。
3. **配置模型地址**：`cp docker/model.env.example /etc/ai-audit/model.env && chmod 600 /etc/ai-audit/model.env`，填内网千问的 `OPENAI_BASE_URL`（OpenAI 兼容接口，一般以 /v1 结尾）、`OPENAI_MODEL`、`OPENAI_API_KEY`。
4. **网络隔离（必填）**：建一个专用 Docker 网络（如 `docker network create --subnet 172.30.0.0/24 ai-audit-net`），在宿主机防火墙上只放行该网段到千问服务的地址和端口，填到 settings.json 的 `agent.network`。被审查的代码是不可信输入，`agent.network` 留空时 runner 拒绝启动（`--dry-run` 只告警）；确需使用 Docker 默认网桥（可访问任意外网），须显式填写 `"bridge"`。
5. **冒烟测试**：`sh docker/smoke_test.sh ai-code-audit:1.0 /etc/ai-audit/model.env`，确认能连上千问、列出文件，并且创建 x.txt 失败。
6. **试扫一个项目**：`python3 runner/run_daily.py --only <项目ID> --force-full`，人工核对报告证据；再按下面第 8 步配置定时任务。

千问模型说明：Qwen Code 没有 Claude Code 的 `--json-schema` 强约束，镜像入口会把 Schema 附在系统提示里要求只输出 JSON；runner 负责解析（容忍代码块包裹和前后文字）、按 `config/result.schema.json` 逐字段校验结构（字段缺失、枚举值写错、类型不对都会带着问题清单重试，结构不合格的输出绝不进入评分）、校验 64 项是否齐全、证据与问题清单是否一致，不合格带着问题清单重试一次。仓库很大时如出现退出码 53（轮次超限），调大 `agent.max_session_turns` 或在 projects.csv 中用 scope_out 排除无关目录。

## 部署步骤（Claude Code 后端，兴华 + 晓晨）

1. **运行机**：内网 Linux 服务器或容器，Python ≥ 3.10、git、Node.js 18+；安装 Claude Code（`npm i -g @anthropic-ai/claude-code` 或官方安装脚本），固定版本，升级前先用测试项目回归。
2. **模型凭据**：设置 `ANTHROPIC_API_KEY`（Claude Console 创建，单独一个工作区便于计费与限额），或按公司要求改用 Amazon Bedrock / Google Vertex / 公司模型网关（Claude Code 支持对应环境变量）。凭据放在运行账号的环境文件中，权限 600。
3. **代码访问**：为扫描账号在 GitLab/GitHub 开只读权限（Reporter / read_repository 级别的 deploy token 或 deploy key），预先配置在运行机的 git 凭据中；脚本只做 clone/fetch，不推送。
4. **网络**：运行机出站只放行 Git 服务器与模型 API 域名。
5. **可选预扫描工具**：cloc、gitleaks、lizard 装上即自动使用；semgrep 需在 settings.json 配置离线规则目录 `semgrep_rules_dir`，否则跳过（避免联网拉规则）。
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
