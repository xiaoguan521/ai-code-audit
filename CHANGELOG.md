# 变更记录

## 未发布
- Skill 重写为无人值守版：合并评分规则、报告格式（含 YAML front matter）、64 项检查清单、增量规则和自检修复规则到 SKILL.md 一个文件；Harness 自主完成审查、自检修复和评分
- 项目报告改为 Harness 直接输出含 YAML front matter 的 Markdown，Runner 不再解析和评分审查内容
- Runner 从 front matter 提取评分元数据写入运行日志；汇总会话独立生成多项目整体报告，汇总失败不影响已完成的项目报告
- 简化 Harness 提示：只传递项目元数据和扫描参数（scan_type full/incremental），不再拼装 MODE/OUTPUT_FORMAT 标志和 JSON 元数据
- 新增 `runner/parsers/frontmatter.py`：纯 stdlib 解析 Markdown YAML front matter，提取评分和统计字段
- 移除 Skill 对 `references/` 子目录的依赖（`audit-method.md`、`report-template.md` 已合并进 SKILL.md）

## v1.0.0 · 2026-11-21
- 八性检查 Skill：8 个维度 64 个检查项，交互式审查与每日批量扫描共用
- 每日扫描：全量 / 增量 / 沿用自动判断，程序化评分，单项目报告、每日汇总、评分 CSV、静态看板
- 模型后端：qwen-docker（内网千问 + Qwen Code 一次性容器）与 claude（Claude Code）
- GitHub Actions：CI 一致性检查；镜像构建推送 GHCR，打 tag 时生成离线镜像包
