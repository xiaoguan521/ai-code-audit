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
import copy
import concurrent.futures as cf
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402
import evidence  # noqa: E402
import code_map  # noqa: E402
import segments  # noqa: E402
import incremental  # noqa: E402
import facts  # noqa: E402

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
SUPPORTED_AGENTS = {"pi-docker", "qwen-docker", "claude"}


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


@contextmanager
def measure(target: dict, name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        target[name] = round(time.perf_counter() - start, 4)


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
        # 任何一步失败都必须报错：否则 HEAD 还停在旧提交，会被误判为“无变更 → 沿用”，静默地扫旧代码
        for cmd in (["git", "reset", "-q", "--hard", "FETCH_HEAD"], ["git", "clean", "-qffdx"]):
            r = run(cmd, cwd=repo, env=env)
            if r.returncode:
                raise RuntimeError(f"{' '.join(cmd[:2])} 失败: {r.stderr.strip()[-500:]}")
    r = run(["git", "rev-parse", "HEAD"], cwd=repo)
    commit = r.stdout.strip()
    if r.returncode or not commit:
        raise RuntimeError(f"git rev-parse HEAD 失败: {r.stderr.strip()[-500:]}")
    return commit


def changed_files(repo: Path, old: str, new: str) -> list[str] | None:
    r = run(["git", "diff", "--name-only", "-z", f"{old}..{new}"], cwd=repo)
    if r.returncode:
        return None  # 浅克隆找不到旧提交 → 退回全量
    return [x for x in r.stdout.split("\0") if x]


def dir_hash(path: Path) -> str:
    if not path.is_dir():
        return ""
    h = hashlib.sha1()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def audit_fingerprint(p: dict, settings: dict) -> str:
    """规则、审查范围或后端变化时，禁止沿用旧口径的结果。"""
    ag = settings.get("agent", {})
    inputs = {
        "project": {k: p.get(k, "") for k in ("git_url", "branch", "tech_stack", "project_type",
                                                   "core_flows", "scope_out", "materials_dir")},
        "default_scope_out": settings.get("default_scope_out", []),
        "prescan_tools": settings.get("prescan_tools", []),
        "semgrep_rules_dir": settings.get("semgrep_rules_dir", ""),
        "code_map_enabled": settings.get("code_map_enabled", True),
        "code_map_max_entries": settings.get("code_map_max_entries", 120),
        "agent": {k: ag.get(k) for k in ("type", "image", "strategy")},
        "model": settings.get("model", ""),
        # 模型别名指向的服务或权重变化时，由部署者更新此值。
        "audit_revision": settings.get("audit_revision", ""),
    }
    # 仅读取影响推理口径的非敏感配置；密钥轮换不应触发全量扫描。
    env_file = ag.get("env_file")
    if env_file:
        try:
            names = {"OPENAI_BASE_URL", "OPENAI_MODEL", "PI_CONTEXT_WINDOW", "PI_MAX_OUTPUT_TOKENS", "PI_TEMPERATURE"}
            selected = {}
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key in names:
                    selected[key] = value
            inputs["model_configuration"] = selected
        except (OSError, UnicodeError):
            inputs["model_configuration"] = "unavailable"
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for file in (SKILL_FILE, SCHEMA_FILE, ROOT / "config" / "checklist.json",
                 ROOT / "runner" / "evidence.py", ROOT / "runner" / "code_map.py", ROOT / "runner" / "scoring.py",
                 ROOT / "runner" / "segments.py", ROOT / "runner" / "incremental.py", ROOT / "runner" / "facts.py"):
        h.update(file.read_bytes())
    return h.hexdigest()


def full_scan_due(pid: str, today: dt.date, last_full: dt.date | None, settings: dict) -> bool:
    """可按项目稳定错峰；错过计划时最多间隔一个周期后补扫。"""
    if last_full is None:
        return True
    interval = int(settings.get("full_scan_interval_days", settings.get("reuse_if_unchanged_days", 7)))
    if interval < 1:
        raise ValueError("full_scan_interval_days 必须为正整数")
    strategy = settings.get("full_scan_strategy", "weekday")
    if strategy not in {"weekday", "staggered"}:
        raise ValueError("full_scan_strategy 必须是 weekday 或 staggered")
    age = (today - last_full).days
    if age < 0:
        return True
    if age >= interval:
        return True
    if age == 0:
        return False
    if strategy == "weekday":
        return today.isoweekday() == int(settings.get("full_scan_weekday", 6))
    slot = int.from_bytes(hashlib.sha256(pid.encode("utf-8")).digest()[:8], "big") % interval
    return today.toordinal() % interval == slot


@contextmanager
def batch_lock(settings: dict):
    """保护共享副本和结果目录，防止两个调度进程相互覆盖。"""
    paths = {Path(settings[key]).resolve() for key in ("workdir", "output_dir")}
    with ExitStack() as stack:
        for path in sorted(paths, key=str):
            path.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context((path / ".ai-audit.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise RuntimeError(f"另一扫描任务正在使用 {path}") from e
            stack.callback(fcntl.flock, lock, fcntl.LOCK_UN)
        yield


def write_json_atomic(path: Path, value: dict, mode: int = 0o644) -> None:
    """先写入同目录的临时文件，再原子替换，避免意外中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_private_json(path: Path, value: dict) -> None:
    """待核实结果可能含代码线索，限制文件权限并原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_atomic(path, value, mode=0o600)


# ---------- 预扫描（可选，确定性工具，结果只作线索） ----------

def prescan(repo: Path, out: Path, tools: list[str], settings: dict,
            files: list[str] | None = None) -> dict[str, dict]:
    """运行确定性预扫描。

    ``files`` 非空时只扫描 Git 变更文件，并统一应用 default_scope_out；全量模式
    仍扫描仓库根目录。工具的输出只作为模型定位线索，不能替代源码证据。
    """
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    tools = list(dict.fromkeys(tools))
    parallelism = int(settings.get("prescan_parallelism", 1))
    if parallelism < 1:
        raise ValueError("prescan_parallelism 必须为正整数")
    if parallelism > 1 and len(tools) > 1:
        # 每个工具各写一份报告；使用有限并发，避免预扫描工具互相挤占资源。
        single = dict(settings, prescan_parallelism=1)
        with cf.ThreadPoolExecutor(max_workers=min(parallelism, len(tools), 4)) as pool:
            entries = pool.map(lambda name: prescan(repo, out, [name], single, files), tools)
            return {name: entry[name] for name, entry in zip(tools, entries)}
    scope_out = [str(pattern) for pattern in settings.get("default_scope_out", [])]
    scan_files = None
    if files is not None:
        scan_files = [path for path in dict.fromkeys(incremental.normalize_path(path) for path in files)
                      if path and not code_map.excluded(path, scope_out) and (repo / path).is_file()]
    targets = scan_files if scan_files is not None else ["."]
    status: dict[str, dict] = {}
    for tool in tools:
        entry = status[tool] = {"status": "skipped"}
        if tool not in {"cloc", "gitleaks", "semgrep", "lizard"}:
            entry["reason"] = "不支持的工具"
            continue
        if not shutil.which(tool):
            entry["reason"] = "未安装"
            continue
        rules = settings.get("semgrep_rules_dir")
        if tool == "semgrep" and not rules:
            entry["reason"] = "未配置离线规则"
            continue
        if scan_files is not None and not scan_files:
            entry.update(reason="没有未排除的变更文件", scan_scope="changed_files")
            continue
        filename = {"cloc": "cloc.json", "gitleaks": "gitleaks.json",
                    "semgrep": "semgrep.json", "lizard": "lizard_warnings.txt"}[tool]
        target = out / filename
        entry["scan_scope"] = "changed_files" if scan_files is not None else "repository"
        entry["file_count"] = len(targets) if scan_files is not None else None
        commands = {
            "cloc": (["cloc", "--json", "--quiet", "--vcs=git", *targets], 900),
            "gitleaks": (["gitleaks", "detect", "--no-git", "--redact", "--no-banner", "--exit-code", "0",
                          "--report-format", "json", "--report-path", str(target), "--source", "."], 1800),
            "semgrep": (["semgrep", "scan", "--config", rules or "", "--json", "--metrics=off", "--quiet",
                         "--output", str(target), *targets], 3600),
            "lizard": (["lizard", "-w", "-C", "15", "-L", "200", *targets], 1800),
        }
        try:
            with measure(entry, "duration_seconds"):
                cmd, timeout = commands[tool]
                if tool == "gitleaks" and scan_files is not None:
                    # gitleaks dir 接受单个文件/目录；对变更文件逐个运行并合并报告，
                    # 避免把未变更的仓库内容重新扫描一遍。
                    findings = []
                    for index, path in enumerate(scan_files):
                        partial = out / f".gitleaks-{index}.json"
                        r = run(["gitleaks", "dir", "--redact", "--no-banner", "--exit-code", "0",
                                 "--report-format", "json", "--report-path", str(partial), path],
                                cwd=repo, timeout=timeout)
                        if r.returncode != 0:
                            entry.update(status="failed", reason="工具退出失败", exit_code=r.returncode)
                            break
                        if partial.is_file():
                            findings.extend(json.loads(partial.read_text(encoding="utf-8")))
                            partial.unlink(missing_ok=True)
                    else:
                        target.write_text(json.dumps(findings, ensure_ascii=False), encoding="utf-8")
                        entry.update(status="ok", output=filename, findings=len(findings))
                    if entry["status"] != "skipped" and entry["status"] != "ok":
                        target.unlink(missing_ok=True)
                    continue
                r = run(cmd, cwd=repo, timeout=timeout)
                entry["exit_code"] = r.returncode
                # Lizard 用 1 表示有复杂度告警；仅在输出确实包含告警时接受。
                lizard_warnings = tool == "lizard" and r.returncode == 1 and ": warning:" in r.stdout
                if r.returncode != 0 and not lizard_warnings:
                    entry.update(status="failed", reason="工具退出失败")
                    continue
                if tool == "cloc":
                    json.loads(r.stdout)
                    target.write_text(r.stdout, encoding="utf-8")
                elif tool == "lizard":
                    target.write_text(r.stdout[-200_000:], encoding="utf-8")
                elif not target.is_file():
                    entry.update(status="no_output", reason="工具未生成报告，无法确认结果")
                    continue
                else:
                    json.loads(target.read_text(encoding="utf-8"))
                entry.update(status="ok", output=filename)
        except subprocess.TimeoutExpired:
            entry.update(status="failed", reason="运行超时")
        except (OSError, UnicodeError, json.JSONDecodeError):
            entry.update(status="failed", reason="工具输出不可读取或格式无效")
        finally:
            if entry["status"] != "ok":
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
    return status


# ---------- 调用模型 ----------

def build_prompt(p: dict, mode: str, commit: str, settings: dict, prescan_dir: str | None,
                 prev_file: str | None, changes: list[str] | None, materials_changed: bool,
                 changes_file: str | None = None, code_map_file: str | None = None,
                 incremental_plan: incremental.IncrementalPlan | None = None,
                 facts_file: str | None = None) -> str:
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
    if code_map_file:
        lines.append(f"- Git 跟踪文件的代码地图: {code_map_file}；候选路径仅供定位，不能代替源码核实")
    if facts_file:
        lines.append(f"- runner 生成的确定性事实与 inventory: {facts_file}；事实用于定位和盘点，语义结论仍须回源码核实")
    if mode == "incremental" and prev_file:
        lines += [
            "",
            "## 增量扫描",
            f"- 上一次结果文件: {prev_file}",
            f"- 补充材料是否有变化: {'是' if materials_changed else '否'}",
            "- 本次变更文件清单:",
        ]
        if incremental_plan is not None:
            affected = sorted(incremental_plan.affected_items, key=lambda value: (value.split(".")[0], value))
            lines.append(f"- runner 预计算的本次重判检查项（必须 carried_over=false）：{', '.join(affected) or '无'}")
            lines.append("- 未列入重判清单的检查项必须原样沿用上次结论、证据和问题，并标记 carried_over=true。")
            lines.append("- 重判原因仅可基于变更文件或补充材料，不得因模型随机性改动沿用项。")
        shown = (changes or [])[:800]
        # 文件名属于不可信仓库数据；JSON 转义防止换行伪造额外提示。
        lines += [f"  - {json.dumps(c, ensure_ascii=False)}" for c in shown]
        if changes and len(changes) > len(shown):
            lines.append(f"  - ……另有 {len(changes) - len(shown)} 个文件，完整清单见 {changes_file or '扫描上下文'}")
    return "\n".join(lines)


def call_claude(prompt: str, repo: Path, extra_dirs: list[Path], settings: dict, timeout: int) -> dict:
    context = extra_dirs[0]
    dimension_schema = context / "dimension.schema.json"
    dimension_skill = context / "dimension.skill.md"
    if dimension_schema.exists() != dimension_skill.exists():
        raise RuntimeError("分维度 Schema 和 Skill 必须同时存在")
    schema_file = dimension_schema if dimension_schema.exists() else SCHEMA_FILE
    skill_file = dimension_skill if dimension_skill.exists() else SKILL_FILE
    schema = schema_file.read_text(encoding="utf-8")
    cmd = [
        settings.get("claude_bin", "claude"), "--bare", "-p", prompt,
        "--output-format", "json",
        "--json-schema", schema,
        "--append-system-prompt-file", str(skill_file),
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
            "session_id": payload.get("session_id"), "usage": payload.get("usage"),
            "stats": {"turns": payload.get("num_turns"), "tool_calls": None}}


class AgentCallError(RuntimeError):
    def __init__(self, msg: str, stats: dict | None = None):
        super().__init__(msg)
        self.stats = stats


class AgentOutputError(AgentCallError):
    """模型输出无法解析为合格 JSON 或结构不符合 Schema；可带问题清单重试。"""

    def __init__(self, msg: str, problems: list[str] | None = None, stats: dict | None = None, retryable: bool = True):
        super().__init__(msg, stats)
        self.problems = problems or [msg]
        self.retryable = retryable


REQUIRED_KEYS = ["inventory", "items", "issues", "need_info_list", "dimension_notes", "summary", "self_check"]
MAX_PROBLEMS_IN_PROMPT = 30


def extract_json(text: str, required_keys: list[str] | None = None) -> dict:
    """从模型最终回复中取出 JSON 对象（容忍 ```json 代码块和前后多余文字）。

    不按 ``` 切分文本：证据里引用的代码片段本身可能带 ```，切分会把 JSON 切断。
    做法是从每个 "{" 尝试解码一个完整 JSON 对象，取最后一个带齐顶层字段的。"""
    required_keys = REQUIRED_KEYS if required_keys is None else required_keys
    t = text or ""
    dec = json.JSONDecoder()
    found: list[dict] = []
    first_err = None
    i = t.find("{")
    while i >= 0:
        try:
            obj, end = dec.raw_decode(t, i)
        except json.JSONDecodeError as e:
            first_err = first_err or e
            i = t.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = t.find("{", end)
    complete = [o for o in found if all(k in o for k in required_keys)]
    if complete:
        return complete[-1]
    best = max(found, key=lambda o: sum(k in o for k in required_keys), default=None)
    # 一个顶层字段都没有、同时又有解析失败：多半是输出被截断，只解出了内部的子对象，按解析失败报
    if best is not None and (any(k in best for k in required_keys) or not first_err):
        raise AgentOutputError(f"JSON 缺少顶层字段：{[k for k in required_keys if k not in best]}")
    if first_err:
        raise AgentOutputError(f"JSON 解析失败：{first_err}")
    raise AgentOutputError("最终回复中没有 JSON 对象")


def check_output(result: dict, checklist: dict, repo: Path | None = None,
                 verification_stats: dict | None = None, schema: dict | None = None) -> list[str]:
    """先按 JSON Schema 校验结构，不合格抛 AgentOutputError（走重试，绝不进入评分）；
    结构合格后再做语义校验（证据、问题清单是否一致），返回语义问题列表。"""
    structural = scoring.schema_errors(result, schema)
    if structural:
        raise AgentOutputError(f"输出不符合 JSON Schema（{len(structural)} 处）：{structural[:3]}", structural)
    if repo is not None:
        evidence.normalize_references(result, repo)
    problems = scoring.validate(result, checklist)
    if repo is not None:
        evidence_problems = evidence.verify(result, repo)
        if verification_stats is not None:
            entries = [*result.get("items", []), *result.get("issues", [])]
            verification_stats.update(references=sum(len(entry.get("evidence", [])) for entry in entries),
                                      invalid_references=len(evidence_problems))
        problems.extend(evidence_problems)
    return problems


def incremental_problems(result: dict, previous: dict, changes: list[str], materials_changed: bool,
                         repo: Path | None = None,
                         plan: incremental.IncrementalPlan | None = None) -> list[str]:
    """核对声称沿用的结论；未命中这些规则不代表变更必然无影响。"""
    previous_items = {item["id"]: item for item in previous.get("items", [])}
    changed = {incremental.normalize_path(path) for path in changes if incremental.normalize_path(path)}
    if plan is None:
        # 兼容旧调用方：没有提供规划结果时仍使用保守的旧规则。
        item_ids = [{"id": item_id} for item_id in previous_items]
        plan = incremental.plan({"items": item_ids}, previous, changes, materials_changed)
    forced = set(plan.affected_items)

    def normalized_issues(data: dict, item_id: str) -> list[str]:
        issues = [{k: v for k, v in issue.items() if k not in {"fingerprint", "_id"}}
                  for issue in data.get("issues", []) if issue.get("item_id") == item_id]
        return sorted(json.dumps(issue, ensure_ascii=False, sort_keys=True) for issue in issues)

    def ref_changed(ref: dict) -> bool:
        path = incremental.normalize_path(ref.get("path", ""))
        if path in changed:
            return True
        if repo is not None and isinstance(path, str):
            try:
                target = (repo / path).resolve(strict=True).relative_to(repo.resolve()).as_posix()
                return target in changed
            except (OSError, RuntimeError, ValueError):
                pass
        return False

    problems = []
    for item in result.get("items", []):
        old = previous_items.get(item["id"])
        clean = lambda value: {k: v for k, v in value.items() if k not in {"carried_over", "change_reason"}}
        if not item.get("carried_over"):
            if old and item["id"] not in forced:
                problems.append(f"{item['id']} 未受变更影响，必须原样沿用并标记 carried_over")
                continue
            changed_judgment = old and (any(old.get(k) != item.get(k) for k in ("result", "risk")) or
                                        bool(old.get("conditional")) != bool(item.get("conditional")))
            if changed_judgment and not str(item.get("change_reason", "")).strip():
                problems.append(f"{item['id']} 的结论或风险等级发生变化，但缺少 change_reason")
            continue
        item_id = item["id"]
        if not old:
            problems.append(f"{item_id} 沿用但上次结果中没有该项")
            continue
        if item_id in forced:
            problems.append(f"{item_id} 受代码、配置或材料变化影响，不得标记为沿用")
        if clean(item) != clean(old):
            problems.append(f"{item_id} 标记为沿用，但检查结论或证据已改变")
        refs = list(old.get("evidence", []))
        for issue in previous.get("issues", []):
            if issue.get("item_id") == item_id:
                refs.extend(issue.get("evidence", []))
        if any(isinstance(ref, dict) and ref_changed(ref) for ref in refs):
            problems.append(f"{item_id} 的旧证据文件已变更，不得标记为沿用")
        if normalized_issues(result, item_id) != normalized_issues(previous, item_id):
            problems.append(f"{item_id} 标记为沿用，但对应问题清单已改变")
    return problems


def _merge_need_info(result: dict, item_ids: set[str], *, missing: str, why: str) -> None:
    """把需要补充的检查项加入清单，并将过多分组压缩到 Schema 上限。"""
    if not item_ids:
        return
    groups = result.setdefault("need_info_list", [])
    remaining = set(item_ids)
    for group in groups:
        selected = remaining & set(group.get("items", []))
        if selected:
            group["items"] = list(dict.fromkeys([*group.get("items", []), *sorted(selected)]))
            remaining -= selected
    if remaining:
        groups.append({"items": sorted(remaining), "missing": missing, "why": why,
                       "form": "源码证据或补充材料", "unlocks": "恢复可评分结论", "priority": "中"})
    # 保留前 14 组，把溢出组聚合成一组，符合 result.schema.json 的 maxItems=15。
    if len(groups) > 15:
        overflow = groups[14:]
        combined = {"items": list(dict.fromkeys(item for group in overflow for item in group.get("items", []))),
                    "missing": "；".join(dict.fromkeys(group.get("missing", "") for group in overflow if group.get("missing"))),
                    "why": "；".join(dict.fromkeys(group.get("why", "") for group in overflow if group.get("why"))),
                    "form": "源码证据或补充材料", "unlocks": "恢复可评分结论", "priority": "中"}
        groups[:] = [*groups[:14], combined]


def _repair_incremental_result(result: dict, previous: dict, plan: incremental.IncrementalPlan,
                               checklist: dict) -> int:
    """将未受影响的输出恢复为上次结果，避免模型抄写错误污染基线。"""
    expected = {item["id"] for item in checklist.get("items", [])}
    affected = set(plan.affected_items) & expected
    old_items = {item.get("id"): item for item in previous.get("items", [])}
    repaired = 0
    for index, item in enumerate(result.get("items", [])):
        item_id = item.get("id")
        old = old_items.get(item_id)
        if old is None or item_id not in expected:
            continue
        if item_id not in affected:
            replacement = copy.deepcopy(old)
            replacement["carried_over"] = True
            replacement.pop("change_reason", None)
            if {k: v for k, v in item.items() if k not in {"carried_over", "change_reason"}} != \
                    {k: v for k, v in replacement.items() if k not in {"carried_over", "change_reason"}} or \
                    not item.get("carried_over"):
                result["items"][index] = replacement
                repaired += 1
        elif item.get("carried_over"):
            item["carried_over"] = False
            old_judgment = (old.get("result"), old.get("risk"), bool(old.get("conditional")))
            new_judgment = (item.get("result"), item.get("risk"), bool(item.get("conditional")))
            if old_judgment != new_judgment and not str(item.get("change_reason", "")).strip():
                item["change_reason"] = "受变更影响，已重新核验"
            repaired += 1

    # 受影响项使用模型当前的问题，未受影响项使用旧问题；这样标题改写不会
    # 触发语义重试，也不会破坏历史问题的连续性。
    current_issues = [issue for issue in result.get("issues", [])
                      if issue.get("item_id") in affected]
    old_issues = [copy.deepcopy(issue) for issue in previous.get("issues", [])
                  if issue.get("item_id") in expected and issue.get("item_id") not in affected]
    if result.get("issues", []) != [*current_issues, *old_issues]:
        result["issues"] = [*current_issues, *old_issues]
        repaired += 1

    # need_info 也按同一边界合并，过滤掉其它维度的 ID。
    groups = []
    for source, allowed in ((result.get("need_info_list", []), affected),
                            (previous.get("need_info_list", []), expected - affected)):
        for group in source:
            selected = [item_id for item_id in group.get("items", []) if item_id in allowed]
            if selected:
                copy_group = copy.deepcopy(group)
                copy_group["items"] = list(dict.fromkeys(selected))
                groups.append(copy_group)
    if result.get("need_info_list", []) != groups:
        result["need_info_list"] = groups
        repaired += 1
    return repaired


def repair_semantics(result: dict, checklist: dict, repo: Path | None = None,
                     verification_stats: dict | None = None,
                     previous: dict | None = None,
                     plan: incremental.IncrementalPlan | None = None,
                     schema: dict | None = None) -> list[str]:
    """修复可确定的语义不一致，不重新启动整次模型会话。

    结构错误仍由 ``check_output`` 抛出并允许一次结构重试；这里处理的是不真实
    的证据、孤立问题和增量沿用抄写错误。无法安全修复的残留问题会原样返回，
    由调用方失败关闭。
    """
    repairs = 0
    if previous is not None and plan is not None:
        repairs += _repair_incremental_result(result, previous, plan, checklist)

    # 待补充分组是模型自由组织的字段；完整 Schema 约束了每个维度的局部输出，
    # 但旧版完整 Schema 没有给其中的 item ID 加 enum，故在这里过滤未知编号。
    expected_ids = {item["id"] for item in checklist.get("items", [])}
    filtered_need_info = []
    for group in result.get("need_info_list", []):
        selected = [item_id for item_id in group.get("items", []) if item_id in expected_ids]
        if selected:
            copy_group = copy.deepcopy(group)
            copy_group["items"] = list(dict.fromkeys(selected))
            filtered_need_info.append(copy_group)
    if filtered_need_info != result.get("need_info_list", []):
        result["need_info_list"] = filtered_need_info
        repairs += 1

    invalid_items: set[str] = set()
    invalid_issue_indices: set[int] = set()
    if repo is not None:
        evidence_problems, invalid_items, invalid_issue_indices = evidence.prune_invalid(result, repo)
        if verification_stats is not None:
            verification_stats["invalid_references_before_repair"] = len(evidence_problems)
            verification_stats["repaired_references"] = len(invalid_items) + len(invalid_issue_indices)
        repairs += len(invalid_items) + len(invalid_issue_indices)

    item_map = {item.get("id"): item for item in result.get("items", [])}
    valid_issues = []
    for index, issue in enumerate(result.get("issues", [])):
        source = item_map.get(issue.get("item_id"))
        if (index in invalid_issue_indices or not issue.get("evidence") or source is None or
                source.get("result") not in ("WARN", "FAIL")):
            repairs += 1
            continue
        valid_issues.append(issue)
    result["issues"] = valid_issues
    issue_ids = {issue.get("item_id") for issue in valid_issues}

    needs: set[str] = set()
    for item in result.get("items", []):
        item_id = item.get("id")
        judged = item.get("result") in ("PASS", "WARN", "FAIL")
        if item_id in invalid_items or (judged and not item.get("evidence")) or \
                (item.get("result") in ("WARN", "FAIL") and item_id not in issue_ids):
            item.update(result="NEED_INFO-证据不足", risk="none", conditional=True,
                        conclusion="源码证据或问题清单无法核验，暂不下结论")
            item["evidence"] = []
            item.pop("change_reason", None)
            needs.add(item_id)
            repairs += 1
    _merge_need_info(result, needs, missing="可核验的源码证据或问题清单",
                     why="当前输出中的证据/问题引用无法通过 runner 核验")
    # 降级后的 NEED_INFO 项不能继续挂着 WARN/FAIL 问题。
    downgraded = needs
    if downgraded:
        result["issues"] = [issue for issue in result.get("issues", [])
                             if issue.get("item_id") not in downgraded]
    if verification_stats is not None:
        verification_stats["semantic_repairs"] = verification_stats.get("semantic_repairs", 0) + repairs
    before_invalid = verification_stats.get("invalid_references", 0) if verification_stats is not None else 0
    residual = check_output(result, checklist, repo, verification_stats, schema)
    if verification_stats is not None and before_invalid:
        # 保留本次模型原始输出中被修复的引用数；最终复核为 0 不应抹掉审计指标。
        verification_stats["invalid_references"] = max(before_invalid,
                                                        verification_stats.get("invalid_references", 0))
    return residual


def backend_problem(settings: dict) -> str | None:
    agent = settings.get("agent")
    kind = agent.get("type") if isinstance(agent, dict) else None
    if kind not in SUPPORTED_AGENTS:
        return "settings.json 的 agent.type 必须显式设为 pi-docker、qwen-docker 或 claude"
    if agent.get("strategy", "single") not in {"single", "dimensions"}:
        return "settings.json 的 agent.strategy 必须为 single 或 dimensions"
    return None


def network_problem(settings: dict) -> str | None:
    """容器后端必须显式指定容器网络。被审查的代码是不可信输入（可能藏提示注入），
    留空会落到 Docker 默认网桥、可以访问任意外网地址。确实要用默认网桥，须显式写 "bridge"。"""
    ag = settings.get("agent", {})
    if ag.get("type") not in {"pi-docker", "qwen-docker"} or (ag.get("network") or "").strip():
        return None
    return ("settings.json 的 agent.network 未配置：容器会落到默认网桥、可访问任意地址。"
            "请创建只放行内网千问的专用 Docker 网络并填入（见 README“网络隔离”）；"
            "确需使用默认网桥，请显式填写 \"bridge\"")


def pi_stderr_stats(stderr: str) -> tuple[dict | None, str]:
    stats, lines = None, []
    for line in stderr.splitlines():
        if line.startswith("AI_AUDIT_STATS:"):
            try:
                value = json.loads(line[len("AI_AUDIT_STATS:"):])
                if isinstance(value, dict):
                    stats = value
            except json.JSONDecodeError:
                pass
        else:
            lines.append(line)
    return stats, "\n".join(lines)


def call_pi_docker(prompt: str, repo: Path, ctx: Path, settings: dict, timeout: int) -> dict:
    """在一次性容器里运行 Pi：代码与上下文只读挂载，容器内没有 Git 凭据。"""
    problem = network_problem(settings)
    if problem:
        raise RuntimeError(problem)
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
    cmd += ["--network", ag["network"].strip()]
    if ag.get("env_file"):
        cmd += ["--env-file", ag["env_file"]]      # OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL
    cmd += [ag.get("image", "ai-code-audit:latest")]
    try:
        r = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        run(["docker", "kill", name], timeout=60)
        raise RuntimeError(f"Pi 运行超过 {timeout} 秒，已终止容器 {name}")
    stats, stderr = pi_stderr_stats(r.stderr)
    if r.returncode in (53, 55):
        raise AgentOutputError(f"Pi 超出轮次或模型输出长度上限（exit={r.returncode}）：{stderr[-400:]}",
                               stats=stats, retryable=False)
    if r.returncode != 0:
        raise AgentCallError(f"Pi 扫描失败（exit={r.returncode}）：{stderr[-600:]}", stats=stats)
    try:
        events = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise AgentCallError(f"Pi 输出不是 JSON（exit={r.returncode}）：{r.stdout[-400:]} {stderr[-600:]}", stats=stats)
    last = events[-1] if isinstance(events, list) and events else {}
    try:
        dimension_schema = ctx / "dimension.schema.json"
        required = json.loads(dimension_schema.read_text(encoding="utf-8"))["required"] if dimension_schema.exists() else None
        result = extract_json(last.get("result", ""), required)
    except AgentOutputError as e:
        e.stats = last.get("stats") or stats
        raise
    return {"result": result, "cost_usd": None, "session_id": last.get("session_id"),
            "usage": last.get("usage"), "stats": last.get("stats") or stats}


def call_agent(prompt: str, repo: Path, ctx: Path, settings: dict, timeout: int) -> dict:
    problem = backend_problem(settings)
    if problem:
        raise RuntimeError(problem)
    kind = settings["agent"]["type"]
    with settings.get("_model_limiter", nullcontext()):
        if kind in {"pi-docker", "qwen-docker"}:
            return call_pi_docker(prompt, repo, ctx, settings, timeout)
        if kind == "claude":
            return call_claude(prompt, repo, [ctx], settings, timeout)
    raise RuntimeError(problem or "不支持的模型后端")


def ctx_view(settings: dict, ctx: Path) -> str:
    """模型看到的上下文目录路径：容器内固定为 /workspace/context。"""
    return "/workspace/context" if settings.get("agent", {}).get("type") in {"pi-docker", "qwen-docker"} else str(ctx)


def call_agent_dimensions(prompt: str, repo: Path, ctx: Path, settings: dict, timeout: int,
                          checklist: dict, previous: dict | None, changes: list[str] | None,
                          materials_changed: bool,
                          plan: incremental.IncrementalPlan | None = None) -> dict:
    """分维度调用模型、独立校验重试，再合并为现有的完整结果。"""
    dimensions = [d["id"] for d in checklist["dimensions"]]
    if previous is not None and plan is None:
        plan = incremental.plan(checklist, previous, changes or [], materials_changed)
    base_schema = scoring.load_schema()
    skill = SKILL_FILE.read_text(encoding="utf-8")
    parts_root = ctx / "dimensions"
    # 外层项目级结构重试会复用同一个 context；不要把上次的维度目录再次当作
    # 输入复制，也不要因 mkdir 已存在而把本应重试的项目直接打成失败。
    if parts_root.exists():
        shutil.rmtree(parts_root)
    common = list(ctx.iterdir())
    parts_root.mkdir()

    def link_or_copy(src: str, dst: str) -> str:
        try:
            os.link(src, dst)
            return dst
        except OSError:
            return shutil.copy2(src, dst)

    def prepare(dimension: str) -> tuple[Path, dict, dict, dict, dict]:
        part_ctx = parts_root / dimension
        part_ctx.mkdir()
        for source in common:
            if source.is_dir():
                shutil.copytree(source, part_ctx / source.name, copy_function=link_or_copy)
            elif source.is_file():
                link_or_copy(str(source), str(part_ctx / source.name))
        full_items = [item for item in checklist["items"] if item["dimension"] == dimension]
        full_ids = {item["id"] for item in full_items}
        if previous is not None and plan is not None:
            selected_ids = full_ids & set(plan.affected_items)
            # 未受影响维度不会启动模型，会直接走 carried_part；为保持其旧契约，
            # 这里仍给它完整的维度 schema。
            if not selected_ids:
                selected_ids = full_ids
        else:
            selected_ids = full_ids
        part_schema = segments.schema_for_dimension(checklist, base_schema, dimension, selected_ids)
        (part_ctx / "dimension.schema.json").write_text(json.dumps(part_schema, ensure_ascii=False), encoding="utf-8")
        (part_ctx / "dimension.skill.md").write_text(
            segments.skill_for_dimension(skill, checklist, dimension, selected_ids), encoding="utf-8")
        part_checklist = dict(checklist,
                              dimensions=[d for d in checklist["dimensions"] if d["id"] == dimension],
                              items=[i for i in full_items if i["id"] in selected_ids])
        full_schema = segments.schema_for_dimension(checklist, base_schema, dimension, full_ids)
        full_checklist = dict(checklist,
                              dimensions=[d for d in checklist["dimensions"] if d["id"] == dimension],
                              items=full_items)
        return part_ctx, part_schema, part_checklist, full_schema, full_checklist

    prepared = {dimension: prepare(dimension) for dimension in dimensions}

    def carried_part(dimension: str) -> dict:
        """为未受影响维度生成可信的沿用结果，不启动模型会话。"""
        if previous is None:
            raise ValueError("没有上次结果，不能沿用维度")
        ids = {item["id"] for item in prepared[dimension][4]["items"]}
        old_items = {item["id"]: item for item in previous.get("items", [])}
        if not ids.issubset(old_items):
            raise ValueError(f"上次结果缺少维度 {dimension} 的检查项")
        items = []
        for item_id in (item["id"] for item in checklist["items"] if item["dimension"] == dimension):
            item = json.loads(json.dumps(old_items[item_id], ensure_ascii=False))
            item["carried_over"] = True
            item.pop("change_reason", None)
            items.append(item)
        issues = [json.loads(json.dumps(issue, ensure_ascii=False))
                  for issue in previous.get("issues", []) if issue.get("item_id") in ids]
        need_info = []
        for group in previous.get("need_info_list", []):
            selected = [item_id for item_id in group.get("items", []) if item_id in ids]
            if selected:
                copy_group = json.loads(json.dumps(group, ensure_ascii=False))
                copy_group["items"] = selected
                need_info.append(copy_group)
        notes = [json.loads(json.dumps(note, ensure_ascii=False))
                 for note in previous.get("dimension_notes", []) if note.get("dimension") == dimension]
        if len(notes) != 1:
            raise ValueError(f"上次结果缺少维度 {dimension} 的结论")
        part = {"items": items, "issues": issues, "need_info_list": need_info,
                "dimension_notes": notes}
        if dimension == "1":
            part["inventory"] = json.loads(json.dumps(previous.get("inventory", {}), ensure_ascii=False))
        if dimension == "6":
            part["prompt_injection_suspects"] = list(previous.get("prompt_injection_suspects", []))
        return part

    def complete_part(dimension: str, part: dict, model_ids: set[str]) -> dict:
        """把增量会话只输出的检查项与旧结果合成为完整维度结果。"""
        if previous is None:
            return part
        full_ids = [item["id"] for item in checklist["items"] if item["dimension"] == dimension]
        old_items = {item.get("id"): item for item in previous.get("items", [])}
        current_items = {item.get("id"): item for item in part.get("items", [])}
        merged_items = []
        for item_id in full_ids:
            if item_id in current_items:
                merged_items.append(current_items[item_id])
            elif item_id in old_items:
                old = copy.deepcopy(old_items[item_id])
                old["carried_over"] = True
                old.pop("change_reason", None)
                merged_items.append(old)
            else:
                raise ValueError(f"上次结果缺少维度 {dimension} 的检查项 {item_id}")
        current_issues = [copy.deepcopy(issue) for issue in part.get("issues", []) if issue.get("item_id") in model_ids]
        old_issues = [copy.deepcopy(issue) for issue in previous.get("issues", [])
                      if issue.get("item_id") in set(full_ids) - model_ids]
        groups = []
        for source, allowed in ((part.get("need_info_list", []), model_ids),
                                (previous.get("need_info_list", []), set(full_ids) - model_ids)):
            for group in source:
                selected = [item_id for item_id in group.get("items", []) if item_id in allowed]
                if selected:
                    copy_group = copy.deepcopy(group)
                    copy_group["items"] = list(dict.fromkeys(selected))
                    groups.append(copy_group)
        merged = dict(part)
        merged["items"] = merged_items
        merged["issues"] = [*current_issues, *old_issues]
        merged["need_info_list"] = groups
        if dimension == "1" and not merged.get("inventory"):
            merged["inventory"] = copy.deepcopy(previous.get("inventory", {}))
        if dimension == "6" and "6.8" not in model_ids:
            merged["prompt_injection_suspects"] = list(previous.get("prompt_injection_suspects", []))
        return merged

    def audit(dimension: str) -> dict:
        part_ctx, part_schema, part_checklist, full_schema, full_checklist = prepared[dimension]
        dimension_ids = {item["id"] for item in full_checklist["items"]}
        model_ids = {item["id"] for item in part_checklist["items"]}
        if previous is not None and plan is not None and not (dimension_ids & plan.affected_items):
            part = carried_part(dimension)
            evidence_stats = {}
            problems = check_output(part, part_checklist, repo, evidence_stats, part_schema)
            if problems:
                problems = repair_semantics(part, part_checklist, repo, evidence_stats,
                                            schema=part_schema)
            if problems:
                return {"attempts": [{"attempt": 0, "status": "invalid", "reused": True,
                                      "evidence_check": evidence_stats}],
                        "error": "沿用维度未通过证据核验"}
            return {"attempts": [{"attempt": 0, "status": "reused", "reused": True,
                                  "evidence_check": evidence_stats}],
                    "out": {"result": part, "cost_usd": 0, "session_id": None,
                             "usage": None, "stats": {"reused": True}}}
        part_prompt = prompt.replace(ctx_view(settings, ctx) + "/", ctx_view(settings, part_ctx) + "/")
        ids = [item["id"] for item in part_checklist["items"]]
        part_prompt += f"\n\n本次仅输出维度 {dimension} 的检查项：{', '.join(ids)}；其他维度由其他会话负责。"
        if plan is not None:
            recheck = sorted(dimension_ids & plan.affected_items, key=lambda value: (value.split(".")[0], value))
            part_prompt += (f"\n本维度本次必须重判：{', '.join(recheck) or '无'}；"
                            "其余检查项必须原样沿用并标记 carried_over=true。")
        attempts, best = [], None
        for attempt in (1, 2):
            metrics = {"attempt": attempt}
            attempts.append(metrics)
            try:
                with measure(metrics, "model_seconds"):
                    out = call_agent(part_prompt, repo, part_ctx, settings, timeout)
                metrics["stats"] = out.get("stats")
                metrics["usage_last_message"] = out.get("usage")
                metrics["cost_usd"] = out.get("cost_usd")
                with measure(metrics, "validation_seconds"):
                    metrics["evidence_check"] = {}
                    problems = check_output(out["result"], part_checklist, repo, metrics["evidence_check"], part_schema)
                    if previous is not None:
                        problems.extend(incremental_problems(out["result"], previous, changes or [], materials_changed, repo, plan))
                    if problems:
                        problems = repair_semantics(out["result"], part_checklist, repo,
                                                    metrics["evidence_check"], previous, plan, part_schema)
                        if previous is not None:
                            problems.extend(incremental_problems(out["result"], previous, changes or [],
                                                                materials_changed, repo, plan))
                best = (out, problems)
                if problems:
                    # 结构合格但语义修复后仍残留的问题不能靠重复同一提示词解决。
                    raise AgentOutputError("维度结果语义校验无法自动修复", problems, retryable=False)
            except AgentOutputError as exc:
                metrics.update(status="invalid", error_type=type(exc).__name__, validation_problem_count=len(exc.problems))
                if exc.stats is not None:
                    metrics["stats"] = exc.stats
                problems = exc.problems
                if not exc.retryable:
                    return {"attempts": attempts, "error": str(exc), "best": best}
            except Exception as exc:
                metrics.update(status="error", error_type=type(exc).__name__)
                if getattr(exc, "stats", None) is not None:
                    metrics["stats"] = exc.stats
                return {"attempts": attempts, "error": str(exc), "best": best}
            else:
                metrics.update(status="invalid" if problems else "ok", validation_problem_count=len(problems))
            if not problems:
                if previous is not None and model_ids != dimension_ids:
                    completed_out = out
                    try:
                        completed_part = complete_part(dimension, out["result"], model_ids)
                        full_stats = {}
                        full_problems = check_output(completed_part, full_checklist, repo, full_stats, full_schema)
                        if full_problems:
                            problems = full_problems
                        elif plan is not None:
                            problems = incremental_problems(completed_part, previous, changes or [],
                                                            materials_changed, repo, plan)
                        if not problems:
                            completed_out = dict(out, result=completed_part)
                            out = completed_out
                    except (ValueError, AgentOutputError) as exc:
                        problems = [str(exc)]
                    if problems:
                        # 合并旧结果后的完整维度仍未通过校验时，不能把未合并的局部结果当作成功。
                        # 这属于不可由重复同一会话修复的契约问题，交给上层以失败收口。
                        best = (completed_out, problems)
                        return {"attempts": attempts,
                                "error": "增量维度合并后校验无法通过",
                                "best": best}
                write_private_json(part_ctx / "verified_result.json", out["result"])
                return {"attempts": attempts, "out": out}
            log(f"维度 {dimension}: 第 {attempt} 次输出校验未通过: {problems[:3]}")
            if attempt == 1:
                shown = problems[:MAX_PROBLEMS_IN_PROMPT]
                if best is not None:
                    write_private_json(part_ctx / "previous_attempt.json", best[0]["result"])
                    part_prompt += (f"\n\n上次本维度的结果保存在 {ctx_view(settings, part_ctx)}/previous_attempt.json；"
                                    "请仅读取并修复有问题的检查项，保留其他已验证内容。")
                part_prompt += "\n\n## 本维度上次输出校验未通过，请修正\n" + "\n".join(f"- {p}" for p in shown)
        return {"attempts": attempts, "error": "两次输出仍未通过校验", "best": best}

    workers = max(1, min(len(dimensions), int(settings.get("agent", {}).get("dimension_parallelism", 2))))
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = {executor.submit(audit, dimension): dimension for dimension in dimensions}
        completed = {}
        for task in cf.as_completed(tasks):
            dimension = tasks[task]
            try:
                completed[dimension] = task.result()
            except Exception as exc:
                completed[dimension] = {"attempts": [], "error": f"{type(exc).__name__}: {exc}"}
    stats = {dimension: {"attempts": completed[dimension]["attempts"],
                         "status": "error" if "error" in completed[dimension] else "ok"}
             for dimension in dimensions}
    failed = [dimension for dimension in dimensions if "error" in completed[dimension]]
    if failed:
        details = "; ".join(f"{dimension}: {completed[dimension]['error']}" for dimension in failed)
        raise AgentOutputError(f"分维度审查失败（{details}），本次不评分", stats={"dimensions": stats}, retryable=False)
    parts = {dimension: completed[dimension]["out"]["result"] for dimension in dimensions}
    costs = [completed[dimension]["out"].get("cost_usd") for dimension in dimensions]
    return {"result": segments.merge(checklist, parts), "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
            "session_id": None, "usage": None, "stats": {"dimensions": stats}}


# ---------- 单项目流程 ----------

def save_unverified(outdir: Path, run_date: str, pid: str, commit: str, best: tuple) -> None:
    candidate, problems = best
    write_private_json(outdir / "unverified" / run_date / f"{pid}.json", {
        "project_id": pid, "date": run_date, "commit": commit,
        "status": "unverified", "problems": problems, "result": candidate["result"],
    })


def scan_project(p: dict, settings: dict, run_date: str, args, checklist: dict) -> dict:
    started = time.perf_counter()
    metrics = {"stages_seconds": {}, "model_attempts": []}
    pid = p["project_id"]
    work = Path(settings["workdir"]) / pid
    outdir = Path(settings["output_dir"])
    state_file = outdir / "state" / f"{pid}.json"
    day_dir = outdir / "results" / run_date
    day_dir.mkdir(parents=True, exist_ok=True)
    rec = {"project_id": pid, "project_name": p["project_name"], "department": p.get("department", ""),
           "owner": p.get("owner", ""), "date": run_date, "status": "ok", "metrics": metrics}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        repo = work / "repo"
        with measure(metrics["stages_seconds"], "git_sync"):
            commit = sync_repo(p, repo, int(settings.get("clone_depth", 200)))
        mdir = (p.get("materials_dir") or "").strip()
        with measure(metrics["stages_seconds"], "materials_hash"):
            mhash = dir_hash(repo / mdir) if mdir else ""
        prev_file = Path(state["last_result"]) if state.get("last_result") else None
        prev_ok = prev_file is not None and prev_file.exists()
        fingerprint = audit_fingerprint(p, settings)
        last_full = dt.date.fromisoformat(state["last_full_date"]) if state.get("last_full_date") else None
        today = dt.date.fromisoformat(run_date)
        materials_changed = mhash != state.get("materials_hash", "")

        changes = None
        if (args.force_full or not prev_ok or state.get("audit_fingerprint") != fingerprint or
                full_scan_due(pid, today, last_full, settings)):
            mode = "full"
        elif commit == state.get("last_commit") and not materials_changed:
            mode = "reuse"
        else:
            changes = changed_files(repo, state.get("last_commit", ""), commit) if commit != state.get("last_commit") else []
            if changes == [] and not materials_changed:
                mode = "reuse"  # 新提交未改变仓库文件；到期全量已在上面判断。
            else:
                mode = "incremental" if changes is not None else "full"
        # 生成文件、依赖缓存和其它配置的排除范围不应仅因为提交而唤醒模型；锁文件
        # 虽然不进入常规源码地图，仍是依赖/可复现性检查的触发源，不能过滤。
        effective_changes = changes
        if mode == "incremental" and changes is not None:
            project_scope_out = [s.strip() for s in (p.get("scope_out") or "").replace("；", ";").split(";") if s.strip()]
            planning_scope_out = project_scope_out + [pattern for pattern in settings.get("default_scope_out", [])
                                                       if pattern != "*.lock"]
            effective_changes = [path for path in changes
                                 if not code_map.excluded(incremental.normalize_path(path), planning_scope_out)]
            if not effective_changes and not materials_changed:
                rec["ignored_changes"] = list(changes)
                mode, changes = "reuse", []
            else:
                changes = effective_changes
        previous_result = None
        incremental_plan = None
        if mode == "incremental":
            try:
                previous_result = json.loads(prev_file.read_text(encoding="utf-8"))
                if check_output(previous_result, checklist):
                    raise ValueError("上次结果不自洽")
                incremental_plan = incremental.plan(checklist, previous_result, changes or [], materials_changed)
                if incremental_plan.full_scan:
                    mode = "full"
                    previous_result = None
                    changes = None
                    incremental_plan = None
                    log(f"{pid}: 变更影响面过大，升级全量扫描")
            except (OSError, UnicodeError, json.JSONDecodeError, AgentOutputError, ValueError):
                previous_result, changes, mode = None, None, "full"
                incremental_plan = None
                log(f"{pid}: 上次结果无效，回退全量扫描")
        rec.update({"commit": commit, "mode": mode})
        if incremental_plan is not None:
            rec["incremental_plan"] = {
                "changed_files": list(incremental_plan.changed_files),
                "affected_items": sorted(incremental_plan.affected_items),
                "affected_dimensions": sorted(incremental_plan.affected_dimensions),
                "reasons": {item_id: list(incremental_plan.reasons.get(item_id, ()))
                            for item_id in sorted(incremental_plan.affected_items)},
            }
        log(f"{pid}: {commit[:10]} → {mode}" + (f"（{len(changes)} 个变更文件）" if changes else ""))
        if args.dry_run:
            rec["status"] = "dry-run"
            return rec

        facts_data = None
        if mode == "reuse":
            data = json.loads(prev_file.read_text(encoding="utf-8"))
            verification_stats = {}
            with measure(metrics["stages_seconds"], "reuse_validation"):
                problems = check_output(data, checklist, repo, verification_stats)
                if problems:
                    problems = repair_semantics(data, checklist, repo, verification_stats)
            metrics["evidence_check"] = verification_stats
            if problems:
                raise AgentOutputError("上次结果无法通过源码引用核验，本次不评分", problems)
            data["meta"].update({"date": run_date, "mode": "reuse", "commit": commit,
                                 "reused_from": data["meta"].get("date")})
            data["meta"]["evidence_verified"] = True
            data["meta"]["evidence_check"] = verification_stats
            data["meta"]["usage"] = None
        else:
            # 只把本项目的上下文目录（预扫描结果 + 上次结果副本）开放给模型，不暴露其它项目
            ctx = work / "context"
            if ctx.exists():
                shutil.rmtree(ctx)
            pre_dir = ctx / "prescan"
            facts_file = None
            prescan_settings = dict(settings)
            project_scope_out = [s.strip() for s in (p.get("scope_out") or "").replace("；", ";").split(";") if s.strip()]
            prescan_settings["default_scope_out"] = project_scope_out + list(settings.get("default_scope_out", []))
            with measure(metrics["stages_seconds"], "prescan"):
                prescan_status = prescan(repo, pre_dir, settings.get("prescan_tools", []), prescan_settings,
                                          changes if mode == "incremental" else None)
            metrics["prescan_tools"] = prescan_status
            tools_done = [tool for tool, info in prescan_status.items() if info["status"] == "ok"]
            unavailable = [f"{tool}: {info.get('reason', info['status'])}" for tool, info in prescan_status.items()
                           if info["status"] != "ok"]
            if unavailable:
                log(f"{pid}: 预扫描未完成：{'；'.join(unavailable)}")
            map_file = None
            if settings.get("code_map_enabled", True):
                try:
                    with measure(metrics["stages_seconds"], "code_map"):
                        scope_out = prescan_settings["default_scope_out"]
                        repo_map = code_map.build(repo, commit, scope_out, settings.get("code_map_max_entries", 120))
                        (ctx / "code_map.json").write_text(json.dumps(repo_map, ensure_ascii=True, indent=2), encoding="utf-8")
                    map_file = "code_map.json"
                    metrics["code_map"] = {"status": "ok", "tracked_files": repo_map["tracked_files"]}
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    metrics["code_map"] = {"status": "failed", "reason": type(e).__name__}
                    log(f"{pid}: 代码地图生成失败，继续常规审查")
            try:
                with measure(metrics["stages_seconds"], "facts"):
                    facts_data = facts.collect(repo, commit, repo_map if map_file else None,
                                               prescan_settings["default_scope_out"], pre_dir,
                                               full_inventory=(mode != "incremental"),
                                               files=changes if mode == "incremental" else None,
                                               previous_inventory=(previous_result or {}).get("inventory"))
                    write_json_atomic(ctx / "facts.json", facts_data)
                facts_file = f"{ctx_view(settings, ctx)}/facts.json"
                metrics["facts"] = {
                    "status": "ok",
                    "finding_count": len(facts_data.get("findings", [])),
                    "tracked_files": facts_data.get("inventory", {}).get("file_count", 0),
                }
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                metrics["facts"] = {"status": "failed", "reason": type(e).__name__}
                log(f"{pid}: 确定性事实生成失败，继续模型审查")
            prev_copy = None
            if mode == "incremental":
                prev_copy = ctx / "previous_result.json"
                shutil.copyfile(prev_file, prev_copy)
            view = ctx_view(settings, ctx)
            changes_file = None
            if mode == "incremental" and changes and len(changes) > 800:
                write_json_atomic(ctx / "changed_files.json", {"files": changes})
                changes_file = f"{view}/changed_files.json"
            prompt = build_prompt(p, mode, commit, settings, f"{view}/prescan" if tools_done else None,
                                  f"{view}/previous_result.json" if prev_copy else None, changes, materials_changed, changes_file,
                                  f"{view}/{map_file}" if map_file else None, incremental_plan, facts_file)
            timeout = int(settings.get("per_project_timeout_sec", 3600))
            best = None  # 最近一次结构合格的输出（可能仍有语义问题）及其问题清单
            for attempt in (1, 2):  # 输出不合格时带着问题清单重试一次
                attempt_metrics = {"attempt": attempt}
                metrics["model_attempts"].append(attempt_metrics)
                try:
                    with measure(attempt_metrics, "model_seconds"):
                        if settings.get("agent", {}).get("strategy", "single") == "dimensions":
                            out = call_agent_dimensions(prompt, repo, ctx, settings, timeout, checklist,
                                                        previous_result, changes, materials_changed, incremental_plan)
                        else:
                            out = call_agent(prompt, repo, ctx, settings, timeout)
                    attempt_metrics["stats"] = out.get("stats")
                    attempt_metrics["usage_last_message"] = out.get("usage")
                    attempt_metrics["cost_usd"] = out.get("cost_usd")
                    with measure(attempt_metrics, "validation_seconds"):
                        attempt_metrics["evidence_check"] = {}
                        problems = check_output(out["result"], checklist, repo, attempt_metrics["evidence_check"])
                        if previous_result is not None:
                            problems.extend(incremental_problems(out["result"], previous_result, changes or [], materials_changed,
                                                                repo, incremental_plan))
                        if problems:
                            problems = repair_semantics(out["result"], checklist, repo,
                                                        attempt_metrics["evidence_check"], previous_result,
                                                        incremental_plan)
                            if previous_result is not None:
                                problems.extend(incremental_problems(out["result"], previous_result,
                                                                    changes or [], materials_changed, repo,
                                                                    incremental_plan))
                    best = (out, problems)
                    if problems:
                        raise AgentOutputError("结果语义校验无法自动修复，本次不评分", problems, retryable=False)
                except AgentOutputError as e:
                    # 结构不合格的输出不能拿去评分；两次都没有结构合格的输出，本项目记为失败
                    attempt_metrics.update({"status": "invalid", "error_type": type(e).__name__,
                                            "validation_problem_count": len(e.problems)})
                    if e.stats is not None:
                        attempt_metrics["stats"] = e.stats
                    if not e.retryable or (attempt == 2 and best is None):
                        if best is not None:
                            save_unverified(outdir, run_date, pid, commit, best)
                        raise
                    problems = e.problems
                except Exception as e:
                    attempt_metrics.update({"status": "error", "error_type": type(e).__name__})
                    if getattr(e, "stats", None) is not None:
                        attempt_metrics["stats"] = e.stats
                    raise
                else:
                    attempt_metrics.update({"status": "invalid" if problems else "ok",
                                            "validation_problem_count": len(problems)})
                if not problems:
                    break
                log(f"{pid}: 第 {attempt} 次输出校验未通过: {problems[:3]}")
                if attempt == 1:
                    shown = problems[:MAX_PROBLEMS_IN_PROMPT]
                    more = len(problems) - len(shown)
                    if best is not None:
                        write_json_atomic(ctx / "previous_attempt.json", best[0]["result"])
                        prompt += (f"\n\n上次结构合格的结果保存在 {view}/previous_attempt.json；"
                                   "请先读取该结果，仅修复下列问题，保留其他已验证内容；"
                                   "涉及源码引用时必须重新核实，最终仍输出完整 JSON。")
                    prompt += ("\n\n## 上次输出校验未通过，请修正后重新输出完整 JSON\n" + "\n".join(f"- {x}" for x in shown)
                               + (f"\n- ……另有 {more} 处同类问题" if more > 0 else ""))
            if problems:
                if best is not None:
                    save_unverified(outdir, run_date, pid, commit, best)
                raise AgentOutputError("两次输出仍未通过校验，本次不评分", problems)
            out, problems = best
            data = out["result"]
            if facts_data is not None:
                # 确定性扫描只允许覆盖本次确实需要重判的项目；不会把增量沿用项
                # 静默改写。发现只会上调风险，模型更严重的结论保持不变。
                allowed = incremental_plan.affected_items if incremental_plan is not None else None
                metrics["deterministic_overrides"] = facts.apply_findings(
                    data, facts_data.get("findings", []), allowed
                )
                post_stats = {}
                post_problems = check_output(data, checklist, repo, post_stats)
                if post_problems:
                    post_problems = repair_semantics(data, checklist, repo, post_stats)
                if post_stats:
                    evidence_stats = attempt_metrics.setdefault("evidence_check", {})
                    for key, value in post_stats.items():
                        if key in {"references", "invalid_references"}:
                            evidence_stats[key] = max(evidence_stats.get(key, 0), value)
                        else:
                            evidence_stats[key] = value
                if post_problems:
                    # 这类问题由 runner 的语义修复阶段处理；若仍残留，拒绝评分。
                    raise AgentOutputError("确定性事实合并后结果不自洽", post_problems, retryable=False)
            data["meta"] = {"date": run_date, "mode": mode, "commit": commit, "cost_usd": out["cost_usd"],
                            "session_id": out["session_id"], "prescan_tools": tools_done, "agent": settings.get("agent", {}).get("type", "claude"),
                            "prescan_status": prescan_status,
                            "usage": out.get("usage"),
                            "evidence_check": attempt_metrics.get("evidence_check", {}),
                            "validation_problems": problems, "evidence_verified": True, "scanned_at": now().isoformat()}
        if facts_data is not None:
            # inventory 是 runner 的确定性输出，不再接受模型对文件数/语言/入口的自由改写。
            data["inventory"] = facts_data["inventory"]
            if facts_data.get("prompt_injection_suspects"):
                data["prompt_injection_suspects"] = list(dict.fromkeys(
                    [*data.get("prompt_injection_suspects", []), *facts_data["prompt_injection_suspects"]]
                ))
            data.setdefault("meta", {})["deterministic_facts"] = {
                "finding_count": len(facts_data.get("findings", [])),
                "signals": facts_data.get("signals", {}),
            }
        data["meta"]["project"] = {k: p.get(k, "") for k in
                                   ("project_id", "project_name", "department", "owner", "git_url", "branch", "tech_stack")}
        with measure(metrics["stages_seconds"], "scoring"):
            weights = scoring.parse_weights(p.get("weights"), checklist)
            data["score"] = scoring.score_result(data, checklist, weights)
            for iss in data.get("issues", []):
                iss["fingerprint"] = scoring.issue_fingerprint(pid, iss)
        metrics["total_seconds"] = round(time.perf_counter() - started, 4)
        data["meta"]["metrics"] = metrics
        result_path = day_dir / f"{pid}.json"
        write_json_atomic(result_path, data)

        state.update({"last_commit": commit, "last_result": str(result_path), "materials_hash": mhash,
                      "last_scan_date": run_date, "audit_fingerprint": fingerprint})
        if mode == "full":
            state["last_full_date"] = run_date
        write_json_atomic(state_file, state)
        (day_dir / f"{pid}.error.txt").unlink(missing_ok=True)
        rec.update({"total": data["score"]["total"], "security": data["score"]["security_score"],
                    "cost_usd": data["meta"].get("cost_usd"), "validation_problems": len(data["meta"].get("validation_problems", []))})
    except Exception as e:  # 单项目失败不影响其它项目
        rec.update({"status": "error", "error": str(e)[:800]})
        log(f"{pid}: 失败 {e}")
        if not args.dry_run:
            (day_dir / f"{pid}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        metrics["total_seconds"] = round(time.perf_counter() - started, 4)
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
    problem = backend_problem(settings)
    if problem:
        log(f"拒绝启动：{problem}")
        return 2
    problem = network_problem(settings)
    if problem:
        if not args.dry_run:
            log(f"拒绝启动：{problem}")
            return 2
        log(f"警告：{problem}")
    try:
        model_concurrency = int(settings.get("max_model_concurrency", settings.get("max_parallel", 3)))
        dimension_parallelism = int(settings.get("agent", {}).get("dimension_parallelism", 2))
        if model_concurrency < 1 or dimension_parallelism < 1:
            raise ValueError
    except (TypeError, ValueError):
        log("拒绝启动：max_model_concurrency 和 dimension_parallelism 必须为正整数")
        return 2
    settings["_model_limiter"] = threading.BoundedSemaphore(model_concurrency)
    statuses = set(settings.get("scan_statuses", ["在研"]))
    projects = load_projects(Path(args.projects), statuses)
    if args.only:
        projects = [p for p in projects if p["project_id"] in set(args.only)]
    checklist = scoring.load_checklist()
    run_date = args.date or now().date().isoformat()
    log(f"开始 {run_date}：{len(projects)} 个项目，并发 {settings.get('max_parallel', 3)}")

    ids = [p["project_id"] for p in projects]
    if any(not pid or pid in {".", ".."} or pid.startswith("_") or len(pid) > 128 or
           not all(char.isalnum() or char in "-_." for char in pid) for pid in ids):
        log("拒绝启动：project_id 只能包含字母、数字、点、横线和下划线，且不能以 _ 开头")
        return 2
    if len(ids) != len(set(ids)):
        log("拒绝启动：项目清单中存在重复的 project_id")
        return 2
    with ExitStack() as stack:
        try:
            stack.enter_context(batch_lock(settings))
        except RuntimeError as e:
            log(f"拒绝启动：{e}")
            return 2
        outdir = Path(settings["output_dir"])
        runlog = outdir / "results" / run_date / ("_dry_run.json" if args.dry_run else "_run.json")
        progress = {"date": run_date, "started_at": now().isoformat(), "status": "running",
                    "finished_at": None, "projects": [], "pending_projects": ids.copy()}
        write_json_atomic(runlog, progress)
        completed = {}
        with cf.ThreadPoolExecutor(max_workers=int(settings.get("max_parallel", 3))) as ex:
            jobs = {ex.submit(scan_project, p, settings, run_date, args, checklist): p for p in projects}
            for job in cf.as_completed(jobs):
                p = jobs[job]
                try:
                    rec = job.result()
                except Exception as e:
                    rec = {"project_id": p["project_id"], "project_name": p["project_name"],
                           "status": "error", "error": str(e)[:800]}
                    log(f"{p['project_id']}: 工作线程失败 {e}")
                    if not args.dry_run:
                        marker = outdir / "results" / run_date / f"{p['project_id']}.error.txt"
                        marker.write_text(f"工作线程失败：{type(e).__name__}\n", encoding="utf-8")
                completed[p["project_id"]] = rec
                progress["projects"] = [completed[pid] for pid in ids if pid in completed]
                progress["pending_projects"] = [pid for pid in ids if pid not in completed]
                write_json_atomic(runlog, progress)
        recs = progress["projects"]
        progress.update(status="complete", finished_at=now().isoformat())
        write_json_atomic(runlog, progress)
        if not args.dry_run:
            import render
            render.render_all(outdir, run_date, Path(args.projects), settings)
        failed = [r for r in recs if r["status"] == "error"]
        log(f"完成：成功 {len(recs) - len(failed)}，失败 {len(failed)}")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
