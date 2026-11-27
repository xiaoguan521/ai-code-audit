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
CHINESE_RESULT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("不通过", "未通过", "失败", "严重缺失"), "FAIL"),
    (("部分通过", "警告", "存在风险", "需改进", "待改进"), "WARN"),
    (("不适用", "无需评估"), "N/A"),
    (("待补充", "证据不足", "无法判断", "信息不足", "未提供"), "NEED_INFO-证据不足"),
    (("通过", "符合", "达标", "正常"), "PASS"),
)
ITEM_ID_RE = re.compile(r"\b(\d+\.\d+)\b")
ITEM_ID_HEAD_RE = re.compile(r"^(\d+\.\d+)(?:\s|$|[:：、\-—（(])")
ITEM_LINE_RE = re.compile(r"^\s*(?:[-*+]|\#{2,6})\s*[*_`]*([1-8]\.\d{1,2})(?:\s|[:：、\-—（(]|$)(.+)")
REF_RE = re.compile(r"([\w./-]+):(\d+)(?:-(\d+))?")
LEVEL_RE = re.compile(r"\b(P[0-3])\b")
CHINESE_LEVEL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("致命", "极高", "严重高危"), "P0"),
    (("高危", "高风险", "严重"), "P1"),
    (("中危", "中低", "中风险", "中等"), "P2"),
    (("低危", "低风险", "轻微"), "P3"),
)
ITEM_SECTION_KEYWORDS: tuple[str, ...] = (
    "分维度检查", "64 项", "64项", "检查结论", "检查结果", "逐项检查",
)
OVERVIEW_SECTION_KEYWORDS: tuple[str, ...] = (
    "评分总览", "维度评分", "得分总览", "质量评分", "八维得分", "八维评分",
)
ISSUE_SECTION_KEYWORDS: tuple[str, ...] = (
    "风险问题清单", "高危与重点问题", "重点风险问题", "问题清单",
)
SKIP_FALLBACK_SECTION_KEYWORDS: tuple[str, ...] = (
    "评分总览", "维度评分", "得分总览", "风险问题清单", "问题清单", "待补充信息",
)
MAX_PROBLEMS = 30
DIMENSION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("1", ("功能适合性", "FS")),
    ("2", ("性能效率", "PE")),
    ("3", ("可靠性", "RE")),
    ("4", ("稳定性", "ST")),
    ("5", ("易用性", "US")),
    ("6", ("安全性", "SE")),
    ("7", ("兼容性", "CO")),
    ("8", ("可维护性", "MA")),
)


OUTER_FENCE_OPEN_RE = re.compile(r"^```(?:markdown|md|txt)?[ \t]*$", re.IGNORECASE)


def unwrap_outer_markdown_fence(text: str) -> str:
    """若整份报告被最外层 ```markdown ... ``` 包裹，剥离外层围栏以便正常解析标题与表格。"""
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 3:
        return text
    if not OUTER_FENCE_OPEN_RE.match(lines[0].strip()) or lines[-1].strip() != "```":
        return text
    inner_lines = lines[1:-1]
    depth = 0
    for line in inner_lines:
        s = line.strip()
        if s.startswith("```"):
            if depth == 0 and s == "```":
                return text
            depth = 0 if (depth > 0 and s == "```") else depth + 1
    if depth != 0:
        return text
    inner = "\n".join(inner_lines)
    if re.search(r"^#{1,3}\s+", inner, re.M) or "|" in inner:
        return inner.strip() + "\n"
    return text


def _strip_md(cell: str) -> str:
    return re.sub(r"[*`_]", "", cell).strip()


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """按二级标题切分（忽略代码块内部；先剥离整篇外层 markdown 围栏）；返回 [(标题, 行列表)]。"""
    text = unwrap_outer_markdown_fence(text)
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if current is not None:
                current[1].append(line)
            continue
        if not in_fence:
            match = re.match(r"^##\s+(.*)", line)
            if match:
                current = (match.group(1).strip(), [])
                sections.append(current)
                continue
        if current is not None:
            current[1].append(line)
    return sections


def _find_section_any(text: str, keywords: tuple[str, ...]) -> list[str]:
    for title, lines in _sections(text):
        if any(keyword in title for keyword in keywords):
            return lines
    return []


def _find_section(text: str, keyword: str) -> list[str]:
    return _find_section_any(text, (keyword,))


