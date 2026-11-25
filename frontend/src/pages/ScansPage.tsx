import { useEffect, useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { api } from "../api";
import type { CatalogProject, ScanTask } from "../types";

export function ScansPage() {
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [scanStatuses, setScanStatuses] = useState<string[]>(["在研"]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [fast, setFast] = useState(false);
  const [forceFull, setForceFull] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [summaryOnly, setSummaryOnly] = useState(false);
  const [tasks, setTasks] = useState<ScanTask[]>([]);
  const [activeTask, setActiveTask] = useState<ScanTask | null>(null);
  const [submitting, setSubmitting] = useState(false);
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
        force_full: forceFull,
        dry_run: dryRun,
        summary_only: summaryOnly,
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
          <button className="btn" onClick={() => void loadAll()}>
            <RefreshCw size={14} /> 刷新
          </button>
        </div>

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
              onChange={(e) => setForceFull(e.target.checked)}
            />
            强制全量 (--force-full)
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
                if (e.target.checked) setDryRun(false);
              }}
            />
            仅生成汇总 (--summary-only)
          </label>
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
                        title={scannable ? "选择参与扫描" : `状态为「${p.status || "未知"}」，不在允许扫描状态（${scanStatuses.join("/")}）中`}
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
                onClick={() => api.scanDetail(t.task_id).then(setActiveTask)}
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
