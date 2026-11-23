# 合成质量回归样例

这里包含 4 个人工设计的小仓库和 5 个标注检查点：SQL 拼接与参数化查询、仓库内提示注入，以及缺少 S1/S2 材料。它用于发现这些具体场景的回归，**不能代表 64 项的整体准确率**。需要继续增加有人工复核、固定提交的真实项目样本。

`prepare.py` 会在一个**不存在的新目录**创建独立 Git 仓库、项目清单、提交清单和设置文件；它不会复制模型密钥。生成的设置继承传入的后端与模型环境文件路径，运行前须确保后端可用、网络已配置。示例：

```sh
python3 quality/prepare.py --dest /tmp/ai-audit-quality --settings config/settings.json
python3 runner/run_daily.py --settings /tmp/ai-audit-quality/settings.json \
  --projects /tmp/ai-audit-quality/projects.csv --date 2026-01-01 --force-full
python3 quality/evaluate.py --manifest /tmp/ai-audit-quality/manifest.json \
  --results /tmp/ai-audit-quality/output/results/2026-01-01
```

评估器首先校验结果结构、自洽性、证据路径和行号，并核对扫描提交与样例摘要；然后只比较标注的检查项和证据位置。任一结果缺失、输出无效或未匹配标注时返回非零退出码。模型服务不可用时，可运行普通单元测试验证评估器，但不能将模拟结果当成模型准确率。

扩充样例时，固定源文件和预期结果，在 `cases.json` 写明证据位置，并通过人工复核；样例内容或标注改变后应更新数据集版本并重新运行 `prepare.py`。