def _parse_level(cell: str) -> str | None:
    match = LEVEL_RE.search(cell)
    if match:
        return match.group(1)
    cleaned = _strip_md(cell)
    for keywords, level in CHINESE_LEVEL_RULES:
        if any(kw in cleaned for kw in keywords):
            return level
    return None


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


def _result_token(cell: str, allow_chinese: bool = False) -> str | None:
    match = RESULT_RE.search(cell)
    if match:
        token = match.group(1)
        return "NEED_INFO-证据不足" if token == "NEED_INFO" else token
    if allow_chinese:
        cleaned = _strip_md(cell)
        for keywords, mapped in CHINESE_RESULT_RULES:
            if any(kw in cleaned for kw in keywords):
                return mapped
    return None


def _extract_item_id(cell: str) -> str | None:
    cleaned = _strip_md(cell)
    match = ITEM_ID_HEAD_RE.match(cleaned)
    return match.group(1) if match else None


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


def _columns_from_header(header: list[str]) -> tuple[int, int | None, int | None, int | None]:
    columns = {_strip_md(name): index for index, name in enumerate(header)}
    result_col = next((i for name, i in columns.items() if "结果" in name), 2)
    level_col = next((i for name, i in columns.items() if any(k in name for k in ("等级", "风险", "级别"))), None)
    evidence_col = next((i for name, i in columns.items() if "证据" in name), None)
    conclusion_col = next((i for name, i in columns.items() if any(k in name for k in ("结论", "说明与建议", "说明"))), None)
    return result_col, level_col, evidence_col, conclusion_col


def _parse_item_rows(lines: list[str], require_result: bool = False) -> tuple[list[dict], list[str]]:
    """从一组 Markdown 行中解析逐项检查表（支持同一节内多张分维度子表、编号与名称合并列、前置维度/序号列）。"""
    items: list[dict] = []
    seen: set[str] = set()
    problems: list[str] = []
    result_col, level_col, evidence_col, conclusion_col = 2, None, None, None
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(re.fullmatch(r"[:\- ]+", cell) for cell in cells if cell):
            continue
        item_id = _extract_item_id(cells[0])
        if item_id is None and len(cells) > 1:
            item_id = _extract_item_id(cells[1])
        if item_id is None:
            if any(k in "".join(cells) for k in ("检查项", "结果", "风险", "等级", "结论")):
                result_col, level_col, evidence_col, conclusion_col = _columns_from_header(cells)
            continue
        result_cell = cells[result_col] if result_col < len(cells) else ""
        result = _result_token(result_cell, allow_chinese=True)
        if result is None:
            # 表头可能与 §11 不一致时，退化为整行搜索标准英文结果 token。
            result = _result_token(" ".join(cells), allow_chinese=False)
        if result is None:
            if not require_result:
                problems.append(f"{item_id} 结果无法识别")
            continue
        if item_id in seen:
            problems.append(f"检查项重复：{item_id}")
            continue
        seen.add(item_id)
        level = "none"
        if level_col is not None and level_col < len(cells):
            parsed_level = _parse_level(cells[level_col])
            if parsed_level:
                level = parsed_level
        refs = _extract_refs(cells[evidence_col]) if evidence_col is not None and evidence_col < len(cells) else []
        conclusion = cells[conclusion_col] if conclusion_col is not None and conclusion_col < len(cells) else ""
        items.append({
            "id": item_id, "result": result, "risk": level,
            "evidence": refs, "conclusion": conclusion,
            "conditional": conclusion.startswith("条件："),
        })
    return items, problems


def _parse_item_lines(lines: list[str]) -> list[dict]:
    """兜底解析非表格形式（无序列表或小标题）写出的检查项行，例如 `- **1.1 功能完整性**：PASS`。"""
    items: list[dict] = []
    seen: set[str] = set()
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("|"):
            continue
        match = ITEM_LINE_RE.match(line)
        if not match:
            continue
        item_id, rest = match.group(1), match.group(2)
        result = _result_token(rest, allow_chinese=False)
        if result is None or item_id in seen:
            continue
        seen.add(item_id)
        level = _parse_level(rest) or "none"
        items.append({
            "id": item_id, "result": result, "risk": level,
            "evidence": _extract_refs(rest), "conclusion": rest.strip(),
            "conditional": False,
        })
    return items


