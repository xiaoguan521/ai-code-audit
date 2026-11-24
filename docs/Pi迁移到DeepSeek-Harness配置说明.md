# 从旧版扫描器迁移到 DeepSeek Harness

请使用最新的 [DeepSeek Harness 新镜像配置与旧版迁移说明](DeepSeek-Harness新镜像配置与迁移说明.md)。

当前版本只使用 DeepSeek Harness：每个项目一个自主审查会话，输出包含评分的 Markdown；Runner 调度和汇总项目，不解析、校验证据或重算项目分数。迁移时需同时更新扫描镜像、Runner 镜像和配套 Compose 配置。旧版 JSON、看板及评分结果不会自动转换，原历史文件保留。
