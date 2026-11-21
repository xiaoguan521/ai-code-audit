#!/usr/bin/env python3
"""从 SKILL.md 重新生成 config/checklist.json（SKILL.md 是检查项的唯一来源）。

修改检查项后执行：python3 tools/gen_checklist.py
维度权重、评分参数保留 checklist.json 里的现值。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
skill = (ROOT / "skill/eight-quality-audit/SKILL.md").read_text(encoding="utf-8")
ck_path = ROOT / "config/checklist.json"
ck = json.loads(ck_path.read_text(encoding="utf-8"))
items = [{"id": a, "dimension": a.split(".")[0], "name": b.strip()}
         for a, b in re.findall(r"^\| (\d+\.\d+) \| ([^|]+)\|", skill, re.M)]
ck["items"] = items
ck_path.write_text(json.dumps(ck, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已写入 {len(items)} 个检查项")