def _parse_items(text: str) -> tuple[list[dict], list[str]]:
    """解析“分维度检查结果”小节的逐项表；未命中标准标题时兜底扫描全文表格及列表项。"""
    lines = _find_section_any(text, ITEM_SECTION_KEYWORDS)
    if lines:
        items, problems = _parse_item_rows(lines)
        if len(items) < 16:
            line_items = _parse_item_lines(lines)
            if len(line_items) > len(items):
                items, problems = line_items, []
        if items:
            return items, problems
    # 兜底：扫描全文所有非评分总览/风险清单/待补充信息小节，提取表格或列表中的检查项
    fallback_lines: list[str] = []
    sections = _sections(text)
    if sections:
        for title, sec_lines in sections:
            if any(kw in title for kw in SKIP_FALLBACK_SECTION_KEYWORDS):
                continue
            fallback_lines.extend(sec_lines)
    else:
        fallback_lines = text.splitlines()
    items, problems = _parse_item_rows(fallback_lines, require_result=True)
    if len(items) < 16:
        line_items = _parse_item_lines(fallback_lines)
        if len(line_items) > len(items):
            items, problems = line_items, []
    if items:
        if not lines:
            problems.insert(0, "未找到标准“分维度检查结果”小节（已从全文兜底解析）")
        return items, problems
    return [], ["未找到“分维度检查结果”小节"]


def _parse_issues(text: str) -> list[dict]:
    """解析“风险问题清单”小节；无问题时写“无”，返回空列表。"""
    lines = _find_section_any(text, ISSUE_SECTION_KEYWORDS)
    if not lines:
        return []
    header, rows = _table_rows(lines)
    columns = {_strip_md(name): index for index, name in enumerate(header or [])}
    item_col = next((i for name, i in columns.items() if "检查项" in name or "维度" in name), None)
    level_col = next((i for name, i in columns.items() if any(k in name for k in ("等级", "级别", "风险"))), None)
    evidence_col = next((i for name, i in columns.items() if "证据" in name), None)
    title_col = next((i for name, i in columns.items() if "标题" in name), None)
    issues: list[dict] = []
    for index, cells in enumerate(rows, 1):
        joined = " ".join(cells)
        if item_col is not None and item_col < len(cells):
            item_ids = list(dict.fromkeys(ITEM_ID_RE.findall(cells[item_col])))
        else:
            item_ids = []
        if not item_ids:
            m = ITEM_ID_RE.search(joined)
            if not m:
                continue
            item_ids = [m.group(1)]
        level_src = cells[level_col] if level_col is not None and level_col < len(cells) else joined
        parsed_level = _parse_level(level_src) or _parse_level(joined) or "P3"
        evidence_cell = cells[evidence_col] if evidence_col is not None and evidence_col < len(cells) else joined
        title = cells[title_col] if title_col is not None and title_col < len(cells) else ""
        raw_id = _strip_md(cells[0]) if cells else ""
        issue_id = raw_id if re.fullmatch(r"[A-Za-z]-?\d+|[A-Za-z]\d+", raw_id) else f"Q{index:03d}"
        refs = _extract_refs(evidence_cell)
        for item_id in item_ids:
            issues.append({
                "item_id": item_id,
                "level": parsed_level,
                "title": title,
                "evidence": refs,
                "impact": "", "suggestion": "", "effort": "M",
                "issue_id": issue_id,
            })
    return issues


def _match_dimension_id(cell: str) -> str | None:
    cleaned = _strip_md(cell)
    m = re.match(r"(?:维度\s*)?([1-8])(?:\s*[.、·:\s]|$)", cleaned)
    if m:
        return m.group(1)
    for dim_id, aliases in DIMENSION_ALIASES:
        for alias in aliases:
            if len(alias) <= 2:
                if re.search(rf"\b{re.escape(alias)}\b", cleaned):
                    return dim_id
            elif alias in cleaned:
                return dim_id
    return None


