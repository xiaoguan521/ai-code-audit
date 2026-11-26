import { useEffect, useRef, useState } from "react";
import { CalendarClock, Check, Play, RefreshCw, Save, Square, Stethoscope } from "lucide-react";
import { api } from "../api";
import type {
  CatalogProject,
  DoctorReport,
  LiveProgressResponse,
  RunProgressItem,
  ScanTask,
  ScheduleResponse,
} from "../types";

interface Props {
  onOpenReport?: (date: string, runId: string, target: string) => void;
}

const DOCTOR_COLOR: Record<string, string> = {
  ok: "var(--good)",
  warn: "var(--warning)",
  error: "var(--critical)",
};

const WEEKDAY_LABELS: [number, string][] = [
  [1, "周一"],
  [2, "周二"],
  [3, "周三"],
  [4, "周四"],
  [5, "周五"],
  [6, "周六"],
  [7, "周日"],
];

function formatElapsed(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const start = Date.parse(startIso);
  const end = endIso ? Date.parse(endIso) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return "—";
  const sec = Math.floor((end - start) / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  return min < 60 ? `${min}m ${rem}s` : `${Math.floor(min / 60)}h ${min % 60}m`;
}

export function ScansPage({ onOpenReport }: Props) {
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [scanStatuses, setScanStatuses] = useState<string[]>(["在研"]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [fast, setFast] = useState(false);
  const [forceFull, setForceFull] = useState(false);
  const [resume, setResume] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [summaryOnly, setSummaryOnly] = useState(false);
  const [snapshotAt, setSnapshotAt] = useState("");

  // 实时进度与任务日志
  const [progress, setProgress] = useState<LiveProgressResponse | null>(null);
  const [tasks, setTasks] = useState<ScanTask[]>([]);
  const [activeTask, setActiveTask] = useState<ScanTask | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const logBoxRef = useRef<HTMLDivElement | null>(null);

  // 定时扫描配置
  const [schedMeta, setSchedMeta] = useState<ScheduleResponse | null>(null);
  const [schedEnabled, setSchedEnabled] = useState(false);
  const [schedTime, setSchedTime] = useState("02:00");
  const [schedDays, setSchedDays] = useState<number[]>([1, 2, 3, 4, 5, 6, 7]);
  const [schedFast, setSchedFast] = useState(true);
  const [schedResume, setSchedResume] = useState(true);
  const [schedForceFull, setSchedForceFull] = useState(false);
  const [schedScope, setSchedScope] = useState<"all" | "selected">("all");
  const [schedOnly, setSchedOnly] = useState<string[]>([]);
  const [savingSched, setSavingSched] = useState(false);
  const [schedSaved, setSchedSaved] = useState(false);

  // 环境自检
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [checkingDoctor, setCheckingDoctor] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyScheduleState = (res: ScheduleResponse) => {
    setSchedMeta(res);
    setSchedEnabled(res.schedule.enabled);
    setSchedTime(res.schedule.time || "02:00");
    setSchedDays(res.schedule.weekdays?.length ? res.schedule.weekdays : [1, 2, 3, 4, 5, 6, 7]);
    setSchedFast(Boolean(res.schedule.fast));
    setSchedResume(Boolean(res.schedule.resume));
    setSchedForceFull(Boolean(res.schedule.force_full));
    const only = res.schedule.only || [];
    setSchedOnly(only);
    setSchedScope(only.length > 0 ? "selected" : "all");
  };

  const refreshProgress = async () => {
    const prog = await api.scanProgress();
    setProgress(prog);
    setTasks(prog.tasks);
    if (prog.running_task) {
      setActiveTask((prev) =>
        !prev || prev.task_id === prog.running_task!.task_id || prev.status !== "running"
          ? prog.running_task
          : prev
      );
    }
    return prog;
  };

  const loadAll = async () => {
    setError(null);
    try {
      const [pRes, progRes, scRes] = await Promise.all([
        api.projects(),
        api.scanProgress(),
        api.schedule(),
      ]);
      setProjects(pRes.projects);
      if (pRes.scan_statuses && pRes.scan_statuses.length > 0) {
        setScanStatuses(pRes.scan_statuses);
      }
      setProgress(progRes);
      setTasks(progRes.tasks);
      applyScheduleState(scRes);
      if (progRes.running_task) {
        setActiveTask(progRes.running_task);
      } else if (!activeTask && progRes.tasks.length > 0) {
        const detail = await api.scanDetail(progRes.tasks[0].task_id);
        setActiveTask(detail);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目、进度与定时配置失败");
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  // 动态轮询：有运行中任务或运行中批次时 2s 轮询，空闲时 10s 轮询（可感知 CLI 触发的批次）
  const isBusy = Boolean(
    activeTask?.status === "running" ||
      progress?.running_task ||
      (progress?.active_runs && progress.active_runs.length > 0)
  );

  useEffect(() => {
    const intervalMs = isBusy ? 2000 : 10000;
    const timer = setInterval(() => {
      refreshProgress().catch(() => {});
      if (activeTask && activeTask.status === "running") {
        api.scanDetail(activeTask.task_id).then(setActiveTask).catch(() => {});
      }
    }, intervalMs);
    return () => clearInterval(timer);
  }, [isBusy, activeTask?.task_id, activeTask?.status]);

  useEffect(() => {
    if (activeTask?.status === "running" && logBoxRef.current) {
      logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
    }
  }, [activeTask?.logs?.length]);

  const toggleId = (pid: string) => {
    setSelectedIds((prev) => (prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid]));
  };

  const toggleSchedDay = (day: number) => {
    setSchedDays((prev) =>
      prev.includes(day) ? prev.filter((d) => d !== day) : [...prev, day].sort((a, b) => a - b)
    );
  };

  const toggleSchedProject = (pid: string) => {
    setSchedOnly((prev) => (prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid]));
  };

  const handleSaveSchedule = async () => {
    setSavingSched(true);
    setError(null);
    setSchedSaved(false);
    try {
      const res = await api.saveSchedule({
        enabled: schedEnabled,
        time: schedTime,
        weekdays: schedDays,
        fast: schedFast,
        resume: schedResume,
        force_full: schedForceFull,
        only: schedScope === "selected" ? schedOnly : [],
      });
      applyScheduleState(res);
      setSchedSaved(true);
      setTimeout(() => setSchedSaved(false), 2200);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存定时扫描配置失败");
    } finally {
      setSavingSched(false);
    }
  };

  const handleStart = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.startScan({
        only: selectedIds.length > 0 ? selectedIds : undefined,
        fast,
        force_full: summaryOnly ? false : forceFull,
        resume: summaryOnly ? false : resume,
        dry_run: summaryOnly ? false : dryRun,
        summary_only: summaryOnly,
        snapshot_at: snapshotAt.trim() || undefined,
      });
      const detail = await api.scanDetail(res.task_id);
      setActiveTask(detail);
      await refreshProgress();
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发扫描失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (taskId?: string) => {
    const id = taskId || activeTask?.task_id;
    if (!id) return;
    setCancelling(true);
    setError(null);
    try {
      await api.cancelScan(id);
      const detail = await api.scanDetail(id);
      setActiveTask(detail);
      await refreshProgress();
    } catch (err) {
      setError(err instanceof Error ? err.message : "中止任务失败");
    } finally {
      setCancelling(false);
    }
  };

  const handleDoctor = async () => {
    setCheckingDoctor(true);
    setError(null);
    try {
      const res = await api.doctor();
      setDoctor(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "环境自检失败");
    } finally {
      setCheckingDoctor(false);
    }
  };

  const renderProgressBlock = (run: RunProgressItem, isRunning: boolean) => {
    const fillClass = isRunning
      ? "progress-fill"
      : run.error_count > 0
      ? "progress-fill warn"
      : "progress-fill done";
    return (
      <div
        key={`${run.date}/${run.run_id}`}
        style={{
          padding: 12,
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--bg-2)",
          marginBottom: 8,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="chip">
              <span
                className={`chip-dot ${isRunning ? "pulse" : ""}`}
                style={{
                  background: isRunning
                    ? "var(--accent)"
                    : run.error_count > 0
                    ? "var(--warning)"
                    : "var(--good)",
                }}
              />
              {isRunning ? "扫描进行中" : run.status}
            </span>
            <b>
              {run.date} / {run.run_id}
            </b>
            <span className="muted" style={{ fontSize: 12 }}>
              {run.report_mode || "standard"} · 耗时 {formatElapsed(run.started_at, run.finished_at)}
            </span>
          </div>
          <div style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
            {run.completed_count} / {run.total_count} 个项目（{run.percent}%）
          </div>
        </div>

        <div className="progress-track">
          <div className={fillClass} style={{ width: `${run.percent}%` }} />
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {run.completed_projects.map((p) => {
            const ok = p.status === "ok";
            return (
              <span
                key={p.project_id}
                className="chip"
                style={{ cursor: ok && onOpenReport ? "pointer" : "default" }}
                title={ok ? "点击查看项目报告" : p.error || "扫描失败"}
                onClick={() => ok && onOpenReport && onOpenReport(run.date, run.run_id, p.project_id)}
              >
                <span className="chip-dot" style={{ background: ok ? "var(--good)" : "var(--critical)" }} />
                {p.project_name}
                {ok && p.total != null ? ` (${p.total.toFixed(1)})` : ok ? " (完成)" : " (失败)"}
              </span>
            );
          })}
          {run.pending_projects.map((pid) => (
            <span key={pid} className="chip" title="等待中或正在扫描">
              <span className="chip-dot pulse" style={{ background: "var(--accent)" }} />
              {pid} (进行中)
            </span>
          ))}
        </div>
      </div>
    );
  };

  const scannableProjects = projects.filter((p) => scanStatuses.includes((p.status || "").trim()));
  const runningTask = progress?.running_task;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {error && (
        <div className="card" style={{ borderColor: "var(--critical)", color: "var(--critical)" }}>
          {error}
        </div>
      )}

      {/* 1. 实时扫描进度面板（同时感知 Web、定时与 CLI 触发的批次） */}
      <div className="card">
        <div className="card-title">
          <span>
            实时扫描进度{" "}
            {isBusy && (
              <span className="sub" style={{ fontWeight: 400, marginLeft: 6 }}>
                （每 2 秒自动刷新）
              </span>
            )}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            {runningTask && (
              <button
                className="btn btn-danger"
                onClick={() => void handleCancel(runningTask.task_id)}
                disabled={cancelling}
              >
                <Square size={13} /> {cancelling ? "中止中…" : "中止当前任务"}
              </button>
            )}
            <button className="btn" onClick={() => void refreshProgress()}>
              <RefreshCw size={14} /> 刷新进度
            </button>
          </div>
        </div>

        {runningTask && (
          <div className="sub" style={{ marginBottom: 10 }}>
            运行中任务：<code>{runningTask.task_id}</code>（
            {runningTask.trigger === "schedule" ? "定时触发" : "手动触发"}）
            {runningTask.latest_log && (
              <span style={{ marginLeft: 10 }}>
                最新动态：<code>{runningTask.latest_log}</code>
              </span>
            )}
          </div>
        )}

        {progress?.active_runs && progress.active_runs.length > 0 ? (
          progress.active_runs.map((r) => renderProgressBlock(r, true))
        ) : runningTask ? (
          <div className="sub" style={{ padding: "8px 0" }}>
            扫描进程已启动，正在初始化批次记录…
          </div>
        ) : progress?.latest_run ? (
          <>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              当前无正在运行的批次，最近一次批次完成情况：
            </div>
            {renderProgressBlock(progress.latest_run, false)}
          </>
        ) : (
          <div className="muted">当前无正在运行或历史扫描批次，可在下方手动触发或配置定时扫描。</div>
        )}
      </div>

      {/* 2. 可视化定时扫描配置 */}
      <div className="card">
        <div className="card-title">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <CalendarClock size={16} /> 定时扫描配置
          </span>
          <span className="sub">
            {schedMeta?.server_time ? `服务器时间：${schedMeta.server_time}` : ""}
            {schedMeta?.next_run_at ? ` · 下次执行：${schedMeta.next_run_at}` : " · 未启用定时"}
          </span>
        </div>

        <div className="filters">
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
            <input
              type="checkbox"
              checked={schedEnabled}
              onChange={(e) => setSchedEnabled(e.target.checked)}
            />
            启用自动定时扫描
          </label>

          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            执行时间
            <input
              type="time"
              value={schedTime}
              onChange={(e) => setSchedTime(e.target.value)}
              style={{ width: 112 }}
            />
          </label>

          <div className="weekday-group">
            {WEEKDAY_LABELS.map(([num, label]) => (
              <button
                key={num}
                type="button"
                className={`weekday-btn ${schedDays.includes(num) ? "active" : ""}`}
                onClick={() => toggleSchedDay(num)}
              >
                {label}
              </button>
            ))}
            <button
              type="button"
              className="weekday-btn"
              onClick={() => setSchedDays([1, 2, 3, 4, 5, 6, 7])}
            >
              每天
            </button>
            <button
              type="button"
              className="weekday-btn"
              onClick={() => setSchedDays([1, 2, 3, 4, 5])}
            >
              工作日
            </button>
          </div>
        </div>

        <div className="filters">
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={schedFast}
              onChange={(e) => setSchedFast(e.target.checked)}
            />
            快速模式 (--fast)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={schedResume}
              onChange={(e) => {
                setSchedResume(e.target.checked);
                if (e.target.checked) setSchedForceFull(false);
              }}
            />
            跳过无变更 (--resume)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={schedForceFull}
              onChange={(e) => {
                setSchedForceFull(e.target.checked);
                if (e.target.checked) setSchedResume(false);
              }}
            />
            强制全量 (--force-full)
          </label>

          <select
            value={schedScope}
            onChange={(e) => setSchedScope(e.target.value as "all" | "selected")}
          >
            <option value="all">扫描全部在研项目</option>
            <option value="selected">仅扫描指定项目</option>
          </select>

          <button
            className="btn btn-primary"
            style={{ marginLeft: "auto" }}
            onClick={() => void handleSaveSchedule()}
            disabled={savingSched}
          >
            {schedSaved ? <Check size={14} /> : <Save size={14} />}{" "}
            {savingSched ? "保存中…" : schedSaved ? "已保存" : "保存定时配置"}
          </button>
        </div>

        {schedScope === "selected" && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 10 }}>
            {scannableProjects.map((p) => (
              <label key={p.project_id} className="chip" style={{ cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={schedOnly.includes(p.project_id)}
                  onChange={() => toggleSchedProject(p.project_id)}
                />
                {p.project_name} ({p.project_id})
              </label>
            ))}
          </div>
        )}

        <div className="muted" style={{ fontSize: 12 }}>
          配置持久化保存在 <code>output/state/schedule.json</code>，需保持 <code>web</code> 容器常驻运行。
          {schedMeta?.schedule.last_triggered_at && (
            <span>
              {" "}
              · 上次定时触发：{schedMeta.schedule.last_triggered_at.replace("T", " ")}
              {schedMeta.schedule.last_task_id ? `（任务 ${schedMeta.schedule.last_task_id}）` : ""}
            </span>
          )}
          {schedMeta?.schedule.last_error && (
            <span style={{ color: "var(--warning)" }}> · 提示：{schedMeta.schedule.last_error}</span>
          )}
        </div>
      </div>

      {/* 3. 项目清单与手动触发 + 环境自检 */}
      <div className="card">
        <div className="card-title">
          <span>项目清单与手动触发扫描</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => void handleDoctor()} disabled={checkingDoctor}>
              <Stethoscope size={14} /> {checkingDoctor ? "检查中…" : "环境自检"}
            </button>
            <button className="btn" onClick={() => void loadAll()}>
              <RefreshCw size={14} /> 刷新
            </button>
          </div>
        </div>

        {doctor && (
          <div
            style={{
              marginBottom: 14,
              padding: 12,
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--bg-2)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <b>
                环境自检：{doctor.verdict.toUpperCase()}（错误 {doctor.errors} / 警告 {doctor.warnings}）
              </b>
              <button className="btn" style={{ padding: "2px 8px", fontSize: 12 }} onClick={() => setDoctor(null)}>
                收起
              </button>
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {doctor.checks.map((c) => (
                <div key={c.name} style={{ display: "flex", gap: 10, alignItems: "baseline", fontSize: 13 }}>
                  <span className="chip" style={{ minWidth: 62, justifyContent: "center" }}>
                    <span className="chip-dot" style={{ background: DOCTOR_COLOR[c.status] }} />
                    {c.status.toUpperCase()}
                  </span>
                  <b style={{ minWidth: 130 }}>{c.name}</b>
                  <span>
                    {c.detail}
                    {c.fix && <span className="muted"> · 建议：{c.fix}</span>}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="filters">
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={fast} onChange={(e) => setFast(e.target.checked)} />
            快速模式 (--fast)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={forceFull}
              disabled={summaryOnly}
              onChange={(e) => {
                setForceFull(e.target.checked);
                if (e.target.checked) setResume(false);
              }}
            />
            强制全量 (--force-full)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={resume}
              disabled={summaryOnly}
              onChange={(e) => {
                setResume(e.target.checked);
                if (e.target.checked) setForceFull(false);
              }}
            />
            跳过无变更 (--resume)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={dryRun}
              disabled={summaryOnly}
              onChange={(e) => setDryRun(e.target.checked)}
            />
            仅演练 (--dry-run)
          </label>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={summaryOnly}
              onChange={(e) => {
                setSummaryOnly(e.target.checked);
                if (e.target.checked) {
                  setDryRun(false);
                  setForceFull(false);
                  setResume(false);
                }
              }}
            />
            仅生成汇总 (--summary-only)
          </label>
          <input
            placeholder="快照时间 ISO-8601（可选）"
            title="例如 2026-11-24T00:00:00+08:00"
            value={snapshotAt}
            onChange={(e) => setSnapshotAt(e.target.value)}
            style={{ width: 215 }}
          />
          <button
            className="btn btn-primary"
            style={{ marginLeft: "auto" }}
            onClick={() => void handleStart()}
            disabled={submitting}
          >
            <Play size={14} />{" "}
            {selectedIds.length > 0 ? `扫描已选 (${selectedIds.length})` : "扫描全部在研项目"}
          </button>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 36 }}>选</th>
                <th>项目 ID</th>
                <th>项目名称</th>
                <th>部门 / 负责人</th>
                <th>分支</th>
                <th>技术栈</th>
                <th>状态</th>
                <th>最近扫描</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((p) => {
                const scannable = scanStatuses.includes((p.status || "").trim());
                return (
                  <tr key={p.project_id} style={{ opacity: scannable ? 1 : 0.6 }}>
                    <td>
                      <input
                        type="checkbox"
                        disabled={!scannable}
                        title={
                          scannable
                            ? "选择参与扫描"
                            : `状态为「${p.status || "未知"}」，不在允许扫描状态（${scanStatuses.join("/")}）中`
                        }
                        checked={scannable && selectedIds.includes(p.project_id)}
                        onChange={() => scannable && toggleId(p.project_id)}
                      />
                    </td>
                    <td>
                      <code>{p.project_id}</code>
                    </td>
                    <td>{p.project_name}</td>
                    <td>
                      {p.department || "—"} / {p.owner || "—"}
                    </td>
                    <td>{p.branch || "main"}</td>
                    <td style={{ whiteSpace: "normal", maxWidth: 260 }}>{p.tech_stack || "—"}</td>
                    <td>
                      <span className="chip">{p.status}</span>
                    </td>
                    <td className="sub">
                      {p.state?.last_scan_date
                        ? `${p.state.last_scan_date} (${(p.state.last_commit || "").slice(0, 8)})`
                        : "未扫描"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 4. 任务记录与实时日志 */}
      <div className="grid-sidebar">
        <div className="card">
          <div className="card-title">触发任务记录 ({tasks.length})</div>
          {tasks.length === 0 ? (
            <div className="muted">尚未由页面或定时任务触发过扫描</div>
          ) : (
            tasks.map((t) => (
              <div
                key={t.task_id}
                className={`list-item ${activeTask?.task_id === t.task_id ? "active" : ""}`}
                onClick={() => api.scanDetail(t.task_id).then(setActiveTask).catch(() => {})}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <b>{t.task_id}</b>
                  <span className="chip">{t.status}</span>
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  {t.trigger === "schedule" ? "定时触发" : "手动触发"} ·{" "}
                  {t.started_at.slice(0, 19).replace("T", " ")}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <div className="card-title">
            <span>
              实时日志{" "}
              {activeTask
                ? `· ${activeTask.task_id} (${activeTask.trigger === "schedule" ? "定时" : "手动"} / ${activeTask.status})`
                : ""}
            </span>
            {activeTask?.status === "running" && (
              <button className="btn btn-danger" onClick={() => void handleCancel()} disabled={cancelling}>
                <Square size={13} /> {cancelling ? "中止中…" : "中止扫描"}
              </button>
            )}
          </div>
          {activeTask ? (
            <>
              <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                $ {activeTask.command}
              </div>
              <div className="log-box" ref={logBoxRef}>
                {activeTask.logs && activeTask.logs.length > 0
                  ? activeTask.logs.join("\n")
                  : "等待输出…"}
              </div>
            </>
          ) : (
            <div className="muted">点击左侧任务查看运行日志，或点击上方按钮触发扫描。</div>
          )}
        </div>
      </div>
    </div>
  );
}
