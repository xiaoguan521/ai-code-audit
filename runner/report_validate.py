"""落盘前最小机器校验：解析人类可读的报告表格，重算分数，核验 WARN/FAIL 证据。

只拦截、不改写、不重跑：校验失败时调用方记 error、不落盘、不更新 state。

注意：不要用 ``parsers.audit_markdown`` 解析报告。现行 SKILL 要求报告是
“便于人阅读的 Markdown，不要求 YAML、机器字段或其他额外格式”，样例报告
里 0 个 audit-* 块；那个解析器的 HTML 注释契约与现行报告格式不匹配。
这里只解析 §11 的人类可读表格，并容忍旧格式样例的差异（带编号的小节
标题、结果单元格的“·沿用”后缀、旧版逐项表列）。
"""
from __future__ import annotations

import re
from pathlib import Path

import evidence as evidence_module
import scoring

RESULT_RE = re.compile(
    r"(NEED_INFO-运行数据|NEED_INFO-补充材料|NEED_INFO-证据不足|NEED_INFO|PASS|WARN|FAIL|N/A)"
)
ITEM_ID_RE = re.compile(r"\b(\d+\.\d+)\b")
REF_RE = re.compile(r"([\w./-]+):(\d+)(?:-(\d+))?")
LEVEL_RE = re.compile(r"\b(P[0-3])\b")
MAX_PROBLEMS = 30


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """按二级标题切分；返回 [(标题, 行列表)]。"""
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        match = re.match(r"##\s+(.*)", line)
        if match:
            current = (match.group(1).strip(), [])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return sections


def _find_section(text: str, keyword: str) -> list[str]:
    for title, lines in _sections(text):
        if keyword in title:
            return lines
    return []


def _table_rows(lines: list[str]) -> tuple[list[str] | None, list[list[str]]]:
    """返回 (表头单元格, 数据行单元格列表)；跳过分隔行。"""
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r"[:\- ]+", cell) for cell in cells if cell):
            continue
        if header is None:
            header = cells
            continue
        rows.append(cells)
    return header, rows


def _result_token(cell: str) -> str | None:
    match = RESULT_RE.search(cell)
    if not match:
        return None
    token = match.group(1)
    return "NEED_INFO-证据不足" if token == "NEED_INFO" else token


def _extract_refs(cell: str) -> list[dict]:
    refs: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for match in REF_RE.finditer(cell):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"path": path, "start_line": start, "end_line": end})
    return refs


def _parse_items(text: str) -> tuple[list[dict], list[str]]:
    """解析“分维度检查结果”小节的逐项表。返回 (items, problems)。"""
    lines = _find_section(text, "分维度检查")
    if not lines:
        return [], ["未找到“分维度检查结果”小节"]
    header, rows = _table_rows(lines)
    columns = {name: index for index, name in enumerate(header or [])}
    result_col = columns.get("结果", 2)
    level_col = columns.get("等级")
    evidence_col = next((i for name, i in columns.items() if "证据" in name), None)
    conclusion_col = next((i for name, i in columns.items() if name in ("结论", "说明与建议")), None)
    items: list[dict] = []
    seen: set[str] = set()
    problems: list[str] = []
    for cells in rows:
        if not cells:
            continue
        item_id = cells[0].strip()
        if not re.fullmatch(r"\d+\.\d+", item_id):
            continue
        if item_id in seen:
            problems.append(f"检查项重复：{item_id}")
            continue
        seen.add(item_id)
        result_cell = cells[result_col] if result_col < len(cells) else ""
        result = _result_token(result_cell)
        if result is None:
            # 表头可能与 §11 不一致时，退化为整行搜索。
            result = _result_token(" ".join(cells))
        if result is None:
            problems.append(f"{item_id} 结果无法识别")
            continue
        level = "none"
        if level_col is not None and level_col < len(cells):
            level_match = LEVEL_RE.search(cells[level_col])
            if level_match:
                level = level_match.group(1)
        refs = _extract_refs(cells[evidence_col]) if evidence_col is not None and evidence_col < len(cells) else []
        conclusion = cells[conclusion_col] if conclusion_col is not None and conclusion_col < len(cells) else ""
        items.append({
            "id": item_id, "result": result, "risk": level,
            "evidence": refs, "conclusion": conclusion,
            "conditional": conclusion.startswith("条件："),
        })
    return items, problems


