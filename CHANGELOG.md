# 变更记录

## v1.0.0 · 2026-11-21
- 八性检查 Skill：8 个维度 64 个检查项，交互式审查与每日批量扫描共用
- 每日扫描：全量 / 增量 / 沿用自动判断，程序化评分，单项目报告、每日汇总、评分 CSV、静态看板
- 模型后端：qwen-docker（内网千问 + Qwen Code 一次性容器）与 claude（Claude Code）
- GitHub Actions：CI 一致性检查；镜像构建推送 GHCR，打 tag 时生成离线镜像包
