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
import subprocess
import sys
import threading
import time
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
import report_validate  # noqa: E402
import scoring  # noqa: E402

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
            payload, ROOT, cfg["settings_path"], cfg["projects_csv"],
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
                        hist_global_avg: float | None = None) -> dict:
    cat = catalog or {}
    cfg_settings = settings or {}
    pid_hist = hist_by_pid or {}
    projects = data.get("projects") or []
    pending = [str(p) for p in (data.get("pending_projects") or [])]
    completed_count = len(projects)
    total_count = completed_count + len(pending)
    ok_count = sum(1 for p in projects if p.get("status") == "ok")
    error_count = sum(1 for p in projects if p.get("status") == "error")
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
    for p in projects:
        pid = p.get("project_id", "")
        meta = cat.get(pid, {})
        dur = _project_duration_seconds(p)
        items.append({
            "project_id": pid,
            "project_name": p.get("project_name") or meta.get("project_name") or pid,
            "department": (p.get("department") or meta.get("department") or "").strip() or "未分组",
            "owner": p.get("owner") or meta.get("owner") or "",
            "state": "ok" if p.get("status") == "ok" else "error",
            "status": p.get("status", "unknown"),
            "total": (p.get("report_scores") or {}).get("total"),
            "error": p.get("error"),
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


def _build_live_progress(output_dir: Path, projects_csv: Path | None = None,
                         settings_path: Path | None = None) -> dict:
    catalog = {p["project_id"]: p for p in _load_projects(projects_csv)} if projects_csv else {}
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
        )
        if idx == 0:
            latest_run = item
        if item["status"] == "running":
            active_runs.append(item)
    with SCAN_LOCK:
        all_tasks = [_serialize_task(t, include_logs=False) for t in reversed(list(SCAN_TASKS.values()))]
        running_task = next(
            (_serialize_task(t, include_logs=True) for t in reversed(list(SCAN_TASKS.values())) if t["status"] == "running"),
            None,
        )
    return {
        "active_runs": active_runs,
        "latest_run": latest_run,
        "running_task": running_task,
        "tasks": all_tasks,
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
            dims_out.append({
                "id": did,
                "name": d["name"],
                "score": ov["score"] if ov else None,
                "coverage": (ov["coverage"] / 100.0) if ov and ov.get("coverage") is not None else None,
                "counts": {
                    "PASS": ov["counts"][0], "WARN": ov["counts"][1],
                    "FAIL": ov["counts"][2], "N/A": ov["counts"][3],
                    "NEED_INFO": ov["counts"][4],
                } if ov else None,
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


def _discover_runs(output_dir: Path) -> list[dict]:
    """扫描 results/<date>/<run_id>/_run.json 与旧格式 results/<date>/_run.json，按时间倒序排列。"""
    results_dir = output_dir / "results"
    runs: list[dict] = []
    if not results_dir.is_dir():
        return runs

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
                    runs.append(_summarize_run(date_str, child.name, data, output_dir))
        # 兼容旧格式: results/<date>/_run.json
        legacy_file = date_dir / "_run.json"
        if legacy_file.is_file():
            data = _read_json(legacy_file)
            if isinstance(data, dict):
                runs.append(_summarize_run(date_str, "default", data, output_dir))

    runs.sort(key=lambda r: (r.get("date", ""), r.get("started_at") or r.get("run_id", "")), reverse=True)
    return runs


def _summarize_run(date_str: str, run_id: str, data: dict, output_dir: Path) -> dict:
    projects = data.get("projects") or []
    ok_count = sum(1 for p in projects if p.get("status") == "ok")
    err_count = sum(1 for p in projects if p.get("status") == "error")
    rep_dir = output_dir / "reports" / date_str if run_id == "default" else output_dir / "reports" / date_str / run_id
    summary_exists = (rep_dir / "_summary.md").is_file()
    return {
        "date": data.get("date") or date_str,
        "run_id": data.get("run_id") or run_id,
        "status": data.get("status", "unknown"),
        "report_mode": data.get("report_mode", ""),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "snapshot_at": data.get("snapshot_at"),
        "summary_status": data.get("summary_status"),
        "has_summary": summary_exists,
        "total_projects": len(projects),
        "ok_count": ok_count,
        "error_count": err_count,
        "pending_count": len(data.get("pending_projects") or []),
    }


def _resolve_report_dir(output_dir: Path, date_str: str, run_id: str) -> Path:
    if run_id == "default":
        return output_dir / "reports" / date_str
    return output_dir / "reports" / date_str / run_id


def _resolve_run_file(output_dir: Path, date_str: str, run_id: str) -> Path:
    if run_id == "default":
        return output_dir / "results" / date_str / "_run.json"
    return output_dir / "results" / date_str / run_id / "_run.json"


def _build_overview(output_dir: Path, projects_csv: Path, checklist: dict) -> dict:
    runs = _discover_runs(output_dir)
    catalog = {p["project_id"]: p for p in _load_projects(projects_csv)}

    # 读取所有 state/<pid>.json 作为项目最新基线
    state_dir = output_dir / "state"
    states: dict[str, dict] = {}
    if state_dir.is_dir():
        for f in sorted(state_dir.glob("*.json")):
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
        rows.append({
            "project_id": pid,
            "project_name": rec.get("project_name") or meta.get("project_name") or pid,
            "department": rec.get("department") or meta.get("department") or "",
            "owner": rec.get("owner") or meta.get("owner") or "",
            "tech_stack": meta.get("tech_stack") or "",
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


def _start_scan_task(payload: dict, root: Path, settings_path: Path, projects_csv: Path,
                     trigger: str = "manual", output_dir: Path | None = None) -> dict:
    task_id = dt.datetime.now().strftime("%H%M%S") + "-" + uuid.uuid4().hex[:8]
    compose_ep = root / "docker" / "compose_entrypoint.py"
    use_compose = bool(os.environ.get("AUDIT_DATA_DIR", "").strip()) and compose_ep.is_file()
    if use_compose:
        cmd = [sys.executable, str(compose_ep)]
        sub_env = dict(os.environ)
        sub_env["AUDIT_SETTINGS_FILE"] = str(settings_path)
        sub_env["AUDIT_PROJECTS_FILE"] = str(projects_csv)
    else:
        cmd = [sys.executable, str(RUNNER_DIR / "run_daily.py"),
               "--settings", str(settings_path), "--projects", str(projects_csv)]
        sub_env = None
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

    def _is_authenticated(self) -> bool:
        expected = self.cfg.get("auth_token") or ""
        if not expected:
            return True
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.startswith("Bearer "):
            candidate = auth_header[7:].strip()
            if candidate and hmac.compare_digest(candidate, expected):
                return True
        return _valid_session(self._session_id_from_cookie())

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

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        output_dir: Path = self.cfg["output_dir"]
        projects_csv: Path = self.cfg["projects_csv"]
        checklist: dict = self.cfg["checklist"]

        if path == "/api/health":
            return self._send_json(200, {
                "ok": True,
                "auth_required": bool(self.cfg.get("auth_token")),
                "has_frontend": self.cfg["dist_dir"].is_dir(),
            })

        if path == "/api/auth/status":
            auth_required = bool(self.cfg.get("auth_token"))
            return self._send_json(200, {
                "auth_required": auth_required,
                "authenticated": self._is_authenticated(),
            })

        if path.startswith("/api/") and not self._is_authenticated():
            return self._send_json(401, {"error": "未登录或会话已过期，请先认证"})

        if path == "/api/overview":
            return self._send_json(200, _build_overview(output_dir, projects_csv, checklist))

        if path == "/api/runs":
            return self._send_json(200, {"runs": _discover_runs(output_dir)})

        if path.startswith("/api/runs/"):
            segs = [urllib.parse.unquote(s) for s in path[len("/api/runs/"):].split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            date_str, run_id = segs
            run_file = _resolve_run_file(output_dir, date_str, run_id)
            data = _read_json(run_file)
            if not isinstance(data, dict):
                return self._send_json(404, {"error": "未找到该批次运行记录"})
            rep_dir = _resolve_report_dir(output_dir, date_str, run_id)
            data["has_summary"] = (rep_dir / "_summary.md").is_file()
            catalog = {p["project_id"]: p for p in _load_projects(projects_csv)}
            for p in data.get("projects") or []:
                meta = catalog.get(p.get("project_id", ""), {})
                if not p.get("department") and meta.get("department"):
                    p["department"] = meta["department"]
                if not p.get("owner") and meta.get("owner"):
                    p["owner"] = meta["owner"]
                if not p.get("project_name") and meta.get("project_name"):
                    p["project_name"] = meta["project_name"]
            return self._send_json(200, data)

        if path.startswith("/api/reports/"):
            segs = [urllib.parse.unquote(s) for s in path[len("/api/reports/"):].split("/") if s]
            if len(segs) != 3 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的报告路径"})
            date_str, run_id, target = segs
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
            catalog = {p["project_id"]: p for p in _load_projects(projects_csv)}
            proj_weights, weights_key = _project_weights(catalog.get(target), checklist)
            parsed_report = _parse_markdown_report(md_file, checklist, proj_weights, weights_key)
            parsed_report.update({
                "type": "project",
                "date": date_str,
                "run_id": run_id,
                "project_id": target,
            })
            return self._send_json(200, parsed_report)

        if path == "/api/projects":
            projects = _load_projects(projects_csv)
            state_dir = output_dir / "state"
            for p in projects:
                pid = p["project_id"]
                st = _read_json(state_dir / f"{pid}.json") if state_dir.is_dir() else None
                if isinstance(st, dict):
                    p["state"] = st
            return self._send_json(200, {
                "projects": projects,
                "scan_statuses": self.cfg.get("scan_statuses", ["在研"]),
                "checklist_dimensions": checklist["dimensions"],
            })

        if path == "/api/doctor":
            return self._send_json(200, doctor.run_checks(
                ROOT, output_dir=output_dir, projects_csv=projects_csv, settings_path=self.cfg["settings_path"]
            ))

        if path == "/api/schedule":
            with SCHEDULE_LOCK:
                sched = _load_schedule(output_dir)
                now = dt.datetime.now()
            return self._send_json(200, {
                "schedule": sched,
                "next_run_at": _next_run_at(sched, now),
                "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            })

        if path == "/api/scans/progress":
            return self._send_json(200, _build_live_progress(output_dir, projects_csv, self.cfg["settings_path"]))

        if path == "/api/scans":
            with SCAN_LOCK:
                tasks = [_serialize_task(t, include_logs=False) for t in reversed(list(SCAN_TASKS.values()))]
            return self._send_json(200, {"tasks": tasks})

        if path.startswith("/api/scans/"):
            task_id = urllib.parse.unquote(path[len("/api/scans/"):])
            with SCAN_LOCK:
                t = SCAN_TASKS.get(task_id)
                if not t:
                    return self._send_json(404, {"error": "未找到任务"})
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/login":
            expected = self.cfg.get("auth_token") or ""
            if not expected:
                return self._send_json(200, {"authenticated": True, "auth_required": False})
            client_ip = (self.client_address[0] if self.client_address else "unknown")
            if _login_rate_limited(client_ip):
                return self._send_json(429, {"error": "认证失败次数过多，请 5 分钟后再试"})
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            candidate = str(payload.get("token") or "").strip()
            if not candidate or not hmac.compare_digest(candidate, expected):
                _record_login_failure(client_ip)
                return self._send_json(401, {"error": "访问令牌不正确"})
            _clear_login_failures(client_ip)
            sid = _issue_session()
            return self._send_json(
                200,
                {"authenticated": True, "auth_required": True},
                extra_headers=[self._cookie_header(sid, SESSION_TTL_SEC)],
            )

        if path == "/api/auth/logout":
            _revoke_session(self._session_id_from_cookie())
            return self._send_json(
                200,
                {"authenticated": False},
                extra_headers=[self._cookie_header("", 0)],
            )

        if path.startswith("/api/") and not self._is_authenticated():
            return self._send_json(401, {"error": "未登录或会话已过期，请先认证"})

        if path == "/api/schedule":
            payload = self._read_json_body()
            if payload is None:
                return self._send_json(400, {"error": "无效的 JSON 请求体"})
            res = _update_schedule(self.cfg["output_dir"], payload)
            if res.get("bad_request"):
                return self._send_json(400, {"error": res["error"]})
            return self._send_json(200, res)

        if path.startswith("/api/runs/") and path.endswith("/mark-aborted"):
            inner = path[len("/api/runs/"):-len("/mark-aborted")]
            segs = [urllib.parse.unquote(s) for s in inner.split("/") if s]
            if len(segs) != 2 or not all(_is_safe_segment(s) for s in segs):
                return self._send_json(400, {"error": "无效的批次路径"})
            res = _mark_run_aborted(self.cfg["output_dir"], segs[0], segs[1], self.cfg["settings_path"])
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            if res.get("conflict"):
                return self._send_json(409, {"error": res["error"]})
            return self._send_json(200, res)

        if path.startswith("/api/scans/") and path.endswith("/cancel"):
            task_id = urllib.parse.unquote(path[len("/api/scans/"):-len("/cancel")])
            res = _cancel_scan_task(task_id)
            if res.get("not_found"):
                return self._send_json(404, {"error": res["error"]})
            if res.get("conflict"):
                return self._send_json(409, {"error": res["error"]})
            return self._send_json(200, res)

        if path != "/api/scans":
            return self._send_json(404, {"error": "Not Found"})
        payload = self._read_json_body()
        if payload is None:
            return self._send_json(400, {"error": "无效的 JSON 请求体"})

        res = _start_scan_task(
            payload, ROOT, self.cfg["settings_path"], self.cfg["projects_csv"],
            output_dir=self.cfg["output_dir"],
        )
        if res.get("bad_request"):
            return self._send_json(400, {"error": res["error"]})
        if res.get("conflict"):
            return self._send_json(409, {"error": res["error"]})
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

    server = ThreadingHTTPServer((args.host, args.port), AuditHandler)
    server.cfg = {  # type: ignore[attr-defined]
        "output_dir": output_dir,
        "settings_path": settings_path,
        "projects_csv": projects_csv,
        "scan_statuses": list(settings.get("scan_statuses", ["在研"])),
        "checklist": checklist,
        "dist_dir": dist_dir,
        "auth_token": auth_token,
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
    auth_mode = "token-enabled" if auth_token else "open (no AUDIT_WEB_TOKEN)"
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
