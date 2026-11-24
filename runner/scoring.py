"""八性评分：按 SKILL.md 第 6 节规则，从逐项结果确定性地计算维度分、覆盖率、总分与安全评分。

评分完全由代码计算，不由模型给出，保证同样的逐项结论永远得到同样的分数。
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
NEED_INFO = ("NEED_INFO-运行数据", "NEED_INFO-补充材料", "NEED_INFO-证据不足")
# 总分门槛：至少这么多个维度可评分，否则项目评分写“暂不评分”。
MIN_SCORED_DIMENSIONS = 5


def load_checklist() -> dict:
    return json.loads((CONFIG_DIR / "checklist.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads((CONFIG_DIR / "result.schema.json").read_text(encoding="utf-8"))


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def schema_errors(value, schema: dict | None = None, path: str = "$") -> list[str]:
    """按 result.schema.json 校验结构（支持其中用到的 type / required / properties / items /
    enum / pattern / minimum / minLength / maxLength / minItems / maxItems）。审查结果在评分前必须先过这一关，
    否则字段缺失或枚举值写错会在评分时直接崩溃。返回问题列表（空表示通过）。"""
    if schema is None:
        schema = load_schema()
    errs: list[str] = []
    t = schema.get("type")
    if t and not _TYPES[t](value):
        return [f"{path} 类型应为 {t}，实际为 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path} 取值 {json.dumps(value, ensure_ascii=False)} 不在允许范围 {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{path} 取值 {value!r} 不符合格式 {schema['pattern']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool) and value < schema["minimum"]:
        errs.append(f"{path} 不能小于 {schema['minimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path} 长度不能小于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path} 长度不能大于 {schema['maxLength']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path} 缺少必填字段 {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs += schema_errors(value[k], sub, f"{path}.{k}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path} 至少需要 {schema['minItems']} 项，实际 {len(value)} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path} 最多 {schema['maxItems']} 项，实际 {len(value)} 项")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += schema_errors(v, schema["items"], f"{path}[{i}]")
    return errs


def parse_weights(raw: str | None, checklist: dict, project_id: str = "") -> dict[str, float]:
    """projects.csv 的 weights 列，格式 "1:15;2:12;..."，留空用默认权重。

    严格校验：未知维度、非数字、负数、总和 <= 0、缺冒号的片段一律 raise
    ValueError；空片段跳过。错误信息写明项目与出错片段。
    """
    def fail(msg: str) -> ValueError:
        prefix = f"项目 {project_id} " if project_id else ""
        return ValueError(f"{prefix}weights 无效：{msg}")

    weights = {d["id"]: float(d["weight"]) for d in checklist["dimensions"]}
    if not raw:
        return weights
    for part in str(raw).replace("，", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise fail(f"片段缺少冒号：{part!r}，应为“维度:权重”")
        key, value = part.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in weights:
            raise fail(f"未知维度：{key!r}（片段 {part!r}）")
        try:
            number = float(value)
        except ValueError:
            raise fail(f"维度 {key} 的权重不是数字：{value!r}（片段 {part!r}）") from None
        if number < 0:
            raise fail(f"维度 {key} 的权重为负数：{value}（片段 {part!r}）")
        weights[key] = number
    if sum(weights.values()) <= 0:
        raise fail(f"权重总和必须大于 0，实际为 {sum(weights.values())}")
    return weights


def grade(score: float | None, checklist: dict) -> str:
    if score is None:
        return "暂不评分"
    for threshold, name in checklist["scoring"]["grades"]:
        if score >= threshold:
            return name
    return checklist["scoring"]["grades"][-1][1]


def validate(result: dict, checklist: dict) -> list[str]:
    """检查模型输出是否齐全、自洽；返回问题列表（空表示通过）。"""
    problems = []
    expected = {i["id"] for i in checklist["items"]}
    got = [i["id"] for i in result.get("items", [])]
    missing, extra = expected - set(got), set(got) - expected
    dup = {x for x in got if got.count(x) > 1}
    if missing:
        problems.append(f"缺少检查项: {sorted(missing)}")
    if extra:
        problems.append(f"多余检查项: {sorted(extra)}")
    if dup:
        problems.append(f"重复检查项: {sorted(dup)}")
    expected_dims = {d["id"] for d in checklist["dimensions"]}
    dims = [d["dimension"] for d in result.get("dimension_notes", [])]
    if set(dims) != expected_dims or len(dims) != len(set(dims)):
        problems.append("维度结论缺失、重复或包含未知维度")
    issue_items = {i["item_id"] for i in result.get("issues", [])}
    need_items = {item for group in result.get("need_info_list", []) for item in group.get("items", [])}
    extra_need = need_items - expected
    if extra_need:
        problems.append(f"待补充清单引用了不存在的检查项: {sorted(extra_need)}")
    for it in result.get("items", []):
        if it["result"] in ("PASS", "WARN", "FAIL"):
            if not it.get("evidence"):
                problems.append(f"{it['id']} 为 {it['result']} 但无证据")
        if it["result"] in ("WARN", "FAIL"):
            if it["id"] not in issue_items:
                problems.append(f"{it['id']} 为 {it['result']} 但问题清单无对应条目")
        if (it["result"] in NEED_INFO or it.get("conditional")) and it["id"] not in need_items:
            problems.append(f"{it['id']} 尚未定论，但待补充清单无对应条目")
    item_map = {i["id"]: i for i in result.get("items", [])}
    for iss in result.get("issues", []):
        src = item_map.get(iss["item_id"])
        if not src:
            problems.append(f"问题「{iss['title']}」引用了不存在的检查项 {iss['item_id']}")
        elif src["result"] not in ("WARN", "FAIL"):
            problems.append(f"问题「{iss['title']}」挂在 {iss['item_id']}，但该项结果为 {src['result']}")
        if not iss.get("evidence"):
            problems.append(f"问题「{iss['title']}」无证据")
    return problems


def issue_fingerprint(project_id: str, issue: dict) -> str:
    """跨天跟踪同一问题：不依赖模型自由文本标题的稳定指纹。

    同一检查项在同一文件的不同位置用首个证据行区分；标题、影响和建议都不
    参与指纹，因此模型换一种措辞不会把同一个问题误报成“新增”。
    """
    ref = (issue.get("evidence") or [""])[0]
    if isinstance(ref, dict):
        first = str(ref.get("path", ""))
        line = ref.get("start_line", "")
    else:
        parts = str(ref).split(":", 1)
        first = parts[0].strip()
        line = parts[1].strip() if len(parts) == 2 else ""
    key = f"{project_id}|{issue['item_id']}|{first}|{line}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def score_result(result: dict, checklist: dict, weights: dict[str, float]) -> dict:
    sc = checklist["scoring"]
    point = {"PASS": sc["PASS"], "WARN": sc["WARN"], "FAIL": sc["FAIL"]}
    p0_items = {i["item_id"] for i in result.get("issues", []) if i["level"] == "P0"}
    for it in result.get("items", []):
        if it.get("risk") == "P0":
            p0_items.add(it["id"])

    dims = {}
    for d in checklist["dimensions"]:
        did = d["id"]
        its = [i for i in result["items"] if i["id"].split(".")[0] == did]
        counts = {k: 0 for k in ("PASS", "WARN", "FAIL", "N/A", "NEED_INFO")}
        pts, judged, p0n = [], 0, 0
        for it in its:
            r = it["result"]
            conditional = bool(it.get("conditional"))
            if r in NEED_INFO or (conditional and r in point):
                counts["NEED_INFO"] += 1
                continue
            counts[r] += 1
            if r == "N/A":
                continue
            judged += 1
            if it["id"] in p0_items:
                pts.append(0.0)
                p0n += 1
            else:
                pts.append(point[r])
        applicable = len(its) - counts["N/A"]
        coverage = judged / applicable if applicable else 0.0
        # N/A 占比过高时该维度也不能代表实际检查范围，同样不评分。
        if applicable == 0 or coverage < sc["min_coverage"] or counts["N/A"] / len(its) > 0.4:
            score = None
        else:
            score = round(max(0.0, sum(pts) / judged * 10 - p0n * sc["p0_item_penalty"]), 1)
        levels = [i["level"] for i in result.get("issues", []) if i["item_id"].split(".")[0] == did]
        top = min(levels) if levels else "-"
        dims[did] = {
            "id": did, "code": d["code"], "name": d["name"], "weight": weights[did],
            "score": score, "coverage": round(coverage, 3), "counts": counts,
            "applicable": applicable, "judged": judged, "top_risk": top,
        }

    scored = [x for x in dims.values() if x["score"] is not None]
    wsum = sum(x["weight"] for x in scored)
    # 总分门槛：可评分维度太少或总体覆盖率不足时，总分写“暂不评分”，
    # 避免个别维度的高分拼出一个看起来可信的总分。
    applicable_all = sum(x["applicable"] for x in dims.values())
    judged_all = sum(x["judged"] for x in dims.values())
    overall_coverage = judged_all / applicable_all if applicable_all else 0.0
    if len(scored) >= MIN_SCORED_DIMENSIONS and overall_coverage >= sc["min_coverage"] and wsum:
        total = round(sum(x["score"] * x["weight"] for x in scored) / wsum, 1)
    else:
        total = None
    level_counts = {lv: 0 for lv in ("P0", "P1", "P2", "P3")}
    for i in result.get("issues", []):
        level_counts[i["level"]] += 1
    sec_issues = [i for i in result.get("issues", []) if i["item_id"].startswith("6.")]
    return {
        "dimensions": dims,
        "total": total,
        "grade": grade(total, checklist),
        "scored_dimensions": len(scored),
        "coverage": round(judged_all / applicable_all, 3) if applicable_all else 0.0,
        "not_evaluated": sum(x["counts"]["NEED_INFO"] for x in dims.values()),
        "issue_counts": level_counts,
        "security_score": dims["6"]["score"],
        "security_p0": sum(1 for i in sec_issues if i["level"] == "P0"),
        "security_p1": sum(1 for i in sec_issues if i["level"] == "P1"),
    }
