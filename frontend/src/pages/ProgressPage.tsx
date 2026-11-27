import { useEffect, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Download, RefreshCw, RotateCcw, Square } from "lucide-react";
import { api } from "../api";
import { DraftModal } from "../components/DraftModal";
import type {
  AuthUser,
  LiveProgressResponse,
  RunProgressItem,
  RunProgressProjectItem,
  ScanTask,
} from "../types";
import { downloadBlob, sanitizeFilename } from "../utils/markdown";

interface Props {
  currentUser?: AuthUser | null;
  initialTaskId?: string | null;
  onOpenReport?: (date: string, runId: string, target: string) => void;
  onScanStarted?: (taskId: string) => void;
}

const STATE_ORDER: Record<RunProgressProjectItem["state"], number> = {
  scanning: 0,
  error: 1,
  ok: 2,
  queued: 3,
};

const STATE_LABEL: Record<RunProgressProjectItem["state"], string> = {
  scanning: "扫描中",
  ok: "已完成",
  error: "失败",
  queued: "排队中",
};

const STATE_COLOR: Record<RunProgressProjectItem["state"], string> = {
  scanning: "var(--accent)",
  ok: "var(--good)",
  error: "var(--critical)",
  queued: "var(--neutral)",
};

function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.slice(0, 19).replace("T", " ");
}

function classifyReason(it: RunProgressProjectItem): string {
  if (it.state !== "error") return STATE_LABEL[it.state];
  const raw = it.error || "";
  const s = raw.toLowerCase();
  if (raw.includes("批次总时限到") || raw.includes("批次已超时")) return "批次总时限到";
  if (
    s.includes("clone") ||
    s.includes("branch") ||
    s.includes("git") ||
    s.includes("repository") ||
    s.includes("not found") ||
    raw.includes("权限")
  ) {
    return "Git 克隆 / 分支";
  }
  if (raw.includes("脱敏") || raw.includes("密钥") || (it.secret_patterns?.length ?? 0) > 0) {
    return "报告脱敏拦截";
  }
  if (raw.includes("校验") || raw.includes("拦截")) return "报告校验拦截";
  if (s.includes("timeout") || raw.includes("超时") || raw.includes("超过")) return "单项目执行超时";
  if (
    s.includes("api") ||
    s.includes("model") ||
    s.includes("http") ||
    s.includes("connection") ||
    s.includes("429") ||
    s.includes("502") ||
    s.includes("503") ||
    s.includes("harness")
  ) {
    return "模型 / Harness 错误";
  }
  return "其他失败";
}

function csvCell(v: string | number | null | undefined): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function normalizeItems(run: RunProgressItem): RunProgressProjectItem[] {
  if (run.items && run.items.length > 0) return run.items;
  const out: RunProgressProjectItem[] = [];
  for (const p of run.completed_projects || []) {
    out.push({
      project_id: p.project_id,
      project_name: p.project_name || p.project_id,
      department: "未分组",
      owner: "",
      state: p.status === "ok" ? "ok" : "error",
      status: p.status,
      total: p.total,
      error: p.error,
      duration_seconds: null,
      elapsed_seconds: null,
      eta_seconds: null,
      timeout_remaining_seconds: null,
    });
  }
  for (const pid of run.pending_projects || []) {
    out.push({
      project_id: pid,
      project_name: pid,
      department: "未分组",
      owner: "",
      state: "queued",
      status: "queued",
      total: null,
      error: null,
      duration_seconds: null,
      elapsed_seconds: null,
      eta_seconds: null,
      timeout_remaining_seconds: null,
    });
  }
  return out;
}

