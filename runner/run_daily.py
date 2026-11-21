#!/usr/bin/env python3
"""八性 AI 代码检查 · 每日定时扫描入口。

用法：
    python3 runner/run_daily.py                 # 按计划扫描全部在研项目（全量 / 增量 / 沿用自动判断）
    python3 runner/run_daily.py --only demo-order --force-full
    python3 runner/run_daily.py --dry-run       # 只打印将执行的动作，不调用模型

每个项目的流程：拉取代码（只读副本）→ 判断扫描类型 → 可选预扫描工具 → 调用 Claude Code
无界面模式（只读工具 + JSON Schema 约束输出）→ 校验 → 确定性评分 → 落盘。
全部项目完成后调用 render.py 生成日报与看板。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402

SKILL_FILE = ROOT / "skill" / "eight-quality-audit" / "SKILL.md"
SCHEMA_FILE = ROOT / "config" / "result.schema.json"
TZ = dt.timezone(dt.timedelta(hours=8))

# 模型只拿到只读能力：读文件、搜索、少量只读 git / 统计命令
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)", "Bash(git ls-files *)",
    "Bash(git rev-parse *)", "Bash(wc *)", "Bash(ls *)",
]
DISALLOWED_TOOLS = ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "mcp__*"]


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def load_projects(path: Path, statuses: set[str]) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("project_id")]
    return [r for r in rows if r.get("status", "").strip() in statuses]


# ---------- 代码获取 ----------

def git_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
    return env


def sync_repo(p: dict, repo: Path, depth: int) -> str:
    """维护一份扫描专用的本地副本（只拉取，不推送）。凭据由运行机上的只读 deploy key / token 提供。"""
    branch = p.get("branch") or "main"
    env = git_env()
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        r = run(["git", "clone", "--no-tags", "--depth", str(depth), "--branch", branch,
                 "--single-branch", p["git_url"], str(repo)], timeout=1800, env=env)
        if r.returncode:
            raise RuntimeError(f"clone 失败: {r.stderr.strip()[-500:]}")
    else:
        r = run(["git", "fetch", "--no-tags", "--depth", str(depth), "origin", branch], cwd=repo, timeout=1800, env=env)
        if r.returncode:
            raise RuntimeError(f"fetch 失败: {r.stderr.strip()[-500:]}")
        run(["git", "reset", "-q", "--hard", "FETCH_HEAD"], cwd=repo, env=env)
        run(["git", "clean", "-qffdx"], cwd=repo, env=env)
    return run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def changed_files(repo: Path, old: str, new: str) -> list[str] | None:
    r = run(["git", "diff", "--name-only", f"{old}..{new}"], cwd=repo)
    if r.returncode:
        return None  # 浅克隆找不到旧提交 → 退回全量
    return [x for x in r.stdout.splitlines() if x.strip()]


def dir_hash(path: Path) -> str:
    if not path.is_dir():
        return ""
    h = hashlib.sha1()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------- 预扫描（可选，确定性工具，结果只作线索） ----------

def prescan(repo: Path, out: Path, tools: list[str], settings: dict) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    done = []
    if "cloc" in tools and shutil.which("cloc"):
        r = run(["cloc", "--json", "--quiet", "--vcs=git", "."], cwd=repo, timeout=900)
        (out / "cloc.json").write_text(r.stdout, encoding="utf-8")
        done.append("cloc")
    if "gitleaks" in tools and shutil.which("gitleaks"):
        # --redact：报告中不出现密钥原文
        run(["gitleaks", "detect", "--no-git", "--redact", "--no-banner", "--exit-code", "0",
             "--report-format", "json", "--report-path", str(out / "gitleaks.json"), "--source", "."],
            cwd=repo, timeout=1800)
        done.append("gitleaks")
    rules = settings.get("semgrep_rules_dir")  # 离线规则目录；未配置则跳过，避免访问外网
    if "semgrep" in tools and shutil.which("semgrep") and rules:
        run(["semgrep", "scan", "--config", rules, "--json", "--metrics=off", "--quiet",
             "--output", str(out / "semgrep.json"), "."], cwd=repo, timeout=3600)
        done.append("semgrep")
    if "lizard" in tools and shutil.which("lizard"):
        r = run(["lizard", "-w", "-C", "15", "-L", "200", "."], cwd=repo, timeout=1800)
        (out / "lizard_warnings.txt").write_text(r.stdout[-200_000:], encoding="utf-8")
        done.append("lizard")
    return done


# ---------- 调用模型 ----------

def build_prompt(p: dict, mode: str, commit: str, settings: dict, prescan_dir: str | None,
                 prev_file: str | None, changes: list[str] | None, materials_changed: bool) -> str:
    scope_out = [s for s in (p.get("scope_out") or "").replace("；", ";").split(";") if s.strip()]
    scope_out += settings.get("default_scope_out", [])
    lines = [
        "MODE: batch",
        f"SCAN_TYPE: {mode}",
        "请严格按系统提示中的《八性代码质量审查 Skill》执行，对当前工作目录下的仓库做只读静态审查，"
        "最终只返回符合 JSON Schema 的结构化结果。",
        "",
        "## 项目元数据",
        f"- PROJECT_ID: {p['project_id']}",
        f"- PROJECT_NAME: {p['project_name']}",
        f"- COMMIT: {commit}（分支 {p.get('branch') or 'main'}）",
        f"- TECH_STACK: {p.get('tech_stack') or '由代码推断'}",
        f"- PROJECT_TYPE: {p.get('project_type') or '未提供'}",
        f"- CORE_FLOWS: {p.get('core_flows') or '未提供'}",
        f"- SCOPE_OUT: {', '.join(scope_out) or '无'}",
    ]
    mdir = (p.get("materials_dir") or "").strip()
    lines.append(f"- 补充材料目录（仓库内相对路径）: {mdir if mdir else '未提供 → 相关项按 NEED_INFO 处理'}"
                 "；文件名以 S1–S11 开头者视为对应编号材料")
    if prescan_dir:
        lines.append(f"- 预扫描工具输出目录（仅作线索，须回源码核实）: {prescan_dir}")
    if mode == "incremental" and prev_file:
        lines += [
            "",
            "## 增量扫描",
            f"- 上一次结果文件: {prev_file}",
            f"- 补充材料是否有变化: {'是' if materials_changed else '否'}",
            "- 本次变更文件清单:",
        ]
        shown = (changes or [])[:800]
        lines += [f"  - {c}" for c in shown]
        if changes and len(changes) > len(shown):
            lines.append(f"  - ……另有 {len(changes) - len(shown)} 个文件，用 git diff --name-only 查看")
    return "\n".join(lines)


def call_claude(prompt: str, repo: Path, extra_dirs: list[Path], settings: dict, timeout: int) -> dict:
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    cmd = [
        settings.get("claude_bin", "claude"), "--bare", "-p", prompt,
        "--output-format", "json",
        "--json-schema", schema,
        "--append-system-prompt-file", str(SKILL_FILE),
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--tools", "Read,Grep,Glob,Bash",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--max-budget-usd", str(settings.get("per_project_budget_usd", 15)),
        "--no-session-persistence",
    ]
    if settings.get("model"):
        cmd += ["--model", settings["model"]]
    for d in extra_dirs:
        cmd += ["--add-dir", str(d)]
    r = run(cmd, cwd=repo, timeout=timeout)
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"模型输出不是 JSON（exit={r.returncode}）: {r.stdout[-400:]} {r.stderr[-400:]}")
    if "structured_output" not in payload:
        raise RuntimeError(f"缺少 structured_output: {str(payload.get('result'))[-400:]}")
    return {"result": payload["structured_output"], "cost_usd": payload.get("total_cost_usd"),
            "session_id": payload.get("session_id")}


class AgentOutputError(RuntimeError):
    """模型输出无法解析为合格 JSON；可带问题清单重试。"""


REQUIRED_KEYS = ["inventory", "items", "issues", "need_info_list", "dimension_notes", "summary", "self_check"]


def extract_json(text: str) -> dict:
    """从模型最终回复中取出 JSON 对象（容忍 ```json 代码块和前后多余文字）。"""
    t = (text or "").strip()
    if "```" in t:
        blocks = [b for b in t.split("```") if b.strip().startswith(("json", "{"))]
        if blocks:
            t = blocks[-1].strip()
            t = t[4:].strip() if t.startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise AgentOutputError("最终回复中没有 JSON 对象")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentOutputError(f"JSON 解析失败：{e}")
    missing = [k for k in REQUIRED_KEYS if k not in obj]
    if missing:
        raise AgentOutputError(f"JSON 缺少顶层字段：{missing}")
    return obj


