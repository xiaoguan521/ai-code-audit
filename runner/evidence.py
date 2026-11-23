"""核验审查结果的源码引用；存在性核验不等同于结论正确性核验。"""
from __future__ import annotations

from pathlib import Path, PurePosixPath


MAX_EVIDENCE_FILE_BYTES = 20 * 1024 * 1024
MAX_EVIDENCE_LINES = 200


def normalize_references(result: dict, repo: Path) -> None:
    """只规范化明确属于该仓库的路径前缀，不猜测文件或修正行号。"""
    roots = ("/workspace/repo/", str(repo.resolve()) + "/")
    for entry in [*result.get("items", []), *result.get("issues", [])]:
        for ref in entry.get("evidence", []):
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                continue
            raw = ref["path"]
            for prefix in roots:
                if raw.startswith(prefix):
                    raw = raw[len(prefix):]
                    break
            path = PurePosixPath(raw)
            if raw and "\\" not in raw and not path.is_absolute() and ".." not in path.parts:
                ref["path"] = path.as_posix()


def verify(result: dict, repo: Path) -> list[str]:
    """检查每条证据的仓库内路径、行号与可选原文片段。

    仓库由调用方固定在被审查的提交。禁止越过仓库、读取 .env 或验证二进制文件；
    未能核验的引用均报告出来，由调用方决定重试或中止评分。
    """
    root = repo.resolve()
    cache: dict[str, tuple[list[str] | None, str | None]] = {}
    problems: list[str] = []

    def read_lines(raw: str) -> tuple[list[str] | None, str | None]:
        if raw in cache:
            return cache[raw]
        path = PurePosixPath(raw)
        if not raw or "\\" in raw or path.is_absolute() or ".." in path.parts or not path.parts:
            outcome = (None, "路径必须是仓库内相对路径")
        elif any(part == ".env" or part.startswith(".env.") for part in path.parts):
            outcome = (None, "路径指向受保护的 .env 文件")
        else:
            try:
                actual = (root / raw).resolve(strict=True)
                relative = actual.relative_to(root)
                if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
                    outcome = (None, "路径指向受保护的 .env 文件")
                elif not actual.is_file():
                    outcome = (None, "路径不是普通文件")
                elif actual.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
                    outcome = (None, "文件过大，无法自动核验")
                else:
                    data = actual.read_bytes()
                    if b"\0" in data:
                        outcome = (None, "二进制文件无法按行核验")
                    else:
                        outcome = (data.decode("utf-8-sig").splitlines(), None)
            except FileNotFoundError:
                outcome = (None, "文件不存在")
            except UnicodeDecodeError:
                outcome = (None, "文件不是可核验的 UTF-8 文本")
            except ValueError:
                outcome = (None, "路径或符号链接超出仓库")
            except (OSError, RuntimeError):
                outcome = (None, "文件不可读取")
        cache[raw] = outcome
        return outcome

    def check(owner: str, refs: list) -> None:
        for index, ref in enumerate(refs):
            # 结构校验先于本函数；这里仍保持失败关闭，便于独立调用。
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                problems.append(f"{owner} 证据 {index + 1} 不是有效的结构化引用")
                continue
            path = ref["path"]
            start, end = ref.get("start_line"), ref.get("end_line", ref.get("start_line"))
            label = f"{owner} 证据 {index + 1}（{path[:200]}）"
            if (not isinstance(start, int) or isinstance(start, bool) or
                    not isinstance(end, int) or isinstance(end, bool) or start < 1 or end < start):
                problems.append(f"{label} 行号无效")
                continue
            if end - start + 1 > MAX_EVIDENCE_LINES:
                problems.append(f"{label} 引用超过 {MAX_EVIDENCE_LINES} 行")
                continue
            lines, error = read_lines(path)
            if error:
                problems.append(f"{label} {error}")
                continue
            if end > len(lines):
                problems.append(f"{label} 行号超出文件范围")
                continue
            snippet = ref.get("snippet")
            if snippet is not None and (not isinstance(snippet, str) or snippet not in "\n".join(lines[start - 1:end])):
                problems.append(f"{label} 片段与所引用行不一致")

    for item in result.get("items", []):
        check(str(item.get("id", "未知检查项")), item.get("evidence", []))
    for issue in result.get("issues", []):
        check(f"问题 {issue.get('item_id', '未知检查项')}", issue.get("evidence", []))
    return problems