export function ProgressPage({ currentUser, initialTaskId, onOpenReport, onScanStarted }: Props) {
  const isAdmin = !currentUser || currentUser.role === "superadmin" || currentUser.role === "admin";
  const [progress, setProgress] = useState<LiveProgressResponse | null>(null);
  const [progStateFilter, setProgStateFilter] = useState<"" | RunProgressProjectItem["state"]>("");
  const [progReasonFilter, setProgReasonFilter] = useState("");
  const [exportScope, setExportScope] = useState("filtered");
  const [progQuery, setProgQuery] = useState("");
  const [collapsedDepts, setCollapsedDepts] = useState<Record<string, boolean>>({});
  const [expandAllOverride, setExpandAllOverride] = useState<boolean | null>(null);

  const [tasks, setTasks] = useState<ScanTask[]>([]);
  const [activeTask, setActiveTask] = useState<ScanTask | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [draftModal, setDraftModal] = useState<{ title: string; markdown: string; filenameBase: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const logBoxRef = useRef<HTMLDivElement | null>(null);

  const refreshProgress = async () => {
    setError(null);
    try {
      const prog = await api.scanProgress();
      setProgress(prog);
      setTasks(prog.tasks);
      if (prog.running_task) {
        setActiveTask((prev) =>
          !prev || prev.task_id === prog.running_task!.task_id || prev.status !== "running"
            ? prog.running_task
            : prev
        );
      } else if (!activeTask && prog.tasks.length > 0) {
        const targetId = initialTaskId || prog.tasks[0].task_id;
        const detail = await api.scanDetail(targetId).catch(() => null);
        if (detail) setActiveTask(detail);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载实时进度失败");
    }
  };

  useEffect(() => {
    void refreshProgress();
  }, []);

  useEffect(() => {
    if (!initialTaskId) return;
    api.scanDetail(initialTaskId).then(setActiveTask).catch(() => {});
  }, [initialTaskId]);

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

  const handleMarkAborted = async (date: string, runId: string) => {
    setActionBusy(true);
    setError(null);
    try {
      await api.markRunAborted(date, runId);
      await refreshProgress();
    } catch (err) {
      setError(err instanceof Error ? err.message : "标记中断失败");
    } finally {
      setActionBusy(false);
    }
  };

  const handleRetryFailed = async (pids: string[], reportMode: string) => {
    if (pids.length === 0) return;
    setActionBusy(true);
    setError(null);
    try {
      const res = await api.startScan({
        only: pids,
        fast: reportMode === "fast",
        dry_run: false,
      });
      if (onScanStarted) onScanStarted(res.task_id);
      await refreshProgress();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重跑失败");
    } finally {
      setActionBusy(false);
    }
  };

  const handleViewRejected = async (date: string, runId: string, pid: string, name: string) => {
    setError(null);
    try {
      const res = await api.rejectedDraft(date, runId, pid);
      setDraftModal({
        title: `${name}（${pid}）被拦截的原始输出`,
        markdown: res.markdown,
        filenameBase: `${date}_${pid}_draft`,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载拦截草稿失败");
    }
  };

  const handleDownloadSummaryWord = async (date: string, runId: string) => {
    setActionBusy(true);
    setError(null);
    try {
      await api.downloadReportDocx(date, runId, "_summary", true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出 Word 汇总失败");
    } finally {
      setActionBusy(false);
    }
  };

  const renderProgressBlock = (run: RunProgressItem, isRunning: boolean) => {
    const allItems = normalizeItems(run);
    const errorItems = allItems.filter((it) => it.state === "error");
    const reasonCountsMap = new Map<string, number>();
    for (const it of errorItems) {
      const cat = classifyReason(it);
      reasonCountsMap.set(cat, (reasonCountsMap.get(cat) || 0) + 1);
    }
    const reasonClusters = [...reasonCountsMap.entries()].sort((a, b) => b[1] - a[1]);

    const q = progQuery.trim().toLowerCase();
    const filtered = allItems.filter(
      (it) =>
        (!progStateFilter || it.state === progStateFilter) &&
        (!progReasonFilter || (it.state === "error" && classifyReason(it) === progReasonFilter)) &&
        (!q ||
          `${it.department} ${it.project_name} ${it.project_id} ${it.owner} ${it.error || ""}`
            .toLowerCase()
            .includes(q))
    );

    const handleExportAppsCsv = (scopeKey: string) => {
      let targetItems: RunProgressProjectItem[] = [];
      let label = "全部";
      if (scopeKey === "filtered") {
        targetItems = filtered;
        label = progReasonFilter || (progStateFilter ? STATE_LABEL[progStateFilter] : "当前筛选");
      } else if (scopeKey === "all") {
        targetItems = allItems;
        label = "全部应用";
      } else if (scopeKey === "all_error") {
        targetItems = errorItems;
        label = "全部失败";
      } else if (scopeKey === "state:ok") {
        targetItems = allItems.filter((x) => x.state === "ok");
        label = "已完成";
      } else if (scopeKey === "state:queued") {
        targetItems = allItems.filter((x) => x.state === "queued");
        label = "排队中";
      } else if (scopeKey === "state:scanning") {
        targetItems = allItems.filter((x) => x.state === "scanning");
        label = "正在扫描";
      } else if (scopeKey.startsWith("reason:")) {
        const reasonName = scopeKey.slice("reason:".length);
        targetItems = errorItems.filter((x) => classifyReason(x) === reasonName);
        label = reasonName;
      }
      const sortedExport = [...targetItems].sort((a, b) => {
        const c1 = (a.department || "未分组").localeCompare(b.department || "未分组", "zh-CN");
        if (c1 !== 0) return c1;
        const c2 = classifyReason(a).localeCompare(classifyReason(b), "zh-CN");
        if (c2 !== 0) return c2;
        return a.project_name.localeCompare(b.project_name, "zh-CN");
      });
      const header = ["部门", "项目名称", "项目ID", "负责人", "状态", "原因分类", "总评分", "耗时", "详细说明"];
      const lines = [
        header.map(csvCell).join(","),
        ...sortedExport.map((it) => {
          const detailParts: string[] = [];
          if (it.error) detailParts.push(it.error);
          if (it.secret_patterns?.length) detailParts.push(`密钥模式: ${it.secret_patterns.join("; ")}`);
          if (it.validation_problems?.length) detailParts.push(`校验提示: ${it.validation_problems.join("; ")}`);
          return [
            it.department || "未分组",
            it.project_name,
            it.project_id,
            it.owner || "",
            STATE_LABEL[it.state],
            classifyReason(it),
            it.total != null ? it.total.toFixed(1) : "",
            it.duration_seconds != null ? formatDuration(it.duration_seconds) : "",
            detailParts.join(" | "),
          ]
            .map(csvCell)
            .join(",");
        }),
      ];
      const fname = `${sanitizeFilename(`${run.date}_${run.run_id}_${label}_${sortedExport.length}项`)}.csv`;
      downloadBlob(fname, "\ufeff" + lines.join("\n"), "text/csv;charset=utf-8");
    };

    const deptMap = new Map<string, RunProgressProjectItem[]>();
    for (const it of filtered) {
      const dept = it.department || "未分组";
      const list = deptMap.get(dept);
      if (list) list.push(it);
      else deptMap.set(dept, [it]);
    }

    const groups = [...deptMap.entries()]
      .map(([dept, items]) => {
        const sorted = [...items].sort((a, b) => {
          const diff = STATE_ORDER[a.state] - STATE_ORDER[b.state];
          return diff !== 0 ? diff : a.project_name.localeCompare(b.project_name, "zh-CN");
        });
        const scanning = sorted.filter((x) => x.state === "scanning").length;
        const ok = sorted.filter((x) => x.state === "ok").length;
        const err = sorted.filter((x) => x.state === "error").length;
        const queued = sorted.filter((x) => x.state === "queued").length;
        const done = ok + err;
        const pct = sorted.length > 0 ? Math.round((done * 100) / sorted.length) : 0;
        return { dept, items: sorted, scanning, ok, err, queued, done, pct };
      })
      .sort((a, b) => {
        if (b.scanning !== a.scanning) return b.scanning - a.scanning;
        if (b.err !== a.err) return b.err - a.err;
        return b.items.length - a.items.length;
      });

    const fillClass = isRunning
      ? "progress-fill"
      : run.error_count > 0
      ? "progress-fill warn"
      : "progress-fill done";

    const scanningCount = run.scanning_count ?? allItems.filter((x) => x.state === "scanning").length;
    const queuedCount = run.queued_count ?? allItems.filter((x) => x.state === "queued").length;
    const maybeStaleOrCli = isRunning && !progress?.running_task;

    const isDeptExpanded = (dept: string, scanning: number) => {
      if (expandAllOverride !== null) return expandAllOverride;
      const key = `${run.run_id}:${dept}`;
      if (key in collapsedDepts) return !collapsedDepts[key];
      if (progQuery.trim() || progStateFilter || progReasonFilter) return true;
      return scanning > 0 || groups.length <= 4;
    };

    const toggleDept = (dept: string, currentExpanded: boolean) => {
      setExpandAllOverride(null);
      setCollapsedDepts((prev) => ({ ...prev, [`${run.run_id}:${dept}`]: currentExpanded }));
    };

    return (
      <div
        key={`${run.date}/${run.run_id}`}
        style={{
          padding: 14,
          borderRadius: 10,
          border: "1px solid var(--border)",
          background: "var(--bg-2)",
          marginBottom: 10,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
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
              {isRunning ? (scanningCount > 0 ? "扫描进行中" : "运行中 / 待确认") : run.status}
            </span>
            <b>
              {run.date} / {run.run_id}
            </b>
            <span className="chip">{run.report_mode || "standard"}</span>
            {maybeStaleOrCli && (
              <span className="muted" style={{ fontSize: 12 }}>
                {scanningCount > 0
                  ? "（由命令行或外部 Runner 进程触发，终端日志不在本 Web 容器内）"
                  : "（未检测到活跃扫描锁：可能由外部 CLI 启动、正在初始化，或上次进程异常中断未落盘结束状态）"}
              </span>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {isAdmin && run.ok_count > 0 && (
              <button
                className="btn"
                style={{ padding: "4px 10px", fontSize: 12 }}
                disabled={actionBusy}
                title="即时按当前已完成项目生成并下载 Word 汇总报告（不影响正在运行的扫描）"
                onClick={() => void handleDownloadSummaryWord(run.date, run.run_id)}
              >
                <Download size={13} /> 导出 Word 汇总 (.docx)
              </button>
            )}
            {isAdmin && maybeStaleOrCli && scanningCount === 0 && (
              <button
                className="btn btn-danger"
                style={{ padding: "4px 10px", fontSize: 12 }}
                disabled={actionBusy}
                onClick={() => void handleMarkAborted(run.date, run.run_id)}
              >
                <AlertTriangle size={13} /> 标记为已中断
              </button>
            )}
            {run.error_count > 0 && !isRunning && (
              <button
                className="btn"
                style={{ padding: "4px 10px", fontSize: 12 }}
                disabled={actionBusy}
                onClick={() =>
                  void handleRetryFailed(
                    allItems.filter((x) => x.state === "error").map((x) => x.project_id),
                    run.report_mode
                  )
                }
              >
                <RotateCcw size={13} /> 重跑失败项 ({run.error_count})
              </button>
            )}
            <div style={{ fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>
              {run.completed_count} / {run.total_count} 个项目（{run.percent}%）
            </div>
          </div>
        </div>

        <div className="progress-meta-grid">
          <div className="progress-meta-box">
            <div className="progress-meta-label">公共扫描开始时间</div>
            <div className="progress-meta-value">{formatDateTime(run.started_at)}</div>
          </div>
          {run.snapshot_at && (
            <div className="progress-meta-box">
              <div className="progress-meta-label">Git 快照基准时间</div>
              <div className="progress-meta-value">{formatDateTime(run.snapshot_at)}</div>
            </div>
          )}
          <div className="progress-meta-box">
            <div className="progress-meta-label">已耗时</div>
            <div className="progress-meta-value">{formatDuration(run.elapsed_seconds)}</div>
          </div>
          <div className="progress-meta-box">
            <div className="progress-meta-label">
              整体预计剩余{run.parallelism ? `（并发 ${run.parallelism}）` : ""}
            </div>
            <div
              className="progress-meta-value"
              style={{ color: isRunning ? "var(--accent)" : undefined }}
              title={
                run.avg_project_seconds
                  ? `按已完成均耗约 ${formatDuration(run.avg_project_seconds)}/项目估算`
                  : undefined
              }
            >
              {isRunning
                ? run.estimated_remaining_seconds != null
                  ? `约 ${formatDuration(run.estimated_remaining_seconds)}`
                  : "估算中…"
                : "已结束"}
            </div>
          </div>
          <div className="progress-meta-box">
            <div className="progress-meta-label">单项目均耗</div>
            <div className="progress-meta-value">{formatDuration(run.avg_project_seconds)}</div>
          </div>
        </div>

        <div className="progress-track">
          <div className={fillClass} style={{ width: `${run.percent}%` }} />
        </div>

        {reasonClusters.length > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              flexWrap: "wrap",
              gap: 6,
              marginBottom: 10,
              padding: "8px 10px",
              borderRadius: 8,
              background: "var(--bg-1)",
              border: "1px solid var(--border)",
              fontSize: 12,
            }}
          >
            <span style={{ fontWeight: 600, color: "var(--critical)" }}>按失败原因筛选 / 导出：</span>
            {reasonClusters.map(([cat, cnt]) => (
              <button
                key={cat}
                type="button"
                className={`weekday-btn ${progReasonFilter === cat ? "active" : ""}`}
                onClick={() => {
                  const next = progReasonFilter === cat ? "" : cat;
                  setProgReasonFilter(next);
                  if (next) {
                    setProgStateFilter("error");
                    setExportScope(`reason:${next}`);
                  } else {
                    setExportScope("filtered");
                  }
                }}
              >
                {cat} ({cnt})
              </button>
            ))}
            {progReasonFilter && (
              <>
                <button
                  type="button"
                  className="weekday-btn"
                  onClick={() => {
                    setProgReasonFilter("");
                    setProgStateFilter("");
                    setExportScope("filtered");
                  }}
                >
                  清除原因筛选
                </button>
                {!isRunning && filtered.length > 0 && (
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "3px 9px", fontSize: 12 }}
                    disabled={actionBusy}
                    onClick={() =>
                      void handleRetryFailed(
                        filtered.map((x) => x.project_id),
                        run.report_mode
                      )
                    }
                  >
                    <RotateCcw size={12} /> 重跑该类失败 ({filtered.length})
                  </button>
                )}
              </>
            )}
          </div>
        )}

        <div className="filters" style={{ marginBottom: 10 }}>
          <button
            type="button"
            className={`weekday-btn ${progStateFilter === "" && !progReasonFilter ? "active" : ""}`}
            onClick={() => {
              setProgStateFilter("");
              setProgReasonFilter("");
            }}
          >
            全部 ({allItems.length})
          </button>
          <button
            type="button"
            className={`weekday-btn ${progStateFilter === "scanning" ? "active" : ""}`}
            onClick={() => {
              setProgStateFilter("scanning");
              setProgReasonFilter("");
            }}
          >
            正在扫描 ({scanningCount})
          </button>
          <button
            type="button"
            className={`weekday-btn ${progStateFilter === "ok" ? "active" : ""}`}
            onClick={() => {
              setProgStateFilter("ok");
              setProgReasonFilter("");
            }}
          >
            已完成 ({run.ok_count})
          </button>
          <button
            type="button"
            className={`weekday-btn ${progStateFilter === "error" && !progReasonFilter ? "active" : ""}`}
            onClick={() => {
              setProgStateFilter("error");
              setProgReasonFilter("");
            }}
          >
            失败 ({run.error_count})
          </button>
          <button
            type="button"
            className={`weekday-btn ${progStateFilter === "queued" ? "active" : ""}`}
            onClick={() => {
              setProgStateFilter("queued");
              setProgReasonFilter("");
            }}
          >
            排队中 ({queuedCount})
          </button>
          <input
            placeholder="筛选部门 / 项目名 / 负责人 / 原因"
            value={progQuery}
            onChange={(e) => setProgQuery(e.target.value)}
            style={{ minWidth: 200 }}
          />
          <div style={{ marginLeft: "auto", display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <select
              value={exportScope}
              onChange={(e) => setExportScope(e.target.value)}
              style={{ padding: "4px 8px", fontSize: 12 }}
              title="选择要导出的应用范围或失败原因"
            >
              <option value="filtered">导出当前筛选 ({filtered.length})</option>
              {reasonClusters.map(([cat, cnt]) => (
                <option key={cat} value={`reason:${cat}`}>
                  按原因：{cat} ({cnt})
                </option>
              ))}
              {errorItems.length > 0 && <option value="all_error">全部失败 ({errorItems.length})</option>}
              {run.ok_count > 0 && <option value="state:ok">全部已完成 ({run.ok_count})</option>}
              {queuedCount > 0 && <option value="state:queued">全部排队中 ({queuedCount})</option>}
              {scanningCount > 0 && <option value="state:scanning">正在扫描 ({scanningCount})</option>}
              <option value="all">全部应用 ({allItems.length})</option>
            </select>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 9px", fontSize: 12 }}
              onClick={() => handleExportAppsCsv(exportScope)}
            >
              <Download size={13} /> 导出应用 CSV
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 9px", fontSize: 12 }}
              onClick={() => setExpandAllOverride(true)}
            >
              展开全部部门 ({groups.length})
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 9px", fontSize: 12 }}
              onClick={() => setExpandAllOverride(false)}
            >
              收起全部
            </button>
          </div>
        </div>

        {groups.length === 0 ? (
          <div className="muted" style={{ padding: "12px 4px" }}>
            没有符合筛选条件的项目
          </div>
        ) : (
          groups.map((g) => {
            const expanded = isDeptExpanded(g.dept, g.scanning);
            return (
              <div className="dept-group" key={g.dept}>
                <div className="dept-header" onClick={() => toggleDept(g.dept, expanded)}>
                  <div className="dept-title">
                    {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                    <span>{g.dept}</span>
                    <span className="muted" style={{ fontWeight: 400 }}>
                      ({g.items.length} 项)
                    </span>
                    {g.scanning > 0 && (
                      <span className="chip" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>
                        <span className="chip-dot pulse" style={{ background: "var(--accent)" }} />
                        扫描中 {g.scanning}
                      </span>
                    )}
                  </div>
                  <div className="dept-stats">
                    <span>
                      完成 {g.done}/{g.items.length}
                    </span>
                    <span className="dept-mini-bar">
                      <span style={{ width: `${g.pct}%` }} />
                    </span>
                    <span style={{ color: "var(--good)" }}>成功 {g.ok}</span>
                    {g.err > 0 && <span style={{ color: "var(--critical)" }}>失败 {g.err}</span>}
                    {g.queued > 0 && <span>排队 {g.queued}</span>}
                  </div>
                </div>

                {expanded && (
                  <div className="dept-table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>项目名称 / ID</th>
                          <th>负责人</th>
                          <th>状态</th>
                          <th>评分 / 结果说明</th>
                          <th>耗时 / 预计剩余</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {g.items.map((it) => (
                          <tr key={it.project_id}>
                            <td>
                              <div style={{ fontWeight: 600 }}>{it.project_name}</div>
                              <div className="muted" style={{ fontSize: 11 }}>
                                {it.project_id}
                              </div>
                            </td>
                            <td>{it.owner || "—"}</td>
                            <td>
                              <span className="chip">
                                <span
                                  className={`chip-dot ${it.state === "scanning" ? "pulse" : ""}`}
                                  style={{ background: STATE_COLOR[it.state] }}
                                />
                                {STATE_LABEL[it.state]}
                              </span>
                            </td>
                            <td style={{ whiteSpace: "normal", maxWidth: 340 }}>
                              {it.state === "ok" ? (
                                <div>
                                  <b>{it.total != null ? `${it.total.toFixed(1)} 分` : "已完成"}</b>
                                  {(it.validation_problems?.length ?? 0) > 0 && (
                                    <details style={{ marginTop: 4, fontSize: 12 }}>
                                      <summary style={{ cursor: "pointer", color: "var(--warning)" }}>
                                        格式提示（{it.validation_problems!.length} 条，已落盘）
                                      </summary>
                                      <ul style={{ margin: "4px 0 0 16px", padding: 0, color: "var(--text-2)" }}>
                                        {it.validation_problems!.map((prob, i) => (
                                          <li key={`w-${i}`}>{prob}</li>
                                        ))}
                                      </ul>
                                    </details>
                                  )}
                                </div>
                              ) : it.state === "error" ? (
                                <div>
                                  <span style={{ color: "var(--critical)" }} title={it.error || ""}>
                                    {(it.error || "扫描失败").slice(0, 90)}
                                  </span>
                                  {((it.validation_problems?.length ?? 0) > 0 ||
                                    (it.secret_patterns?.length ?? 0) > 0) && (
                                    <details style={{ marginTop: 4, fontSize: 12 }}>
                                      <summary style={{ cursor: "pointer", color: "var(--accent)" }}>
                                        查看拦截原因（
                                        {(it.validation_problems?.length ?? 0) +
                                          (it.secret_patterns?.length ?? 0)}{" "}
                                        条）
                                      </summary>
                                      <ul style={{ margin: "4px 0 0 16px", padding: 0, color: "var(--text-2)" }}>
                                        {(it.secret_patterns || []).map((pat, i) => (
                                          <li key={`s-${i}`}>命中高置信密钥模式：{pat}</li>
                                        ))}
                                        {(it.validation_problems || []).map((prob, i) => (
                                          <li key={`v-${i}`}>{prob}</li>
                                        ))}
                                      </ul>
                                    </details>
                                  )}
                                  {it.has_rejected_draft && (
                                    <button
                                      type="button"
                                      className="btn"
                                      style={{ marginTop: 4, padding: "2px 8px", fontSize: 11 }}
                                      onClick={() =>
                                        void handleViewRejected(run.date, run.run_id, it.project_id, it.project_name)
                                      }
                                    >
                                      查看被拦截原始草稿
                                    </button>
                                  )}
                                </div>
                              ) : it.state === "scanning" ? (
                                <span style={{ color: "var(--accent)" }}>Harness 正在分析代码…</span>
                              ) : (
                                <span className="muted">等待并发名额</span>
                              )}
                            </td>
                            <td>
                              {it.state === "scanning" ? (
                                <div>
                                  <span style={{ color: "var(--accent)", fontWeight: 600 }}>
                                    {it.eta_seconds == null
                                      ? "估算中…"
                                      : it.eta_seconds > 0
                                      ? `预计剩 ~${formatDuration(it.eta_seconds)}`
                                      : "已超均值（即将完成）"}
                                  </span>
                                  <div className="muted" style={{ fontSize: 11 }}>
                                    已扫 {formatDuration(it.elapsed_seconds)}
                                    {it.timeout_remaining_seconds != null
                                      ? ` · 超时上限剩 ${formatDuration(it.timeout_remaining_seconds)}`
                                      : ""}
                                  </div>
                                </div>
                              ) : it.duration_seconds != null ? (
                                <span>{formatDuration(it.duration_seconds)}</span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                            <td>
                              {it.state === "ok" && onOpenReport ? (
                                <button
                                  className="btn"
                                  style={{ padding: "3px 9px", fontSize: 12 }}
                                  onClick={() => onOpenReport(run.date, run.run_id, it.project_id)}
                                >
                                  阅读报告
                                </button>
                              ) : it.state === "error" && !isRunning ? (
                                <button
                                  className="btn"
                                  style={{ padding: "3px 9px", fontSize: 12 }}
                                  disabled={actionBusy}
                                  onClick={() => void handleRetryFailed([it.project_id], run.report_mode)}
                                >
                                  <RotateCcw size={12} /> 重跑
                                </button>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    );
  };

  const runningTask = progress?.running_task;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {error && (
        <div className="card" style={{ borderColor: "var(--critical)", color: "var(--critical)" }}>
          {error}
        </div>
      )}

      {/* 1. 实时扫描进度清册 */}
      <div className="card">
        <div className="card-title">
          <span>
            实时扫描进度清册{" "}
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
              当前无正在运行的批次，最近一次批次清册：
            </div>
            {renderProgressBlock(progress.latest_run, false)}
          </>
        ) : (
          <div className="muted">当前无正在运行或历史扫描批次，请前往「扫描配置」页手动触发或启用定时扫描。</div>
        )}
      </div>

      {/* 2. Web / 定时触发任务记录与实时日志 */}
      <div className="grid-sidebar">
        <div className="card">
          <div className="card-title">Web / 定时触发记录 ({tasks.length})</div>
          {tasks.length === 0 ? (
            <div className="muted" style={{ fontSize: 13 }}>
              当前 Web 容器启动后尚未触发过扫描。
              <div style={{ marginTop: 6, fontSize: 12 }}>
                注：通过宿主机命令行（<code>docker compose run audit</code>）启动的批次仅体现在上方「实时扫描进度清册」，其终端日志需在宿主机查看。
              </div>
            </div>
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
            <div className="muted">
              从「扫描配置」页手动触发或由定时任务触发后，可在此实时查看扫描输出日志。
            </div>
          )}
        </div>
      </div>

      {draftModal && (
        <DraftModal
          title={draftModal.title}
          markdown={draftModal.markdown}
          filenameBase={draftModal.filenameBase}
          onClose={() => setDraftModal(null)}
        />
      )}
    </div>
  );
}