def _parse_issues(text: str) -> list[dict]:
    """解析“风险问题清单”小节；无问题时写“无”，返回空列表。"""
    lines = _find_section(text, "风险问题清单")
    if not lines:
        return []
    header, rows = _table_rows(lines)
    columns = {name: index for index, name in enumerate(header or [])}
    evidence_col = next((i for name, i in columns.items() if "证据" in name), None)
    title_col = next((i for name, i in columns.items() if "标题" in name), None)
    issues: list[dict] = []
    for index, cells in enumerate(rows, 1):
        joined = " ".join(cells)
        item_match = ITEM_ID_RE.search(joined)
        if not item_match:
            continue
        level_match = LEVEL_RE.search(joined)
        evidence_cell = cells[evidence_col] if evidence_col is not None and evidence_col < len(cells) else joined
        title = cells[title_col] if title_col is not None and title_col < len(cells) else ""
        issues.append({
            "item_id": item_match.group(1),
            "level": level_match.group(1) if level_match else "P3",
            "title": title,
            "evidence": _extract_refs(evidence_cell),
            "impact": "", "suggestion": "", "effort": "M",
            "issue_id": f"Q{index:03d}",
        })
    return issues


def _parse_overview(text: str) -> tuple[dict[str, dict], list[str]]:
    """解析“评分总览”小节。返回 ({维度编号: {score, coverage, counts}}, problems)。"""
    lines = _find_section(text, "评分总览")
    if not lines:
        return {}, ["未找到“评分总览”小节"]
    _header, rows = _table_rows(lines)
    overview: dict[str, dict] = {}
    problems: list[str] = []
    for cells in rows:
        if not cells:
            continue
        dim_match = re.match(r"(\d+)[.、\s]", cells[0].strip())
        if not dim_match:
            continue
        dim = dim_match.group(1)
        score_cell = cells[1] if len(cells) > 1 else ""
        if re.search(r"暂不评分|证据不足", score_cell):
            score = None
        else:
            number = re.search(r"\d+(?:\.\d+)?", score_cell)
            score = float(number.group(0)) if number else None
            if score is None:
                problems.append(f"维度 {dim} 的得分无法识别")
        coverage_cell = cells[2] if len(cells) > 2 else ""
        coverage_match = re.search(r"(\d+(?:\.\d+)?)\s*%", coverage_cell)
        coverage = float(coverage_match.group(1)) if coverage_match else None
        if coverage is None:
            problems.append(f"维度 {dim} 的覆盖率无法识别")
        counts_cell = cells[3] if len(cells) > 3 else ""
        counts = [int(part) for part in re.findall(r"\d+", counts_cell)]
        if len(counts) != 5:
            problems.append(f"维度 {dim} 的计数无法识别：{counts_cell!r}")
            counts = [0, 0, 0, 0, 0]
        overview[dim] = {"score": score, "coverage": coverage, "counts": counts}
    return overview, problems


def _parse_declaration(text: str, keywords: tuple[str, ...], formula_tail: bool = False) -> str | None:
    """在全文搜索“关键词 + 数字/n/a/暂不评分”声明；找不到返回 None。

    formula_tail 只用于总分：算式尾部形式“项目评分 = (...) / (...) = 6.6（基于 N/8 个维度）”。
    """
    for keyword in keywords:
        match = re.search(
            rf"{re.escape(keyword)}\s*[=：:﹕]?\s*(\d+(?:\.\d+)?|n/a|暂不评分)",
            text, re.IGNORECASE)
        if match:
            return match.group(1)
    if formula_tail:
        match = re.search(r"=\s*(\d+(?:\.\d+)?)\s*（基于", text)
        if match:
            return match.group(1)
    return None


def build_result(items: list[dict], issues: list[dict]) -> dict:
    """把解析出的逐项结果与问题清单组装成 scoring.score_result 的输入。"""
    return {
        "items": items,
        "issues": issues,
        "dimension_notes": [{"dimension": str(n), "conclusion": ""} for n in range(1, 9)],
        "need_info_list": [],
    }


def summarize_scores(scores: dict) -> dict:
    return {
        "total": scores.get("total"),
        "grade": scores.get("grade"),
        "scored_dimensions": scores.get("scored_dimensions"),
        "coverage": scores.get("coverage"),
        "not_evaluated": scores.get("not_evaluated"),
        "security_score": scores.get("security_score"),
        "security_p0": scores.get("security_p0"),
        "security_p1": scores.get("security_p1"),
    }