def call_qwen_docker(prompt: str, repo: Path, ctx: Path, settings: dict, timeout: int) -> dict:
    """在一次性容器里运行 Qwen Code：代码与上下文只读挂载，容器内没有 Git 凭据。"""
    ag = settings["agent"]
    (ctx / "prompt.md").write_text(prompt, encoding="utf-8")
    # 容器带唯一名字：超时时 subprocess 只能杀掉 docker 客户端，容器本身要单独 kill，否则会一直跑下去
    name = f"ai-audit-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--init", "--name", name, "--label", "ai-code-audit=1",
        "--read-only", "--tmpfs", "/tmp:rw,size=512m", "--tmpfs", "/home/node:rw,size=256m,uid=1000,gid=1000",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "512", "--memory", ag.get("memory", "6g"), "--cpus", str(ag.get("cpus", 2)),
        "-v", f"{repo}:/workspace/repo:ro", "-v", f"{ctx}:/workspace/context:ro",
        "-w", "/workspace/repo",
        "-e", f"MAX_SESSION_TURNS={ag.get('max_session_turns', 300)}",
    ]
    if ag.get("network"):
        cmd += ["--network", ag["network"]]
    if ag.get("env_file"):
        cmd += ["--env-file", ag["env_file"]]      # OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL
    cmd += [ag.get("image", "ai-code-audit:latest")]
    try:
        r = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        run(["docker", "kill", name], timeout=60)
        raise RuntimeError(f"Qwen Code 运行超过 {timeout} 秒，已终止容器 {name}")
    if r.returncode in (53, 55):
        raise AgentOutputError(f"超出轮次或运行预算上限（exit={r.returncode}），请缩小范围或调大 max_session_turns")
    try:
        events = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Qwen Code 输出不是 JSON（exit={r.returncode}）：{r.stdout[-400:]} {r.stderr[-600:]}")
    last = events[-1] if isinstance(events, list) and events else {}
    result = extract_json(last.get("result", ""))
    return {"result": result, "cost_usd": None, "session_id": last.get("session_id"),
            "usage": last.get("usage"), "stats": last.get("stats")}


