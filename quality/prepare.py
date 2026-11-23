#!/usr/bin/env python3
"""把合成样例准备成独立 Git 仓库和 runner 项目清单。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from dataset import fixture_digest

DATASET = Path(__file__).resolve().parent
ROOT = DATASET.parent


def prepare(destination: Path, settings_file: Path) -> None:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    dataset = json.loads((DATASET / "cases.json").read_text(encoding="utf-8"))
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    rows = []
    manifest = {"dataset_version": dataset["version"],
                "dataset_sha256": hashlib.sha256((DATASET / "cases.json").read_bytes()).hexdigest(), "cases": {}}
    for case in dataset["cases"]:
        repo = destination / "fixtures" / case["id"]
        shutil.copytree(DATASET / "fixtures" / case["id"], repo)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Audit Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "quality fixture"],
                       check=True, capture_output=True)
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        manifest["cases"][case["id"]] = {"commit": commit, "fixture_sha256": fixture_digest(repo)}
        rows.append({"project_id": case["id"], "project_name": case["id"], "git_url": repo.as_uri(),
                     "branch": "main", "status": "在研", "project_type": "合成测试项目"})
    with (destination / "projects.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["project_id", "project_name", "git_url", "branch", "status", "project_type"])
        writer.writeheader()
        writer.writerows(rows)
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    settings.update(workdir=str(destination / "work"), output_dir=str(destination / "output"))
    fd = os.open(destination / "settings.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="在新目录中准备隔离的质量回归样例")
    parser.add_argument("--dest", type=Path, required=True, help="不存在的目标目录")
    parser.add_argument("--settings", type=Path, default=ROOT / "config/settings.json")
    args = parser.parse_args()
    prepare(args.dest, args.settings)
    print(f"已准备：{args.dest}")


if __name__ == "__main__":
    main()