def _parse_overview(text: str) -> tuple[dict[str, dict], list[str]]:
    """解析“评分总览”小节（按表头动态定位维度/得分/覆盖率/计数列，兼容含权重列、拆分计数列、精简 2-3 列表）。"""
    lines = _find_section_any(text, OVERVIEW_SECTION_KEYWORDS)
    if not lines:
        return {}, ["未找到“评分总览”小节"]
    header, rows = _table_rows(lines)
    norm_header = [_strip_md(h) for h in (header or [])]
    dim_col = next((i for i, h in enumerate(norm_header) if "维度" in h), 0)
    weight_cols = {i for i, h in enumerate(norm_header) if "权重" in h}
    score_col = next(
        (i for i, h in enumerate(norm_header) if i != dim_col and any(k in h for k in ("得分", "评分", "分数", "维度分"))),
        None,
    )
    if score_col is None:
        score_col = next((i for i in range(len(norm_header)) if i != dim_col and i not in weight_cols), 1)
    coverage_col = next(
        (i for i, h in enumerate(norm_header) if i not in (dim_col, score_col) and i not in weight_cols and "覆盖" in h),
        None,
    )
    counts_col = next(
        (
            i
            for i, h in enumerate(norm_header)
            if i not in (dim_col, score_col, coverage_col)
            and i not in weight_cols
            and (any(k in h for k in ("计数", "统计", "分布")) or ("PASS" in h.upper() and "WARN" in h.upper()))
        ),
        None,
    )
    split_count_cols: list[int] | None = None
    if counts_col is None and norm_header:
        split_tokens = ("PASS", "WARN", "FAIL", "N/A", "NEED_INFO")
        found_split: list[int] = []
        for tok in split_tokens:
            idx = next(
                (
                    i
                    for i, h in enumerate(norm_header)
                    if i not in (dim_col, score_col, coverage_col)
                    and i not in weight_cols
                    and tok in h.upper()
                ),
                None,
            )
            if idx is not None:
                found_split.append(idx)
        if len(found_split) == 5 and len(set(found_split)) == 5:
            split_count_cols = found_split

    overview: dict[str, dict] = {}
    problems: list[str] = []
    for cells in rows:
        if not cells:
            continue
        dim = _match_dimension_id(cells[dim_col] if dim_col < len(cells) else cells[0])
        if not dim:
            continue
        score_cell = cells[score_col] if score_col < len(cells) else ""
        if re.search(r"暂不评分|证据不足|n/a|—|-$", score_cell, re.IGNORECASE):
            score = None
        else:
            number = re.search(r"\d+(?:\.\d+)?", score_cell)
            score = float(number.group(0)) if number else None
            if score is not None and 10.0 < score <= 100.0:
                # 兼容偶发 100 分制输出（如 69 / 83 / 25），自动归一为 10 分制
                score = round(score / 10.0, 1)
            if score is None:
                problems.append(f"维度 {dim} 的得分无法识别")

        coverage: float | None = None
        if coverage_col is not None:
            coverage_cell = cells[coverage_col] if coverage_col < len(cells) else ""
            coverage_match = re.search(r"(\d+(?:\.\d+)?)\s*%", coverage_cell)
            coverage = float(coverage_match.group(1)) if coverage_match else None
            if coverage is None:
                problems.append(f"维度 {dim} 的覆盖率无法识别")
        else:
            for idx, cell in enumerate(cells):
                if idx in (dim_col, score_col, counts_col) or idx in weight_cols:
                    continue
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", cell)
                if m:
                    coverage = float(m.group(1))
                    break

        counts: list[int] | None = None
        if counts_col is not None:
            counts_cell = cells[counts_col] if counts_col < len(cells) else ""
            parsed_counts = [int(part) for part in re.findall(r"\d+", counts_cell)]
            if len(parsed_counts) != 5:
                problems.append(f"维度 {dim} 的计数无法识别：{counts_cell!r}")
                counts = [0, 0, 0, 0, 0]
            else:
                counts = parsed_counts
        elif split_count_cols is not None:
            vals: list[int] = []
            for idx in split_count_cols:
                m = re.search(r"\d+", cells[idx]) if idx < len(cells) else None
                if m:
                    vals.append(int(m.group(0)))
            counts = vals if len(vals) == 5 else None
        else:
            for idx, cell in enumerate(cells):
                if idx in (dim_col, score_col, coverage_col) or idx in weight_cols:
                    continue
                if re.search(r"\d+\s*/\s*\d+\s*/\s*\d+\s*/\s*\d+\s*/\s*\d+", cell):
                    parsed_counts = [int(part) for part in re.findall(r"\d+", cell)]
                    if len(parsed_counts) == 5:
                        counts = parsed_counts
                        break
        overview[dim] = {"score": score, "coverage": coverage, "counts": counts}
    return overview, problems


