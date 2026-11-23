"""Independent AI summary adapter and deterministic fallback.

The summary session receives only audit artifacts, never the source checkout.
It may group existing issues into themes, but its output is advisory and is
validated against the issue IDs already produced by the audit pipeline.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from . import harness


SCHEMA_VERSION = 1
SUMMARY_BLOCK_RE = re.compile(
    r"<!--\s*(summary-(?:meta|theme|complete))\s*\n?(.*?)\n?\s*-->",
    re.DOTALL,
)
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class SummaryError(ValueError):
    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


def _scalar(raw: str):
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SummaryError(f"Summary 字段不是合法字符串：{value[:80]}") from exc
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SummaryError(f"Summary 列表不是合法 JSON：{value[:80]}") from exc
        return parsed
    if value == "true":
        return True
    if value == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _fields(body: str) -> dict:
    result = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line.startswith((" ", "\t")):
            raise SummaryError(f"Summary 机器字段不支持该行：{line[:120]}")
        key, raw = line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise SummaryError(f"Summary 字段名不合法：{key!r}")
        if key in result:
            raise SummaryError(f"Summary 字段重复：{key}")
        result[key] = _scalar(raw)
    return result


def _issue_id(issue: dict, index: int) -> str:
    return str(issue.get("issue_id") or issue.get("fingerprint") or f"Q{index:03d}")


def validate(text: str, data: dict) -> dict:
    """Validate summary references without validating or changing audit facts."""
    if not isinstance(text, str) or not text.strip():
        raise SummaryError("Summary 没有返回内容")
    blocks = [(match.group(1), _fields(match.group(2))) for match in SUMMARY_BLOCK_RE.finditer(text)]
    meta = [fields for kind, fields in blocks if kind == "summary-meta"]
    complete = [fields for kind, fields in blocks if kind == "summary-complete"]
    if len(meta) != 1 or len(complete) != 1:
        raise SummaryError("Summary 必须包含唯一的 summary-meta 和 summary-complete 块")
    if meta[0].get("schema_version") != SCHEMA_VERSION or complete[0].get("schema_version") != SCHEMA_VERSION:
        raise SummaryError("Summary schema_version 不受支持")
    source_hash = meta[0].get("source_audit_sha256")
    audit_hash = data.get("meta", {}).get("audit_sha256")
    if audit_hash and source_hash != audit_hash:
        raise SummaryError("Summary 引用的 audit_sha256 与当前审查结果不一致或缺失")
    known = {_issue_id(issue, index) for index, issue in enumerate(data.get("issues", []), 1)}
    themes = []
    seen = set()
    for kind, fields in blocks:
        if kind != "summary-theme":
            continue
        if fields.get("schema_version") != SCHEMA_VERSION:
            raise SummaryError("summary-theme schema_version 不受支持")
        theme_id = fields.get("theme_id")
        if not isinstance(theme_id, str) or not theme_id.strip() or theme_id in seen:
            raise SummaryError(f"Summary theme_id 无效或重复：{theme_id!r}")
        seen.add(theme_id)
        priority = fields.get("priority")
        if priority not in LEVEL_ORDER:
            raise SummaryError(f"Summary theme {theme_id} 的 priority 无效")
        source_ids = fields.get("source_issue_ids", [])
        if not isinstance(source_ids, list) or not all(isinstance(value, str) for value in source_ids):
            raise SummaryError(f"Summary theme {theme_id} 的 source_issue_ids 无效")
        unknown = set(source_ids) - known
        if unknown:
            raise SummaryError(f"Summary 引用了不存在的问题：{sorted(unknown)}")
        themes.append({"theme_id": theme_id, "priority": priority, "source_issue_ids": source_ids})
    return {"schema_version": SCHEMA_VERSION, "themes": themes, "source_issue_ids": sorted(
        {source_id for theme in themes for source_id in theme["source_issue_ids"]}
    )}


def fallback(data: dict, reason: str | None = None) -> str:
    score = data.get("score", {})
    counts = score.get("issue_counts", {})
    issues = sorted(data.get("issues", []), key=lambda issue: (
        LEVEL_ORDER.get(issue.get("level"), 9), str(issue.get("item_id", "")),
    ))
    source_hash = data.get("meta", {}).get("audit_sha256", "")
    lines = [
        "<!-- summary-meta",
        f"schema_version: {SCHEMA_VERSION}",
        f"source_audit_sha256: {json.dumps(source_hash, ensure_ascii=False)}",
        "-->",
        "# 质量总结",
        "",
        f"- 总评分：{score.get('total', '—')}（{score.get('grade', '暂不评分')}）",
        f"- 安全评分：{score.get('security_score', '—')}",
        f"- 风险问题：P0 {counts.get('P0', 0)} / P1 {counts.get('P1', 0)} / "
        f"P2 {counts.get('P2', 0)} / P3 {counts.get('P3', 0)}",
    ]
    if reason:
        lines.append(f"- AI 总结未启用或不可用，以下为程序生成的确定性摘要：{reason}")
    lines += ["", "## 优先风险主题", ""]
    if not issues:
        lines.append("- 当前没有已验证的 WARN / FAIL 问题。")
    for index, issue in enumerate(issues[:8], 1):
        issue_id = _issue_id(issue, index)
        lines += [
            f"<!-- summary-theme",
            f"schema_version: {SCHEMA_VERSION}",
            f"theme_id: T{index:03d}",
            f"priority: {issue.get('level', 'P3')}",
            f"source_issue_ids: {json.dumps([issue_id], ensure_ascii=False)}",
            "-->",
            f"- {issue_id}（{issue.get('level', 'P3')}）：{issue.get('title', '未命名问题')}",
        ]
    lines += ["", f"<!-- summary-complete", f"schema_version: {SCHEMA_VERSION}", "-->", ""]
    return "\n".join(lines)


def _prompt() -> str:
    return """你是独立的代码质量管理总结 Agent。你只可以读取 /workspace/repo/audit.json 和
