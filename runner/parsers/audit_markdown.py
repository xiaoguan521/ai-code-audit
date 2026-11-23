"""Parse the versioned Markdown contract emitted by a Harness agent.

The report remains human-readable Markdown. Small HTML-comment blocks carry
only the fields that the runner must validate. The parser accepts a strict,
dependency-free YAML subset and never evaluates YAML constructors or tags.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = 1
PARSER_VERSION = "1"
RESULTS = {
    "PASS", "WARN", "FAIL", "N/A",
    "NEED_INFO-运行数据", "NEED_INFO-补充材料", "NEED_INFO-证据不足",
}
RISKS = {"P0", "P1", "P2", "P3", "none"}
LEVELS = {"P0", "P1", "P2", "P3"}
EFFORTS = {"S", "M", "L"}
BLOCK_RE = re.compile(
    r"<!--\s*(audit-(?:meta|item|issue|dimension|need-info|complete))\s*\n?(.*?)\n?\s*-->",
    re.DOTALL,
)


class AuditMarkdownError(ValueError):
    """The model output cannot be projected into the audit result."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


@dataclass(frozen=True)
class ParsedAudit:
    result: dict
    metadata: dict
    raw_sha256: str


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise AuditMarkdownError(f"无效的双引号字符串：{value[:80]}") from exc
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AuditMarkdownError(f"列表字段必须使用 JSON 列表语法：{value[:80]}") from exc
        if not isinstance(parsed, list):
            raise AuditMarkdownError("列表字段不是数组")
        return parsed
    return value


