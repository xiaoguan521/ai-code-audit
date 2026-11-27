import { useEffect, useMemo, useState } from "react";
import { CalendarClock, Check, Play, RefreshCw, Save, Stethoscope } from "lucide-react";
import { api } from "../api";
import type { CatalogProject, DoctorReport, ScheduleResponse } from "../types";

interface Props {
  onScanStarted?: (taskId: string) => void;
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

export function ConfigPage({ onScanStarted }: Props) {
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [scanStatuses, setScanStatuses] = useState<string[]>(["在研"]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deptFilter, setDeptFilter] = useState("");
  const [query, setQuery] = useState("");
  const [fast, setFast] = useState(false);
  const [forceFull, setForceFull] = useState(false);
  const [resume, setResume] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [summaryOnly, setSummaryOnly] = useState(false);
  const [snapshotAt, setSnapshotAt] = useState("");
  const [submitting, setSubmitting] = useState(false);

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

  const loadAll = async () => {
    setError(null);
    try {
      const [pRes, scRes] = await Promise.all([api.projects(), api.schedule()]);
      setProjects(pRes.projects);
      if (pRes.scan_statuses && pRes.scan_statuses.length > 0) {
        setScanStatuses(pRes.scan_statuses);
      }
      applyScheduleState(scRes);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目清单与定时配置失败");
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const departments = useMemo(() => {
    const s = new Set<string>();
    for (const p of projects) s.add((p.department || "").trim() || "未分组");
    return [...s].sort((a, b) => a.localeCompare(b, "zh-CN"));
  }, [projects]);

  const scannableProjects = useMemo(
    () => projects.filter((p) => scanStatuses.includes((p.status || "").trim())),
    [projects, scanStatuses]
  );

  const filteredProjects = useMemo(() => {
    const q = query.trim().toLowerCase();
    return projects.filter((p) => {
      const dept = (p.department || "").trim() || "未分组";
      if (deptFilter && dept !== deptFilter) return false;
      if (
        q &&
        !`${p.project_id} ${p.project_name} ${dept} ${p.owner || ""} ${p.branch || ""} ${p.tech_stack || ""}`
          .toLowerCase()
          .includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [projects, deptFilter, query]);

  const toggleId = (pid: string) => {
    setSelectedIds((prev) => (prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid]));
  };

  const toggleSelectFiltered = () => {
    const eligible = filteredProjects
      .filter((p) => scanStatuses.includes((p.status || "").trim()))
      .map((p) => p.project_id);
    const allSelected = eligible.length > 0 && eligible.every((id) => selectedIds.includes(id));
    if (allSelected) {
      setSelectedIds((prev) => prev.filter((id) => !eligible.includes(id)));
    } else {
      setSelectedIds((prev) => [...new Set([...prev, ...eligible])]);
    }
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
      if (onScanStarted) onScanStarted(res.task_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发扫描失败");
    } finally {
      setSubmitting(false);
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

      {/* 1. 可视化定时扫描配置 */}
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
          <div style={{ maxHeight: 160, overflow: "auto", display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
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

      {/* 2. 项目清单与手动触发 + 环境自检 */}
      <div className="card">
        <div className="card-title">
          <span>项目清单与手动触发扫描（共 {projects.length} 项）</span>
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

        {/* 清单筛选条 */}
        <div className="filters">
          <select value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
            <option value="">全部部门 ({departments.length})</option>
            {departments.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <input
            placeholder="搜索项目 ID / 名称 / 负责人 / 分支 / 技术栈"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ minWidth: 260 }}
          />
          <button type="button" className="btn" onClick={toggleSelectFiltered}>
            全选/取消当前筛选
          </button>
          {selectedIds.length > 0 && (
            <button type="button" className="btn" onClick={() => setSelectedIds([])}>
              清空已选 ({selectedIds.length})
            </button>
          )}
          <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
            显示 {filteredProjects.length} / {projects.length} 项
          </span>
        </div>

        <div className="table-wrap" style={{ maxHeight: 540, overflowY: "auto" }}>
          <table>
            <thead>
              <tr>
                <th style={{ width: 36 }}>选</th>
                <th>项目名称 / ID</th>
                <th>部门 / 负责人</th>
                <th>分支</th>
                <th>技术栈</th>
                <th>状态</th>
                <th>最近扫描</th>
              </tr>
            </thead>
            <tbody>
              {filteredProjects.map((p) => {
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
                    <td style={{ whiteSpace: "normal", minWidth: 220, maxWidth: 340 }}>
                      <div style={{ fontWeight: 600 }}>{p.project_name}</div>
                      <div className="muted" style={{ fontSize: 11, wordBreak: "break-all" }}>
                        <code>{p.project_id}</code>
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 120 }}>
                      {p.department || "—"} / {p.owner || "—"}
                    </td>
                    <td style={{ whiteSpace: "normal", maxWidth: 160, wordBreak: "break-all" }}>
                      {p.branch || "main"}
                    </td>
                    <td style={{ whiteSpace: "normal", maxWidth: 220 }}>{p.tech_stack || "—"}</td>
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
              {filteredProjects.length === 0 && (
                <tr>
                  <td colSpan={7} className="muted" style={{ textAlign: "center", padding: 24 }}>
                    没有符合筛选条件的项目
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
