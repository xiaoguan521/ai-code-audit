"""从仓库和预扫描报告生成确定性事实与 inventory。

接口只返回事实，不替模型做需要语义判断的结论。事实会写入上下文供模型核对，
inventory 由 runner 在最终结果中覆盖，避免模型反复用 ls/find 盘点项目。
"""
from __future__ import annotations

import json
import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath


MAX_FILE_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".html", ".java", ".js", ".jsx", ".json",
    ".kt", ".md", ".php", ".py", ".rb", ".rs", ".scss", ".sql", ".swift", ".toml",
    ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
}
TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX)\b|未实现|not\s+implemented", re.IGNORECASE)
EMPTY_EXCEPTION_RE = re.compile(r"(?:catch\s*\([^)]*\)\s*\{\s*\}|except(?:\s+[^:]+)?\s*:\s*(?:pass\b|#))", re.IGNORECASE)
WEAK_CRYPTO_RE = re.compile(
    r"\b(?:md5|sha1|des|desede|rc4|ecb|Math\.random\s*\(|random\.random\s*\()\b",
    re.IGNORECASE,
)
HEALTH_RE = re.compile(r"(?:/health(?:z|iness)?\b|readiness|liveness|health_check|healthcheck)", re.IGNORECASE)
METRICS_RE = re.compile(r"(?:prometheus|opentelemetry|micrometer|statsd|metrics?\s*[:=(])", re.IGNORECASE)
LOGGING_RE = re.compile(r"(?:structlog|loguru|zap\.Logger|logrus|pino|winston|logback|log4j|slog\.Logger)", re.IGNORECASE)
PROMPT_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all|any|the|previous)\s+(?:instructions?|rules?|审查)|skip\s+audit|"
    r"give\s+(?:all|full)\s+(?:points?|score)|忽略(?:以上|之前|所有).{0,30}(?:规则|指令|审查)|"
    r"(?:给|输出).{0,12}(?:满分|全部通过))",
    re.IGNORECASE,
)


def _tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, timeout=120)
    if result.returncode:
        # 单元测试和人工试扫可能给一个未初始化 Git 的临时目录；退化为有界文件清单，
        # 正式扫描仍优先使用 Git 跟踪清单。
        files = []
        for path in repo.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                files.append(path.relative_to(repo).as_posix())
        return sorted(files)
    return [part.decode("utf-8", "surrogateescape") for part in result.stdout.split(b"\0") if part]


def _read_lines(repo: Path, relative: str) -> list[str] | None:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        return None
    if any(part == ".env" or part.startswith(".env.") for part in path.parts):
        return None
    actual = (repo / relative).resolve()
    try:
        actual.relative_to(repo.resolve())
        if not actual.is_file() or actual.stat().st_size > MAX_FILE_BYTES:
            return None
        if actual.suffix.lower() not in TEXT_SUFFIXES:
            return None
        return actual.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError, ValueError):
        return None


def _ref(path: str, line: int) -> dict:
    return {"path": PurePosixPath(path).as_posix(), "start_line": max(1, line)}


def _normalize_path(path: str) -> str:
    raw = str(path).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    normalized = PurePosixPath(raw).as_posix()
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def _excluded(path: str, patterns: list[str]) -> bool:
    parts = PurePosixPath(path).parts
    for raw in patterns:
        pattern = str(raw).strip().strip("/")
        if not pattern:
            continue
        if "/" not in pattern and any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
        if fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + "/"):
            return True
    return False


def _finding(item_id: str, result: str, risk: str, conclusion: str, evidence: list[dict],
             title: str | None = None) -> dict:
    return {
        "item_id": item_id,
        "result": result,
        "risk": risk,
        "conclusion": conclusion,
        "evidence": evidence,
        "title": title or conclusion,
    }


