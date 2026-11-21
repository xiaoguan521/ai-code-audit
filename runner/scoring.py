"""八性评分：按 SKILL.md 第 6 节规则，从逐项结果确定性地计算维度分、覆盖率、总分与安全评分。

评分完全由代码计算，不由模型给出，保证同样的逐项结论永远得到同样的分数。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
NEED_INFO = ("NEED_INFO-运行数据", "NEED_INFO-补充材料")


def load_checklist() -> dict:
    return json.loads((CONFIG_DIR / "checklist.json").read_text(encoding="utf-8"))


def parse_weights(raw: str | None, checklist: dict) -> dict[str, float]:
    """projects.csv 的 weights 列，格式 "1:15;2:12;..."，留空用默认权重。"""
    weights = {d["id"]: float(d["weight"]) for d in checklist["dimensions"]}
    if raw:
        for part in raw.replace("，", ";").split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                if k.strip() in weights:
                    weights[k.strip()] = float(v)
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
    issue_items = {i["item_id"] for i in result.get("issues", [])}
    for it in result.get("items", []):
        if it["result"] in ("WARN", "FAIL"):
            ev = [e for e in it.get("evidence", []) if e and "静态未见" not in e]
            if not ev:
                problems.append(f"{it['id']} 为 {it['result']} 但无证据")
            if it["id"] not in issue_items:
                problems.append(f"{it['id']} 为 {it['result']} 但问题清单无对应条目")
    item_map = {i["id"]: i for i in result.get("items", [])}
    for iss in result.get("issues", []):
        src = item_map.get(iss["item_id"])
        if src and src["result"] not in ("WARN", "FAIL"):
            problems.append(f"问题「{iss['title']}」挂在 {iss['item_id']}，但该项结果为 {src['result']}")
    return problems


def issue_fingerprint(project_id: str, issue: dict) -> str:
    """跨天跟踪同一问题：项目 + 检查项 + 首个证据文件路径（不含行号）+ 标题。"""
    first = (issue.get("evidence") or [""])[0].split(":")[0].strip()
    key = f"{project_id}|{issue['item_id']}|{first}|{issue['title'].strip()}"
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
        if applicable == 0 or coverage < sc["min_coverage"]:
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
    total = round(sum(x["score"] * x["weight"] for x in scored) / wsum, 1) if wsum else None
    applicable_all = sum(x["applicable"] for x in dims.values())
    judged_all = sum(x["judged"] for x in dims.values())
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
