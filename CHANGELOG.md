# 变更记录

## 未发布
- 扫描引擎切换为 Pi，继续使用现有内网 OpenAI 兼容模型、只读工具与双架构镜像
- 修复 Qwen 0.26.1 把以 `-` 开头的提示词误识别为参数，镜像构建时以模拟模型验证真实入口
- 同一 GitHub Actions 工作流构建并发布扫描与调度镜像，分别验证 amd64 / arm64，并为版本发布生成两种镜像的离线包
- 镜像支持 ARM64：GHCR 推送 amd64 + arm64 多架构镜像，两个架构分别冒烟测试，Release 按架构各附一个离线包；`docker/build.sh` 支持 `PLATFORM`，离线包名带架构
- 模型输出按 `result.schema.json` 逐字段校验，不合格带问题清单重试；结构不合格的输出不再进入评分导致崩溃
- JSON 提取不再按 ``` 切分，证据中含代码块时也能正确解析
- qwen-docker 后端必须配置 `agent.network`，留空拒绝启动
- 同步代码时 `git reset` / `git clean` / `rev-parse` 失败立即报错，避免静默扫描旧提交
- 新增 `tests/test_runner.py` 单元测试并纳入 CI

## v1.0.0 · 2026-11-21
- 八性检查 Skill：8 个维度 64 个检查项，交互式审查与每日批量扫描共用
- 每日扫描：全量 / 增量 / 沿用自动判断，程序化评分，单项目报告、每日汇总、评分 CSV、静态看板
- 模型后端：qwen-docker（内网千问 + Qwen Code 一次性容器）与 claude（Claude Code）
- GitHub Actions：CI 一致性检查；镜像构建推送 GHCR，打 tag 时生成离线镜像包