def call_agent(prompt: str, repo: Path, ctx: Path, settings: dict, timeout: int) -> dict:
    kind = settings.get("agent", {}).get("type", "claude")
    if kind == "qwen-docker":
        return call_qwen_docker(prompt, repo, ctx, settings, timeout)
    return call_claude(prompt, repo, [ctx], settings, timeout)


def ctx_view(settings: dict, ctx: Path) -> str:
    """模型看到的上下文目录路径：容器内固定为 /workspace/context。"""
    return "/workspace/context" if settings.get("agent", {}).get("type") == "qwen-docker" else str(ctx)


# ---------- 单项目流程 ----------

def scan_project(p: dict, settings: dict, run_date: str, args, checklist: dict) -> dict:
    pid = p["project_id"]
    work = Path(settings["workdir"]) / pid
    outdir = Path(settings["output_dir"])
    state_file = outdir / "state" / f"{pid}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    day_dir = outdir / "results" / run_date
    day_dir.mkdir(parents=True, exist_ok=True)
    rec = {"project_id": pid, "project_name": p["project_name"], "department": p.get("department", ""),
           "owner": p.get("owner", ""), "date": run_date, "status": "ok"}
    try:
        repo = work / "repo"
        commit = sync_repo(p, repo, int(settings.get("clone_depth", 200)))
        mdir = (p.get("materials_dir") or "").strip()
        mhash = dir_hash(repo / mdir) if mdir else ""
        prev_file = Path(state["last_result"]) if state.get("last_result") else None
        prev_ok = prev_file is not None and prev_file.exists()
        last_full = dt.date.fromisoformat(state["last_full_date"]) if state.get("last_full_date") else None
        today = dt.date.fromisoformat(run_date)
        full_day = today.isoweekday() == int(settings.get("full_scan_weekday", 6))
        materials_changed = mhash != state.get("materials_hash", "")

        changes = None
        if args.force_full or not prev_ok or full_day or last_full is None:
            mode = "full"
        elif commit == state.get("last_commit") and not materials_changed and \
                (today - last_full).days < int(settings.get("reuse_if_unchanged_days", 7)):
            mode = "reuse"
        else:
            changes = changed_files(repo, state.get("last_commit", ""), commit) if commit != state.get("last_commit") else []
            mode = "incremental" if changes is not None else "full"
        rec.update({"commit": commit, "mode": mode})
        log(f"{pid}: {commit[:10]} → {mode}" + (f"（{len(changes)} 个变更文件）" if changes else ""))
        if args.dry_run:
            rec["status"] = "dry-run"
            return rec

        if mode == "reuse":
            data = json.loads(prev_file.read_text(encoding="utf-8"))
            data["meta"].update({"date": run_date, "mode": "reuse", "reused_from": data["meta"].get("date")})
        else:
            # 只把本项目的上下文目录（预扫描结果 + 上次结果副本）开放给模型，不暴露其它项目
            ctx = work / "context"
            if ctx.exists():
                shutil.rmtree(ctx)
            pre_dir = ctx / "prescan"
            tools_done = prescan(repo, pre_dir, settings.get("prescan_tools", []), settings)
            prev_copy = None
            if mode == "incremental":
                prev_copy = ctx / "previous_result.json"
                shutil.copyfile(prev_file, prev_copy)
            view = ctx_view(settings, ctx)
            prompt = build_prompt(p, mode, commit, settings, f"{view}/prescan" if tools_done else None,
                                  f"{view}/previous_result.json" if prev_copy else None, changes, materials_changed)
            timeout = int(settings.get("per_project_timeout_sec", 3600))
            out, problems = None, []
            for attempt in (1, 2):  # 输出不合格时带着问题清单重试一次
                try:
                    out = call_agent(prompt, repo, ctx, settings, timeout)
                    problems = scoring.validate(out["result"], checklist)
                except AgentOutputError as e:
                    problems = [str(e)]
                    if attempt == 2:
                        raise
                if not problems:
                    break
                log(f"{pid}: 第 {attempt} 次输出校验未通过: {problems[:3]}")
                prompt += "\n\n## 上次输出校验未通过，请修正后重新输出完整 JSON\n" + "\n".join(f"- {x}" for x in problems)
            data = out["result"]
            data["meta"] = {"date": run_date, "mode": mode, "commit": commit, "cost_usd": out["cost_usd"],
                            "session_id": out["session_id"], "prescan_tools": tools_done, "agent": settings.get("agent", {}).get("type", "claude"),
                            "usage": out.get("usage"),
                            "validation_problems": problems, "scanned_at": now().isoformat()}
        data["meta"]["project"] = {k: p.get(k, "") for k in
                                   ("project_id", "project_name", "department", "owner", "git_url", "branch", "tech_stack")}
        weights = scoring.parse_weights(p.get("weights"), checklist)
        data["score"] = scoring.score_result(data, checklist, weights)
        for iss in data.get("issues", []):
            iss["fingerprint"] = scoring.issue_fingerprint(pid, iss)
        result_path = day_dir / f"{pid}.json"
        result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        state.update({"last_commit": commit, "last_result": str(result_path), "materials_hash": mhash,
                      "last_scan_date": run_date})
        if mode == "full":
            state["last_full_date"] = run_date
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        rec.update({"total": data["score"]["total"], "security": data["score"]["security_score"],
                    "cost_usd": data["meta"].get("cost_usd"), "validation_problems": len(data["meta"].get("validation_problems", []))})
    except Exception as e:  # 单项目失败不影响其它项目
        rec.update({"status": "error", "error": str(e)[:800]})
        log(f"{pid}: 失败 {e}")
        (day_dir / f"{pid}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="八性 AI 代码检查 · 每日扫描")
    ap.add_argument("--settings", default=str(ROOT / "config" / "settings.json"))
    ap.add_argument("--projects", default=str(ROOT / "config" / "projects.csv"))
    ap.add_argument("--only", nargs="*", help="只扫描这些 project_id")
    ap.add_argument("--force-full", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="覆盖运行日期 YYYY-MM-DD（补跑用）")
    args = ap.parse_args()

    settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    statuses = set(settings.get("scan_statuses", ["在研"]))
    projects = load_projects(Path(args.projects), statuses)
    if args.only:
        projects = [p for p in projects if p["project_id"] in set(args.only)]
    checklist = scoring.load_checklist()
    run_date = args.date or now().date().isoformat()
    log(f"开始 {run_date}：{len(projects)} 个项目，并发 {settings.get('max_parallel', 3)}")

    with cf.ThreadPoolExecutor(max_workers=int(settings.get("max_parallel", 3))) as ex:
        recs = list(ex.map(lambda p: scan_project(p, settings, run_date, args, checklist), projects))

    outdir = Path(settings["output_dir"])
    runlog = outdir / "results" / run_date / "_run.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(json.dumps({"date": run_date, "finished_at": now().isoformat(), "projects": recs},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.dry_run:
        import render
        render.render_all(outdir, run_date, Path(args.projects), settings)
    failed = [r for r in recs if r["status"] == "error"]
    log(f"完成：成功 {len(recs) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
