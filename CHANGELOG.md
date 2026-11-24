# 变更记录

## 未发布
- 修复批次超时：超时后孤儿线程不再落盘、不写 state（落盘前与写 state 前各设作废检查点），工作线程改为 daemon 且进程退出不再被孤儿线程拖住，超时后汇总只等模型名额 1 秒
- 报告校验补 WARN/FAIL 与风险问题清单的关联：WARN/FAIL 项必须在清单里有对应条目，清单条目必须对应 WARN/FAIL 项
- 报告校验的总分/安全分声明改为先在评分总览小节内找、否则取全文最后一次出现，执行摘要里回顾上次分数不再导致误拦
- 汇总元数据只对 status 为 ok 的项目印分数，被拦截报告的分数不再出现在汇总里
- Git diff 失败走全量时不再把上次报告当上下文传给模型
- Skill 重写为无人值守版：合并评分规则、报告格式（含 YAML front matter）、64 项检查清单、增量规则和自检修复规则到 SKILL.md 一个文件；Harness 自主完成审查、自检修复和评分
- 项目报告改为 Harness 直接输出含 YAML front matter 的 Markdown，Runner 不再解析和评分审查内容
- 汇总会话独立生成多项目整体报告，汇总失败不影响已完成的项目报告
- 简化 Harness 提示：只传递项目元数据和扫描参数（scan_type full/incremental），不再拼装 MODE/OUTPUT_FORMAT 标志和 JSON 元数据
- 移除 Skill 对 `references/` 子目录的依赖（`audit-method.md`、`report-template.md` 已合并进 SKILL.md）

## v1.0.0 · 2026-11-21
- 八性检查 Skill：8 个维度 64 个检查项，交互式审查与每日批量扫描共用
- 每日扫描：全量 / 增量 / 沿用自动判断，程序化评分，单项目报告、每日汇总、评分 CSV、静态看板
- 模型后端：qwen-docker（内网千问 + Qwen Code 一次性容器）与 claude（Claude Code）
- GitHub Actions：CI 一致性检查；镜像构建推送 GHCR，打 tag 时生成离线镜像包