def _key_value(line: str) -> tuple[str, Any]:
    if ":" not in line:
        raise AuditMarkdownError(f"机器字段缺少冒号：{line[:120]}")
    key, raw = line.split(":", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
        raise AuditMarkdownError(f"机器字段名不合法：{key!r}")
    return key, _scalar(raw)


def _mapping(body: str) -> dict:
    """Parse root scalars and one-level lists of mappings."""
    data: dict[str, Any] = {}
    list_key: str | None = None
    current_item: dict | None = None
    for raw_line in body.replace("\t", "    ").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            key, value = _key_value(line)
            if key in data:
                raise AuditMarkdownError(f"机器字段重复：{key}")
            data[key] = [] if value == "" else value
            list_key = key if value == "" else None
            current_item = None
            continue
        if indent == 2 and line.startswith("- "):
            if list_key is None or not isinstance(data.get(list_key), list):
                raise AuditMarkdownError(f"列表项没有父字段：{line[:120]}")
            entry = line[2:].strip()
            if ":" in entry:
                key, value = _key_value(entry)
                current_item = {key: value}
                data[list_key].append(current_item)
            else:
                current_item = None
                data[list_key].append(_scalar(entry))
            continue
        if indent >= 4 and current_item is not None:
            key, value = _key_value(line)
            if key in current_item:
                raise AuditMarkdownError(f"列表对象字段重复：{key}")
            current_item[key] = value
            continue
        raise AuditMarkdownError(f"机器字段缩进或嵌套层级不受支持：{raw_line[:120]}")
    return data


def _require_version(fields: dict, label: str) -> None:
    if fields.get("schema_version") != SCHEMA_VERSION:
        raise AuditMarkdownError(f"{label} 缺少 schema_version: {SCHEMA_VERSION}")


def _text(fields: dict, name: str, label: str, required: bool = True) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or (required and not value.strip()):
        raise AuditMarkdownError(f"{label} 的 {name} 必须是非空字符串")
    return value


def _evidence(fields: dict, label: str) -> list[dict]:
    raw = fields.get("evidence", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AuditMarkdownError(f"{label} 的 evidence 必须是列表")
    refs = []
    for index, ref in enumerate(raw, 1):
        if not isinstance(ref, dict):
            raise AuditMarkdownError(f"{label} 的 evidence[{index}] 必须是对象")
        path = ref.get("path")
        start = ref.get("start_line")
        end = ref.get("end_line", start)
        if not isinstance(path, str) or not path.strip():
            raise AuditMarkdownError(f"{label} 的 evidence[{index}].path 无效")
        if not isinstance(start, int) or isinstance(start, bool) or start < 1:
            raise AuditMarkdownError(f"{label} 的 evidence[{index}].start_line 无效")
        if not isinstance(end, int) or isinstance(end, bool) or end < start:
            raise AuditMarkdownError(f"{label} 的 evidence[{index}].end_line 无效")
        item = {"path": path, "start_line": start, "end_line": end}
        if "snippet" in ref:
            if not isinstance(ref["snippet"], str) or not ref["snippet"]:
                raise AuditMarkdownError(f"{label} 的 evidence[{index}].snippet 无效")
            item["snippet"] = ref["snippet"]
        refs.append(item)
    return refs


def _default_inventory() -> dict:
    return {
        "languages": [], "frameworks": [], "build_tools": [], "file_count": 0,
        "loc_estimate": 0, "modules": [], "checked_scope": "由 runner 确定",
        "unchecked": [],
    }


def _dimension_notes(checklist: dict, blocks: list[dict]) -> list[dict]:
    notes = {str(block.get("dimension")): block for block in blocks}
    result = []
    for dimension in checklist.get("dimensions", []):
        did = str(dimension["id"])
        block = notes.get(did)
        result.append({
            "dimension": did,
            "conclusion": str(block.get("conclusion", "")) if block else "未提供维度摘要",
            "key_analysis": str(block.get("key_analysis", "")) if block else "",
        })
    return result


def parse(text: str, checklist: dict) -> ParsedAudit:
    if not isinstance(text, str) or not text.strip():
        raise AuditMarkdownError("Harness 没有返回 Markdown")
    blocks = []
    for match in BLOCK_RE.finditer(text):
        kind = match.group(1)
        fields = _mapping(match.group(2)) if match.group(2).strip() else {}
        blocks.append((kind, fields))
    if not blocks:
        raise AuditMarkdownError("Markdown 中没有 audit-* 机器字段块")

    meta_blocks = [fields for kind, fields in blocks if kind == "audit-meta"]
    complete_blocks = [fields for kind, fields in blocks if kind == "audit-complete"]
    if len(meta_blocks) != 1:
        raise AuditMarkdownError("必须有且只能有一个 audit-meta 块")
    if len(complete_blocks) != 1:
        raise AuditMarkdownError("必须有且只能有一个 audit-complete 块")
    _require_version(meta_blocks[0], "audit-meta")
    _require_version(complete_blocks[0], "audit-complete")

    expected = {item["id"] for item in checklist.get("items", [])}
    items: list[dict] = []
    seen_items: set[str] = set()
    issues: list[dict] = []
    seen_issues: set[str] = set()
    dimension_blocks: list[dict] = []
    need_info_blocks: list[dict] = []
    for kind, fields in blocks:
        if kind == "audit-item":
            _require_version(fields, "audit-item")
            item_id = _text(fields, "id", "audit-item")
            if item_id in seen_items:
                raise AuditMarkdownError(f"检查项重复：{item_id}")
            seen_items.add(item_id)
            result = _text(fields, "result", f"audit-item {item_id}")
            risk = fields.get("risk", "none")
            if result not in RESULTS:
                raise AuditMarkdownError(f"{item_id} 的 result 不合法：{result}")
            if risk not in RISKS:
                raise AuditMarkdownError(f"{item_id} 的 risk 不合法：{risk}")
            item = {
                "id": item_id, "result": result,
                "evidence": _evidence(fields, f"audit-item {item_id}"),
                "risk": risk, "conclusion": str(fields.get("conclusion", "")),
                "carried_over": bool(fields.get("carried_over", False)),
            }
            for optional in ("audience", "conditional", "materials_cited", "change_reason"):
                if optional in fields:
                    item[optional] = fields[optional]
            items.append(item)
        elif kind == "audit-issue":
            _require_version(fields, "audit-issue")
            issue_id = fields.get("issue_id", fields.get("id"))
            if not isinstance(issue_id, str) or not issue_id.strip():
                raise AuditMarkdownError("audit-issue 缺少 issue_id")
            if issue_id in seen_issues:
                raise AuditMarkdownError(f"问题 ID 重复：{issue_id}")
            seen_issues.add(issue_id)
            item_id = _text(fields, "item_id", f"audit-issue {issue_id}")
            level = _text(fields, "level", f"audit-issue {issue_id}")
            if level not in LEVELS:
                raise AuditMarkdownError(f"{issue_id} 的 level 不合法：{level}")
            effort = fields.get("effort")
            if effort not in EFFORTS:
                raise AuditMarkdownError(f"{issue_id} 的 effort 不合法：{effort}")
            issues.append({
                "issue_id": issue_id, "item_id": item_id, "level": level,
                "title": _text(fields, "title", f"audit-issue {issue_id}"),
                "evidence": _evidence(fields, f"audit-issue {issue_id}"),
                "impact": _text(fields, "impact", f"audit-issue {issue_id}"),
                "suggestion": _text(fields, "suggestion", f"audit-issue {issue_id}"),
                "effort": effort,
                **({"depends_on": fields["depends_on"]} if "depends_on" in fields else {}),
            })
        elif kind == "audit-dimension":
            _require_version(fields, "audit-dimension")
            did = _text(fields, "dimension", "audit-dimension")
            if did not in {str(d["id"]) for d in checklist.get("dimensions", [])}:
                raise AuditMarkdownError(f"未知维度：{did}")
            dimension_blocks.append(fields)
        elif kind == "audit-need-info":
            _require_version(fields, "audit-need-info")
            raw_items = fields.get("items")
            if not isinstance(raw_items, list) or not raw_items or not all(isinstance(x, str) for x in raw_items):
                raise AuditMarkdownError("audit-need-info.items 必须是非空字符串列表")
            need_info_blocks.append({
                "items": raw_items,
                "missing": _text(fields, "missing", "audit-need-info"),
                "why": _text(fields, "why", "audit-need-info"),
                "form": _text(fields, "form", "audit-need-info"),
                "unlocks": _text(fields, "unlocks", "audit-need-info"),
                "priority": _text(fields, "priority", "audit-need-info"),
            })

    missing = expected - seen_items
    extra = seen_items - expected
    if missing or extra:
        problems = []
        if missing:
            problems.append(f"缺少检查项: {sorted(missing)}")
        if extra:
            problems.append(f"多余检查项: {sorted(extra)}")
        raise AuditMarkdownError("Markdown 检查项覆盖不完整", problems)
    complete_count = complete_blocks[0].get("item_count")
    if complete_count is not None and complete_count != len(items):
        raise AuditMarkdownError(f"audit-complete.item_count={complete_count}，实际为 {len(items)}")

    result = {
        "inventory": _default_inventory(),
        "items": items,
        "issues": issues,
        "need_info_list": need_info_blocks,
        "dimension_notes": _dimension_notes(checklist, dimension_blocks),
        "summary": {"key_findings": [], "top_risks": [], "undetermined": ""},
        "self_check": [{"check": "audit-complete 标记", "ok": True}],
    }
    if "prompt_injection_suspects" in meta_blocks[0]:
        result["prompt_injection_suspects"] = meta_blocks[0]["prompt_injection_suspects"]
    metadata = dict(meta_blocks[0])
    metadata.update({
        "schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
        "item_count": len(items), "issue_count": len(issues), "complete": True,
    })
    return ParsedAudit(
        result=result,
        metadata=metadata,
        raw_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def output_instructions() -> str:
    """Prompt fragment for the Harness task and structural retry."""
    return """最终只返回 Markdown 审查报告，不要返回 JSON。报告必须包含：
1. 一个 audit-meta HTML 注释块，schema_version 必须为 1；
2. 每个检查项一个 audit-item HTML 注释块，至少包括 schema_version、id、result、risk、evidence；
3. 每个问题一个 audit-issue HTML 注释块，必须有独立 issue_id（如 Q001）和 item_id；
4. 一个 audit-complete HTML 注释块，schema_version 为 1，item_count 为 64；
5. evidence 使用 path、start_line、end_line，不要使用合并的 path:line 字符串。

机器字段块之外可以使用自然语言、表格和代码块，但不得省略 64 个 audit-item 块。"""
