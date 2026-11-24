#!/usr/bin/env python3
"""一致性检查（CI 必跑）：SKILL.md、checklist.json、result.schema.json、各配置文件互相对得上。"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors = []


def load(p):
    try:
        return json.loads((ROOT / p).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"{p} 不是合法 JSON：{e}")
        return {}


skill = (ROOT / "skill/eight-quality-audit/SKILL.md").read_text(encoding="utf-8")
skill_items = re.findall(r"^\| (\d+\.\d+) \| ([^|]+)\|", skill, re.M)
ck = load("config/checklist.json")
schema = load("config/result.schema.json")
load("config/settings.json")

if len(skill_items) != 64:
    errors.append(f"SKILL.md 检查项应为 64 个，实际 {len(skill_items)}")
ck_items = [(i["id"], i["name"]) for i in ck.get("items", [])]
if [(a, b.strip()) for a, b in skill_items] != ck_items:
    errors.append("checklist.json 与 SKILL.md 检查项不一致，请运行 python3 tools/gen_checklist.py")
weights = sum(d["weight"] for d in ck.get("dimensions", []))
if abs(weights - 100) > 1e-6:
    errors.append(f"维度权重合计应为 100，实际 {weights}")
pattern = schema.get("properties", {}).get("items", {}).get("items", {}).get("properties", {}).get("id", {}).get("pattern", "")
bad = [a for a, _ in skill_items if not re.match(pattern, a)]
if bad:
    errors.append(f"result.schema.json 的 id 规则不接受：{bad}")
if not re.search(r"^name: [a-z0-9-]+$", skill, re.M):
    errors.append("SKILL.md frontmatter 缺少合法 name")
# JSON string syntax is valid YAML and keeps quotes in the description unambiguous.
description = re.search(r"^description: (.+)$", skill, re.M)
try:
    if not description or not isinstance(json.loads(description.group(1)), str):
        raise ValueError("missing description")
except (json.JSONDecodeError, ValueError):
    errors.append("SKILL.md description 必须是合法的 JSON/YAML 字符串")

dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
for option, src in re.findall(r"^COPY\s+(?:(--\S+)\s+)?(\S+)", dockerfile, re.M):
    if option.startswith("--from="):
        continue  # 多阶段镜像的源来自构建阶段，不是本地文件。
    if not (ROOT / src).exists():
        errors.append(f"Dockerfile COPY 的源不存在：{src}")

if errors:
    print("一致性检查失败：")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("一致性检查通过：64 个检查项，配置文件均有效")
