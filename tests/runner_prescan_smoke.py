#!/usr/bin/env python3
"""在精简调度镜像内执行四种预扫描工具，确认不仅仅是命令存在。"""
import json
import subprocess
import tempfile
from pathlib import Path

from runner import run_daily


def main():
    with tempfile.TemporaryDirectory(prefix="audit-prescan-smoke-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        (repo / "sample.py").write_text('eval("1+2")\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
        rules = root / "rules.yml"
        rules.write_text(
            "rules:\n"
            "  - id: smoke-eval\n"
            "    pattern: eval(...)\n"
            "    message: smoke finding\n"
            "    languages: [python]\n"
            "    severity: WARNING\n",
            encoding="utf-8",
        )
        output = root / "output"
        status = run_daily.prescan(repo, output, ["cloc", "gitleaks", "semgrep", "lizard"],
                                   {"semgrep_rules_dir": str(rules)})
        failed = {tool: details for tool, details in status.items() if details.get("status") != "ok"}
        if failed:
            raise RuntimeError(f"预扫描工具未正常运行：{failed}")
        report = json.loads((output / "semgrep.json").read_text(encoding="utf-8"))
        if not report.get("results"):
            raise RuntimeError("semgrep 未检测到测试规则")
        print("通过：cloc、gitleaks、semgrep、lizard 均可执行真实预扫描")


if __name__ == "__main__":
    main()