def _parse_declaration(text: str, keywords: tuple[str, ...], formula_tail: bool = False) -> str | None:
    """找总分/安全分声明；找不到返回 None。

    先在“评分总览”小节内找（SKILL §11 要求总分算式写在总览表后）；
    小节内找不到时，在全文里取最后一次出现——执行摘要里常先回顾上次
    分数（“上次总评分 7.0，本次总评分 6.6”），取第一次会误读成上次分数。
    formula_tail 只用于总分：算式尾部形式
    “项目评分 = (...) / (...) = 6.6（基于 N/8 个维度）”。
    """
    def pattern(keyword: str) -> str:
        return rf"{re.escape(keyword)}\s*[=：:﹕]?\s*(\d+(?:\.\d+)?|n/a|暂不评分)"

    overview_text = "\n".join(_find_section_any(text, OVERVIEW_SECTION_KEYWORDS))
    for keyword in keywords:
        match = re.search(pattern(keyword), overview_text, re.IGNORECASE)
        if match:
            return match.group(1)
    if formula_tail:
        match = re.search(r"=\s*(\d+(?:\.\d+)?)\s*（基于", overview_text)
        if match:
            return match.group(1)
    for keyword in keywords:
        matches = list(re.finditer(pattern(keyword), text, re.IGNORECASE))
        if matches:
            return matches[-1].group(1)
    if formula_tail:
        matches = list(re.finditer(r"=\s*(\d+(?:\.\d+)?)\s*（基于", text))
        if matches:
            return matches[-1].group(1)
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
    text = unwrap_outer_markdown_fence(text)
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

    if items:
        # WARN/FAIL 与风险问题清单的关联：每条 WARN/FAIL 必须在清单里有
        # 对应条目，每条清单条目必须对应一个 WARN/FAIL 项。注意不要调
        # scoring.validate()：它还要求 NEED_INFO 项进待补充清单，而这里
        # need_info_list 是空列表，直接调会把每个 NEED_INFO 项误报。
        issue_by_item: dict[str, list[dict]] = {}
        for issue in issues:
            issue_by_item.setdefault(issue["item_id"], []).append(issue)
        item_by_id = {item["id"]: item for item in items}
        for item in items:
            if item["result"] in ("WARN", "FAIL") and item["id"] not in issue_by_item:
                problems.append(f"{item['id']} 为 {item['result']} 但未列入风险问题清单")
        for issue in issues:
            target = item_by_id.get(issue["item_id"])
            if target is None:
                problems.append(
                    f"风险问题 {issue.get('issue_id', '?')} 对应的检查项 {issue['item_id']} 不存在")
            elif target["result"] not in ("WARN", "FAIL"):
                problems.append(
                    f"风险问题 {issue.get('issue_id', '?')} 对应的检查项 {issue['item_id']} "
                    f"不是 WARN/FAIL（为 {target['result']}）")

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
            if declared["counts"] is not None and declared["counts"] != expected_counts:
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
        based_matches = list(re.finditer(r"基于\s*(\d+)\s*/\s*8", text))
        based_match = based_matches[-1] if based_matches else None
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
        if overview:
            scored_dims = {did: d["score"] for did, d in overview.items() if d.get("score") is not None}
            total_declared = _parse_declaration(text, ("总评分", "项目评分"), formula_tail=True)
            total_val: float | None = None
            if total_declared and total_declared.lower() not in ("n/a", "暂不评分"):
                try:
                    total_val = round(float(total_declared), 2)
                except ValueError:
                    total_val = None
            if total_val is None and len(scored_dims) >= scoring.MIN_SCORED_DIMENSIONS:
                w_sum = sum(weights.get(did, 0.0) for did in scored_dims)
                if w_sum > 0:
                    total_val = round(sum(scored_dims[did] * weights.get(did, 0.0) for did in scored_dims) / w_sum, 1)
            sec_declared = _parse_declaration(text, ("安全评分",))
            sec_val: float | None = None
            if sec_declared and sec_declared.lower() not in ("n/a", "暂不评分"):
                try:
                    sec_val = round(float(sec_declared), 2)
                except ValueError:
                    sec_val = None
            if sec_val is None and "6" in scored_dims:
                sec_val = scored_dims["6"]
            cov_vals = [d["coverage"] for d in overview.values() if d.get("coverage") is not None]
            avg_cov = round(sum(cov_vals) / len(cov_vals) / 100.0, 3) if cov_vals else None
            scores = {
                "total": total_val,
                "grade": scoring.grade(total_val, checklist),
                "scored_dimensions": len(scored_dims),
                "coverage": avg_cov,
                "not_evaluated": None,
                "security_score": sec_val,
                "security_p0": None,
                "security_p1": None,
            }

    if len(problems) > MAX_PROBLEMS:
        total = len(problems)
        problems = problems[:MAX_PROBLEMS]
        problems.append(f"……共 {total} 条问题，仅列出前 {MAX_PROBLEMS} 条")
    return {
        "problems": problems,
        "scores": scores,
        "items_count": len(items),
        "overview_count": len(overview),
    }
