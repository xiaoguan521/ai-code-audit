"""生成现行 SKILL §11 格式的有效报告，供 P0 校验与流水线测试使用。

不要复用 tests/audit_fixture.py：那是旧 HTML 注释契约，现行报告是人类
可读表格。生成的报告由 scoring.score_result() 算出分数再填入，数字自洽，
可直接过 runner/report_validate.py 的校验。
"""
import scoring


def _format_refs(refs):
    parts = []
    for ref in refs:
        if isinstance(ref, str):
            parts.append(ref)
            continue
        if isinstance(ref, dict):
            path, start, end = ref["path"], ref["start_line"], ref.get("end_line", ref["start_line"])
            parts.append(f"{path}:{start}-{end}" if end != start else f"{path}:{start}")
            continue
        path, start = ref[0], ref[1]
        end = ref[2] if len(ref) > 2 else start
        parts.append(f"{path}:{start}-{end}" if end != start else f"{path}:{start}")
    return "；".join(parts)


def build_structured(checklist, results, weights=None):
    """results: {item_id: (result, risk, evidence_refs)}，未传入的项默认 NEED_INFO。"""
    parsed_weights = scoring.parse_weights(weights, checklist)
    items = []
    issues = []
    for item in checklist["items"]:
        result, risk, refs = results.get(item["id"], ("NEED_INFO-证据不足", "none", []))
        evidence = []
        for ref in refs:
            if isinstance(ref, str):
                path, _, rest = ref.rpartition(":")
                start, _, end = rest.partition("-")
                evidence.append({"path": path, "start_line": int(start),
                                 "end_line": int(end) if end else int(start)})
            else:
                evidence.append({"path": ref[0], "start_line": ref[1],
                                 "end_line": ref[2] if len(ref) > 2 else ref[1]})
        items.append({"id": item["id"], "result": result, "risk": risk,
                      "evidence": evidence, "conclusion": "示例结论", "conditional": False})
        if result in ("WARN", "FAIL"):
            level = risk if risk in ("P0", "P1", "P2", "P3") else "P2"
            issues.append({"item_id": item["id"], "level": level,
                           "title": f"{item['name']}：示例问题", "evidence": evidence,
                           "impact": "示例影响", "suggestion": "示例修复建议", "effort": "M"})
    data = {"items": items, "issues": issues,
            "dimension_notes": [{"dimension": d["id"], "conclusion": ""} for d in checklist["dimensions"]],
            "need_info_list": []}
    return data, parsed_weights


def readable_report(checklist, results=None, weights=None, report_mode="standard",
                    project_name="示例项目"):
    """生成 §11 结构的有效报告；results 同 build_structured。"""
    results = results or {}
    data, parsed_weights = build_structured(checklist, results, weights)
    scores = scoring.score_result(data, checklist, parsed_weights)
    dims = {d["id"]: d for d in checklist["dimensions"]}
    item_names = {item["id"]: item["name"] for item in checklist["items"]}

    total = scores["total"]
    total_text = f"{total}" if total is not None else "暂不评分"
    security = scores["security_score"]
    security_text = f"{security}" if security is not None else "n/a"
    lines = [
        f"# 《{project_name} 八性代码质量审查报告》",
        "",
        "## 执行摘要",
        "",
        f"- 总评分 {total_text}（{scores['grade']}），基于 {scores['scored_dimensions']}/8 个维度；"
        f"安全评分 {security_text}",
        "",
        "## 评分总览",
        "",
        "| 维度 | 得分 | 覆盖率 | PASS/WARN/FAIL/N/A/NEED_INFO 数 | 最高风险 | 一句话结论 |",
        "|---|---|---|---|---|---|",
    ]
    for dim in checklist["dimensions"]:
        did = dim["id"]
        computed = scores["dimensions"][did]
        score_text = f"{computed['score']}" if computed["score"] is not None else "证据不足，暂不评分"
        counts = computed["counts"]
        count_text = "/".join(str(counts[k]) for k in ("PASS", "WARN", "FAIL", "N/A", "NEED_INFO"))
        lines.append(f"| {did}. {dim['name']} | {score_text} | {round(computed['coverage'] * 100)}% | "
                     f"{count_text} | {computed['top_risk']} | {dim['name']}示例结论 |")
    if total is not None:
        formula = " + ".join(
            f"{scores['dimensions'][d['id']]['score']}×{scores['dimensions'][d['id']]['weight']}"
            for d in checklist["dimensions"] if scores["dimensions"][d["id"]]["score"] is not None)
        weights_sum = "+".join(
            str(scores["dimensions"][d["id"]]["weight"])
            for d in checklist["dimensions"] if scores["dimensions"][d["id"]]["score"] is not None)
        lines.append("")
        lines.append(f"项目评分算式：（{formula}）/（{weights_sum}）= {total}"
                     f"（基于 {scores['scored_dimensions']}/8 个维度）")
    else:
        lines.append("")
        lines.append(f"项目评分：暂不评分（基于 {scores['scored_dimensions']}/8 个维度）")
    lines += ["", "## 风险问题清单", ""]
    if data["issues"]:
        lines.append("| ID | 检查项 | 等级 | 标题 | 证据 | 影响 | 修复建议 | 工作量 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for index, issue in enumerate(data["issues"], 1):
            item_id = issue["item_id"]
            lines.append(f"| I-{index:03d} | {item_id} {item_names[item_id]} | {issue['level']} | "
                         f"{issue['title']} | {_format_refs(issue['evidence']) or '—'} | "
                         f"{issue['impact']} | {issue['suggestion']} | {issue['effort']} |")
    else:
        lines.append("无")
    lines += ["", "## 分维度检查结果", ""]
    for dim in checklist["dimensions"]:
        did = dim["id"]
        lines.append(f"### {dim['name']} {dim['code']}")
        lines.append("")
        if report_mode == "fast":
            lines.append("| 编号 | 检查项 | 结果 | 等级 | 结论 | 沿用 |")
            lines.append("|---|---|---|---|---|---|")
        else:
            lines.append("| 编号 | 检查项 | 结果 | 等级 | 证据 | 结论 | 沿用 |")
            lines.append("|---|---|---|---|---|---|---|")
        for item in checklist["items"]:
            if item["id"].split(".")[0] != did:
                continue
            result, risk, refs = results.get(item["id"], ("NEED_INFO-证据不足", "none", []))
            risk_text = risk if risk in ("P0", "P1", "P2", "P3") else "—"
            if report_mode == "fast":
                lines.append(f"| {item['id']} | {item['name']} | {result} | {risk_text} | 示例结论 | 否 |")
            else:
                lines.append(f"| {item['id']} | {item['name']} | {result} | {risk_text} | "
                             f"{_format_refs(refs) or '—'} | 示例结论 | 否 |")
        lines.append("")
    lines += ["## 待补充信息清单", "", "无", ""]
    return "\n".join(lines)
