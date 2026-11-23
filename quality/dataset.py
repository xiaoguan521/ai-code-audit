"""固定质量样例内容的摘要。"""
import hashlib
from pathlib import Path


def fixture_digest(path: Path) -> str:
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file() and ".git" not in p.relative_to(path).parts):
        name = file.relative_to(path).as_posix().encode("utf-8")
        content = file.read_bytes()
        h.update(len(name).to_bytes(8, "big"))
        h.update(name)
        h.update(len(content).to_bytes(8, "big"))
        h.update(content)
    return h.hexdigest()