def _read_prescan(prescan_dir: Path | None, name: str):
    if not prescan_dir:
        return None
    path = prescan_dir / name
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def collect(repo: Path, commit: str, code_map_data: dict | None = None,
            scope_out: list[str] | None = None, prescan_dir: Path | None = None,
            full_inventory: bool = True, files: list[str] | None = None,
            previous_inventory: dict | None = None) -> dict:
    """收集 inventory、静态信号和预扫描摘要。

    ``findings`` 是高置信候选，不直接替换模型结论；调用方可以将其作为事实文件
    提供给模型，并在后续规则引擎中逐项接管。
    """
    scope_out = list(scope_out or [])
    tracked = _tracked_files(repo)
    data = code_map_data or {}
    counts = data.get("counts", {})
    languages = sorted(data.get("language_file_counts", {}))
    top_counts = data.get("top_level_file_counts", {})
    manifests = list(data.get("manifests", []))
    lockfiles = list(data.get("lockfiles", []))
    entrypoints = list(data.get("entrypoints", []))
    tests = list(data.get("tests", []))
    ci = list(data.get("ci", []))
    unchecked = [{"path": pattern, "reason": "配置的排除范围"} for pattern in scope_out]

    # 增量模式的事实扫描只读取变更文件；inventory 仍从 Git 清单得到，避免
    # 为了几个语义线索把整个仓库重新读一遍。
    if files is None:
        inspected = list(tracked)
    else:
        changed = {_normalize_path(path) for path in files}
        inspected = [path for path in tracked if path in changed]
    inspected = [path for path in inspected
                 if path and not _excluded(path, scope_out)
                 and not any(part == ".env" or part.startswith(".env.")
                             for part in PurePosixPath(path).parts)]

    cloc = _read_prescan(prescan_dir, "cloc.json")
    loc_estimate = 0
    if full_inventory and isinstance(cloc, dict):
        total = cloc.get("SUM", {})
        loc_estimate = int(total.get("code", total.get("lines", 0)) or 0)
    elif not full_inventory and previous_inventory:
        # 增量报告沿用上次全量 inventory 的总行数；下一次 full 扫描会刷新它。
        try:
            loc_estimate = max(0, int(previous_inventory.get("loc_estimate", 0)))
        except (AttributeError, TypeError, ValueError):
            loc_estimate = 0
    if not loc_estimate:
        # 没有 cloc 时只对本次扫描范围内的小文本文件计数；不要在增量模式
        # 退化成全仓库读取。
        for relative in inspected:
            lines = _read_lines(repo, relative)
            if lines is not None:
                loc_estimate += len(lines)

    inventory = {
        "languages": languages,
        "frameworks": [],
        "build_tools": sorted({PurePosixPath(path).name for path in manifests}),
        "file_count": int(data.get("tracked_files", len(tracked))),
        "inspected_files": len(inspected),
        "scan_scope": "repository" if files is None else "changed_files",
        "loc_estimate": loc_estimate,
        "modules": [{"path": path, "role": "顶层模块/目录", "files": count}
                    for path, count in sorted(top_counts.items())],
        "entrypoints": entrypoints,
        "has_tests": bool(tests),
        "has_ci": bool(ci),
        "lockfiles": lockfiles,
        "suspected_cycles": [],
        "checked_scope": "Git 跟踪文件；源码证据由模型回仓库核实",
        "unchecked": unchecked,
    }

    findings: list[dict] = []
    signals = {
        "tracked_files": len(tracked),
        "inspected_files": len(inspected),
        "scan_scope": "repository" if files is None else "changed_files",
        "readme_files": [],
        "todo_matches": [],
        "empty_exception_matches": [],
        "weak_crypto_matches": [],
        "health_signals": [],
        "metrics_signals": [],
        "logging_signals": [],
        "prompt_injection_matches": [],
        "lint_configs": [],
        "backup_files": [],
        "gitleaks_findings": 0,
        "semgrep_findings": 0,
        "lizard_findings": 0,
    }

    for relative in inspected:
        lower = relative.lower()
        name = PurePosixPath(lower).name
        if name.startswith("readme"):
            signals["readme_files"].append(relative)
        if name in {".eslintrc", ".eslintrc.js", ".eslintrc.json", ".pylintrc", ".flake8",
                    "ruff.toml", "biome.json", "tslint.json", ".golangci.yml", ".golangci.yaml"}:
            signals["lint_configs"].append(relative)
        if any(token in name for token in ("backup", "restore", "snapshot")):
            signals["backup_files"].append(relative)
        lines = _read_lines(repo, relative)
        if lines is None:
            continue
        for number, line in enumerate(lines, 1):
            if TODO_RE.search(line) and len(signals["todo_matches"]) < 100:
                signals["todo_matches"].append({"path": relative, "line": number, "text": line[:240]})
            next_line = lines[number] if number < len(lines) else ""
            empty_exception = EMPTY_EXCEPTION_RE.search(line) or (
                line.strip().lower().startswith("except") and next_line.strip().lower().startswith("pass")
            )
            if empty_exception and len(signals["empty_exception_matches"]) < 100:
                signals["empty_exception_matches"].append({"path": relative, "line": number, "text": line[:240]})
            if WEAK_CRYPTO_RE.search(line) and len(signals["weak_crypto_matches"]) < 100:
                signals["weak_crypto_matches"].append({"path": relative, "line": number, "text": line[:240]})
            if HEALTH_RE.search(line) and len(signals["health_signals"]) < 50:
                signals["health_signals"].append({"path": relative, "line": number})
            if METRICS_RE.search(line) and len(signals["metrics_signals"]) < 50:
                signals["metrics_signals"].append({"path": relative, "line": number})
            if LOGGING_RE.search(line) and len(signals["logging_signals"]) < 50:
                signals["logging_signals"].append({"path": relative, "line": number})
            if PROMPT_INJECTION_RE.search(line) and len(signals["prompt_injection_matches"]) < 50:
                signals["prompt_injection_matches"].append({"path": relative, "line": number})

    if signals["todo_matches"]:
        first = signals["todo_matches"][:5]
        findings.append(_finding("1.5", "WARN", "P2", f"发现 {len(signals['todo_matches'])} 处 TODO/FIXME/未实现线索",
                                 [_ref(x["path"], x["line"]) for x in first], "未实现或死代码线索"))
    if signals["empty_exception_matches"]:
        first = signals["empty_exception_matches"][:5]
        findings.append(_finding("3.1", "WARN", "P2", "发现空异常处理或直接吞异常线索",
                                 [_ref(x["path"], x["line"]) for x in first], "空异常处理线索"))
    if signals["weak_crypto_matches"]:
        first = signals["weak_crypto_matches"][:5]
        findings.append(_finding("6.6", "WARN", "P1", "发现弱哈希、弱加密或非安全随机数线索",
                                 [_ref(x["path"], x["line"]) for x in first], "密码学风险线索"))
    if signals["health_signals"]:
        first = signals["health_signals"][:3]
        findings.append(_finding("4.7", "PASS", "none", "发现健康检查或存活探针相关配置/代码",
                                 [_ref(x["path"], x["line"]) for x in first], "健康检查信号"))
    if signals["metrics_signals"]:
        first = signals["metrics_signals"][:3]
        findings.append(_finding("2.8", "PASS", "none", "发现指标或性能观测相关信号",
                                 [_ref(x["path"], x["line"]) for x in first], "性能可观测性信号"))
    if signals["logging_signals"]:
        first = signals["logging_signals"][:3]
        findings.append(_finding("5.6", "PASS", "none", "发现结构化日志或日志框架相关信号",
                                 [_ref(x["path"], x["line"]) for x in first], "结构化日志信号"))
    if signals["backup_files"]:
        findings.append(_finding("3.6", "PASS", "none", "发现备份/恢复脚本或文件命名信号",
                                 [_ref(signals["backup_files"][0], 1)], "备份恢复信号"))
    if signals["lint_configs"]:
        findings.append(_finding("8.3", "PASS", "none", "发现静态检查或格式化配置",
                                 [_ref(signals["lint_configs"][0], 1)], "代码风格配置"))
    if signals["prompt_injection_matches"]:
        first = signals["prompt_injection_matches"][:5]
        findings.append(_finding("6.8", "WARN", "P1", "发现疑似要求审查助手改变规则或结论的仓库文字",
                                 [_ref(x["path"], x["line"]) for x in first], "疑似提示注入"))

    gitleaks = _read_prescan(prescan_dir, "gitleaks.json")
    if isinstance(gitleaks, list):
        signals["gitleaks_findings"] = len(gitleaks)
        for finding in gitleaks[:5]:
            path = finding.get("File") or finding.get("file")
            line = finding.get("StartLine") or finding.get("startLine") or 1
            if isinstance(path, str) and path:
                findings.append(_finding("6.4", "FAIL", "P1", "gitleaks 发现疑似敏感信息，需回源码核实",
                                         [_ref(path, int(line) if str(line).isdigit() else 1)], "敏感信息扫描线索"))
    semgrep = _read_prescan(prescan_dir, "semgrep.json")
    if isinstance(semgrep, dict) and isinstance(semgrep.get("results"), list):
        signals["semgrep_findings"] = len(semgrep["results"])
    lizard_path = prescan_dir / "lizard_warnings.txt" if prescan_dir else None
    if lizard_path and lizard_path.is_file():
        try:
            lizard_lines = lizard_path.read_text(encoding="utf-8", errors="replace").splitlines()
            signals["lizard_findings"] = sum(": warning:" in line for line in lizard_lines)
        except OSError:
            pass
        if signals["lizard_findings"]:
            refs = []
            for line in lizard_lines:
                match = re.match(r"([^:]+):(\d+): warning:", line)
                if match:
                    refs.append(_ref(match.group(1), int(match.group(2))))
            if refs:
                findings.append(_finding("8.2", "WARN", "P2", "lizard 发现圈复杂度或函数规模告警",
                                         refs[:5], "复杂度扫描线索"))

    suspects = [f"{match['path']}:{match['line']}" for match in signals["prompt_injection_matches"]]
    return {"commit": commit, "inventory": inventory, "signals": signals, "findings": findings,
            "prompt_injection_suspects": suspects}


