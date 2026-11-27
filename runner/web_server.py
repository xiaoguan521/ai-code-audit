#!/usr/bin/env python3
"""八性代码质量审查平台 · Web API 与静态前端服务。

零外部依赖（仅用 Python 标准库 + 项目内 report_validate / scoring），支持：
- 质量看板总览（KPI、历史趋势、8 维度均分、项目排行）
- 批次与运行记录浏览（results/<date>/<run_id>/_run.json）
- Markdown 报告在线阅读与结构化解析（单项目报告 + _summary.md）
- 项目清单查看、触发扫描任务（run_daily.py）与实时日志轮询
- 生产模式下自动托管 frontend/dist 静态资源

用法：
    python3 runner/web_server.py --port 8787
    python3 runner/web_server.py --data-dir /data/ai-audit/output --port 8787
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNNER_DIR))

import doctor  # noqa: E402
import docx_export  # noqa: E402
import mailer  # noqa: E402
import platform_db  # noqa: E402
import report_validate  # noqa: E402
import scoring  # noqa: E402
from adapters import summary as summary_adapter  # noqa: E402

SAFE_SEG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LEVELS = ("P0", "P1", "P2", "P3")
SCAN_TASKS: dict[str, dict] = {}
SCAN_LOCK = threading.Lock()
REPORT_CACHE: dict[tuple[str, int, str], dict] = {}
REPORT_CACHE_LOCK = threading.Lock()
REPORT_CACHE_MAX = 256
AUTH_COOKIE_NAME = "audit_session"
SESSION_TTL_SEC = 24 * 3600
LOGIN_WINDOW_SEC = 300
LOGIN_MAX_FAILURES = 10
AUTH_LOCK = threading.Lock()
SESSIONS: dict[str, float] = {}
LOGIN_FAILURES: dict[str, deque[float]] = {}
SCHEDULE_LOCK = threading.Lock()
TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ACTIVE_PROJECT_SEEN: dict[str, float] = {}
ACTIVE_SEEN_LOCK = threading.Lock()


def _default_schedule() -> dict:
    return {
        "enabled": False,
        "time": "02:00",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
        "fast": True,
        "resume": True,
        "force_full": False,
        "only": [],
        "last_triggered_slot": None,
        "last_triggered_at": None,
        "last_task_id": None,
        "last_error": None,
    }


def _schedule_file(output_dir: Path) -> Path:
    return output_dir / "state" / "schedule.json"


def _load_schedule(output_dir: Path) -> dict:
    base = _default_schedule()
    raw = _read_json(_schedule_file(output_dir))
    if isinstance(raw, dict):
        base.update({k: v for k, v in raw.items() if k in base})
    return base


def _save_schedule(output_dir: Path, data: dict) -> None:
    target = _schedule_file(output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _next_run_at(schedule: dict, now_dt: dt.datetime | None = None) -> str | None:
    if not schedule.get("enabled"):
        return None
    m = TIME_HHMM_RE.fullmatch(str(schedule.get("time") or ""))
    weekdays = set(schedule.get("weekdays") or [])
    if not m or not weekdays:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    now = now_dt or dt.datetime.now()
    for offset in range(0, 8):
        cand_date = now.date() + dt.timedelta(days=offset)
        if cand_date.isoweekday() not in weekdays:
            continue
        cand = dt.datetime.combine(cand_date, dt.time(hour, minute))
        slot = cand.strftime("%Y-%m-%d %H:%M")
        if cand >= now.replace(second=0, microsecond=0) and slot != schedule.get("last_triggered_slot"):
            if cand > now or (cand.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M")):
                return slot
    return None


def _update_schedule(output_dir: Path, payload: dict) -> dict:
    with SCHEDULE_LOCK:
        cur = _load_schedule(output_dir)
        enabled = bool(payload.get("enabled", cur["enabled"]))
        time_str = str(payload.get("time", cur["time"])).strip()
        if not TIME_HHMM_RE.fullmatch(time_str):
            return {"bad_request": True, "error": "执行时间格式必须为 HH:MM（24 小时制，例如 02:00）"}
        raw_days = payload.get("weekdays", cur["weekdays"])
        if not isinstance(raw_days, list):
            return {"bad_request": True, "error": "weekdays 必须为 1–7 的整数列表"}
        try:
            weekdays = sorted({int(d) for d in raw_days if 1 <= int(d) <= 7})
        except (TypeError, ValueError):
            return {"bad_request": True, "error": "weekdays 必须为 1–7 的整数列表"}
        if enabled and not weekdays:
            return {"bad_request": True, "error": "启用定时扫描时至少需要选择一个星期"}
        fast = bool(payload.get("fast", cur["fast"]))
        resume = bool(payload.get("resume", cur["resume"]))
        force_full = bool(payload.get("force_full", cur["force_full"]))
        if resume and force_full:
            return {"bad_request": True, "error": "--resume 不能与 --force-full 同时启用"}
        raw_only = payload.get("only", cur["only"])
        if not isinstance(raw_only, list):
            return {"bad_request": True, "error": "only 必须为项目 ID 列表"}
        only = [str(x) for x in raw_only if _is_safe_segment(str(x))]
        cur.update({
            "enabled": enabled,
            "time": time_str,
            "weekdays": weekdays or [1, 2, 3, 4, 5, 6, 7],
            "fast": fast,
            "resume": resume,
            "force_full": force_full,
            "only": only,
        })
        _save_schedule(output_dir, cur)
        now = dt.datetime.now()
        return {
            "schedule": cur,
            "next_run_at": _next_run_at(cur, now),
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        }


def _schedule_tick(cfg: dict, now_dt: dt.datetime | None = None) -> dict | None:
    """检查当前分钟是否命中定时配置；命中则异步启动扫描并持久化触发槽位。"""
    output_dir: Path = cfg["output_dir"]
    now = now_dt or dt.datetime.now()
    slot = now.strftime("%Y-%m-%d %H:%M")
    hhmm = now.strftime("%H:%M")
    weekday = now.isoweekday()
    with SCHEDULE_LOCK:
        sched = _load_schedule(output_dir)
        if not sched.get("enabled"):
            return None
        if sched.get("time") != hhmm or weekday not in (sched.get("weekdays") or []):
            return None
        if sched.get("last_triggered_slot") == slot:
            return None
        sched["last_triggered_slot"] = slot
        sched["last_triggered_at"] = now.isoformat(timespec="seconds")
        payload = {
            "only": sched.get("only") or None,
            "fast": bool(sched.get("fast", True)),
            "resume": bool(sched.get("resume", True)),
            "force_full": bool(sched.get("force_full", False)),
            "dry_run": False,
            "summary_only": False,
        }
        res = _start_scan_task(
            payload, ROOT, cfg["settings_path"], _effective_projects_csv(cfg),
            trigger="schedule", output_dir=output_dir,
        )
        if res.get("task_id"):
            sched["last_task_id"] = res["task_id"]
            sched["last_error"] = None
        else:
            sched["last_error"] = res.get("error") or "定时触发失败"
        _save_schedule(output_dir, sched)
        return res


def _is_project_actively_scanning(workdir: Path | None, pid: str) -> tuple[bool, float | None]:
    """通过非阻塞检查 <workdir>/.locks/<pid>.lock 判断项目是否正在扫描，并估算其开始时间戳。"""
    if not workdir or fcntl is None:
        return False, None
    lock_path = workdir / ".locks" / f"{pid}.lock"
    if not lock_path.is_file():
        return False, None
    locked = False
    try:
        with lock_path.open("r+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                locked = True
    except OSError:
        return False, None
    if not locked:
        with ACTIVE_SEEN_LOCK:
            ACTIVE_PROJECT_SEEN.pop(pid, None)
        return False, None

    now_ts = time.time()
    # 优先使用最近重建的 <workdir>/<pid>/context 目录时间作为该项目的扫描开始时间
    started_ts: float | None = None
    ctx_dir = workdir / pid / "context"
    try:
        if ctx_dir.exists():
            st = ctx_dir.stat().st_mtime
            if 0 <= now_ts - st <= 86400:
                started_ts = st
    except OSError:
        pass
    with ACTIVE_SEEN_LOCK:
        if started_ts is not None:
            ACTIVE_PROJECT_SEEN[pid] = started_ts
        else:
            started_ts = ACTIVE_PROJECT_SEEN.setdefault(pid, now_ts)
    return True, started_ts


def _project_duration_seconds(rec: dict) -> float | None:
    metrics = rec.get("metrics") or {}
    total = metrics.get("total_seconds")
    if isinstance(total, (int, float)) and total >= 0:
        return float(total)
    stages = metrics.get("stages_seconds") or {}
    vals = [float(v) for v in stages.values() if isinstance(v, (int, float)) and v >= 0]
    return sum(vals) if vals else None


def _parse_iso_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_real_model_scan(rec: dict, dur: float | None) -> bool:
    """只把真正进入模型审查的项目计入耗时样本，剔除秒级 clone 失败与 resume 复用。"""
    if dur is None or dur < 15.0:
        return False
    if rec.get("scan_type") == "resume" or rec.get("status") == "dry-run":
        return False
    stages = (rec.get("metrics") or {}).get("stages_seconds") or {}
    model_sec = stages.get("model")
    if isinstance(model_sec, (int, float)):
        return model_sec >= 10.0
    return rec.get("status") == "ok"


def _build_run_progress(date_str: str, run_id: str, data: dict,
                        catalog: dict[str, dict] | None = None,
                        workdir: Path | None = None,
                        settings: dict | None = None,
                        hist_by_pid: dict[str, float] | None = None,
                        hist_global_avg: float | None = None,
                        output_dir: Path | None = None) -> dict:
    cat = catalog or {}
    cfg_settings = settings or {}
    pid_hist = hist_by_pid or {}
    projects = data.get("projects") or []
    pending = [str(p) for p in (data.get("pending_projects") or [])]
    completed_count = len(projects)
    total_count = completed_count + len(pending)
    ok_count = sum(1 for p in projects if p.get("status") == "ok")
    error_count = sum(1 for p in projects if p.get("status") != "ok")
    percent = round(completed_count * 100 / total_count) if total_count > 0 else (100 if data.get("status") != "running" else 0)

    now_ts = time.time()
    started_ts = _parse_iso_ts(data.get("started_at"))
    finished_ts = _parse_iso_ts(data.get("finished_at"))
    elapsed_seconds = (
        max(0, round((finished_ts or now_ts) - started_ts))
        if started_ts is not None else None
    )

    # 只使用真实跑过模型的项目耗时作为均值样本（剔除几秒钟的 clone 失败和 resume 缓存命中）
    real_durations = []
    for p in projects:
        d = _project_duration_seconds(p)
        if _is_real_model_scan(p, d):
            assert d is not None
            real_durations.append(d)
    configured_parallel = max(1, int(cfg_settings.get("max_parallel", 2) or 2))
    per_project_timeout = float(cfg_settings.get("per_project_timeout_sec", 3600) or 3600)

    # 检查 pending 项目中哪些正在扫描（持锁）
    scanning_map: dict[str, float | None] = {}
    if data.get("status") == "running" and pending:
        for pid in pending:
            active, proj_start = _is_project_actively_scanning(workdir, pid)
            if active:
                scanning_map[pid] = proj_start

    effective_parallel = max(1, len(scanning_map) or configured_parallel)
    avg_project_sec: float | None = None
    if real_durations:
        avg_project_sec = sum(real_durations) / len(real_durations)
    elif hist_global_avg is not None:
        avg_project_sec = hist_global_avg

    items: list[dict] = []
    res_dir = _resolve_run_file(output_dir, date_str, run_id).parent if output_dir else None
    for p in projects:
        pid = p.get("project_id", "")
        meta = cat.get(pid, {})
        dur = _project_duration_seconds(p)
        v_probs = (p.get("report_validation") or {}).get("problems") or []
        s_pats = (p.get("secret_scan") or {}).get("patterns") or []
        has_rej = bool(res_dir and _is_safe_segment(pid) and (res_dir / f"{pid}.rejected.md").is_file())
        items.append({
            "project_id": pid,
            "project_name": p.get("project_name") or meta.get("project_name") or pid,
            "department": (p.get("department") or meta.get("department") or "").strip() or "未分组",
            "owner": p.get("owner") or meta.get("owner") or "",
            "state": "ok" if p.get("status") == "ok" else "error",
            "status": p.get("status", "unknown"),
            "total": (p.get("report_scores") or {}).get("total"),
            "error": p.get("error"),
            "validation_problems": v_probs,
            "secret_patterns": s_pats,
            "has_rejected_draft": has_rej,
            "duration_seconds": round(dur) if dur is not None else None,
            "elapsed_seconds": None,
            "eta_seconds": None,
            "timeout_remaining_seconds": None,
        })

    scanning_etas: list[float] = []
    queued_etas: list[float] = []
    queued_count = 0
    for pid in pending:
        meta = cat.get(pid, {})
        is_scanning = pid in scanning_map
        proj_elapsed = None
        proj_eta = None
        timeout_rem = None
        # 优先取该项目的历史真实耗时，否则回退到当前/历史全局均耗
        expected_dur = pid_hist.get(pid) if pid in pid_hist else avg_project_sec
        if is_scanning:
            p_start = scanning_map[pid]
            proj_elapsed = max(0, round(now_ts - p_start)) if p_start is not None else 0
            timeout_rem = max(0, round(per_project_timeout - proj_elapsed))
            if expected_dur is not None:
                proj_eta = max(0, round(expected_dur - proj_elapsed))
                scanning_etas.append(float(proj_eta))
        else:
            queued_count += 1
            if expected_dur is not None:
                queued_etas.append(float(expected_dur))
        items.append({
            "project_id": pid,
            "project_name": meta.get("project_name") or pid,
            "department": (meta.get("department") or "").strip() or "未分组",
            "owner": meta.get("owner") or "",
            "state": "scanning" if is_scanning else "queued",
            "status": "scanning" if is_scanning else "queued",
            "total": None,
            "error": None,
            "duration_seconds": None,
            "elapsed_seconds": proj_elapsed,
            "eta_seconds": proj_eta,
            "timeout_remaining_seconds": timeout_rem,
        })

    estimated_remaining_seconds: int | None = None
    if data.get("status") == "running" and pending and (scanning_etas or queued_etas):
        # 整体耗时至少不小于当前最慢的并行扫描项，同时计入排队项目按并发折算的时间
        max_scanning = max(scanning_etas) if scanning_etas else 0.0
        total_work = sum(scanning_etas) + sum(queued_etas)
        estimated_remaining_seconds = max(0, round(max(max_scanning, total_work / effective_parallel)))

    return {
        "date": data.get("date") or date_str,
        "run_id": data.get("run_id") or run_id,
        "status": data.get("status", "unknown"),
        "report_mode": data.get("report_mode", ""),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "snapshot_at": data.get("snapshot_at"),
        "summary_status": data.get("summary_status"),
        "elapsed_seconds": elapsed_seconds,
        "avg_project_seconds": round(avg_project_sec) if avg_project_sec is not None else None,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "parallelism": effective_parallel,
        "total_count": total_count,
        "completed_count": completed_count,
        "ok_count": ok_count,
        "error_count": error_count,
        "scanning_count": len(scanning_map),
        "queued_count": queued_count,
        "pending_count": len(pending),
        "percent": percent,
        "items": items,
        "completed_projects": [
            {
                "project_id": p.get("project_id", ""),
                "project_name": p.get("project_name") or p.get("project_id", ""),
                "status": p.get("status", "unknown"),
                "total": (p.get("report_scores") or {}).get("total"),
                "error": p.get("error"),
            }
            for p in projects
        ],
        "pending_projects": pending,
    }


def _resolve_workdir(output_dir: Path, settings: dict) -> Path:
    env_data_dir = os.environ.get("AUDIT_DATA_DIR", "").strip()
    if env_data_dir:
        return (Path(env_data_dir) / "work").resolve()
    if settings.get("workdir"):
        cand = Path(settings["workdir"]).resolve()
        if cand.exists():
            return cand
    return (output_dir.parent / "work").resolve()


def _managed_projects_csv(output_dir: Path) -> Path:
    return output_dir / "state" / "projects.managed.csv"


def _prompts_dir(output_dir: Path) -> Path:
    return output_dir / "state" / "prompts"


def _runtime_model_env_file(output_dir: Path) -> Path:
    return output_dir / "state" / "model.runtime.env"


def _runtime_overrides_file(output_dir: Path) -> Path:
    return output_dir / "state" / "runtime_overrides.json"


def _runtime_git_creds_file(output_dir: Path) -> Path:
    return output_dir / "state" / "git-credentials.runtime"


def _base_model_env_file(settings_path: Path | None) -> Path | None:
    env_p = os.environ.get("AUDIT_MODEL_ENV_PATH", "").strip()
    if env_p and Path(env_p).is_file():
        return Path(env_p)
    settings = (_read_json(settings_path) if settings_path else None) or {}
    ag_env = (settings.get("agent") or {}).get("env_file") if isinstance(settings, dict) else None
    if ag_env and Path(str(ag_env)).is_file():
        return Path(str(ag_env))
    cand = Path("/run/secrets/model.env")
    return cand if cand.is_file() else None


def _base_git_creds_file() -> Path | None:
    cand = Path(os.environ.get("AUDIT_GIT_CREDENTIAL_FILE", "/run/secrets/git-credentials"))
    return cand if cand.is_file() else None


def _default_prompt_content(prompt_key: str) -> str:
    if prompt_key == "skill_md":
        p = ROOT / "skill" / "eight-quality-audit" / "SKILL.md"
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    if prompt_key == "summary_prompt":
        return summary_adapter.DEFAULT_SUMMARY_PROMPT
    return ""


def _build_prompts_payload(db: platform_db.PlatformDB) -> list[dict]:
    meta_map = [
        (
            "skill_md",
            "Harness 核心审查规范 (SKILL.md)",
            "每次启动扫描容器时热挂载覆盖 /opt/audit/skills/eight-quality-code-audit/SKILL.md，即时生效、免重构建镜像。",
        ),
        (
            "dispatch_extra",
            "单项目调度补充提示词",
            "追加到每个项目的 Harness 调度指令末尾，适合添加公司专项审查要求、框架规范或临时审查口径。",
        ),
        (
            "summary_prompt",
            "批次整体汇总提示词",
            "供汇总 Agent 深度撰写管理层报告第四、五章使用，支持 {compact_digest} 统计摘要占位符。",
        ),
    ]
    out: list[dict] = []
    for key, title, desc in meta_map:
        default_text = _default_prompt_content(key)
        active = db.get_active_prompt(key)
        is_custom = bool(active and (key != "dispatch_extra" or str(active.get("content") or "").strip()))
        out.append({
            "key": key,
            "title": title,
            "description": desc,
            "is_custom": is_custom,
            "active_version": active["version"] if active else 0,
            "updated_by": active["updated_by"] if active else "builtin",
            "updated_at": active["created_at"] if active else None,
            "content": active["content"] if active else default_text,
            "default_content": default_text,
            "versions": db.list_prompt_versions(key, limit=20),
        })
    return out


def _read_effective_model_env(output_dir: Path, settings_path: Path | None) -> tuple[dict[str, str], str]:
    base_file = _base_model_env_file(settings_path)
    base_data = platform_db.parse_env_file(base_file) if base_file else {}
    rt_file = _runtime_model_env_file(output_dir)
    if rt_file.is_file():
        merged = dict(base_data)
        merged.update(platform_db.parse_env_file(rt_file))
        return merged, "runtime_override"
    return base_data, ("mounted_env" if base_file else "unconfigured")


def _build_ops_config(output_dir: Path, settings_path: Path | None, db: platform_db.PlatformDB) -> dict:
    raw_model, model_source = _read_effective_model_env(output_dir, settings_path)
    known_model_keys = (
        "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
        "OPENAI_CONTEXT_WINDOW", "OPENAI_MAX_OUTPUT_TOKENS",
        "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
    )
    extra_env = {}
    for k, v in raw_model.items():
        if k in known_model_keys:
            continue
        is_secret = any(tok in k.upper() for tok in ("KEY", "SECRET", "TOKEN", "PASS"))
        extra_env[k] = platform_db.mask_secret(v) if is_secret else v

    model_view = {
        "source": model_source,
        "OPENAI_BASE_URL": raw_model.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL": raw_model.get("OPENAI_MODEL", ""),
        "OPENAI_API_KEY_SET": bool(raw_model.get("OPENAI_API_KEY")),
        "OPENAI_API_KEY_MASKED": platform_db.mask_secret(raw_model.get("OPENAI_API_KEY", "")),
        "OPENAI_CONTEXT_WINDOW": raw_model.get("OPENAI_CONTEXT_WINDOW", "131072"),
        "OPENAI_MAX_OUTPUT_TOKENS": raw_model.get("OPENAI_MAX_OUTPUT_TOKENS", "8192"),
        "DEEPSEEK_BASE_URL": raw_model.get("DEEPSEEK_BASE_URL", ""),
        "DEEPSEEK_MODEL": raw_model.get("DEEPSEEK_MODEL", ""),
        "DEEPSEEK_API_KEY_SET": bool(raw_model.get("DEEPSEEK_API_KEY")),
        "DEEPSEEK_API_KEY_MASKED": platform_db.mask_secret(raw_model.get("DEEPSEEK_API_KEY", "")),
        "extra_env": extra_env,
    }

    settings = (_read_json(settings_path) if settings_path else None) or {}
    if not isinstance(settings, dict):
        settings = {}
    scale = settings.get("scale_timeouts") if isinstance(settings.get("scale_timeouts"), dict) else {}
    ag = settings.get("agent") if isinstance(settings.get("agent"), dict) else {}
    summ = settings.get("summary") if isinstance(settings.get("summary"), dict) else {}

    def _env_int(env_k: str, fallback: int) -> int:
        raw = os.environ.get(env_k, "").strip()
        try:
            return int(raw) if raw else fallback
        except ValueError:
            return fallback

    def _env_bool_val(env_k: str, fallback: bool) -> bool:
        raw = os.environ.get(env_k, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return fallback

    runtime = {
        "max_parallel": _env_int("AUDIT_MAX_PARALLEL", int(settings.get("max_parallel", 6))),
        "max_model_concurrency": _env_int("AUDIT_MAX_MODEL_CONCURRENCY", int(settings.get("max_model_concurrency", 3))),
        "per_project_timeout_sec": _env_int("AUDIT_PER_PROJECT_TIMEOUT_SEC", int(settings.get("per_project_timeout_sec", 3600))),
        "timeout_small_sec": _env_int("AUDIT_TIMEOUT_SMALL_SEC", int(scale.get("small", 2700))),
        "timeout_medium_sec": _env_int("AUDIT_TIMEOUT_MEDIUM_SEC", int(scale.get("medium", 3600))),
        "timeout_large_sec": _env_int("AUDIT_TIMEOUT_LARGE_SEC", int(scale.get("large", 7200))),
        "batch_timeout_sec": _env_int("AUDIT_BATCH_TIMEOUT_SEC", int(settings.get("batch_timeout_sec", 129600))),
        "fast_mode": _env_bool_val("AUDIT_FAST_MODE", bool(settings.get("fast_mode", False))),
        "fast_skip_lizard": _env_bool_val("AUDIT_FAST_SKIP_LIZARD", bool(settings.get("fast_skip_lizard", True))),
        "adaptive_timeout": _env_bool_val("AUDIT_ADAPTIVE_TIMEOUT", bool(settings.get("adaptive_timeout", True))),
        "validation_mode": (os.environ.get("AUDIT_VALIDATION_MODE") or settings.get("validation_mode") or "lenient").strip(),
        "summary_enabled": _env_bool_val("AUDIT_SUMMARY_ENABLED", bool(summ.get("enabled", True))),
        "scanner_image": (os.environ.get("AUDIT_SCANNER_IMAGE") or ag.get("image") or "").strip(),
        "model_network": (os.environ.get("AUDIT_MODEL_NETWORK") or ag.get("network") or "").strip(),
        "semgrep_rules_dir": (os.environ.get("AUDIT_SEMGREP_RULES_DIR") or settings.get("semgrep_rules_dir") or "").strip(),
    }
    ov_file = _runtime_overrides_file(output_dir)
    ov = _read_json(ov_file) if ov_file.is_file() else None
    if isinstance(ov, dict):
        for k in runtime:
            if k in ov and ov[k] is not None:
                runtime[k] = ov[k]

    return {
        "model": model_view,
        "runtime": runtime,
        "runtime_overridden": ov_file.is_file(),
        "git_credentials": db.list_git_credentials(include_secret=False),
        "base_git_creds_present": _base_git_creds_file() is not None,
    }


def _effective_projects_csv(cfg: dict) -> Path:
    managed = _managed_projects_csv(cfg["output_dir"])
    return managed if managed.is_file() else cfg["projects_csv"]


def _pause_flag_path(output_dir: Path) -> Path:
    return output_dir / "state" / "pause.flag"


def _get_pause_state(output_dir: Path) -> dict:
    flag = _pause_flag_path(output_dir)
    if not flag.is_file():
        return {"paused": False, "paused_at": None, "paused_by": "", "reason": ""}
    data = _read_json(flag)
    if isinstance(data, dict):
        return {
            "paused": True,
            "paused_at": data.get("paused_at"),
            "paused_by": data.get("paused_by") or "",
            "reason": data.get("reason") or "",
        }
    return {"paused": True, "paused_at": None, "paused_by": "", "reason": ""}


def _set_pause_state(output_dir: Path, paused: bool, actor: dict | None = None, reason: str = "") -> dict:
    flag = _pause_flag_path(output_dir)
    flag.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        info = {
            "paused": True,
            "paused_at": dt.datetime.now().isoformat(timespec="seconds"),
            "paused_by": (actor or {}).get("username", ""),
            "reason": str(reason or "").strip()[:200],
        }
        tmp = flag.with_suffix(".flag.tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, flag)
        return info
    try:
        flag.unlink(missing_ok=True)
    except OSError:
        pass
    return {"paused": False, "paused_at": None, "paused_by": "", "reason": ""}


def _merge_project_code_metrics(meta: dict, st: dict, thresholds: dict[str, int]) -> dict:
    """合并 SQLite 项目元数据与 state/<pid>.json 的代码量与最后提交时间，计算长期未维护预警。"""
    cs = st.get("code_stats") if isinstance(st.get("code_stats"), dict) else {}
    loc_total = cs.get("loc_total") if cs.get("loc_total") is not None else meta.get("loc_total")
    file_count = cs.get("file_count") if cs.get("file_count") is not None else meta.get("file_count")
    languages = cs.get("languages") or meta.get("languages") or {}
    last_commit_at = cs.get("last_commit_at") or meta.get("last_commit_at") or None
    last_commit_author = cs.get("last_commit_author") or meta.get("last_commit_author") or ""
    last_commit_subject = cs.get("last_commit_subject") or meta.get("last_commit_subject") or ""
    inactive_days, stale_level = platform_db.compute_stale_info(
        last_commit_at,
        warn_days=thresholds.get("warn_days", 90),
        critical_days=thresholds.get("critical_days", 180),
    )
    return {
        "loc_total": loc_total,
        "file_count": file_count,
        "languages": languages if isinstance(languages, dict) else {},
        "last_commit_at": last_commit_at,
        "last_commit_author": last_commit_author,
        "last_commit_subject": last_commit_subject,
        "inactive_days": inactive_days,
        "stale_level": stale_level,
    }


def _refresh_projects_metrics(output_dir: Path, settings_path: Path | None,
                              db: platform_db.PlatformDB,
                              project_ids: list[str] | None = None) -> dict:
    """扫描 workdir/<pid>/repo 已有本地仓库，刷新代码量、语言分布与最后提交时间（不调用模型）。"""
    settings = (_read_json(settings_path) if settings_path else None) or {}
    workdir = _resolve_workdir(output_dir, settings if isinstance(settings, dict) else {})
    projects = db.list_projects()
    target_set = set(project_ids) if project_ids else None
    scanned, updated, missing_repo = 0, 0, 0
    for p in projects:
        pid = p["project_id"]
        if target_set is not None and pid not in target_set:
            continue
        scanned += 1
        repo_dir = workdir / pid / "repo"
        if not repo_dir.is_dir():
            missing_repo += 1
            continue
        cloc_cand = workdir / pid / "context" / "prescan" / "cloc.json"
        metrics = platform_db.collect_repo_metrics(repo_dir, cloc_cand if cloc_cand.is_file() else None)
        if metrics.get("loc_total") is not None or metrics.get("last_commit_at"):
            db.update_project_metrics(
                pid,
                loc_total=metrics.get("loc_total"),
                file_count=metrics.get("file_count"),
                languages=metrics.get("languages"),
                last_commit_at=metrics.get("last_commit_at"),
                last_commit_author=metrics.get("last_commit_author") or "",
                last_commit_subject=metrics.get("last_commit_subject") or "",
            )
            updated += 1
    return {"scanned": scanned, "updated": updated, "missing_repo": missing_repo}


def _allowed_pids_for_user(user: dict | None, projects_csv: Path | None,
                           db: platform_db.PlatformDB | None = None) -> set[str] | None:
    """superadmin/admin 返回 None（不限）；普通用户返回其负责的 project_id 集合。"""
    if not user or user.get("role") in ("superadmin", "admin"):
        return None
    catalog = _load_catalog(projects_csv, db=db)
    return {p["project_id"] for p in catalog if platform_db.user_owns_project(user, p)}


def _filter_run_progress(item: dict, allowed_pids: set[str]) -> dict:
    filtered_items = [x for x in (item.get("items") or []) if x.get("project_id") in allowed_pids]
    filtered_completed = [x for x in (item.get("completed_projects") or []) if x.get("project_id") in allowed_pids]
    filtered_pending = [pid for pid in (item.get("pending_projects") or []) if pid in allowed_pids]
    ok_count = sum(1 for x in filtered_items if x.get("state") == "ok")
    err_count = sum(1 for x in filtered_items if x.get("state") == "error")
    scanning_count = sum(1 for x in filtered_items if x.get("state") == "scanning")
    queued_count = sum(1 for x in filtered_items if x.get("state") == "queued")
    completed_count = ok_count + err_count
    total_count = len(filtered_items)
    percent = round(completed_count * 100 / total_count) if total_count > 0 else 0
    out = dict(item)
    out.update({
        "total_count": total_count,
        "completed_count": completed_count,
        "ok_count": ok_count,
        "error_count": err_count,
        "scanning_count": scanning_count,
        "queued_count": queued_count,
        "pending_count": len(filtered_pending),
        "percent": percent,
        "items": filtered_items,
        "completed_projects": filtered_completed,
        "pending_projects": filtered_pending,
    })
    return out


def _build_live_progress(output_dir: Path, projects_csv: Path | None = None,
                         settings_path: Path | None = None,
                         user: dict | None = None,
                         db: platform_db.PlatformDB | None = None) -> dict:
    allowed_pids = _allowed_pids_for_user(user, projects_csv, db=db)
    catalog = {p["project_id"]: p for p in _load_catalog(projects_csv, db=db)}
    settings = (_read_json(settings_path) if settings_path else None) or {}
    if not isinstance(settings, dict):
        settings = {}
    workdir = _resolve_workdir(output_dir, settings)
    runs = _discover_runs(output_dir)
    loaded_runs: list[tuple[dict, dict]] = []
    hist_by_pid_mode: dict[tuple[str, str], float] = {}
    hist_by_pid_any: dict[str, float] = {}
    hist_by_mode: dict[str, list[float]] = {}
    hist_all: list[float] = []
    for r in runs[:10]:
        data = _read_json(_resolve_run_file(output_dir, r["date"], r["run_id"]))
        if not isinstance(data, dict):
            continue
        loaded_runs.append((r, data))
        mode = str(data.get("report_mode") or "")
        for p in data.get("projects") or []:
            d = _project_duration_seconds(p)
            if _is_real_model_scan(p, d):
                assert d is not None
                pid = str(p.get("project_id") or "")
                if pid:
                    hist_by_pid_mode.setdefault((pid, mode), d)
                    hist_by_pid_any.setdefault(pid, d)
                hist_by_mode.setdefault(mode, []).append(d)
                hist_all.append(d)

    active_runs: list[dict] = []
    latest_run: dict | None = None
    for idx, (r, data) in enumerate(loaded_runs):
        mode = str(data.get("report_mode") or "")
        mode_samples = hist_by_mode.get(mode) or hist_all
        hist_avg = (sum(mode_samples) / len(mode_samples)) if mode_samples else None
        pid_hist = dict(hist_by_pid_any)
        for (pid, m), val in hist_by_pid_mode.items():
            if m == mode:
                pid_hist[pid] = val
        item = _build_run_progress(
            r["date"], r["run_id"], data,
            catalog=catalog, workdir=workdir, settings=settings,
            hist_by_pid=pid_hist, hist_global_avg=hist_avg,
            output_dir=output_dir,
        )
        if allowed_pids is not None:
            item = _filter_run_progress(item, allowed_pids)
            if item["total_count"] == 0:
                continue
        if latest_run is None:
            latest_run = item
        if item["status"] == "running":
            active_runs.append(item)
    uname = (user or {}).get("username")
    is_admin = not user or user.get("role") in ("superadmin", "admin")
    with SCAN_LOCK:
        visible_tasks = [
            t for t in reversed(list(SCAN_TASKS.values()))
            if is_admin or t.get("triggered_by") == uname
        ]
        all_tasks = [_serialize_task(t, include_logs=False) for t in visible_tasks]
        running_task = next(
            (_serialize_task(t, include_logs=True) for t in visible_tasks if t["status"] == "running"),
            None,
        )
    return {
        "active_runs": active_runs,
        "latest_run": latest_run,
        "running_task": running_task,
        "tasks": all_tasks,
        "pause_state": _get_pause_state(output_dir),
    }


def _prune_sessions(now_ts: float) -> None:
    expired = [sid for sid, exp in SESSIONS.items() if exp <= now_ts]
    for sid in expired:
        del SESSIONS[sid]


def _issue_session() -> str:
    now_ts = time.time()
    sid = secrets.token_hex(32)
    with AUTH_LOCK:
        _prune_sessions(now_ts)
        if len(SESSIONS) >= 512:
            SESSIONS.clear()
        SESSIONS[sid] = now_ts + SESSION_TTL_SEC
    return sid


def _valid_session(sid: str | None) -> bool:
    if not sid:
        return False
    now_ts = time.time()
    with AUTH_LOCK:
        exp = SESSIONS.get(sid)
        if exp is None:
            return False
        if exp <= now_ts:
            del SESSIONS[sid]
            return False
        return True


def _revoke_session(sid: str | None) -> None:
    if not sid:
        return
    with AUTH_LOCK:
        SESSIONS.pop(sid, None)


def _login_rate_limited(client_ip: str) -> bool:
    now_ts = time.monotonic()
    with AUTH_LOCK:
        bucket = LOGIN_FAILURES.get(client_ip)
        if not bucket:
            return False
        while bucket and now_ts - bucket[0] > LOGIN_WINDOW_SEC:
            bucket.popleft()
        return len(bucket) >= LOGIN_MAX_FAILURES


def _record_login_failure(client_ip: str) -> None:
    now_ts = time.monotonic()
    with AUTH_LOCK:
        bucket = LOGIN_FAILURES.setdefault(client_ip, deque())
        while bucket and now_ts - bucket[0] > LOGIN_WINDOW_SEC:
            bucket.popleft()
        bucket.append(now_ts)


def _clear_login_failures(client_ip: str) -> None:
    with AUTH_LOCK:
        LOGIN_FAILURES.pop(client_ip, None)


def _is_safe_segment(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and bool(SAFE_SEG_RE.fullmatch(value))


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _default_projects_csv(root: Path) -> Path:
    local = root / "config" / "projects.local.csv"
    return local if local.is_file() else (root / "config" / "projects.csv")


def _load_projects(projects_csv: Path) -> list[dict]:
    if not projects_csv.is_file():
        return []
    with projects_csv.open(encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f) if r.get("project_id")]


def _load_catalog(projects_csv: Path | None, db: platform_db.PlatformDB | None = None) -> list[dict]:
    """优先从 SQLite 管理端读取项目清册，否则回退到 CSV 文件。"""
    if db is not None:
        db_projs = db.list_projects()
        if db_projs:
            return db_projs
    if projects_csv and projects_csv.is_file():
        return _load_projects(projects_csv)
    return []


def _project_weights(meta: dict | None, checklist: dict) -> tuple[dict[str, float] | None, str]:
    raw = (meta or {}).get("weights") or ""
    if not str(raw).strip():
        return None, ""
    try:
        return scoring.parse_weights(raw, checklist, (meta or {}).get("project_id", "")), str(raw).strip()
    except ValueError:
        return None, ""


def _parse_markdown_report(md_path: Path, checklist: dict, weights: dict | None = None,
                           weights_key: str = "") -> dict:
    """解析 Markdown 报告，提取 8 维度得分、问题清单与摘要（带 mtime 内存缓存）。"""
    resolved = md_path.resolve()
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    cache_key = (str(resolved), mtime_ns, weights_key)
    with REPORT_CACHE_LOCK:
        cached = REPORT_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    text = resolved.read_text(encoding="utf-8", errors="replace")
    items, _ = report_validate._parse_items(text)
    issues = report_validate._parse_issues(text)
    overview, _ = report_validate._parse_overview(text)

    # 补充问题清单中的详细列（影响、修复建议、工作量、证据原文）
    # 按与 report_validate._parse_issues 相同的规则过滤有效行，避免占位行导致下标错位
    issue_lines = report_validate._find_section(text, "风险问题清单")
    _, raw_issue_rows = report_validate._table_rows(issue_lines)
    issue_rows = [row for row in raw_issue_rows if report_validate.ITEM_ID_RE.search(" ".join(row))]
    rich_issues: list[dict] = []
    for idx, parsed in enumerate(issues):
        row = issue_rows[idx] if idx < len(issue_rows) else []
        rich_issues.append({
            "issue_id": parsed.get("issue_id", f"Q{idx + 1:03d}"),
            "item_id": parsed.get("item_id", ""),
            "level": parsed.get("level", "P3"),
            "title": parsed.get("title", ""),
            "evidence": parsed.get("evidence", []),
            "evidence_text": row[4] if len(row) > 4 else "",
            "impact": row[5] if len(row) > 5 else "",
            "suggestion": row[6] if len(row) > 6 else "",
            "effort": row[7] if len(row) > 7 else "",
        })

    # 计算 8 维度得分（优先使用 scoring.score_result，回退到表格解析的 overview）
    dims_out: list[dict] = []
    computed_scores = None
    if items:
        w = weights or {d["id"]: float(d["weight"]) for d in checklist["dimensions"]}
        result = report_validate.build_result(items, issues)
        computed_scores = scoring.score_result(result, checklist, w)
        for d in checklist["dimensions"]:
            did = str(d["id"])
            cd = computed_scores["dimensions"][did]
            dims_out.append({
                "id": did,
                "name": d["name"],
                "score": cd["score"],
                "coverage": cd["coverage"],
                "counts": cd["counts"],
                "top_risk": cd["top_risk"],
            })
    else:
        for d in checklist["dimensions"]:
            did = str(d["id"])
            ov = overview.get(did)
            ov_counts = ov.get("counts") if isinstance(ov, dict) else None
            dims_out.append({
                "id": did,
                "name": d["name"],
                "score": ov["score"] if ov else None,
                "coverage": (ov["coverage"] / 100.0) if ov and ov.get("coverage") is not None else None,
                "counts": {
                    "PASS": ov_counts[0], "WARN": ov_counts[1],
                    "FAIL": ov_counts[2], "N/A": ov_counts[3],
                    "NEED_INFO": ov_counts[4],
                } if isinstance(ov_counts, list) and len(ov_counts) == 5 else None,
                "top_risk": "-",
            })

    issue_counts = {lv: 0 for lv in LEVELS}
    for iss in rich_issues:
        lv = iss.get("level")
        if lv in issue_counts:
            issue_counts[lv] += 1

    parsed_result = {
        "markdown": text,
        "dimensions": dims_out,
        "issues": rich_issues,
        "issue_counts": issue_counts,
        "scores": report_validate.summarize_scores(computed_scores) if computed_scores else None,
    }
    if mtime_ns:
        with REPORT_CACHE_LOCK:
            if len(REPORT_CACHE) >= REPORT_CACHE_MAX:
                REPORT_CACHE.pop(next(iter(REPORT_CACHE)))
            REPORT_CACHE[cache_key] = parsed_result
    return dict(parsed_result)


def _can_delete_run(user: dict | None, run_data: dict) -> bool:
    """判断用户是否可删除该批次：运行中批次禁止删除；管理员/超管可删任意非运行批次，普通用户仅可删自己触发的。"""
    if not user or run_data.get("status") == "running":
        return False
    if user.get("role") in ("superadmin", "admin"):
        return True
    triggered_by = str(run_data.get("triggered_by") or "").strip().lower()
    username = str(user.get("username") or "").strip().lower()
    return bool(triggered_by and username and triggered_by == username)


def _discover_runs(output_dir: Path, allowed_pids: set[str] | None = None,
                   user: dict | None = None) -> list[dict]:
    """扫描 results/<date>/<run_id>/_run.json 与旧格式 results/<date>/_run.json，按时间倒序排列。"""
    results_dir = output_dir / "results"
    runs: list[dict] = []
    if not results_dir.is_dir():
        return runs

    def _add(date_str: str, run_id: str, data: dict) -> None:
        item = _summarize_run(date_str, run_id, data, output_dir, allowed_pids=allowed_pids, user=user)
        if allowed_pids is not None and item["total_projects"] == 0 and item["pending_count"] == 0:
            return
        runs.append(item)

    for date_dir in sorted(results_dir.iterdir(), reverse=True):
        if not date_dir.is_dir() or not _is_safe_segment(date_dir.name):
            continue
        date_str = date_dir.name
        # 新格式: results/<date>/<run_id>/_run.json
        for child in sorted(date_dir.iterdir(), reverse=True):
            if child.is_dir() and _is_safe_segment(child.name):
                run_file = child / "_run.json"
                data = _read_json(run_file)
                if isinstance(data, dict):
                    _add(date_str, child.name, data)
        # 兼容旧格式: results/<date>/_run.json
        legacy_file = date_dir / "_run.json"
        if legacy_file.is_file():
            data = _read_json(legacy_file)
            if isinstance(data, dict):
                _add(date_str, "default", data)

    runs.sort(key=lambda r: (r.get("date", ""), r.get("started_at") or r.get("run_id", "")), reverse=True)
    return runs


def _summarize_run(date_str: str, run_id: str, data: dict, output_dir: Path,
                   allowed_pids: set[str] | None = None, user: dict | None = None) -> dict:
    projects = data.get("projects") or []
    pending = data.get("pending_projects") or []
    if allowed_pids is not None:
        projects = [p for p in projects if p.get("project_id") in allowed_pids]
        pending = [pid for pid in pending if pid in allowed_pids]
    ok_count = sum(1 for p in projects if p.get("status") == "ok")
    err_count = sum(1 for p in projects if p.get("status") != "ok")
    rep_dir = output_dir / "reports" / date_str if run_id == "default" else output_dir / "reports" / date_str / run_id
    summary_exists = (rep_dir / "_summary.md").is_file() and allowed_pids is None
    return {
        "date": data.get("date") or date_str,
        "run_id": data.get("run_id") or run_id,
        "status": data.get("status", "unknown"),
        "report_mode": data.get("report_mode", ""),
        "triggered_by": data.get("triggered_by") or "",
        "trigger": data.get("trigger") or "",
        "can_delete": _can_delete_run(user, data),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "snapshot_at": data.get("snapshot_at"),
        "summary_status": data.get("summary_status"),
        "has_summary": summary_exists,
        "total_projects": len(projects),
        "ok_count": ok_count,
        "error_count": err_count,
        "pending_count": len(pending),
    }


def _resolve_report_dir(output_dir: Path, date_str: str, run_id: str) -> Path:
    if run_id == "default":
        return output_dir / "reports" / date_str
    return output_dir / "reports" / date_str / run_id


def _resolve_run_file(output_dir: Path, date_str: str, run_id: str) -> Path:
    if run_id == "default":
        return output_dir / "results" / date_str / "_run.json"
    return output_dir / "results" / date_str / run_id / "_run.json"


def _build_overview(output_dir: Path, projects_csv: Path, checklist: dict,
                    user: dict | None = None, db: platform_db.PlatformDB | None = None) -> dict:
    allowed_pids = _allowed_pids_for_user(user, projects_csv, db=db)
    runs = _discover_runs(output_dir, allowed_pids=allowed_pids)
    catalog = {p["project_id"]: p for p in _load_catalog(projects_csv, db=db)}
    thresholds = db.get_stale_thresholds() if db is not None else {"warn_days": 90, "critical_days": 180}

    # 读取所有 state/<pid>.json 作为项目最新基线
    state_dir = output_dir / "state"
    states: dict[str, dict] = {}
    if state_dir.is_dir():
        for f in sorted(state_dir.glob("*.json")):
            if f.stem == "schedule":
                continue
            data = _read_json(f)
            if isinstance(data, dict):
                states[f.stem] = data

    # 按日期聚合趋势，同时记录每个项目最近两次有效评分以便计算较上次变化
    trend_by_date: dict[str, dict] = {}
    proj_history: dict[str, list[dict]] = {}
    for run in reversed(runs):  # 从早到晚遍历，同日后批次覆盖前批次
        run_data = _read_json(_resolve_run_file(output_dir, run["date"], run["run_id"]))
        if not isinstance(run_data, dict):
            continue
        totals, secs = [], []
        for p in run_data.get("projects") or []:
            if allowed_pids is not None and p.get("project_id") not in allowed_pids:
                continue
            if p.get("status") != "ok":
                continue
            sc = p.get("report_scores") or {}
            if sc.get("total") is not None:
                totals.append(float(sc["total"]))
            if sc.get("security_score") is not None:
                secs.append(float(sc["security_score"]))
            if sc.get("total") is not None or sc.get("security_score") is not None:
                proj_history.setdefault(p["project_id"], []).append({
                    "date": run["date"],
                    "run_id": run["run_id"],
                    "total": sc.get("total"),
                    "security": sc.get("security_score"),
                })
        if totals or secs:
            trend_by_date[run["date"]] = {
                "date": run["date"],
                "run_id": run["run_id"],
                "total": round(sum(totals) / len(totals), 2) if totals else None,
                "security": round(sum(secs) / len(secs), 2) if secs else None,
                "count": len(totals),
            }

    # 构建项目排行表：优先取最新一次运行的结果，合并 state 与 projects.csv 元信息
    latest_run = runs[0] if runs else None
    latest_run_data = _read_json(_resolve_run_file(output_dir, latest_run["date"], latest_run["run_id"])) if latest_run else None
    latest_projects = {p["project_id"]: p for p in (latest_run_data or {}).get("projects", [])}

    all_pids = sorted(set(catalog) | set(states) | set(latest_projects))
    if allowed_pids is not None:
        all_pids = [pid for pid in all_pids if pid in allowed_pids]
    rows: list[dict] = []
    dim_buckets: dict[str, list[float]] = {str(d["id"]): [] for d in checklist["dimensions"]}

    for pid in all_pids:
        meta = catalog.get(pid, {})
        st = states.get(pid, {})
        rec = latest_projects.get(pid, {})
        scores = rec.get("report_scores") or st.get("scores") or {}
        run_date = (latest_run["date"] if pid in latest_projects else None) or st.get("last_scan_date") or ""
        run_id = (latest_run["run_id"] if pid in latest_projects else None)
        report_path = rec.get("report_path") or st.get("last_report")

        # 尝试解析报告获取 8 维度均分与 P0-P3 计数（仅读取 output_dir/reports 内的文件）
        issue_counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        dim_scores = {}
        reports_root = (output_dir / "reports").resolve()
        safe_report: Path | None = None
        if report_path:
            try:
                cand = Path(report_path).resolve()
                if cand.is_relative_to(reports_root) and cand.is_file():
                    safe_report = cand
            except OSError:
                safe_report = None
        if safe_report is not None:
            try:
                proj_weights, weights_key = _project_weights(meta, checklist)
                parsed = _parse_markdown_report(safe_report, checklist, proj_weights, weights_key)
                issue_counts = parsed["issue_counts"]
                for d in parsed["dimensions"]:
                    dim_scores[d["id"]] = d["score"]
                    if d["score"] is not None:
                        dim_buckets[d["id"]].append(float(d["score"]))
                if not scores and parsed.get("scores"):
                    scores = parsed["scores"]
            except OSError:
                pass

        # 若没有 run_id，尝试从 report_path 推断 (reports/<date>/<run_id>/<pid>.md)
        if not run_id and safe_report is not None:
            parts = safe_report.parts
            if len(parts) >= 3 and parts[-1] == f"{pid}.md":
                run_id = parts[-2]
                if not run_date:
                    run_date = parts[-3]

        status = rec.get("status") or ("ok" if st.get("last_report") else "pending")
        hist = proj_history.get(pid, [])
        prev_entry = None
        for entry in reversed(hist):
            if entry["date"] != run_date or entry["run_id"] != run_id:
                prev_entry = entry
                break
        cur_total = scores.get("total")
        cur_sec = scores.get("security_score")
        delta_total = (
            round(float(cur_total) - float(prev_entry["total"]), 1)
            if prev_entry and cur_total is not None and prev_entry.get("total") is not None
            else None
        )
        delta_security = (
            round(float(cur_sec) - float(prev_entry["security"]), 1)
            if prev_entry and cur_sec is not None and prev_entry.get("security") is not None
            else None
        )
        code_metrics = _merge_project_code_metrics(meta, st, thresholds)
        rows.append({
            "project_id": pid,
            "project_name": rec.get("project_name") or meta.get("project_name") or pid,
            "department": rec.get("department") or meta.get("department") or "",
            "owner": rec.get("owner") or meta.get("owner") or "",
            "tech_stack": meta.get("tech_stack") or "",
            "branch": meta.get("branch") or st.get("branch") or "",
            "catalog_status": meta.get("status") or "",
            "status": status,
            "error": rec.get("error"),
            "scan_type": rec.get("scan_type") or "",
            "report_mode": rec.get("report_mode") or st.get("report_mode") or "",
            "commit": (rec.get("commit") or st.get("last_commit") or "")[:10],
            "date": run_date,
            "run_id": run_id,
            "total": cur_total,
            "delta_total": delta_total,
            "delta_security": delta_security,
            "prev_date": prev_entry["date"] if prev_entry else None,
            "grade": scores.get("grade") or ("扫描失败" if status == "error" else "暂未扫描"),
            "security": cur_sec,
            "coverage": scores.get("coverage"),
            "scored_dimensions": scores.get("scored_dimensions"),
            "security_p0": scores.get("security_p0", 0),
            "security_p1": scores.get("security_p1", 0),
            **issue_counts,
            **code_metrics,
            "dim_scores": dim_scores,
        })

    dims_avg = []
    for d in checklist["dimensions"]:
        did = str(d["id"])
        vals = dim_buckets[did]
        dims_avg.append({
            "id": did,
            "name": d["name"],
            "avg": round(sum(vals) / len(vals), 2) if vals else None,
            "count": len(vals),
        })

    return {
        "latest_run": latest_run,
        "runs_count": len(runs),
        "rows": rows,
        "trend": list(trend_by_date.values()),
        "dims": dims_avg,
        "stale_thresholds": thresholds,
    }


def _web_tasks_dir(output_dir: Path) -> Path:
    return output_dir / "results" / "_web_tasks"


def _persist_task_meta(output_dir: Path | None, task: dict) -> None:
    if not output_dir:
        return
    try:
        tdir = _web_tasks_dir(output_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        meta = _serialize_task(task, include_logs=False)
        target = tdir / f"{task['task_id']}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def _append_task_log(output_dir: Path | None, task_id: str, line: str) -> None:
    if not output_dir:
        return
    try:
        tdir = _web_tasks_dir(output_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        with (tdir / f"{task_id}.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _hydrate_persisted_tasks(output_dir: Path) -> None:
    """服务启动时从 results/_web_tasks/ 恢复最近 30 条 Web/定时任务记录与日志。"""
    tdir = _web_tasks_dir(output_dir)
    if not tdir.is_dir():
        return
    files = sorted(tdir.glob("*.json"), key=lambda p: p.stat().st_mtime)[-30:]
    with SCAN_LOCK:
        for mf in files:
            meta = _read_json(mf)
            if not isinstance(meta, dict) or not meta.get("task_id"):
                continue
            tid = str(meta["task_id"])
            if tid in SCAN_TASKS:
                continue
            if meta.get("status") == "running":
                meta["status"] = "interrupted"
                if not meta.get("finished_at"):
                    meta["finished_at"] = dt.datetime.now().isoformat()
            log_file = tdir / f"{tid}.log"
            logs: deque[str] = deque(maxlen=500)
            if log_file.is_file():
                try:
                    for raw in log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
                        logs.append(raw)
                except OSError:
                    pass
            meta["logs"] = logs
            meta["_proc"] = None
            meta["_cancelled"] = False
            SCAN_TASKS[tid] = meta


def _mark_run_aborted(output_dir: Path, date_str: str, run_id: str,
                      settings_path: Path | None = None) -> dict:
    """将已滞留且无活跃进程持锁的 running 批次收尾标记为已中断。"""
    run_file = _resolve_run_file(output_dir, date_str, run_id)
    data = _read_json(run_file)
    if not isinstance(data, dict):
        return {"not_found": True, "error": "未找到该批次运行记录"}
    if data.get("status") != "running":
        return {"conflict": True, "error": f"该批次当前状态为 {data.get('status')}，无需标记中断"}

    settings = (_read_json(settings_path) if settings_path else None) or {}
    workdir = _resolve_workdir(output_dir, settings if isinstance(settings, dict) else {})
    pending = [str(p) for p in (data.get("pending_projects") or [])]
    active_pids = [pid for pid in pending if _is_project_actively_scanning(workdir, pid)[0]]
    if active_pids:
        return {
            "conflict": True,
            "error": f"检测到项目 {', '.join(active_pids[:3])} 仍持有扫描锁，请先等待或中止扫描进程",
        }

    projects = data.get("projects") or []
    ok_count = sum(1 for p in projects if p.get("status") == "ok")
    data["status"] = "partial_failed" if ok_count > 0 else "failed"
    data["finished_at"] = dt.datetime.now().isoformat()
    data["aborted_manually"] = True
    tmp = run_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, run_file)
    return {"ok": True, "date": date_str, "run_id": run_id, "status": data["status"]}


def _rollback_states_for_deleted_run(output_dir: Path, deleted_rep_dir: Path, deleted_pids: set[str]) -> tuple[list[str], list[str]]:
    """批次删除后，若 state/<pid>.json 的 last_report 指向已被删除的目录，自动回退到最近一次有效历史批次；无历史则移除。"""
    state_dir = output_dir / "state"
    if not state_dir.is_dir():
        return [], []
    deleted_rep_resolved = deleted_rep_dir.resolve()
    affected_pids: list[str] = []
    for sf in sorted(state_dir.glob("*.json")):
        if sf.stem == "schedule":
            continue
        st = _read_json(sf)
        if not isinstance(st, dict):
            continue
        last_rep = str(st.get("last_report") or "").strip()
        if not last_rep:
            continue
        try:
            rep_p = Path(last_rep).resolve()
            if rep_p.is_relative_to(deleted_rep_resolved) or (sf.stem in deleted_pids and not rep_p.is_file()):
                affected_pids.append(sf.stem)
        except OSError:
            if sf.stem in deleted_pids:
                affected_pids.append(sf.stem)
    if not affected_pids:
        return [], []

    remaining_runs = _discover_runs(output_dir)
    rebuilt: list[str] = []
    cleared: list[str] = []
    reports_root = (output_dir / "reports").resolve()

    for pid in affected_pids:
        sf = state_dir / f"{pid}.json"
        old_st = _read_json(sf) or {}
        found_state: dict | None = None
        for r in remaining_runs:
            rdata = _read_json(_resolve_run_file(output_dir, r["date"], r["run_id"]))
            if not isinstance(rdata, dict):
                continue
            rrep_dir = _resolve_report_dir(output_dir, r["date"], r["run_id"])
            for rec in rdata.get("projects") or []:
                if rec.get("project_id") != pid or rec.get("status") != "ok":
                    continue
                cand_raw = str(rec.get("report_path") or "").strip()
                cand = Path(cand_raw) if cand_raw else (rrep_dir / f"{pid}.md")
                if not cand.is_file():
                    cand = rrep_dir / f"{pid}.md"
                try:
                    cand_res = cand.resolve()
                except OSError:
                    continue
                if not cand_res.is_relative_to(reports_root) or not cand_res.is_file():
                    continue
                found_state = dict(old_st) if isinstance(old_st, dict) else {}
                found_state.update({
                    "last_commit": rec.get("commit") or found_state.get("last_commit") or "",
                    "last_report": str(cand_res),
                    "last_scan_date": r["date"],
                    "report_mode": rec.get("report_mode") or rdata.get("report_mode") or "standard",
                    "scores": rec.get("report_scores"),
                })
                if "incremental_streak" in rec:
                    found_state["incremental_streak"] = rec["incremental_streak"]
                if "last_full_date" in rec:
                    found_state["last_full_date"] = rec["last_full_date"]
                break
            if found_state is not None:
                break

        if found_state is not None:
            tmp = sf.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(found_state, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, sf)
            rebuilt.append(pid)
        else:
            try:
                sf.unlink(missing_ok=True)
                cleared.append(pid)
            except OSError:
                pass
    return rebuilt, cleared


def _delete_run(output_dir: Path, date_str: str, run_id: str,
                settings_path: Path | None, user: dict) -> dict:
    """删除冗余/测试扫描批次（含权限校验、运行中保护、目录清理与 state 基线回退）。"""
    run_file = _resolve_run_file(output_dir, date_str, run_id)
    data = _read_json(run_file)
    if not isinstance(data, dict):
        return {"not_found": True, "error": "未找到该批次运行记录"}

    is_admin = user.get("role") in ("superadmin", "admin")
    triggered_by = str(data.get("triggered_by") or "").strip()
    if not is_admin:
        uname = str(user.get("username") or "").strip().lower()
        if not triggered_by or triggered_by.lower() != uname:
            return {"forbidden": True, "error": "普通用户仅可删除自己触发的扫描批次"}

    if data.get("status") == "running":
        return {"conflict": True, "error": "该批次正在运行中，请先中止或标记为已中断后再删除"}

    settings = (_read_json(settings_path) if settings_path else None) or {}
    workdir = _resolve_workdir(output_dir, settings if isinstance(settings, dict) else {})
    pending = [str(p) for p in (data.get("pending_projects") or [])]
    active_pids = [pid for pid in pending if _is_project_actively_scanning(workdir, pid)[0]]
    if active_pids:
        return {"conflict": True, "error": f"检测到项目 {', '.join(active_pids[:3])} 仍持有扫描锁，无法删除"}

    deleted_pids = {str(p.get("project_id") or "") for p in (data.get("projects") or []) if p.get("project_id")}
    results_root = (output_dir / "results").resolve()
    reports_root = (output_dir / "reports").resolve()
    res_dir = run_file.parent.resolve()
    rep_dir = _resolve_report_dir(output_dir, date_str, run_id).resolve()

    if not res_dir.is_relative_to(results_root) or not rep_dir.is_relative_to(reports_root):
        return {"conflict": True, "error": "批次目录路径越界，已拒绝删除"}

    if run_id == "default":
        for f in (run_file, res_dir / "summary.events.jsonl"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for rf in res_dir.glob("*.rejected.md"):
            try:
                rf.unlink(missing_ok=True)
            except OSError:
                pass
        if rep_dir.is_dir():
            for mf in rep_dir.glob("*.md"):
                try:
                    mf.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        if res_dir.is_dir() and res_dir != results_root:
            shutil.rmtree(res_dir, ignore_errors=True)
        if rep_dir.is_dir() and rep_dir != reports_root:
            shutil.rmtree(rep_dir, ignore_errors=True)

    # 清理空日期目录
    for parent in (results_root / date_str, reports_root / date_str):
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    # 清理内存报告解析缓存
    rep_prefix = str(rep_dir)
    with REPORT_CACHE_LOCK:
        stale_keys = [k for k in REPORT_CACHE if str(k[0]).startswith(rep_prefix)]
        for k in stale_keys:
            REPORT_CACHE.pop(k, None)

    rebuilt, cleared = _rollback_states_for_deleted_run(output_dir, rep_dir, deleted_pids)
    return {
        "ok": True,
        "date": date_str,
        "run_id": run_id,
        "triggered_by": triggered_by,
        "rebuilt_states": rebuilt,
        "cleared_states": cleared,
    }


def _start_scan_task(payload: dict, root: Path, settings_path: Path, projects_csv: Path,
                     trigger: str = "manual", output_dir: Path | None = None,
                     triggered_by: str = "") -> dict:
    task_id = dt.datetime.now().strftime("%H%M%S") + "-" + uuid.uuid4().hex[:8]
    compose_ep = root / "docker" / "compose_entrypoint.py"
    use_compose = bool(os.environ.get("AUDIT_DATA_DIR", "").strip()) and compose_ep.is_file()
    sub_env = dict(os.environ)
    sub_env["AUDIT_TRIGGER"] = trigger
    if triggered_by:
        sub_env["AUDIT_TRIGGERED_BY"] = triggered_by
    if output_dir is not None:
        rt_git_creds = _runtime_git_creds_file(output_dir)
        if rt_git_creds.is_file():
            sub_env["AUDIT_GIT_CREDENTIAL_FILE"] = str(rt_git_creds)
    if use_compose:
        cmd = [sys.executable, str(compose_ep)]
        sub_env["AUDIT_SETTINGS_FILE"] = str(settings_path)
        sub_env["AUDIT_PROJECTS_FILE"] = str(projects_csv)
    else:
        cmd = [sys.executable, str(RUNNER_DIR / "run_daily.py"),
               "--settings", str(settings_path), "--projects", str(projects_csv)]
    only = payload.get("only")
    if isinstance(only, list) and only:
        safe_only = [str(x) for x in only if _is_safe_segment(str(x))]
        if safe_only:
            cmd.extend(["--only", *safe_only])
    if payload.get("fast") is True:
        cmd.append("--fast")
    elif payload.get("fast") is False:
        cmd.append("--no-fast")
    if payload.get("resume") and payload.get("force_full"):
        return {"bad_request": True, "error": "--resume 不能与 --force-full 同时使用"}
    if payload.get("summary_only") and payload.get("dry_run"):
        return {"bad_request": True, "error": "--summary-only 不能与 --dry-run 同时使用"}
    if payload.get("force_full"):
        cmd.append("--force-full")
    if payload.get("resume"):
        cmd.append("--resume")
    if payload.get("dry_run"):
        cmd.append("--dry-run")
    if payload.get("summary_only"):
        cmd.append("--summary-only")
    snapshot_at = str(payload.get("snapshot_at") or "").strip()
    if snapshot_at:
        try:
            dt.datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
        except ValueError:
            return {"bad_request": True, "error": "snapshot_at 必须是合法的 ISO-8601 时间，例如 2026-11-24T00:00:00+08:00"}
        cmd.extend(["--snapshot-at", snapshot_at])

    task = {
        "task_id": task_id,
        "trigger": trigger,
        "triggered_by": triggered_by,
        "command": " ".join(cmd),
        "status": "running",
        "started_at": dt.datetime.now().isoformat(),
        "finished_at": None,
        "returncode": None,
        "logs": deque(maxlen=500),
        "_proc": None,
        "_cancelled": False,
    }

    def runner_thread():
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(root), env=sub_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
            with SCAN_LOCK:
                task["_proc"] = proc
                if task["_cancelled"]:
                    proc.terminate()
            assert proc.stdout is not None
            for line in proc.stdout:
                clean = line.rstrip("\n")
                with SCAN_LOCK:
                    task["logs"].append(clean)
                _append_task_log(output_dir, task_id, clean)
            rc = proc.wait()
            with SCAN_LOCK:
                task["_proc"] = None
                task["returncode"] = rc
                if task["_cancelled"]:
                    task["status"] = "cancelled"
                else:
                    task["status"] = "completed" if rc == 0 else "failed"
                task["finished_at"] = dt.datetime.now().isoformat()
                _persist_task_meta(output_dir, task)
        except Exception as exc:
            msg = f"[web_server] 启动失败: {exc}"
            with SCAN_LOCK:
                task["_proc"] = None
                task["logs"].append(msg)
                task["returncode"] = -1
                task["status"] = "failed"
                task["finished_at"] = dt.datetime.now().isoformat()
                _persist_task_meta(output_dir, task)
            _append_task_log(output_dir, task_id, msg)

    with SCAN_LOCK:
        if any(t["status"] == "running" for t in SCAN_TASKS.values()):
            return {"conflict": True, "error": "已有扫描任务正在运行中，请等待当前任务完成"}
        SCAN_TASKS[task_id] = task
        _persist_task_meta(output_dir, task)
    threading.Thread(target=runner_thread, daemon=True).start()
    return {"task_id": task_id, "status": "running", "command": task["command"]}


def _cancel_scan_task(task_id: str) -> dict:
    with SCAN_LOCK:
        task = SCAN_TASKS.get(task_id)
        if not task:
            return {"not_found": True, "error": "未找到任务"}
        if task["status"] != "running":
            return {"conflict": True, "error": f"任务当前状态为 {task['status']}，无法中止"}
        task["_cancelled"] = True
        task["logs"].append("[web_server] 收到中止请求，正在停止扫描进程…")
        proc = task.get("_proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return {"task_id": task_id, "status": "cancelling"}


def _generate_batch_summary_now(output_dir: Path, date_str: str, run_id: str, projects_csv: Path) -> dict:
    """按最新 Word 汇报模板即时生成/刷新指定批次的 _summary.md（不调用 LLM，秒级完成）。"""
    run_file = _resolve_run_file(output_dir, date_str, run_id)
    data = _read_json(run_file)
    if not isinstance(data, dict):
        return {"not_found": True, "error": "未找到该批次运行记录"}
    rep_dir = _resolve_report_dir(output_dir, date_str, run_id)
    catalog = {p["project_id"]: p for p in _load_projects(projects_csv)}
    enriched: list[dict] = []
    ok_count = 0
    for orig in data.get("projects") or []:
        rec = dict(orig)
        pid = str(rec.get("project_id") or "")
        meta = catalog.get(pid, {})
        if not rec.get("department") and meta.get("department"):
            rec["department"] = meta["department"]
        if not rec.get("owner") and meta.get("owner"):
            rec["owner"] = meta["owner"]
        if not rec.get("project_name") and meta.get("project_name"):
            rec["project_name"] = meta["project_name"]
        if not rec.get("weights") and meta.get("weights"):
            rec["weights"] = meta["weights"]
        if rec.get("status") == "ok":
            ok_count += 1
            cand = Path(str(rec.get("report_path") or ""))
            if not cand.is_file():
                local_md = rep_dir / f"{pid}.md"
                if local_md.is_file():
                    rec["report_path"] = str(local_md)
        enriched.append(rec)

    res = summary_adapter.generate_batch(enriched, rep_dir, {"summary": {"enabled": False}}, run_id)
    md_text = str(res.get("text") or "")
    rep_dir.mkdir(parents=True, exist_ok=True)
    summary_file = rep_dir / "_summary.md"
    tmp_md = rep_dir / f"._summary.{uuid.uuid4().hex[:8]}.tmp"
    tmp_md.write_text(md_text, encoding="utf-8")
    try:
        os.chmod(tmp_md, 0o600)
    except OSError:
        pass
    os.replace(tmp_md, summary_file)

    # 若批次未在运行，顺带回写 _run.json 的 summary_status（运行中则避免与 run_daily.py 竞争写 _run.json）
    if data.get("status") != "running":
        data["summary_status"] = "generated"
        data["summary_path"] = str(summary_file)
        tmp_json = run_file.with_name(f"._run.{uuid.uuid4().hex[:8]}.tmp")
        tmp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_json, run_file)

    return {
        "ok": True,
        "date": date_str,
        "run_id": run_id,
        "ok_count": ok_count,
        "markdown": md_text,
    }


def _serialize_task(task: dict, include_logs: bool) -> dict:
    out = {}
    for k, v in task.items():
        if k.startswith("_"):
            continue
        if k == "logs":
            if include_logs:
                out[k] = list(v)
            out["latest_log"] = v[-1] if v else ""
        else:
            out[k] = v
    return out


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "AuditWebServer/1.0"

    @property
    def cfg(self) -> dict:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        # 仅在出错或调试时打印，避免刷屏
        pass

    def _send_json(self, code: int, payload: object, extra_headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if not self.cfg.get("auth_token"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session_id_from_cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(AUTH_COOKIE_NAME)
        return morsel.value if morsel else None

    def _current_user(self) -> dict | None:
        db: platform_db.PlatformDB | None = self.cfg.get("db")
        # 1. Cookie 会话优先（SQLite 持久化）
        sid = self._session_id_from_cookie()
        if db is not None and sid:
            user = db.get_session_user(sid)
            if user is not None:
                return user
        # 2. 兼容脚本/CI 通过 Authorization: Bearer <AUDIT_WEB_TOKEN> 调用
        expected = self.cfg.get("auth_token") or ""
        if expected:
            auth_header = (self.headers.get("Authorization") or "").strip()
            if auth_header.startswith("Bearer "):
                candidate = auth_header[7:].strip()
                if candidate and hmac.compare_digest(candidate, expected):
                    return {
                        "id": 0,
                        "username": "api-token",
                        "display_name": "API 令牌",
                        "role": "superadmin",
                        "email": "",
                        "must_change_password": False,
                        "email_notify": "off",
                        "is_active": True,
                    }
        return None

    def _is_authenticated(self) -> bool:
        return self._current_user() is not None

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _require_role(self, user: dict, roles: tuple[str, ...]) -> bool:
        if user.get("role") not in roles:
            self._send_json(403, {"error": "当前账号无权执行此操作"})
            return False
        return True

    def _cookie_header(self, sid: str, max_age: int) -> tuple[str, str]:
        parts = [f"{AUTH_COOKIE_NAME}={sid}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 1_048_576:
                return None
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return None

    def _send_file(self, path: Path) -> None:
        mime, _ = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_binary(self, data: bytes, content_type: str, download_name: str) -> None:
        encoded = urllib.parse.quote(download_name)
        ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name).strip("_") or "report.docx"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}',
        )
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}: {exc}"})
            except OSError:
                pass

    def _handle_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        output_dir: Path = self.cfg["output_dir"]
        projects_csv: Path = self.cfg["projects_csv"]
        checklist: dict = self.cfg["checklist"]

        if path == "/api/health":
            return self._send_json(200, {
                "ok": True,
                "auth_required": True,
                "has_frontend": self.cfg["dist_dir"].is_dir(),
            })

        if path == "/api/auth/status":
            cur_user = self._current_user()
            return self._send_json(200, {
                "auth_required": True,
                "authenticated": cur_user is not None,
                "user": cur_user,
            })

        cur_user = self._current_user() if path.startswith("/api/") else None
        if path.startswith("/api/") and cur_user is None:
            return self._send_json(401, {"error": "未登录或会话已过期，请先登录"})

        db: platform_db.PlatformDB = self.cfg["db"]
        effective_csv = _effective_projects_csv(self.cfg)
        allowed_pids = _allowed_pids_for_user(cur_user, effective_csv, db=db) if cur_user else None

        if path == "/api/me":
            return self._send_json(200, {"user": cur_user})

        if path == "/api/users":
            assert cur_user is not None
            qs = urllib.parse.parse_qs(parsed.query)
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            if qs.get("scope", [""])[0] == "owners":
                users = [
                    {"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                     "role": u["role"], "email": u["email"], "is_active": u["is_active"]}
                    for u in db.list_users() if u["is_active"]
                ]
                return self._send_json(200, {"users": users})
            return self._send_json(200, {"users": db.list_users()})

        if path == "/api/audit-logs":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin",)):
                return
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["150"])[0])
            except ValueError:
                limit = 150
            return self._send_json(200, {"logs": db.list_audit_logs(limit=limit)})

        if path == "/api/overview":
            return self._send_json(200, _build_overview(output_dir, effective_csv, checklist, user=cur_user, db=db))

        if path == "/api/runs":
            return self._send_json(200, {"runs": _discover_runs(output_dir, allowed_pids=allowed_pids, user=cur_user)})

        if path.startswith("/api/runs/"):
            segs = [urllib.parse.unquote(s) for s in path[len("/api/runs/"):].split("/") if s]
            # GET /api/runs/<date>/<run_id>/<project_id>/rejected -> 查看被校验拦截的原始草稿
            if len(segs) == 4 and segs[3] == "rejected" and all(_is_safe_segment(s) for s in segs[:3]):
                date_str, run_id, pid = segs[0], segs[1], segs[2]
                if allowed_pids is not None and pid not in allowed_pids:
                    return self._send_json(403, {"error": "无权查看其他负责人的项目草稿"})
                res_dir = _resolve_run_file(output_dir, date_str, run_id).parent
                rej_file = (res_dir / f"{pid}.rejected.md").resolve()
                if not rej_file.is_relative_to(res_dir.resolve()) or not rej_file.is_file():
                    return self._send_json(404, {"error": "未找到被拦截的原始草稿（仅新版本拦截落盘且无密钥泄露时保留）"})
                return self._send_json(200, {
                    "date": date_str,
                    "run_id": run_id,
                    "project_id": pid,
                    "markdown": rej_file.read_text(encoding="utf-8", errors="replace"),
                })
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            date_str, run_id = segs
            run_file = _resolve_run_file(output_dir, date_str, run_id)
            data = _read_json(run_file)
            if not isinstance(data, dict):
                return self._send_json(404, {"error": "未找到该批次运行记录"})
            rep_dir = _resolve_report_dir(output_dir, date_str, run_id)
            res_dir = run_file.parent
            data["has_summary"] = (rep_dir / "_summary.md").is_file() and allowed_pids is None
            data["can_delete"] = _can_delete_run(cur_user, data)
            data.setdefault("triggered_by", "")
            data.setdefault("trigger", "")
            catalog = {p["project_id"]: p for p in _load_catalog(effective_csv, db=db)}
            visible_projects = []
            for p in data.get("projects") or []:
                pid = str(p.get("project_id") or "")
                if allowed_pids is not None and pid not in allowed_pids:
                    continue
                meta = catalog.get(pid, {})
                if not p.get("department") and meta.get("department"):
                    p["department"] = meta["department"]
                if not p.get("owner") and meta.get("owner"):
                    p["owner"] = meta["owner"]
                if not p.get("project_name") and meta.get("project_name"):
                    p["project_name"] = meta["project_name"]
                p["has_rejected_draft"] = bool(_is_safe_segment(pid) and (res_dir / f"{pid}.rejected.md").is_file())
                visible_projects.append(p)
            data["projects"] = visible_projects
            if allowed_pids is not None:
                data["pending_projects"] = [pid for pid in (data.get("pending_projects") or []) if pid in allowed_pids]
            return self._send_json(200, data)

        if path.startswith("/api/reports/"):
            segs = [urllib.parse.unquote(s) for s in path[len("/api/reports/"):].split("/") if s]
            # GET /api/reports/<date>/<run_id>/<target>/docx -> 导出 Word (.docx)
            if len(segs) == 4 and segs[3] == "docx" and all(_is_safe_segment(s) for s in segs[:3]):
                date_str, run_id, target = segs[0], segs[1], segs[2]
                if allowed_pids is not None and (target == "_summary" or target not in allowed_pids):
                    return self._send_json(403, {"error": "无权导出该报告"})
                qs = urllib.parse.parse_qs(parsed.query)
                force_refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
                rep_dir = _resolve_report_dir(output_dir, date_str, run_id)
                if target == "_summary":
                    summary_file = rep_dir / "_summary.md"
                    if force_refresh or not summary_file.is_file():
                        gen = _generate_batch_summary_now(output_dir, date_str, run_id, projects_csv)
                        if gen.get("not_found"):
                            return self._send_json(404, {"error": gen["error"]})
                        md_text = gen["markdown"]
                    else:
                        md_text = summary_file.read_text(encoding="utf-8", errors="replace")
                    docx_bytes = docx_export.markdown_to_docx_bytes(md_text, title="代码检查分析报告汇总")
                    return self._send_binary(
                        docx_bytes,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        f"代码检查分析报告汇总_{date_str}_{run_id}.docx",
                    )
                md_file = rep_dir / f"{target}.md"
                if not md_file.is_file():
                    return self._send_json(404, {"error": f"未找到项目报告 {target}.md"})
                md_text = md_file.read_text(encoding="utf-8", errors="replace")
                docx_bytes = docx_export.markdown_to_docx_bytes(md_text, title=target)
                return self._send_binary(
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    f"{date_str}_{target}.docx",
                )
            if len(segs) != 3 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的报告路径"})
            date_str, run_id, target = segs
            if allowed_pids is not None and (target == "_summary" or target not in allowed_pids):
                return self._send_json(403, {"error": "无权查看该报告"})
            rep_dir = _resolve_report_dir(output_dir, date_str, run_id)
            if target == "_summary":
                summary_file = rep_dir / "_summary.md"
                if not summary_file.is_file():
                    return self._send_json(404, {"error": "未找到汇总报告 _summary.md"})
                return self._send_json(200, {
                    "type": "summary",
                    "date": date_str,
                    "run_id": run_id,
                    "markdown": summary_file.read_text(encoding="utf-8", errors="replace"),
                })
            md_file = rep_dir / f"{target}.md"
            if not md_file.is_file():
                return self._send_json(404, {"error": f"未找到项目报告 {target}.md"})
            catalog = {p["project_id"]: p for p in _load_catalog(effective_csv, db=db)}
            proj_weights, weights_key = _project_weights(catalog.get(target), checklist)
            parsed_report = _parse_markdown_report(md_file, checklist, proj_weights, weights_key)
            parsed_report.update({
                "type": "project",
                "date": date_str,
                "run_id": run_id,
                "project_id": target,
            })
            return self._send_json(200, parsed_report)

        if path == "/api/projects/export":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            managed_csv = _managed_projects_csv(output_dir)
            if db.list_projects():
                db.export_projects_csv(managed_csv)
            src = managed_csv if managed_csv.is_file() else effective_csv
            raw_csv = src.read_text(encoding="utf-8-sig") if src.is_file() else (",".join(platform_db.PROJECT_CSV_FIELDS) + "\n")
            data_bytes = ("\ufeff" + raw_csv).encode("utf-8")
            return self._send_binary(data_bytes, "text/csv; charset=utf-8", "projects.csv")

        if path == "/api/projects":
            projects = _load_catalog(effective_csv, db=db)
            if allowed_pids is not None:
                projects = [p for p in projects if p["project_id"] in allowed_pids]
            thresholds = db.get_stale_thresholds()
            state_dir = output_dir / "state"
            enriched_projects = []
            for p in projects:
                item = dict(p)
                pid = item["project_id"]
                st = _read_json(state_dir / f"{pid}.json") if state_dir.is_dir() else None
                if isinstance(st, dict):
                    item["state"] = st
                item.update(_merge_project_code_metrics(item, st if isinstance(st, dict) else {}, thresholds))
                enriched_projects.append(item)
            return self._send_json(200, {
                "projects": enriched_projects,
                "stale_thresholds": thresholds,
                "scan_statuses": self.cfg.get("scan_statuses", ["在研"]),
                "checklist_dimensions": checklist["dimensions"],
            })

        if path == "/api/scans/pause":
            return self._send_json(200, _get_pause_state(output_dir))

        if path == "/api/prompts":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            return self._send_json(200, {"prompts": _build_prompts_payload(db)})

        if path == "/api/ops/config":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin",)):
                return
            return self._send_json(200, _build_ops_config(output_dir, self.cfg["settings_path"], db))

        if path == "/api/ops/smtp":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin",)):
                return
            return self._send_json(200, {"smtp": db.get_smtp_config(include_password=False)})

        if path == "/api/email-logs":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            return self._send_json(200, {"logs": db.list_email_logs(limit=limit)})

        if path == "/api/doctor":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            return self._send_json(200, doctor.run_checks(
                ROOT, output_dir=output_dir, projects_csv=effective_csv, settings_path=self.cfg["settings_path"]
            ))

        if path == "/api/schedule":
            assert cur_user is not None
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            with SCHEDULE_LOCK:
                sched = _load_schedule(output_dir)
                now = dt.datetime.now()
            return self._send_json(200, {
                "schedule": sched,
                "next_run_at": _next_run_at(sched, now),
                "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            })

        if path == "/api/scans/progress":
            return self._send_json(200, _build_live_progress(
                output_dir, effective_csv, self.cfg["settings_path"], user=cur_user, db=db
            ))

        if path == "/api/scans":
            assert cur_user is not None
            is_admin = cur_user.get("role") in ("superadmin", "admin")
            with SCAN_LOCK:
                tasks = [
                    _serialize_task(t, include_logs=False)
                    for t in reversed(list(SCAN_TASKS.values()))
                    if is_admin or t.get("triggered_by") == cur_user.get("username")
                ]
            return self._send_json(200, {"tasks": tasks})

        if path.startswith("/api/scans/"):
            assert cur_user is not None
            is_admin = cur_user.get("role") in ("superadmin", "admin")
            task_id = urllib.parse.unquote(path[len("/api/scans/"):])
            with SCAN_LOCK:
                t = SCAN_TASKS.get(task_id)
                if not t:
                    return self._send_json(404, {"error": "未找到任务"})
                if not is_admin and t.get("triggered_by") != cur_user.get("username"):
                    return self._send_json(403, {"error": "无权查看其他用户触发的扫描日志"})
                snap = _serialize_task(t, include_logs=True)
            return self._send_json(200, snap)

        # 托管静态前端 (frontend/dist)
        dist_dir: Path = self.cfg["dist_dir"]
        if dist_dir.is_dir():
            rel = path.lstrip("/") or "index.html"
            candidate = (dist_dir / rel).resolve()
            if candidate.is_relative_to(dist_dir.resolve()) and candidate.is_file():
                return self._send_file(candidate)
            index = dist_dir / "index.html"
            if index.is_file() and not path.startswith("/api/"):
                return self._send_file(index)

        return self._send_json(404, {"error": "Not Found"})

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}: {exc}"})
            except OSError:
                pass

    def _handle_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        db: platform_db.PlatformDB = self.cfg["db"]
        client_ip = self._client_ip()

        if path == "/api/auth/login":
            if _login_rate_limited(client_ip):
                return self._send_json(429, {"error": "认证失败次数过多，请 5 分钟后再试"})
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            user = db.authenticate(username, password)
            if not user:
                _record_login_failure(client_ip)
                return self._send_json(401, {"error": "用户名或密码不正确，或账号已停用"})
            _clear_login_failures(client_ip)
            sid = db.create_session(user["id"], ttl_sec=SESSION_TTL_SEC)
            db.record_audit(user, "login", target=user["username"], client_ip=client_ip)
            return self._send_json(
                200,
                {"authenticated": True, "auth_required": True, "user": user},
                extra_headers=[self._cookie_header(sid, SESSION_TTL_SEC)],
            )

        if path == "/api/auth/logout":
            sid = self._session_id_from_cookie()
            cur_user = self._current_user()
            db.revoke_session(sid)
            if cur_user:
                db.record_audit(cur_user, "logout", target=cur_user["username"], client_ip=client_ip)
            return self._send_json(
                200,
                {"authenticated": False},
                extra_headers=[self._cookie_header("", 0)],
            )

        cur_user = self._current_user() if path.startswith("/api/") else None
        if path.startswith("/api/") and cur_user is None:
            return self._send_json(401, {"error": "未登录或会话已过期，请先登录"})
        assert cur_user is not None

        # 个人资料与改密（所有已登录真实账号可用）
        if path == "/api/me/profile":
            if cur_user["id"] == 0:
                return self._send_json(400, {"error": "API 令牌不支持修改个人资料"})
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                can_edit_name = cur_user.get("role") != "user"
                updated = db.update_user(
                    cur_user["id"],
                    display_name=payload.get("display_name") if (can_edit_name and "display_name" in payload) else None,
                    email=payload.get("email") if "email" in payload else None,
                    email_notify=payload.get("email_notify") if "email_notify" in payload else None,
                )
            except (ValueError, KeyError) as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "update_profile", target=cur_user["username"],
                            detail={"email": updated["email"], "email_notify": updated["email_notify"]},
                            client_ip=client_ip)
            return self._send_json(200, {"user": updated})

        if path == "/api/me/password":
            if cur_user["id"] == 0:
                return self._send_json(400, {"error": "API 令牌不支持修改密码"})
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                db.change_password(
                    cur_user["id"],
                    str(payload.get("new_password") or ""),
                    old_password=str(payload.get("old_password") or ""),
                    must_change_password=False,
                )
            except (ValueError, KeyError) as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "change_password", target=cur_user["username"], client_ip=client_ip)
            return self._send_json(200, {"ok": True, "user": db.get_user(cur_user["id"])})

        # 用户管理（superadmin 管理全部角色；admin 仅可管理普通用户 user，禁止创建/修改/删除管理员或超管）
        if path == "/api/users":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            req_role = str(payload.get("role") or "user")
            if cur_user.get("role") == "admin" and req_role != "user":
                return self._send_json(403, {"error": "管理员仅可创建普通用户（负责人）账号"})
            try:
                created = db.create_user(
                    username=str(payload.get("username") or ""),
                    display_name=str(payload.get("display_name") or ""),
                    role=req_role,
                    password=str(payload.get("password") or ""),
                    email=str(payload.get("email") or ""),
                    must_change_password=bool(payload.get("must_change_password", True)),
                    email_notify=str(payload.get("email_notify") or "batch"),
                )
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "create_user", target=created["username"],
                            detail={"role": created["role"], "email": created["email"]}, client_ip=client_ip)
            return self._send_json(201, {"user": created})

        if path == "/api/users/preview-from-projects":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body() or {}
            domain = str(payload.get("email_domain") or "shineyue.com").strip()
            owners = [p.get("owner", "") for p in _load_catalog(_effective_projects_csv(self.cfg), db=db)]
            items = db.preview_users_from_owners(owners, email_domain=domain)
            return self._send_json(200, {"items": items, "email_domain": domain})

        if path == "/api/users/batch-create":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            items = payload.get("items") or []
            default_password = str(payload.get("default_password") or "")
            if not isinstance(items, list) or not items:
                return self._send_json(400, {"error": "请至少选择一位要生成的负责人"})
            if len(default_password) < 6:
                return self._send_json(400, {"error": "初始密码长度不能少于 6 位"})
            if cur_user.get("role") == "admin":
                if any(isinstance(it, dict) and str(it.get("role") or "user") != "user" for it in items):
                    return self._send_json(403, {"error": "管理员仅可批量创建普通用户（负责人）账号"})
            created_list, skipped_list = [], []
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                try:
                    u = db.create_user(
                        username=str(entry.get("username") or ""),
                        display_name=str(entry.get("display_name") or ""),
                        role=str(entry.get("role") or "user"),
                        password=default_password,
                        email=str(entry.get("email") or ""),
                        must_change_password=True,
                    )
                    created_list.append(u)
                except ValueError as exc:
                    skipped_list.append({"username": entry.get("username"), "display_name": entry.get("display_name"), "reason": str(exc)})
            db.record_audit(cur_user, "batch_create_users", target=f"{len(created_list)} users",
                            detail={"created": [u["username"] for u in created_list], "skipped": len(skipped_list)},
                            client_ip=client_ip)
            return self._send_json(200, {"created": created_list, "skipped": skipped_list})

        if path.startswith("/api/users/"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            segs = [urllib.parse.unquote(s) for s in path[len("/api/users/"):].split("/") if s]
            if len(segs) != 2 or not segs[0].isdigit():
                return self._send_json(400, {"error": "无效的用户操作路径"})
            target_uid = int(segs[0])
            action = segs[1]
            payload = self._read_json_body() or {}
            target_u = db.get_user(target_uid)
            if not target_u:
                return self._send_json(404, {"error": "用户不存在"})
            if cur_user.get("role") == "admin":
                if target_u.get("role") != "user":
                    return self._send_json(403, {"error": "管理员无权操作超级管理员或其他管理员账号"})
                if action == "update" and "role" in payload and payload.get("role") != "user":
                    return self._send_json(403, {"error": "管理员无权将普通用户提权为管理员或超级管理员"})
            try:
                if action == "update":
                    updated = db.update_user(
                        target_uid,
                        display_name=payload.get("display_name") if "display_name" in payload else None,
                        role=payload.get("role") if "role" in payload else None,
                        email=payload.get("email") if "email" in payload else None,
                        is_active=payload.get("is_active") if "is_active" in payload else None,
                        email_notify=payload.get("email_notify") if "email_notify" in payload else None,
                    )
                    db.record_audit(cur_user, "update_user", target=updated["username"],
                                    detail={"role": updated["role"], "is_active": updated["is_active"], "email": updated["email"]},
                                    client_ip=client_ip)
                    return self._send_json(200, {"user": updated})
                if action == "reset-password":
                    db.change_password(
                        target_uid,
                        str(payload.get("new_password") or ""),
                        must_change_password=bool(payload.get("must_change_password", True)),
                    )
                    db.revoke_user_sessions(target_uid)
                    db.record_audit(cur_user, "reset_password", target=target_u["username"], client_ip=client_ip)
                    return self._send_json(200, {"ok": True})
                if action == "delete":
                    db.delete_user(target_uid, actor_id=cur_user["id"])
                    db.record_audit(cur_user, "delete_user", target=target_u["username"],
                                    client_ip=client_ip)
                    return self._send_json(200, {"ok": True})
            except KeyError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(404, {"error": "未知的用户操作"})

        # 项目管理 CRUD / Git 分支探测 / CSV 导入 / 代码量刷新 / 长期未维护阈值（superadmin + admin）
        if path == "/api/projects":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            managed_csv = _managed_projects_csv(self.cfg["output_dir"])
            try:
                created = db.upsert_project(payload, create_only=True, managed_csv=managed_csv)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "create_project", target=created["project_id"],
                            detail={"branch": created["branch"], "owner": created["owner"]}, client_ip=client_ip)
            return self._send_json(201, {"project": created})

        if path == "/api/projects/probe-git":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                rt_creds = _runtime_git_creds_file(self.cfg["output_dir"])
                res = platform_db.probe_git_remote(
                    str(payload.get("git_url") or ""),
                    branch=str(payload.get("branch") or ""),
                    credential_file=rt_creds if rt_creds.is_file() else _base_git_creds_file(),
                )
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(200, res)

        if path == "/api/projects/import":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            managed_csv = _managed_projects_csv(self.cfg["output_dir"])
            try:
                res = db.import_projects_csv(
                    str(payload.get("csv_text") or ""),
                    mode=str(payload.get("mode") or "upsert"),
                    managed_csv=managed_csv,
                )
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "import_projects", target=str(payload.get("mode") or "upsert"),
                            detail={"created": res["created"], "updated": res["updated"], "errors": len(res["errors"])},
                            client_ip=client_ip)
            return self._send_json(200, res)

        if path == "/api/projects/refresh-metrics":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body() or {}
            pids = payload.get("project_ids")
            pid_list = [str(x) for x in pids if _is_safe_segment(str(x))] if isinstance(pids, list) and pids else None
            res = _refresh_projects_metrics(self.cfg["output_dir"], self.cfg["settings_path"], db, project_ids=pid_list)
            db.record_audit(cur_user, "refresh_metrics", target=f"{res['updated']}/{res['scanned']}",
                            detail=res, client_ip=client_ip)
            return self._send_json(200, res)

        if path == "/api/projects/stale-thresholds":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                saved = db.set_stale_thresholds(payload.get("warn_days", 90), payload.get("critical_days", 180))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "update_stale_thresholds", target="stale_thresholds",
                            detail=saved, client_ip=client_ip)
            return self._send_json(200, {"stale_thresholds": saved})

        if path.startswith("/api/projects/"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            segs = [urllib.parse.unquote(s) for s in path[len("/api/projects/"):].split("/") if s]
            if len(segs) != 2 or not _is_safe_segment(segs[0]):
                return self._send_json(400, {"error": "无效的项目操作路径"})
            target_pid, action = segs[0], segs[1]
            managed_csv = _managed_projects_csv(self.cfg["output_dir"])
            try:
                if action == "update":
                    payload = self._read_json_body()
                    if payload is None:
                        return self._send_json(400, {"error": "无效的 JSON 请求体"})
                    existing = db.get_project(target_pid)
                    if not existing:
                        return self._send_json(404, {"error": f"项目不存在：{target_pid}"})
                    merged = dict(existing)
                    merged.update({k: v for k, v in payload.items() if k in platform_db.PROJECT_CSV_FIELDS})
                    merged["project_id"] = target_pid
                    updated = db.upsert_project(merged, create_only=False, managed_csv=managed_csv)
                    db.record_audit(cur_user, "update_project", target=target_pid,
                                    detail={"branch": updated["branch"], "owner": updated["owner"], "status": updated["status"]},
                                    client_ip=client_ip)
                    return self._send_json(200, {"project": updated})
                if action == "delete":
                    deleted = db.delete_project(target_pid, managed_csv=managed_csv)
                    db.record_audit(cur_user, "delete_project", target=target_pid,
                                    detail={"project_name": deleted.get("project_name")}, client_ip=client_ip)
                    return self._send_json(200, {"ok": True, "project_id": target_pid})
            except KeyError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(404, {"error": "未知的项目操作"})

        # 提示词在线配置与版本回滚（superadmin + admin）
        if path.startswith("/api/prompts/"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            segs = [urllib.parse.unquote(s) for s in path[len("/api/prompts/"):].split("/") if s]
            if len(segs) != 2 or segs[0] not in platform_db.PROMPT_KEYS:
                return self._send_json(400, {"error": "无效的提示词操作路径"})
            pkey, action = segs[0], segs[1]
            pdir = _prompts_dir(self.cfg["output_dir"])
            payload = self._read_json_body() or {}
            try:
                if action == "save":
                    saved = db.save_prompt(
                        pkey,
                        str(payload.get("content") or ""),
                        updated_by=cur_user.get("username", ""),
                        note=str(payload.get("note") or ""),
                        prompts_dir=pdir,
                    )
                    db.record_audit(cur_user, "save_prompt", target=f"{pkey}@v{saved['version']}",
                                    detail={"note": saved["note"], "length": len(saved["content"])},
                                    client_ip=client_ip)
                    return self._send_json(200, {"prompts": _build_prompts_payload(db)})
                if action == "rollback":
                    vid = int(payload.get("version_id") or 0)
                    saved = db.rollback_prompt(pkey, vid, updated_by=cur_user.get("username", ""), prompts_dir=pdir)
                    db.record_audit(cur_user, "rollback_prompt", target=f"{pkey}@v{saved['version']}",
                                    detail={"source_version_id": vid}, client_ip=client_ip)
                    return self._send_json(200, {"prompts": _build_prompts_payload(db)})
                if action == "reset":
                    db.reset_prompt(pkey, prompts_dir=pdir)
                    db.record_audit(cur_user, "reset_prompt", target=pkey, client_ip=client_ip)
                    return self._send_json(200, {"prompts": _build_prompts_payload(db)})
            except KeyError as exc:
                return self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(404, {"error": "未知的提示词操作"})

        # 超级管理员（运维）专属配置：模型 / 运行环境变量 / Git 账号凭据
        if path == "/api/ops/model":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            existing_env, _ = _read_effective_model_env(self.cfg["output_dir"], self.cfg["settings_path"])
            next_env = dict(existing_env)
            for k in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_CONTEXT_WINDOW", "OPENAI_MAX_OUTPUT_TOKENS",
                      "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
                if k in payload:
                    val = str(payload.get(k) or "").strip()
                    if val:
                        next_env[k] = val
                    else:
                        next_env.pop(k, None)
            # 密钥字段留空表示保持原有密钥不变；传入明确值则覆盖
            for secret_k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
                val = str(payload.get(secret_k) or "").strip()
                if val:
                    next_env[secret_k] = val
                elif payload.get(f"clear_{secret_k}"):
                    next_env.pop(secret_k, None)
            try:
                platform_db.write_env_file(_runtime_model_env_file(self.cfg["output_dir"]), next_env)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "update_model_config", target="model.runtime.env",
                            detail={"OPENAI_BASE_URL": next_env.get("OPENAI_BASE_URL", ""),
                                    "OPENAI_MODEL": next_env.get("OPENAI_MODEL", "")},
                            client_ip=client_ip)
            return self._send_json(200, _build_ops_config(self.cfg["output_dir"], self.cfg["settings_path"], db))

        if path == "/api/ops/test-model":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body() or {}
            existing_env, _ = _read_effective_model_env(self.cfg["output_dir"], self.cfg["settings_path"])
            base_url = str(payload.get("OPENAI_BASE_URL") or existing_env.get("OPENAI_BASE_URL") or "").strip()
            model = str(payload.get("OPENAI_MODEL") or existing_env.get("OPENAI_MODEL") or "").strip()
            api_key = str(payload.get("OPENAI_API_KEY") or "").strip() or existing_env.get("OPENAI_API_KEY", "")
            try:
                res = platform_db.test_openai_endpoint(base_url, model, api_key)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(200, res)

        if path == "/api/ops/runtime":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            validated: dict = {}
            int_limits = {
                "max_parallel": (1, 32),
                "max_model_concurrency": (1, 32),
                "per_project_timeout_sec": (60, 28800),
                "timeout_small_sec": (60, 28800),
                "timeout_medium_sec": (60, 28800),
                "timeout_large_sec": (60, 28800),
                "batch_timeout_sec": (300, 604800),
            }
            for k, (lo, hi) in int_limits.items():
                if k in payload and payload[k] is not None and str(payload[k]).strip() != "":
                    try:
                        v = int(payload[k])
                    except (TypeError, ValueError):
                        return self._send_json(400, {"error": f"{k} 必须为整数"})
                    if v < lo or v > hi:
                        return self._send_json(400, {"error": f"{k} 需在 {lo}–{hi} 之间"})
                    validated[k] = v
            for k in ("fast_mode", "fast_skip_lizard", "adaptive_timeout", "summary_enabled"):
                if k in payload:
                    validated[k] = bool(payload[k])
            if "validation_mode" in payload:
                vm = str(payload["validation_mode"] or "lenient").strip().lower()
                if vm not in ("lenient", "strict"):
                    return self._send_json(400, {"error": "validation_mode 仅支持 lenient 或 strict"})
                validated["validation_mode"] = vm
            for k in ("scanner_image", "model_network", "semgrep_rules_dir"):
                if k in payload:
                    val = str(payload[k] or "").strip()
                    if any(ch.isspace() for ch in val):
                        return self._send_json(400, {"error": f"{k} 不能包含空白字符"})
                    if k == "semgrep_rules_dir" and val and not Path(val).is_absolute():
                        return self._send_json(400, {"error": "semgrep_rules_dir 必须为容器内绝对路径"})
                    validated[k] = val
            ov_file = _runtime_overrides_file(self.cfg["output_dir"])
            ov_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = ov_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, ov_file)
            db.record_audit(cur_user, "update_runtime_config", target="runtime_overrides.json",
                            detail=validated, client_ip=client_ip)
            return self._send_json(200, _build_ops_config(self.cfg["output_dir"], self.cfg["settings_path"], db))

        if path == "/api/ops/git-credentials":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                created = db.upsert_git_credential(
                    payload,
                    runtime_file=_runtime_git_creds_file(self.cfg["output_dir"]),
                    base_file=_base_git_creds_file(),
                )
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "create_git_credential", target=f"{created['protocol']}://{created['host']}{created['path_prefix']}",
                            detail={"username": created["username"], "label": created["label"]}, client_ip=client_ip)
            return self._send_json(201, {"credential": created, "git_credentials": db.list_git_credentials(include_secret=False)})

        if path.startswith("/api/ops/git-credentials/"):
            if not self._require_role(cur_user, ("superadmin",)):
                return
            segs = [urllib.parse.unquote(s) for s in path[len("/api/ops/git-credentials/"):].split("/") if s]
            if len(segs) != 2 or not segs[0].isdigit():
                return self._send_json(400, {"error": "无效的 Git 账号操作路径"})
            cid, action = int(segs[0]), segs[1]
            rt_file = _runtime_git_creds_file(self.cfg["output_dir"])
            base_file = _base_git_creds_file()
            try:
                if action == "update":
                    payload = self._read_json_body()
                    if payload is None:
                        return self._send_json(400, {"error": "无效的 JSON 请求体"})
                    updated = db.upsert_git_credential(payload, cred_id=cid, runtime_file=rt_file, base_file=base_file)
                    db.record_audit(cur_user, "update_git_credential", target=f"{updated['protocol']}://{updated['host']}{updated['path_prefix']}",
                                    detail={"username": updated["username"], "is_active": updated["is_active"]}, client_ip=client_ip)
                    return self._send_json(200, {"credential": updated, "git_credentials": db.list_git_credentials(include_secret=False)})
                if action == "delete":
                    deleted = db.delete_git_credential(cid, runtime_file=rt_file, base_file=base_file)
                    db.record_audit(cur_user, "delete_git_credential", target=f"{deleted['protocol']}://{deleted['host']}{deleted['path_prefix']}",
                                    detail={"username": deleted["username"]}, client_ip=client_ip)
                    return self._send_json(200, {"ok": True, "git_credentials": db.list_git_credentials(include_secret=False)})
            except KeyError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(404, {"error": "未知的 Git 账号操作"})

        # SMTP 邮件投递配置（仅 superadmin）
        if path == "/api/ops/smtp":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            try:
                saved = db.save_smtp_config(payload)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "update_smtp_config", target=saved.get("host", ""),
                            detail={"enabled": saved["enabled"], "port": saved["port"],
                                    "security": saved["security"], "attachment_format": saved["attachment_format"]},
                            client_ip=client_ip)
            return self._send_json(200, {"smtp": saved})

        if path == "/api/ops/smtp/test":
            if not self._require_role(cur_user, ("superadmin",)):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            to_email = str(payload.get("to_email") or "").strip()
            override = {k: v for k, v in payload.items() if k != "to_email"}
            try:
                res = mailer.send_test_email(db, to_email, override_cfg=override or None)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            db.record_audit(cur_user, "test_smtp_email", target=to_email,
                            detail={"ok": res["ok"], "elapsed_ms": res["elapsed_ms"]}, client_ip=client_ip)
            return self._send_json(200, res)

        # 邮件失败一键重试（superadmin + admin）
        if path.startswith("/api/email-logs/") and path.endswith("/retry"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            raw_id = path[len("/api/email-logs/"):-len("/retry")].strip("/")
            if not raw_id.isdigit():
                return self._send_json(400, {"error": "无效的邮件日志 ID"})
            log_item = db.get_email_log(int(raw_id))
            if not log_item:
                return self._send_json(404, {"error": "未找到该邮件投递记录"})
            if log_item["recipient_type"] == "test":
                try:
                    t_res = mailer.send_test_email(db, log_item["recipient_email"])
                except ValueError as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(200, {
                    "ok": t_res["ok"], "sent": 1 if t_res["ok"] else 0,
                    "failed": 0 if t_res["ok"] else 1, "skipped": 0, "details": [],
                    "skipped_reason": t_res.get("error", ""),
                })
            res = mailer.dispatch_batch_emails(
                self.cfg["output_dir"], log_item["run_date"], log_item["run_id"], db,
                force=True, only_email=log_item["recipient_email"], only_type=log_item["recipient_type"],
            )
            db.record_audit(cur_user, "retry_email", target=log_item["recipient_email"],
                            detail={"run": f"{log_item['run_date']}/{log_item['run_id']}", "sent": res["sent"], "failed": res["failed"]},
                            client_ip=client_ip)
            return self._send_json(200, res)

        # 扫描暂停 / 恢复（superadmin + admin）
        if path == "/api/scans/pause":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body() or {}
            st = _set_pause_state(self.cfg["output_dir"], True, actor=cur_user, reason=str(payload.get("reason") or ""))
            db.record_audit(cur_user, "pause_scan", target="pause.flag",
                            detail={"reason": st.get("reason")}, client_ip=client_ip)
            return self._send_json(200, st)

        if path == "/api/scans/resume":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            st = _set_pause_state(self.cfg["output_dir"], False, actor=cur_user)
            db.record_audit(cur_user, "resume_scan", target="pause.flag", client_ip=client_ip)
            return self._send_json(200, st)

        if path == "/api/schedule":
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            res = _update_schedule(self.cfg["output_dir"], payload)
            if res.get("bad_request"):
                return self._send_json(400, {"error": res["error"]})
            db.record_audit(cur_user, "update_schedule", target="schedule.json",
                            detail={"enabled": payload.get("enabled"), "time": payload.get("time")}, client_ip=client_ip)
            return self._send_json(200, res)

        if path.startswith("/api/runs/") and path.endswith("/delete"):
            inner = path[len("/api/runs/"):-len("/delete")]
            segs = [urllib.parse.unquote(s) for s in inner.split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            res = _delete_run(self.cfg["output_dir"], segs[0], segs[1], self.cfg["settings_path"], cur_user)
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            if res.get("forbidden"):
                return self._send_json(403, {"error": res["error"]})
            if res.get("conflict"):
                return self._send_json(409, {"error": res["error"]})
            db.record_audit(cur_user, "delete_run", target=f"{segs[0]}/{segs[1]}",
                            detail={"triggered_by": res.get("triggered_by"),
                                    "rebuilt_states": res.get("rebuilt_states"),
                                    "cleared_states": res.get("cleared_states")},
                            client_ip=client_ip)
            return self._send_json(200, res)

        if path.startswith("/api/runs/") and path.endswith("/mark-aborted"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            inner = path[len("/api/runs/"):-len("/mark-aborted")]
            segs = [urllib.parse.unquote(s) for s in inner.split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            res = _mark_run_aborted(self.cfg["output_dir"], segs[0], segs[1], self.cfg["settings_path"])
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            if res.get("conflict"):
                return self._send_json(409, {"error": res["error"]})
            db.record_audit(cur_user, "mark_run_aborted", target=f"{segs[0]}/{segs[1]}", client_ip=client_ip)
            return self._send_json(200, res)

        if path.startswith("/api/runs/") and path.endswith("/send-emails"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            inner = path[len("/api/runs/"):-len("/send-emails")]
            segs = [urllib.parse.unquote(s) for s in inner.split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            payload = self._read_json_body() or {}
            res = mailer.dispatch_batch_emails(
                self.cfg["output_dir"], segs[0], segs[1], db,
                force=True,
                only_email=str(payload.get("recipient_email") or "").strip() or None,
            )
            if res.get("skipped_reason") and res["sent"] == 0 and res["failed"] == 0:
                return self._send_json(400, {"error": res["skipped_reason"]})
            db.record_audit(cur_user, "send_batch_emails", target=f"{segs[0]}/{segs[1]}",
                            detail={"sent": res["sent"], "failed": res["failed"], "skipped": res["skipped"]},
                            client_ip=client_ip)
            return self._send_json(200, res)

        if path.startswith("/api/runs/") and path.endswith("/summary"):
            if not self._require_role(cur_user, ("superadmin", "admin")):
                return
            inner = path[len("/api/runs/"):-len("/summary")]
            segs = [urllib.parse.unquote(s) for s in inner.split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            res = _generate_batch_summary_now(
                self.cfg["output_dir"], segs[0], segs[1], _effective_projects_csv(self.cfg)
            )
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            db.record_audit(cur_user, "generate_summary", target=f"{segs[0]}/{segs[1]}", client_ip=client_ip)
            return self._send_json(200, res)

        if path.startswith("/api/scans/") and path.endswith("/cancel"):
            task_id = urllib.parse.unquote(path[len("/api/scans/"):-len("/cancel")])
            if cur_user.get("role") == "user":
                with SCAN_LOCK:
                    t = SCAN_TASKS.get(task_id)
                    if not t or t.get("triggered_by") != cur_user.get("username"):
                        return self._send_json(403, {"error": "无权中止其他用户触发的扫描任务"})
            res = _cancel_scan_task(task_id)
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            if res.get("conflict"):
                return self._send_json(409, {"error": res["error"]})
            db.record_audit(cur_user, "cancel_scan", target=task_id, client_ip=client_ip)
            return self._send_json(200, res)

        if path != "/api/scans":
            return self._send_json(404, {"error": "Not Found"})
        payload = self._read_json_body()
        if payload is None:
            return self._send_json(400, {"error": "无效的 JSON 请求体"})

        # 普通用户仅允许触发自己负责的项目，禁止全量触发与全局汇总
        effective_csv = _effective_projects_csv(self.cfg)
        if cur_user.get("role") == "user":
            if payload.get("summary_only"):
                return self._send_json(403, {"error": "普通用户无权生成全局汇总报告"})
            only = payload.get("only")
            if not isinstance(only, list) or not only:
                return self._send_json(403, {"error": "普通用户必须勾选指定自己负责的项目进行扫描"})
            allowed = _allowed_pids_for_user(cur_user, effective_csv, db=db) or set()
            forbidden = [str(pid) for pid in only if str(pid) not in allowed]
            if forbidden:
                return self._send_json(403, {"error": f"无权扫描非本人负责的项目：{', '.join(forbidden[:5])}"})

        res = _start_scan_task(
            payload, ROOT, self.cfg["settings_path"], effective_csv,
            output_dir=self.cfg["output_dir"],
            triggered_by=cur_user.get("username", ""),
        )
        if res.get("bad_request"):
            return self._send_json(400, {"error": res["error"]})
        if res.get("conflict"):
            return self._send_json(409, {"error": res["error"]})
        db.record_audit(cur_user, "start_scan", target=res.get("task_id", ""),
                        detail={"only_count": len(payload.get("only") or []), "fast": payload.get("fast")},
                        client_ip=client_ip)
        return self._send_json(202, res)


def main() -> int:
    ap = argparse.ArgumentParser(description="八性代码质量审查平台 · Web API 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--settings", default=str(ROOT / "config" / "settings.json"))
    ap.add_argument("--projects", default=str(_default_projects_csv(ROOT)))
    ap.add_argument("--data-dir", help="覆盖 output_dir（即包含 reports/ results/ state/ 的目录）")
    ap.add_argument("--token", help="访问认证令牌（也可用环境变量 AUDIT_WEB_TOKEN 设置；留空表示免密）")
    args = ap.parse_args()

    settings_path = Path(args.settings).resolve()
    settings = _read_json(settings_path) or {}
    env_data_dir = os.environ.get("AUDIT_DATA_DIR", "").strip()
    default_output = (
        args.data_dir
        or os.environ.get("AUDIT_OUTPUT_DIR", "").strip()
        or (str(Path(env_data_dir) / "output") if env_data_dir else "")
        or settings.get("output_dir")
        or str(ROOT / "audit-data" / "output")
    )
    output_dir = Path(default_output).resolve()
    projects_csv = Path(args.projects).resolve()
    checklist = scoring.load_checklist()
    dist_dir = (ROOT / "frontend" / "dist").resolve()
    auth_token = (args.token if args.token is not None else os.environ.get("AUDIT_WEB_TOKEN", "")).strip()
    db = platform_db.PlatformDB(output_dir / "state" / "platform.db")
    seeded = db.seed_projects_from_csv(projects_csv, _managed_projects_csv(output_dir))
    if seeded:
        print(f"[web_server] 已从 {projects_csv.name} 初始化导入 {seeded} 个项目到管理端数据库", flush=True)
    db.sync_all_active_prompts(_prompts_dir(output_dir))
    if db.list_git_credentials():
        db.export_git_credentials_file(_runtime_git_creds_file(output_dir), base_file=_base_git_creds_file())
    init_admin = db.ensure_initial_superadmin(legacy_token=auth_token)
    if init_admin:
        init_user, init_pass = init_admin
        if auth_token and not os.environ.get("AUDIT_INIT_ADMIN_PASS"):
            print(
                f"[web_server] 首次初始化超级管理员：用户名={init_user}，初始密码沿用 AUDIT_WEB_TOKEN（请登录后及时修改密码）",
                flush=True,
            )
        elif os.environ.get("AUDIT_INIT_ADMIN_PASS"):
            print(f"[web_server] 首次初始化超级管理员：用户名={init_user}（密码来自 AUDIT_INIT_ADMIN_PASS）", flush=True)
        else:
            print(
                f"[web_server] 首次初始化超级管理员：用户名={init_user}，初始密码={init_pass}（请登录后立即修改密码）",
                flush=True,
            )

    server = ThreadingHTTPServer((args.host, args.port), AuditHandler)
    server.cfg = {  # type: ignore[attr-defined]
        "output_dir": output_dir,
        "settings_path": settings_path,
        "projects_csv": projects_csv,
        "scan_statuses": list(settings.get("scan_statuses", ["在研"])),
        "checklist": checklist,
        "dist_dir": dist_dir,
        "auth_token": auth_token,
        "db": db,
    }
    _hydrate_persisted_tasks(output_dir)
    stop_event = threading.Event()

    def scheduler_worker() -> None:
        while not stop_event.wait(15.0):
            try:
                _schedule_tick(server.cfg)  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"[web_server] scheduler tick error: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=scheduler_worker, daemon=True, name="audit-scheduler").start()
    auth_mode = "rbac+api-token" if auth_token else "rbac"
    print(f"[web_server] Listening on http://{args.host}:{args.port} (output_dir={output_dir}, auth={auth_mode})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
