#!/usr/bin/env python3
"""以 Compose 的宿主机路径和模型配置启动现有批量扫描程序。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} 必须是 true/false")


def _json_args(raw: str, key: str) -> list[str]:
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key} 必须是 JSON 字符串数组，例如 [\"headless\",\"--json\"]") from exc
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise ValueError(f"{key} 必须是 JSON 字符串数组")
    return list(args)


def configure_settings(settings: dict, env: dict) -> dict:
    data_dir = env.get("AUDIT_DATA_DIR", "")
    if not data_dir or not Path(data_dir).is_absolute() or ":" in data_dir:
        raise ValueError("AUDIT_DATA_DIR 必须是宿主机上的绝对路径，且不能包含冒号")
    network = env.get("AUDIT_MODEL_NETWORK", "").strip()
    if not network:
        raise ValueError("AUDIT_MODEL_NETWORK 不能为空")

    for variable, setting in (("AUDIT_MAX_PARALLEL", "max_parallel"),
                              ("AUDIT_MAX_MODEL_CONCURRENCY", "max_model_concurrency"),
                              ("AUDIT_PER_PROJECT_TIMEOUT_SEC", "per_project_timeout_sec")):
        raw = env.get(variable, "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{variable} 必须是正整数") from exc
            if value < 1:
                raise ValueError(f"{variable} 必须是正整数")
            settings[setting] = value

    settings["workdir"] = str(Path(data_dir) / "work")
    settings["output_dir"] = str(Path(data_dir) / "output")
    semgrep_rules = env.get("AUDIT_SEMGREP_RULES_DIR", "").strip()
    if semgrep_rules:
        if not Path(semgrep_rules).is_absolute():
            raise ValueError("AUDIT_SEMGREP_RULES_DIR 必须是容器内可读的绝对路径")
        settings["semgrep_rules_dir"] = semgrep_rules
    agent = settings.setdefault("agent", {})
    agent_type = (env.get("AUDIT_AGENT_TYPE") or agent.get("type") or "deepseek-harness").strip()
    if agent_type != "deepseek-harness":
        raise ValueError("AUDIT_AGENT_TYPE 必须为 deepseek-harness")
    image = env.get("AUDIT_SCANNER_IMAGE") or agent.get("image") or "ghcr.io/xiaoguan521/ai-code-audit:main"
    env_file = env.get("AUDIT_MODEL_ENV_PATH") or agent.get("env_file") or "/run/secrets/model.env"
    agent.update({"type": agent_type, "image": image, "env_file": env_file, "network": network})
    if agent_type == "deepseek-harness":
        agent["strategy"] = "single"
        agent["launcher"] = env.get("AUDIT_HARNESS_LAUNCHER", agent.get("launcher", "docker"))
        agent["bin"] = env.get("AUDIT_HARNESS_BIN", agent.get("bin", "dsh"))
        raw_args = env.get("AUDIT_HARNESS_ARGS")
        if raw_args:
            agent["args"] = _json_args(raw_args, "AUDIT_HARNESS_ARGS")
        else:
            agent["args"] = [str(value) for value in agent.get(
                "args", ["headless", "--json", "--permission-mode", "read-only", "--approval-policy", "never"]
            )]
    summary = settings.setdefault("summary", {})
    summary["enabled"] = _env_bool(env, "AUDIT_SUMMARY_ENABLED", bool(summary.get("enabled", True)))
    if summary["enabled"] and agent_type == "deepseek-harness":
        summary_agent = summary.setdefault("agent", {})
        summary_agent.update({
            "type": "deepseek-harness",
            "strategy": "single",
            "launcher": "docker",
            "image": env.get("AUDIT_SUMMARY_IMAGE") or image,
            "network": env.get("AUDIT_SUMMARY_NETWORK") or network,
            "env_file": env.get("AUDIT_SUMMARY_MODEL_ENV_PATH") or env_file,
            "bin": env.get("AUDIT_SUMMARY_BIN") or agent.get("bin", "dsh"),
            "args": agent.get("args", ["headless", "--json", "--permission-mode", "read-only", "--approval-policy", "never"]),
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