def apply_findings(result: dict, findings: list[dict], allowed_item_ids: set[str] | frozenset[str] | None = None) -> int:
    """把高置信确定性发现合并到模型结果，返回被覆盖的检查项数。

    只会上调风险（PASS → WARN → FAIL），不会把模型的更严重结论降级；增量模式
    可通过 ``allowed_item_ids`` 限制只修改本次计划重判的项。
    """
    items = {item.get("id"): item for item in result.get("items", [])}
    issues = result.setdefault("issues", [])
    changed: set[str] = set()
    rank = {"N/A": -1, "NEED_INFO-运行数据": 0, "NEED_INFO-补充材料": 0,
            "NEED_INFO-证据不足": 0, "PASS": 1, "WARN": 2, "FAIL": 3}
    for finding in findings:
        item_id = finding.get("item_id")
        if not item_id or item_id not in items or (allowed_item_ids is not None and item_id not in allowed_item_ids):
            continue
        item = items[item_id]
        desired = finding.get("result", "")
        current = item.get("result", "NEED_INFO-证据不足")
        if desired not in {"PASS", "WARN", "FAIL"} or rank.get(desired, 0) < rank.get(current, 0):
            continue
        before = (item.get("result"), item.get("risk"), json.dumps(item.get("evidence", []), sort_keys=True))
        item["result"] = desired
        item["risk"] = finding.get("risk", item.get("risk", "none")) if desired != "PASS" else item.get("risk", "none")
        item["conclusion"] = finding.get("conclusion", item.get("conclusion", ""))
        item["conditional"] = False
        item.pop("need_info", None)
        refs = []
        seen_refs = set()
        for ref in [*item.get("evidence", []), *finding.get("evidence", [])]:
            marker = json.dumps(ref, ensure_ascii=False, sort_keys=True)
            if marker not in seen_refs:
                seen_refs.add(marker)
                refs.append(ref)
        item["evidence"] = refs
        item["carried_over"] = False
        # 事实把 NEED_INFO 提升为可判定结论时，旧的待补充分组也必须同步移除，
        # 否则报告会同时宣称“已判定”和“仍待补充”。
        if desired in {"PASS", "WARN", "FAIL"}:
            groups = []
            for group in result.get("need_info_list", []):
                selected = [value for value in group.get("items", []) if value != item_id]
                if selected:
                    copy_group = dict(group)
                    copy_group["items"] = selected
                    groups.append(copy_group)
            result["need_info_list"] = groups
        if desired in {"WARN", "FAIL"}:
            existing = next((issue for issue in issues if issue.get("item_id") == item_id), None)
            if existing is None:
                issues.append({
                    "item_id": item_id,
                    "level": finding.get("risk", "P2"),
                    "title": finding.get("title", finding.get("conclusion", "确定性扫描发现")),
                    "evidence": finding.get("evidence", []),
                    "impact": finding.get("conclusion", "确定性扫描发现风险"),
                    "suggestion": "回到源码核实该扫描发现并修复或补充误报依据",
                    "effort": "M",
                })
        after = (item.get("result"), item.get("risk"), json.dumps(item.get("evidence", []), sort_keys=True))
        if before != after:
            changed.add(item_id)
    return len(changed)