/workspace/repo/audit.md（如果存在），不能读取任何源码、仓库目录或外部资料。

请把已验证的检查结果归纳成管理层摘要、系统性风险主题和整改优先级。禁止重新判断检查项，
禁止修改结论、风险等级、证据、评分，禁止新增不存在的问题。每个主题必须使用独立的
summary-theme HTML 注释块，并在 source_issue_ids 中只引用 audit.json 已存在的 issue_id。
最终输出 Markdown，并包含唯一的 summary-meta、summary-theme（可多个）和 summary-complete 块：

<!-- summary-meta
schema_version: 1
source_audit_sha256: "从 audit.json 的 meta.audit_sha256 原样填写"
-->

<!-- summary-theme
schema_version: 1
theme_id: T001
priority: P1
source_issue_ids: ["Q001"]
-->

<!-- summary-complete
schema_version: 1
-->
"""


def generate(data: dict, artifact_dir: Path, settings: dict, timeout: int) -> dict:
    """Generate an independent summary or a deterministic fallback."""
    config = settings.get("summary", {})
    fallback_text = fallback(data)
    if not config.get("enabled", False):
        return {
            "text": fallback_text,
            "status": "disabled",
            "agent_status": "not_run",
            "metadata": {"schema_version": SCHEMA_VERSION, "themes": []},
            "event_log": [],
        }

    summary_agent = copy.deepcopy(config.get("agent") or settings.get("agent", {}))
    summary_agent["strategy"] = "single"
    summary_settings = {"agent": summary_agent}
    try:
        with tempfile.TemporaryDirectory(prefix="ai-audit-summary-") as temp:
            context = Path(temp)
            (context / "audit.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            source_markdown = artifact_dir / "audit.md"
            if source_markdown.is_file():
                shutil.copyfile(source_markdown, context / "audit.md")
            out = harness.run(_prompt(), context, context, summary_settings,
                              int(config.get("timeout_sec", timeout)))
        metadata = validate(out["raw_text"], data)
        return {
            "text": out["raw_text"],
            "status": "verified",
            "agent_status": out.get("agent_status", "completed"),
            "metadata": metadata,
            "event_log": out.get("event_log", []),
            "stats": out.get("stats"),
            "session_id": out.get("session_id"),
            "agent_model": out.get("agent_model"),
            "raw_sha256": hashlib.sha256(out["raw_text"].encode("utf-8")).hexdigest(),
        }
    except Exception as exc:  # Summary 失败不能污染已验证的审查结果
        return {
            "text": fallback(data, str(exc)[:300]),
            "status": "fallback",
            "agent_status": getattr(exc, "agent_status", "failed"),
            "metadata": {"schema_version": SCHEMA_VERSION, "themes": [], "error": str(exc)[:500]},
            "event_log": getattr(exc, "event_log", []),
            "error": str(exc)[:500],
        }
