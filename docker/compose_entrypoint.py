#!/usr/bin/env python3
"""以 Compose 的宿主机路径和模型配置启动现有批量扫描程序。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def configure_settings(settings: dict, env: dict) -> dict:
    data_dir = env.get("AUDIT_DATA_DIR", "")
    if not data_dir or not Path(data_dir).is_absolute() or ":" in data_dir:
        raise ValueError("AUDIT_DATA_DIR 必须是宿主机上的绝对路径，且不能包含冒号")
    network = env.get("AUDIT_MODEL_NETWORK", "").strip()
    if not network:
        raise ValueError("AUDIT_MODEL_NETWORK 不能为空")

    settings["workdir"] = str(Path(data_dir) / "work")
    settings["output_dir"] = str(Path(data_dir) / "output")
    agent = settings.setdefault("agent", {})
    agent.update({
        "type": "qwen-docker",
        "image": env.get("AUDIT_SCANNER_IMAGE") or "ghcr.io/xiaoguan521/ai-code-audit:main",
        "env_file": env.get("AUDIT_MODEL_ENV_PATH") or "/run/secrets/model.env",
        "network": network,
    })
    return settings


def main() -> None:
    settings_file = Path(os.environ.get("AUDIT_SETTINGS_FILE", ROOT / "config/settings.json"))
    projects_file = os.environ.get("AUDIT_PROJECTS_FILE", str(ROOT / "config/projects.csv"))
    with settings_file.open(encoding="utf-8") as f:
        settings = configure_settings(json.load(f), os.environ)

    # 只含路径与非敏感选项；API Key 始终只保存在 model.env 中。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="ai-audit-settings-", suffix=".json", delete=False) as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        generated_settings = f.name
    args = [sys.executable, str(ROOT / "runner/run_daily.py"), "--settings", generated_settings,
            "--projects", projects_file, *sys.argv[1:]]
    os.execv(sys.executable, args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Compose 启动失败：{exc}", file=sys.stderr)
        sys.exit(2)
