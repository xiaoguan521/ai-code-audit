#!/usr/bin/env python3
"""八性代码质量审查平台 · 一键初始化与环境自检工具。

用法：
    python3 runner/doctor.py --init            # 一键生成缺失的部署模板与占位目录（不覆盖已有文件）
    python3 runner/doctor.py                   # 执行部署环境自检
    python3 runner/doctor.py --check-model     # 额外探测 model.env 中的模型服务连通性
    python3 runner/doctor.py --json            # 以 JSON 输出检查结果
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNNER_DIR))

import scoring  # noqa: E402


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def init_workspace(root: Path = ROOT) -> list[dict]:
    """创建部署所需的模板文件与空占位目录；已存在的文件绝不覆盖。"""
    actions: list[dict] = []

    def copy_if_missing(src: Path, dst: Path, mode: int | None = None, post_edit=None) -> None:
        if dst.exists():
            actions.append({"path": str(dst), "action": "skipped", "note": "已存在，保留原文件"})
            return
        if not src.is_file():
            actions.append({"path": str(dst), "action": "error", "note": f"模板不存在：{src}"})
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = src.read_text(encoding="utf-8")
        if post_edit:
            content = post_edit(content)
        dst.write_text(content, encoding="utf-8")
        if mode is not None:
            dst.chmod(mode)
        actions.append({"path": str(dst), "action": "created", "note": f"从 {src.name} 生成"})

    def _inject_web_token(text: str) -> str:
        token = secrets.token_urlsafe(24)
        lines = []
        replaced = False
        for line in text.splitlines():
            if line.strip() == "AUDIT_WEB_TOKEN=":
                lines.append(f"AUDIT_WEB_TOKEN={token}")
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.append(f"AUDIT_WEB_TOKEN={token}")
        return "\n".join(lines) + "\n"

    copy_if_missing(root / ".env.example", root / ".env", mode=0o600, post_edit=_inject_web_token)
    copy_if_missing(root / "docker" / "model.env.example", root / "model.env", mode=0o600)
    copy_if_missing(root / "config" / "projects.compose.example.csv", root / "config" / "projects.local.csv")

    secrets_dir = root / "secrets"
    ssh_dir = secrets_dir / "ssh"
    git_cred = secrets_dir / "git-credentials"
    data_dir = root / "audit-data"

    if not ssh_dir.exists():
        ssh_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir.chmod(0o700)
        actions.append({"path": str(ssh_dir), "action": "created", "note": "创建空 SSH 占位目录（Compose 挂载必需）"})
    else:
        actions.append({"path": str(ssh_dir), "action": "skipped", "note": "已存在"})

    if not git_cred.exists():
        git_cred.parent.mkdir(parents=True, exist_ok=True)
        git_cred.write_text("", encoding="utf-8")
        git_cred.chmod(0o600)
        actions.append({"path": str(git_cred), "action": "created", "note": "创建空 Git 凭据占位文件（权限 600）"})
    else:
        actions.append({"path": str(git_cred), "action": "skipped", "note": "已存在"})

    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        actions.append({"path": str(data_dir), "action": "created", "note": "创建运行数据目录"})
    else:
        actions.append({"path": str(data_dir), "action": "skipped", "note": "已存在"})

    return actions


def run_checks(root: Path = ROOT, output_dir: Path | None = None,
               projects_csv: Path | None = None, settings_path: Path | None = None,
               check_model: bool = False) -> dict:
    """执行只读环境诊断，返回结构化检查项（绝不输出密钥原文）。"""
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 1. Python 版本
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    add("Python 运行时", "ok" if sys.version_info >= (3, 10) else "error",
        f"Python {py_ver}", "" if sys.version_info >= (3, 10) else "请使用 Python 3.10+")

    # 2. 核心配置与检查清单
    sp = settings_path or (root / "config" / "settings.json")
    try:
        settings = json.loads(sp.read_text(encoding="utf-8"))
        add("settings.json", "ok", f"已加载 {sp.name}")
    except Exception as exc:
        settings = {}
        add("settings.json", "error", f"无法解析：{exc}", "检查 config/settings.json 的 JSON 格式")

    try:
        checklist = scoring.load_checklist()
        add("检查清单 checklist.json", "ok", f"{len(checklist['dimensions'])} 个维度，{len(checklist['items'])} 个检查项")
    except Exception as exc:
        checklist = None
        add("检查清单 checklist.json", "error", f"加载失败：{exc}", "运行 python3 tools/gen_checklist.py 重新生成")

    # 3. 项目清单 CSV 与 weights 校验
    pcsv = projects_csv or (
        root / "config" / "projects.local.csv"
        if (root / "config" / "projects.local.csv").is_file()
        else root / "config" / "projects.csv"
    )
    if not pcsv.is_file():
        add("项目清单 CSV", "error", f"未找到 {pcsv}", "运行 python3 runner/doctor.py --init 生成 config/projects.local.csv")
    else:
        try:
            with pcsv.open(encoding="utf-8-sig") as f:
                rows = [r for r in csv.DictReader(f) if r.get("project_id")]
            statuses = set(settings.get("scan_statuses", ["在研"]))
            active = [r for r in rows if (r.get("status") or "").strip() in statuses]
            weight_errs = []
            if checklist:
                for r in rows:
                    try:
                        scoring.parse_weights(r.get("weights"), checklist, r["project_id"])
                    except ValueError as exc:
                        weight_errs.append(str(exc))
            if weight_errs:
                add("项目清单 CSV", "error", f"权重配置错误：{weight_errs[0]}", "修正 weights 列（格式 1:15;2:12;...）")
            elif not active:
                add("项目清单 CSV", "warn", f"共 {len(rows)} 行，但无状态为 {'/'.join(statuses)} 的在研项目",
                    f"编辑 {pcsv.name} 添加在研项目")
            else:
                add("项目清单 CSV", "ok", f"{pcsv.name}：共 {len(rows)} 项，在研 {len(active)} 项")
        except Exception as exc:
            add("项目清单 CSV", "error", f"解析失败：{exc}", f"检查 {pcsv.name} 编码与列格式")

    # 4. 运行数据目录与输出目录读写
    env_data_dir = os.environ.get("AUDIT_DATA_DIR", "").strip()
    out_dir = output_dir or (
        Path(env_data_dir) / "output" if env_data_dir
        else Path(settings.get("output_dir") or (root / "audit-data" / "output"))
    )
    if out_dir.exists():
        writable = os.access(out_dir, os.W_OK)
        add("输出目录 output_dir", "ok" if writable else "warn",
            f"{out_dir}（{'可写' if writable else '只读'}）",
            "" if writable else "确认运行用户对该目录有写权限")
    else:
        add("输出目录 output_dir", "warn", f"{out_dir} 尚未创建（首次扫描时自动创建）",
            "可运行 python3 runner/doctor.py --init 预先创建")

    # 5. Compose 挂载依赖文件（宿主机或容器内分别检查）
    in_container = Path("/run/secrets/model.env").is_file() or bool(env_data_dir)
    model_env_path = Path("/run/secrets/model.env") if Path("/run/secrets/model.env").is_file() else (root / "model.env")
    if model_env_path.is_file():
        menv = _parse_env_file(model_env_path)
        has_openai = bool(menv.get("OPENAI_BASE_URL") and menv.get("OPENAI_MODEL"))
        has_ds = bool(menv.get("DEEPSEEK_API_KEY"))
        if has_openai or has_ds:
            provider = f"OpenAI 兼容 ({menv.get('OPENAI_MODEL')})" if has_openai else "DeepSeek 官方接口"
            add("模型配置 model.env", "ok", f"已配置 {provider}")
        else:
            add("模型配置 model.env", "warn", "文件存在但尚未填写真实模型地址或密钥",
                "编辑 model.env 配置 OPENAI_BASE_URL / OPENAI_MODEL 或 DEEPSEEK_API_KEY")
    else:
        add("模型配置 model.env", "warn", "未找到 model.env", "运行 python3 runner/doctor.py --init 生成模板并填写")

    if not in_container:
        missing_mounts = []
        for label, rel in ((".env", ".env"), ("secrets/git-credentials", "secrets/git-credentials"), ("secrets/ssh", "secrets/ssh")):
            if not (root / rel).exists():
                missing_mounts.append(label)
        if missing_mounts:
            add("Compose 挂载项", "warn", f"缺少：{', '.join(missing_mounts)}",
                "运行 python3 runner/doctor.py --init 一键补齐占位文件与目录")
        else:
            env_vars = _parse_env_file(root / ".env")
            has_token = bool(env_vars.get("AUDIT_WEB_TOKEN"))
            add("Compose 挂载项", "ok",
                f".env 与 secrets 挂载齐全（Web 鉴权：{'已开启' if has_token else '未设置 AUDIT_WEB_TOKEN'}）")

    # 6. Docker 与预扫描工具
    docker_bin = shutil.which("docker")
    if docker_bin:
        sock = Path(os.environ.get("AUDIT_DOCKER_SOCKET", "/var/run/docker.sock"))
        if sock.exists():
            add("Docker 运行环境", "ok", f"已安装 docker CLI，socket 存在 ({sock})")
        else:
            add("Docker 运行环境", "warn", f"已安装 docker CLI，但未检测到 {sock}", "确认 Docker Desktop / dockerd 已启动")
    else:
        add("Docker 运行环境", "warn", "当前环境未找到 docker 命令（纯本地看板浏览不受影响，触发容器扫描需要 Docker）")

    tools = ["git", *settings.get("prescan_tools", ["cloc", "gitleaks", "semgrep", "lizard"])]
    found_tools = [t for t in tools if shutil.which(t)]
    missing_tools = [t for t in tools if not shutil.which(t)]
    if not missing_tools:
        add("预扫描工具链", "ok", f"齐全：{', '.join(found_tools)}")
    else:
        add("预扫描工具链", "warn", f"已安装：{', '.join(found_tools) or '无'}；缺失：{', '.join(missing_tools)}",
            "Docker Runner 镜像已内置全部工具；本机直跑可按需安装")

    # 7. 前端静态产物
    dist_index = root / "frontend" / "dist" / "index.html"
    if dist_index.is_file():
        add("前端静态产物", "ok", "frontend/dist/index.html 已就绪")
    else:
        add("前端静态产物", "warn", "尚未构建 frontend/dist", "执行 cd frontend && npm ci && npm run build")

    # 8. 可选模型连通性探测（仅 --check-model）
    if check_model and model_env_path.is_file():
        menv = _parse_env_file(model_env_path)
        base_url = (menv.get("OPENAI_BASE_URL") or "").rstrip("/")
        if base_url:
            url = f"{base_url}/models"
            req = urllib.request.Request(url, method="GET")
            api_key = menv.get("OPENAI_API_KEY", "")
            if api_key and api_key != "EMPTY":
                req.add_header("Authorization", f"Bearer {api_key}")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    add("模型接口连通性", "ok", f"GET {url} 返回 HTTP {resp.status}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                add("模型接口连通性", "error", f"无法访问 {url}：{exc}", "确认模型服务已启动且网络互通")

    errors = sum(1 for c in checks if c["status"] == "error")
    warns = sum(1 for c in checks if c["status"] == "warn")
    verdict = "error" if errors else ("warn" if warns else "ok")
    return {"verdict": verdict, "errors": errors, "warnings": warns, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser(description="八性代码质量审查平台 · 初始化与环境自检")
    ap.add_argument("--init", action="store_true", help="生成缺失的 .env、model.env、projects.local.csv 与 secrets 占位项")
    ap.add_argument("--check-model", action="store_true", help="额外检测 model.env 中的模型服务连通性")
    ap.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = ap.parse_args()

    if args.init:
        actions = init_workspace(ROOT)
        if args.json:
            print(json.dumps({"actions": actions}, ensure_ascii=False, indent=2))
        else:
            print("== 初始化结果 ==")
            for a in actions:
                mark = {"created": "[新建]", "skipped": "[跳过]", "error": "[错误]"}.get(a["action"], "[?]")
                print(f"{mark} {a['path']} — {a['note']}")
            print("\n下一步：")
            print("  1. 编辑 model.env，填入你的模型服务地址与模型名")
            print("  2. 编辑 config/projects.local.csv，填入待扫描 Git 仓库")
            print("  3. 在 secrets/git-credentials 或 secrets/ssh/ 放入只读 Git 凭据")
            print("  4. 运行 python3 runner/doctor.py 复查，随后 docker compose up -d web 启动看板")
        return 1 if any(a["action"] == "error" for a in actions) else 0

    report = run_checks(ROOT, check_model=args.check_model)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        icons = {"ok": "OK  ", "warn": "WARN", "error": "FAIL"}
        print(f"== 环境自检结果：{report['verdict'].upper()}（错误 {report['errors']}，警告 {report['warnings']}）==")
        for c in report["checks"]:
            print(f"[{icons[c['status']]}] {c['name']}: {c['detail']}")
            if c.get("fix"):
                print(f"       建议：{c['fix']}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
