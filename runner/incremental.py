"""Git 变更到检查项的保守影响规划。

这个模块是增量扫描的接口：调用方只需要提供检查清单、上次结果、变更文件和
补充材料是否变化，就能得到本次必须重判的检查项及原因。映射故意偏保守；无法
确认影响范围时扩大重判集合，绝不把未知变更误判成可沿用。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


SECURITY_ITEMS = frozenset(f"6.{number}" for number in range(1, 10))
DOCUMENTATION_ITEMS = frozenset({"5.3", "5.4", "8.9"})
CI_ITEMS = frozenset({"4.6", "4.7", "6.7", "7.1", "7.8", "8.8"})
DEPENDENCY_ITEMS = frozenset({"4.6", "6.4", "6.7", "7.2", "7.8", "8.8"})
CONFIG_ITEMS = frozenset({
    "1.6", "2.4", "4.5", "5.5", "6.2", "6.3", "6.4", "6.8",
    "7.1", "7.3", "7.5", "7.6", "7.8", "8.7", "8.8",
})
TEST_ITEMS = frozenset({"1.7", "3.7", "8.6"})
FRONTEND_ITEMS = frozenset({"2.7", "5.1", "5.2", "5.7", "7.4"})
GENERIC_SOURCE_ITEMS = frozenset({
    "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "2.1", "2.4", "2.5", "2.8",
    "3.1", "3.2", "3.3", "3.5", "4.1", "4.2", "4.3", "4.4",
    "6.1", "6.2", "6.3", "6.4", "6.5", "6.8", "6.9",
    "7.1", "7.3", "7.5", "7.6", "8.1", "8.2", "8.3", "8.5", "8.6", "8.7",
})

LOCK_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "pipfile.lock", "uv.lock", "go.sum", "cargo.lock",
    "gemfile.lock", "composer.lock",
})
MANIFEST_NAMES = frozenset({
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json",
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "go.mod", "cargo.toml",
    "gemfile", "composer.json", "dockerfile", "docker-compose.yml", "compose.yaml",
})
SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".java", ".js", ".jsx", ".kt",
    ".php", ".py", ".rb", ".rs", ".swift", ".ts", ".tsx", ".vue",
})
DOC_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})
CONFIG_SUFFIXES = frozenset({".conf", ".env", ".ini", ".json", ".properties", ".toml", ".xml", ".yaml", ".yml"})


@dataclass(frozen=True)
class IncrementalPlan:
    """增量规划结果；这是 runner 与模型分段调用之间的唯一接口。"""

    changed_files: tuple[str, ...]
    affected_items: frozenset[str]
    reasons: dict[str, tuple[str, ...]]
    full_scan: bool = False

    @property
    def carried_items(self) -> frozenset[str]:
        return frozenset(set(self.reasons) - set(self.affected_items))

    @property
    def affected_dimensions(self) -> frozenset[str]:
        return frozenset(item.split(".", 1)[0] for item in self.affected_items)

    def reason_text(self, item_id: str) -> str:
        return "；".join(self.reasons.get(item_id, ()))


def normalize_path(path: str) -> str:
    """把 Git 路径固定成 POSIX 相对路径，拒绝越出仓库的路径。"""
    raw = str(path).replace("\\", "/")
    if raw.startswith("/workspace/repo/"):
        raw = raw[len("/workspace/repo/"):]
    # 只移除显式的相对路径前缀；不能用 lstrip("./")，否则 .github/、.env
    # 这类合法文件名会被错误改写成 github/、env。
    while raw.startswith("./"):
        raw = raw[2:]
    normalized = PurePosixPath(raw).as_posix()
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def classify(path: str) -> set[str]:
    """为路径生成多个保守类别；一个文件可以同时属于配置、依赖和 CI。"""
    path = normalize_path(path)
    if not path:
        return {"unknown"}
    lower = path.lower()
    name = PurePosixPath(lower).name
    parts = set(PurePosixPath(lower).parts)
    categories: set[str] = set()

    if (name.startswith("readme") or name.startswith("changelog") or name.startswith("license") or
            PurePosixPath(lower).suffix in DOC_SUFFIXES or lower.startswith(("docs/", "doc/")) or
            "/docs/" in lower):
        categories.add("documentation")
    if (lower.startswith((".github/workflows/", ".circleci/", ".buildkite/")) or
            name in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"}):
        categories.add("ci")
    if name in LOCK_NAMES or name.endswith(".lock") or name in MANIFEST_NAMES or name.startswith("requirements"):
        categories.add("dependency")
    if (PurePosixPath(lower).suffix in CONFIG_SUFFIXES or lower.startswith(("config/", "conf/")) or
            name in {".editorconfig", ".eslintrc", ".prettierrc", "tsconfig.json"}):
        categories.add("config")
    if any(part in {"test", "tests", "__tests__", "spec", "specs", "fixtures"} for part in parts):
        categories.add("tests")
    if (PurePosixPath(lower).suffix in {".css", ".scss", ".less", ".html", ".htm", ".jsx", ".tsx", ".vue"} or
            any(part in {"frontend", "front", "web", "ui", "client", "pages", "components"} for part in parts)):
        categories.add("frontend")
    if PurePosixPath(lower).suffix in SOURCE_SUFFIXES:
        categories.add("source")
    if not categories:
        categories.add("unknown")
    return categories


def _add(reasons: dict[str, list[str]], item_ids: Iterable[str], reason: str) -> None:
    for item_id in item_ids:
        reasons.setdefault(item_id, []).append(reason)


def plan(checklist: dict, previous: dict | None, changes: Iterable[str] | None,
         materials_changed: bool = False, full_threshold: float = 0.6) -> IncrementalPlan:
    """根据变更规划受影响检查项。

    证据文件命中、材料变化和路径类别都能扩大重判集合；超过阈值时返回
    ``full_scan=True``，由上层直接走全量流程。
    """
    item_ids = tuple(item["id"] for item in checklist.get("items", []))
    reasons: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    normalized = tuple(dict.fromkeys(path for path in (normalize_path(p) for p in (changes or [])) if path))
    affected: set[str] = set()

    if materials_changed:
        affected.update(item_ids)
        for item_id in item_ids:
            reasons[item_id].append("补充材料发生变化")

    previous_items = {item.get("id"): item for item in (previous or {}).get("items", [])}
    changed = set(normalized)
    for item_id, item in previous_items.items():
        for ref in item.get("evidence", []):
            if isinstance(ref, dict) and normalize_path(ref.get("path", "")) in changed:
                affected.add(item_id)
                reasons.setdefault(item_id, []).append("该项旧证据文件发生变化")
        for issue in (previous or {}).get("issues", []):
            if issue.get("item_id") != item_id:
                continue
            for ref in issue.get("evidence", []):
                if isinstance(ref, dict) and normalize_path(ref.get("path", "")) in changed:
                    affected.add(item_id)
                    reasons.setdefault(item_id, []).append("该项旧问题证据文件发生变化")

    for path in normalized:
        categories = classify(path)
        if "documentation" in categories:
            _add(reasons, DOCUMENTATION_ITEMS | {"6.8"}, f"文档变化：{path}")
            affected.update(DOCUMENTATION_ITEMS | {"6.8"})
        if "ci" in categories:
            _add(reasons, CI_ITEMS, f"CI 变化：{path}")
            affected.update(CI_ITEMS)
        if "dependency" in categories:
            _add(reasons, DEPENDENCY_ITEMS, f"依赖清单变化：{path}")
            affected.update(DEPENDENCY_ITEMS)
        if "config" in categories:
            _add(reasons, CONFIG_ITEMS, f"配置变化：{path}")
            affected.update(CONFIG_ITEMS)
        if "tests" in categories:
            _add(reasons, TEST_ITEMS, f"测试变化：{path}")
            affected.update(TEST_ITEMS)
        if "frontend" in categories:
            _add(reasons, FRONTEND_ITEMS, f"前端变化：{path}")
            affected.update(FRONTEND_ITEMS)
        if "source" in categories:
            _add(reasons, GENERIC_SOURCE_ITEMS, f"源码变化：{path}")
            affected.update(GENERIC_SOURCE_ITEMS)
            # 新增或修改源码必须复核所有安全检查，不能仅依赖旧证据路径。
            _add(reasons, SECURITY_ITEMS, f"源码安全复核：{path}")
            affected.update(SECURITY_ITEMS)
        if "unknown" in categories:
            _add(reasons, item_ids, f"未知文件类型，保守重判：{path}")
            affected.update(item_ids)

    # planner 的 reasons 覆盖完整清单，便于调用方直接计算 carried_over。
    for item_id in item_ids:
        if item_id not in affected:
            reasons[item_id].append("未命中变更影响映射")
    ratio = len(affected) / len(item_ids) if item_ids else 1.0
    full_scan = bool(item_ids and ratio > full_threshold)
    if full_scan:
        affected = set(item_ids)
        for item_id in item_ids:
            reasons[item_id].append(f"影响面 {ratio:.0%} 超过 {full_threshold:.0%}，升级全量")
    return IncrementalPlan(normalized, frozenset(affected),
                           {item_id: tuple(dict.fromkeys(values)) for item_id, values in reasons.items()}, full_scan)
