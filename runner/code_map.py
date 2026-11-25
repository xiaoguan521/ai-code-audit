"""从 Git 文件清单生成有界代码地图；只收集路径及文件大小，不读取源码。"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath


LANGUAGES = {".py": "Python", ".java": "Java", ".go": "Go", ".js": "JavaScript",
             ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue",
             ".rs": "Rust", ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++",
             ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift", ".php": "PHP", ".rb": "Ruby"}
MANIFESTS = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json",
             "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "go.mod", "Cargo.toml",
             "Gemfile", "composer.json", "Dockerfile", "docker-compose.yml", "compose.yaml"}
LOCKS = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
         "Pipfile.lock", "uv.lock", "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock"}
ENTRYPOINTS = {"main.py", "app.py", "manage.py", "main.go", "main.rs", "main.c", "main.cpp",
               "main.js", "main.ts", "index.js", "index.ts", "server.js", "server.ts"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".jar",
              ".class", ".so", ".dll", ".exe", ".mp4", ".mp3", ".woff", ".woff2", ".ttf"}
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def excluded(path: str, patterns: list[str]) -> bool:
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


def build(repo: Path, commit: str, scope_out: list[str], limit: int = 120) -> dict:
    """根据当前 Git index 盘点路径；返回的候选项不是对代码内容的判定。"""
    limit = max(1, min(int(limit), 1000))
    result = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("无法读取 Git 跟踪文件清单")
    root = repo.resolve()
    raw_paths = [os.fsdecode(s) for s in result.stdout.split(b"\0") if s]
    counts: Counter = Counter()
    languages: Counter = Counter()
    top: Counter = Counter()
    categories: dict[str, list[str]] = {k: [] for k in ("manifests", "lockfiles", "entrypoints", "tests", "ci", "files")}
    omitted: Counter = Counter()

    def add(category: str, path: str) -> None:
        if len(categories[category]) < limit:
            categories[category].append(path)
        else:
            omitted[category] += 1

    # 锁文件可以列出名称用于依赖审查，但不把其内容作为常规源码索引。
    scope_without_locks = [p for p in scope_out if p != "*.lock"]
    for path in sorted(raw_paths):
        parts = PurePosixPath(path).parts
        name = parts[-1] if parts else ""
        if not parts or any(part == ".env" or part.startswith(".env.") for part in parts):
            counts["protected"] += 1
            continue
        if excluded(path, scope_without_locks):
            counts["scope_out"] += 1
            continue
        is_lock = name in LOCKS or name.endswith(".lock")
        if excluded(path, scope_out) and not is_lock:
            counts["scope_out"] += 1
            continue
        try:
            absolute = (root / path).resolve(strict=True)
            resolved_parts = absolute.relative_to(root).parts
            if any(part == ".env" or part.startswith(".env.") for part in resolved_parts):
                counts["protected"] += 1
                continue
            if not absolute.is_file():
                counts["non_file"] += 1
                continue
            size = absolute.stat().st_size
        except (OSError, ValueError):
            counts["unreadable_or_outside"] += 1
            continue
        if is_lock:
            add("lockfiles", path)
            counts["lock_metadata_only"] += 1
            continue
        if name in MANIFESTS or fnmatch.fnmatchcase(name, "requirements*.txt"):
            add("manifests", path)
        if size > MAX_SOURCE_BYTES:
            counts["large"] += 1
            continue
        if PurePosixPath(name).suffix.lower() in BINARY_EXT:
            counts["binary_by_extension"] += 1
            continue
        counts["indexed"] += 1
        top[parts[0] if len(parts) > 1 else "."] += 1
        language = LANGUAGES.get(PurePosixPath(name).suffix.lower())
        if language:
            languages[language] += 1
        if name in ENTRYPOINTS:
            add("entrypoints", path)
        if any(part in {"test", "tests", "__tests__", "spec"} for part in parts[:-1]) or name.startswith("test_"):
            add("tests", path)
        if path.startswith((".github/workflows/", ".circleci/", ".buildkite/")) or name in {".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"}:
            add("ci", path)
        add("files", path)
    return {
        "commit": commit,
        "source": "git ls-files；仅盘点跟踪文件，候选路径不代表已检查或测试有效",
        "tracked_files": len(raw_paths),
        "counts": dict(counts),
        "language_file_counts": dict(languages),
        "top_level_file_counts": dict(top),
        **categories,
        "omitted_per_list": dict(omitted),
        "max_entries_per_list": limit,
    }


def summarize(data: dict) -> str:
    """将代码地图压缩为几行 Prompt 摘要，减少模型开局重复列目录的步数。"""
    if not isinstance(data, dict):
        return ""
    counts = data.get("counts") or {}
    indexed = int(counts.get("indexed", 0) or 0)
    tracked = int(data.get("tracked_files", 0) or 0)
    lines = [f"代码地图摘要：tracked={tracked}，indexed={indexed}"]

    def _top_pairs(mapping: dict, limit: int) -> str:
        items = sorted(mapping.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:limit]
        return "、".join(f"{k}({v})" for k, v in items)

    langs = data.get("language_file_counts") or {}
    if langs:
        lines.append(f"- 语言分布：{_top_pairs(langs, 6)}")
    tops = data.get("top_level_file_counts") or {}
    if tops:
        lines.append(f"- 一级目录：{_top_pairs(tops, 8)}")
    for key, label in (("manifests", "构建清单"), ("entrypoints", "入口文件"), ("ci", "CI 配置")):
        items = data.get(key) or []
        if items:
            preview = "、".join(items[:8]) + (" 等" if len(items) > 8 else "")
            lines.append(f"- {label}：{preview}")
    return "\n".join(lines)