def validate_report(text: str, repo: Path, checklist: dict, weights: dict,
                    report_mode: str) -> dict:
    """校验报告原文；返回 {"problems": [...], "scores": {...}|None}。"""
    problems: list[str] = []
    items, item_problems = _parse_items(text)
    problems.extend(item_problems)
    issues = _parse_issues(text)

    expected = {item["id"] for item in checklist["items"]}
    got = [item["id"] for item in items]
    missing = sorted(expected - set(got))
    extra = sorted(set(got) - expected)
    if missing:
        shown = "、".join(missing[:10])
        suffix = f"等共 {len(missing)} 项" if len(missing) > 10 else ""
        problems.append(f"缺少检查项：{shown}{suffix}")
    if extra:
        problems.append(f"多余检查项：{', '.join(extra)}")

    # 风险等级回填：逐项表无等级列时（旧格式），用问题清单的等级补。
    if items:
        issue_level = {issue["item_id"]: issue["level"] for issue in issues}
        for item in items:
            if item["risk"] == "none" and item["id"] in issue_level:
                item["risk"] = issue_level[item["id"]]

    overview, overview_problems = _parse_overview(text)
    problems.extend(overview_problems)

    scores = None
    if items:
        result = build_result(items, issues)
        recomputed = scoring.score_result(result, checklist, weights)
        scores = summarize_scores(recomputed)
        for dim in checklist["dimensions"]:
            did = str(dim["id"])
            computed = recomputed["dimensions"][did]
            declared = overview.get(did)
            if declared is None:
                problems.append(f"评分总览缺少维度 {did}")
                continue
            if declared["score"] is None and computed["score"] is not None:
                problems.append(f"维度 {did} 声明暂不评分，但重算得分为 {computed['score']}")
            elif declared["score"] is not None and computed["score"] is None:
                problems.append(f"维度 {did} 声明得分为 {declared['score']}，但重算为暂不评分")
            elif declared["score"] is not None and abs(declared["score"] - computed["score"]) > 0.1:
                problems.append(f"维度 {did} 得分不一致：声明 {declared['score']}，重算 {computed['score']}")
            if declared["coverage"] is not None and abs(declared["coverage"] - computed["coverage"] * 100) > 1:
                problems.append(
                    f"维度 {did} 覆盖率不一致：声明 {declared['coverage']}%，"
                    f"重算 {round(computed['coverage'] * 100, 1)}%")
            expected_counts = [computed["counts"][key] for key in ("PASS", "WARN", "FAIL", "N/A", "NEED_INFO")]
            if declared["counts"] != expected_counts:
                problems.append(
                    f"维度 {did} 计数不一致：声明 {'/'.join(map(str, declared['counts']))}，"
                    f"重算 {'/'.join(map(str, expected_counts))}")

        total_declared = _parse_declaration(text, ("总评分", "项目评分"), formula_tail=True)
        if recomputed["total"] is None:
            if total_declared is not None and total_declared not in ("n/a", "暂不评分"):
                problems.append(f"总分应为暂不评分，报告却声明为 {total_declared}")
            elif total_declared is None:
                problems.append("报告未声明项目总分")
        else:
            if total_declared is None:
                problems.append("报告未声明项目总分")
            elif total_declared in ("n/a", "暂不评分"):
                problems.append(f"总分声明为暂不评分，但重算得分为 {recomputed['total']}")
            elif abs(float(total_declared) - recomputed["total"]) > 0.1:
                problems.append(f"项目总分不一致：声明 {total_declared}，重算 {recomputed['total']}")
        based_match = re.search(r"基于\s*(\d+)\s*/\s*8", text)
        if based_match and int(based_match.group(1)) != recomputed["scored_dimensions"]:
            problems.append(
                f"可评分维度数不一致：声明基于 {based_match.group(1)}/8 个维度，"
                f"重算为 {recomputed['scored_dimensions']}/8")
        security_declared = _parse_declaration(text, ("安全评分",))
        if security_declared is not None:
            if recomputed["security_score"] is None:
                if security_declared not in ("n/a", "暂不评分"):
                    problems.append(f"安全分应为 n/a，报告却声明为 {security_declared}")
            elif security_declared in ("n/a", "暂不评分"):
                problems.append(f"安全分声明为 n/a，但重算得分为 {recomputed['security_score']}")
            elif abs(float(security_declared) - recomputed["security_score"]) > 0.1:
                problems.append(
                    f"安全分不一致：声明 {security_declared}，重算 {recomputed['security_score']}")

        if report_mode != "fast":
            for item in items:
                if item["result"] in ("WARN", "FAIL") and not item["evidence"]:
                    problems.append(f"{item['id']} 为 {item['result']} 但无证据")
            evidence_module.normalize_references(result, repo)
            problems.extend(evidence_module.verify(result, repo))
    else:
        problems.append("未解析出任何检查项")

    if len(problems) > MAX_PROBLEMS:
        total = len(problems)
        problems = problems[:MAX_PROBLEMS]
        problems.append(f"……共 {total} 条问题，仅列出前 {MAX_PROBLEMS} 条")
    return {"problems": problems, "scores": scores}
