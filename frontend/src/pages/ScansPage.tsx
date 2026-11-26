import { useEffect, useState } from "react";
import { Play, RefreshCw, Square, Stethoscope } from "lucide-react";
import { api } from "../api";
import type { CatalogProject, DoctorReport, ScanTask } from "../types";

const DOCTOR_COLOR: Record<string, string> = {
  ok: "var(--good)",
  warn: "var(--warning)",
  error: "var(--critical)",
};

export function ScansPage() {
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [scanStatuses, setScanStatuses] = useState<string[]>(["在研"]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [fast, setFast] = useState(false);
  const [forceFull, setForceFull] = useState(false);
  const [resume, setResume] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [summaryOnly, setSummaryOnly] = useState(false);
  const [snapshotAt, setSnapshotAt] = useState("");
  const [tasks, setTasks] = useState<ScanTask[]>([]);
  const [activeTask, setActiveTask] = useState<ScanTask | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [checkingDoctor, setCheckingDoctor] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAll = async () => {
    setError(null);
    try {
      const [pRes, sRes] = await Promise.all([api.projects(), api.scans()]);
      setProjects(pRes.projects);
      if (pRes.scan_statuses && pRes.scan_statuses.length > 0) {
        setScanStatuses(pRes.scan_statuses);
      }
      setTasks(sRes.tasks);
      if (!activeTask && sRes.tasks.length > 0) {
        const detail = await api.scanDetail(sRes.tasks[0].task_id);
        setActiveTask(detail);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目与任务列表失败");
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  useEffect(() => {
    if (!activeTask || activeTask.status !== "running") return;
    const timer = setInterval(() => {
      api
        .scanDetail(activeTask.task_id)
        .then((d) => {
          setActiveTask(d);
          if (d.status !== "running") {
            api.scans().then((r) => setTasks(r.tasks)).catch(() => {});
          }
        })
        .catch((err) => setError(err instanceof Error ? err.message : "轮询任务状态失败"));
    }, 1500);
    return () => clearInterval(timer);
  }, [activeTask?.task_id, activeTask?.status]);

  const toggleId = (pid: string) => {
    setSelectedIds((prev) => (prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid]));
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
      const sRes = await api.scans();
      setTasks(sRes.tasks);
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发扫描失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async () => {
    if (!activeTask || activeTask.status !== "running") return;
    setCancelling(true);
    setError(null);
    try {
      await api.cancelScan(activeTask.task_id);
      const detail = await api.scanDetail(activeTask.task_id);
      setActiveTask(detail);
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

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {error && (
        <div className="card" style={{ borderColor: "var(--critical)", color: "var(--critical)" }}>
          {error}
        </div>
      )}

      <div className="card">
        <div className="card-title">
          <span>项目清单与扫描触发</span>
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

      <div className="grid-sidebar">
        <div className="card">
          <div className="card-title">触发任务记录 ({tasks.length})</div>
          {tasks.length === 0 ? (
            <div className="muted">尚未从页面触发过扫描任务</div>
          ) : (
            tasks.map((t) => (
              <div
                key={t.task_id}
                className={`list-item ${activeTask?.task_id === t.task_id ? "active" : ""}`}
                onClick={() => api.scanDetail(t.task_id).then(setActiveTask).catch(() => {})}
              >
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <b>{t.task_id}</b>
                  <span className="chip">{t.status}</span>
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  {t.started_at.slice(0, 19).replace("T", " ")}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <div className="card-title">
            <span>实时日志 {activeTask ? `· ${activeTask.task_id} (${activeTask.status})` : ""}</span>
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
              <div className="log-box">
                {(activeTask.logs && activeTask.logs.length > 0
                  ? activeTask.logs.join("\n")
                  : "等待输出…")}
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
