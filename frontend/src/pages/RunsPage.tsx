import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Download, FileText, GitBranch, Mail, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { api } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DraftModal } from "../components/DraftModal";
import type { AuthUser, RunDetailResponse, RunSummary } from "../types";

interface Props {
  currentUser?: AuthUser | null;
  onOpenReport: (date: string, runId: string, target: string) => void;
  onScanStarted?: (taskId: string) => void;
  onFixProject?: (projectId: string) => void;
}

const STATUS_COLOR: Record<string, string> = {
  complete: "var(--good)",
  ok: "var(--good)",
  partial_failed: "var(--warning)",
  running: "var(--accent)",
  failed: "var(--critical)",
  error: "var(--critical)",
};

function fmtSec(sec: number): string {
  if (!sec || sec <= 0) return "—";
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m < 60 ? `${m}m ${rem}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function classifyError(raw: string | undefined): string {
  const s = (raw || "").toLowerCase();
  if (!s) return "其他错误";
  if (
    s.includes("clone") ||
    s.includes("branch") ||
    s.includes("git") ||
    s.includes("svn") ||
    s.includes("subversion") ||
    s.includes("repository") ||
    s.includes("权限") ||
    s.includes("分支")
  ) {
    return "Git 克隆 / 分支";
  }
  if (s.includes("timeout") || s.includes("超时") || s.includes("时限")) {
    return "执行超时";
  }
  if (s.includes("校验") || s.includes("拦截") || s.includes("密钥")) {
    return "报告校验拦截";
  }
  if (s.includes("api") || s.includes("model") || s.includes("http") || s.includes("connection") || s.includes("429") || s.includes("502") || s.includes("503")) {
    return "模型 / 网络错误";
  }
  return "其他错误";
}

export function RunsPage({ currentUser, onOpenReport, onScanStarted, onFixProject }: Props) {
  const isAdmin = !currentUser || currentUser.role === "superadmin" || currentUser.role === "admin";
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<{ date: string; run_id: string } | null>(null);
  const [detail, setDetail] = useState<RunDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [draftModal, setDraftModal] = useState<{ title: string; markdown: string; filenameBase: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmKind, setConfirmKind] = useState<"delete_run" | "send_emails" | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  // 清册筛选状态
  const [statusFilter, setStatusFilter] = useState<"" | "ok" | "error">("");
  const [errCatFilter, setErrCatFilter] = useState("");
  const [deptFilter, setDeptFilter] = useState("");
  const [query, setQuery] = useState("");

  const loadRuns = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.runs();
      setRuns(res.runs);
      if (!selected && res.runs.length > 0) {
        setSelected({ date: res.runs[0].date, run_id: res.runs[0].run_id });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载批次列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadRuns();
  }, []);

  useEffect(() => {
    if (!selected) return;
    api.runDetail(selected.date, selected.run_id).then(setDetail).catch(() => setDetail(null));
  }, [selected]);

  const projects = detail?.projects ?? [];

  const departments = useMemo(() => {
    const set = new Set<string>();
    for (const p of projects) {
      set.add((p.department || "").trim() || "未分组");
    }
    return [...set].sort((a, b) => a.localeCompare(b, "zh-CN"));
  }, [projects]);

  const failedProjects = useMemo(() => projects.filter((p) => p.status !== "ok"), [projects]);
  const okCount = projects.length - failedProjects.length;
  const errCount = failedProjects.length;

  // 失败原因自动聚类
  const errorClusters = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of failedProjects) {
      const cat = classifyError(p.error);
      counts.set(cat, (counts.get(cat) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [failedProjects]);

  const filteredProjects = useMemo(() => {
    const q = query.trim().toLowerCase();
    return projects.filter((p) => {
      const dept = (p.department || "").trim() || "未分组";
      if (statusFilter === "ok" && p.status !== "ok") return false;
      if (statusFilter === "error" && p.status === "ok") return false;
      if (errCatFilter && (p.status === "ok" || classifyError(p.error) !== errCatFilter)) return false;
      if (deptFilter && dept !== deptFilter) return false;
      if (
        q &&
        !`${p.project_name || ""} ${p.project_id} ${dept} ${p.owner || ""} ${p.error || ""}`
          .toLowerCase()
          .includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [projects, statusFilter, errCatFilter, deptFilter, query]);

  const handleRetryProjects = async (pids: string[]) => {
    if (pids.length === 0) return;
    setActionBusy(true);
    setError(null);
    try {
      const res = await api.startScan({
        only: pids,
        fast: detail?.report_mode === "fast",
        dry_run: false,
      });
      if (onScanStarted) onScanStarted(res.task_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "重跑失败");
    } finally {
      setActionBusy(false);
    }
  };

  const activeDate = detail?.date || selected?.date || "";
  const activeRunId = detail?.run_id || selected?.run_id || "";

  const handleMarkAborted = async () => {
    if (!detail || !activeDate || !activeRunId) return;
    setActionBusy(true);
    setError(null);
    try {
      await api.markRunAborted(activeDate, activeRunId);
      await loadRuns();
      const updated = await api.runDetail(activeDate, activeRunId);
      setDetail(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "标记中断失败");
    } finally {
      setActionBusy(false);
    }
  };

  const handleDeleteRun = () => {
    if (!detail) return;
    setConfirmError(null);
    setConfirmKind("delete_run");
  };

  const confirmDeleteRun = async () => {
    if (!detail || !activeDate || !activeRunId) return;
    setActionBusy(true);
    setError(null);
    setConfirmError(null);
    try {
      await api.deleteRun(activeDate, activeRunId);
      setConfirmKind(null);
      setNotice(`已永久删除批次 ${activeDate} / ${activeRunId}`);
      const res = await api.runs();
      setRuns(res.runs);
      if (res.runs.length > 0) {
        setSelected({ date: res.runs[0].date, run_id: res.runs[0].run_id });
      } else {
        setSelected(null);
        setDetail(null);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "删除批次失败";
      setConfirmError(msg);
      setError(msg);
    } finally {
      setActionBusy(false);
    }
  };

  const handleViewRejected = async (pid: string, name: string) => {
    if (!detail || !activeDate || !activeRunId) return;
    setError(null);
    try {
      const res = await api.rejectedDraft(activeDate, activeRunId, pid);
      setDraftModal({
        title: `${name}（${pid}）被拦截的原始输出`,
        markdown: res.markdown,
        filenameBase: `${activeDate}_${pid}_draft`,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载拦截草稿失败");
    }
  };

  const handleGenerateSummary = async () => {
    if (!detail || !activeDate || !activeRunId) return;
    setActionBusy(true);
    setError(null);
    try {
      await api.generateSummary(activeDate, activeRunId);
      const updated = await api.runDetail(activeDate, activeRunId);
      setDetail(updated);
      onOpenReport(activeDate, activeRunId, "_summary");
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成汇总报告失败");
    } finally {
      setActionBusy(false);
    }
  };

  const handleDownloadSummaryWord = async () => {
    if (!detail || !activeDate || !activeRunId) return;
    setActionBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.downloadReportDocx(activeDate, activeRunId, "_summary", true);
      const updated = await api.runDetail(activeDate, activeRunId).catch(() => null);
      if (updated) setDetail(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出 Word 汇总失败");
    } finally {
      setActionBusy(false);
    }
  };

  const handleSendBatchEmails = () => {
    if (!detail) return;
    setConfirmError(null);
    setConfirmKind("send_emails");
  };

  const confirmSendBatchEmails = async () => {
    if (!detail || !activeDate || !activeRunId) return;
    setActionBusy(true);
    setError(null);
    setConfirmError(null);
    setNotice(null);
    try {
      const res = await api.sendRunEmails(activeDate, activeRunId);
      setConfirmKind(null);
      setNotice(`邮件投递完成：成功 ${res.sent} 封，失败 ${res.failed} 封，按偏好跳过 ${res.skipped} 人`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "发送批次邮件失败";
      setConfirmError(msg);
      setError(msg);
    } finally {
      setActionBusy(false);
    }
  };

  return (
    <div className="grid-sidebar">
      <div className="card">
        <div className="card-title">
          <span>扫描批次 ({runs.length})</span>
          <button className="btn" onClick={() => void loadRuns()} disabled={loading}>
            <RefreshCw size={14} /> 刷新
          </button>
        </div>
        {error && (
          <div style={{ color: "var(--critical)", fontSize: 12, marginBottom: 8 }}>{error}</div>
        )}
        {notice && (
          <div style={{ color: "var(--good)", fontSize: 12, marginBottom: 8 }}>{notice}</div>
        )}
        {runs.length === 0 ? (
          <div className="muted" style={{ padding: "24px 0" }}>
            暂无批次记录（output/results/&lt;日期&gt;/&lt;run_id&gt;/_run.json）
          </div>
        ) : (
          runs.map((r) => {
            const active = selected?.date === r.date && selected?.run_id === r.run_id;
            return (
              <div
                key={`${r.date}-${r.run_id}`}
                className={`list-item ${active ? "active" : ""}`}
                onClick={() => {
                  setSelected({ date: r.date, run_id: r.run_id });
                  setDeptFilter("");
                  setErrCatFilter("");
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <b>{r.date}</b>
                  <span className="chip">
                    <span
                      className="chip-dot"
                      style={{ background: STATUS_COLOR[r.status] || "var(--neutral)" }}
                    />
                    {r.status}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  {r.run_id} · {r.report_mode || "standard"}
                  {r.triggered_by ? ` · ${r.triggered_by}` : ""}
                </div>
                <div className="sub" style={{ fontSize: 12, marginTop: 4 }}>
                  成功 {r.ok_count} / 失败 {r.error_count} / 共 {r.total_projects}
                </div>
              </div>
            );
          })
        )}
      </div>

      <div className="card">
        {!detail ? (
          <div className="muted" style={{ padding: 24 }}>
            请选择左侧的一个运行批次查看详情
          </div>
        ) : (
          <div>
            <div className="card-title" style={{ flexWrap: "wrap" }}>
              <span>
                批次详情：{activeDate} / {activeRunId}
              </span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {isAdmin && detail.status === "running" && (
                  <button
                    className="btn btn-danger"
                    disabled={actionBusy}
                    title="若该批次已异常中断且无活跃进程，点击收尾为已中断状态"
                    onClick={() => void handleMarkAborted()}
                  >
                    <AlertTriangle size={14} /> 标记为已中断
                  </button>
                )}
                {failedProjects.length > 0 && (
                  <button
                    className="btn"
                    disabled={actionBusy}
                    onClick={() => void handleRetryProjects(failedProjects.map((p) => p.project_id))}
                  >
                    <RotateCcw size={14} /> 重跑失败项 ({failedProjects.length})
                  </button>
                )}
                {isAdmin && detail.status !== "running" && projects.length > 0 && (
                  <button
                    className="btn"
                    disabled={actionBusy}
                    title="按负责人聚合发送本批次结果邮件（含 Word/Markdown 报告附件）"
                    onClick={() => void handleSendBatchEmails()}
                  >
                    <Mail size={14} /> 发送邮件通知
                  </button>
                )}
                {isAdmin && okCount > 0 && (
                  <>
                    <button
                      className="btn"
                      disabled={actionBusy}
                      title="按最新 Word 汇总模板即时生成并导出 .docx 汇总报告"
                      onClick={() => void handleDownloadSummaryWord()}
                    >
                      <Download size={14} /> 导出 Word 汇总 (.docx)
                    </button>
                    <button
                      className="btn btn-primary"
                      disabled={actionBusy}
                      onClick={() =>
                        detail.has_summary
                          ? onOpenReport(detail.date, detail.run_id, "_summary")
                          : void handleGenerateSummary()
                      }
                    >
                      <FileText size={14} />{" "}
                      {detail.has_summary ? "查看整体汇总 (_summary.md)" : "生成汇总报告"}
                    </button>
                  </>
                )}
                {detail.can_delete && (
                  <button
                    className="btn btn-danger"
                    disabled={actionBusy}
                    title="永久删除该冗余/测试批次及其报告"
                    onClick={() => void handleDeleteRun()}
                  >
                    <Trash2 size={14} /> 删除批次
                  </button>
                )}
              </div>
            </div>
            <div className="sub" style={{ marginBottom: 12 }}>
              状态：<b>{detail.status}</b> · 模式：{detail.report_mode || "—"}
              {detail.triggered_by ? ` · 触发人：${detail.triggered_by}` : ""}
              {" · "}开始：{detail.started_at?.slice(0, 19).replace("T", " ") || "—"}
              {detail.snapshot_at ? ` · 快照：${detail.snapshot_at.slice(0, 19).replace("T", " ")}` : ""}
              {" · "}结束：{detail.finished_at?.slice(0, 19).replace("T", " ") || "—"} · 汇总状态：
              {detail.summary_status || "—"}
            </div>

            {/* 失败原因聚类速览 */}
            {errorClusters.length > 0 && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: 6,
                  marginBottom: 10,
                  padding: "8px 10px",
                  borderRadius: 8,
                  background: "var(--bg-2)",
                  border: "1px solid var(--border)",
                  fontSize: 12,
                }}
              >
                <span style={{ fontWeight: 600, color: "var(--critical)" }}>失败原因聚类：</span>
                {errorClusters.map(([cat, cnt]) => (
                  <button
                    key={cat}
                    type="button"
                    className={`weekday-btn ${errCatFilter === cat ? "active" : ""}`}
                    onClick={() => {
                      const next = errCatFilter === cat ? "" : cat;
                      setErrCatFilter(next);
                      if (next) setStatusFilter("error");
                    }}
                  >
                    {cat} ({cnt})
                  </button>
                ))}
                {errCatFilter && (
                  <button
                    type="button"
                    className="weekday-btn"
                    onClick={() => {
                      setErrCatFilter("");
                      setStatusFilter("");
                    }}
                  >
                    清除聚类筛选
                  </button>
                )}
              </div>
            )}

            <div className="filters">
              <button
                type="button"
                className={`weekday-btn ${statusFilter === "" && !errCatFilter ? "active" : ""}`}
                onClick={() => {
                  setStatusFilter("");
                  setErrCatFilter("");
                }}
              >
                全部 ({projects.length})
              </button>
              <button
                type="button"
                className={`weekday-btn ${statusFilter === "ok" ? "active" : ""}`}
                onClick={() => {
                  setStatusFilter("ok");
                  setErrCatFilter("");
                }}
              >
                成功 ({okCount})
              </button>
              <button
                type="button"
                className={`weekday-btn ${statusFilter === "error" ? "active" : ""}`}
                onClick={() => setStatusFilter("error")}
              >
                失败 ({errCount})
              </button>

              <select value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
                <option value="">全部部门 ({departments.length})</option>
                {departments.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>

              <input
                placeholder="搜索项目 / ID / 部门 / 负责人 / 报错"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                style={{ minWidth: 220 }}
              />

              <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
                显示 {filteredProjects.length} / {projects.length} 项
              </span>
            </div>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>项目</th>
                    <th>部门</th>
                    <th>状态</th>
                    <th>扫描类型</th>
                    <th className="num">总分</th>
                    <th className="num">安全分</th>
                    <th className="num">覆盖率</th>
                    <th>提交</th>
                    <th>耗时 / 错误</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredProjects.map((p) => {
                    const stages = p.metrics?.stages_seconds || {};
                    const totalSec =
                      (p.metrics as { total_seconds?: number } | undefined)?.total_seconds ??
                      Object.values(stages).reduce((a, b) => a + b, 0);
                    return (
                      <tr key={p.project_id}>
                        <td>
                          <b>{p.project_name || p.project_id}</b>
                          <div className="muted" style={{ fontSize: 12 }}>
                            {p.project_id}
                          </div>
                        </td>
                        <td>{(p.department || "").trim() || "未分组"}</td>
                        <td>
                          <span className="chip">
                            <span
                              className="chip-dot"
                              style={{ background: STATUS_COLOR[p.status] || "var(--neutral)" }}
                            />
                            {p.status}
                          </span>
                        </td>
                        <td>{p.scan_type || "—"}</td>
                        <td className="num">
                          {p.report_scores?.total != null ? p.report_scores.total.toFixed(1) : "—"}
                        </td>
                        <td className="num">
                          {p.report_scores?.security_score != null
                            ? p.report_scores.security_score.toFixed(1)
                            : "—"}
                        </td>
                        <td className="num">
                          {p.report_scores?.coverage != null
                            ? `${Math.round(p.report_scores.coverage * 100)}%`
                            : "—"}
                        </td>
                        <td className="muted">{(p.commit || "").slice(0, 10) || "—"}</td>
                        <td style={{ whiteSpace: "normal", maxWidth: 320 }}>
                          {p.error ? (
                            <div>
                              <span style={{ color: "var(--critical)", fontSize: 12 }} title={p.error}>
                                {p.error.length > 120 ? `${p.error.slice(0, 120)}…` : p.error}
                              </span>
                              {((p.report_validation?.problems?.length ?? 0) > 0 ||
                                (p.secret_scan?.patterns?.length ?? 0) > 0) && (
                                <details style={{ marginTop: 4, fontSize: 12 }}>
                                  <summary style={{ cursor: "pointer", color: "var(--accent)" }}>
                                    查看拦截原因（
                                    {(p.report_validation?.problems?.length ?? 0) +
                                      (p.secret_scan?.patterns?.length ?? 0)}{" "}
                                    条）
                                  </summary>
                                  <ul style={{ margin: "4px 0 0 16px", padding: 0, color: "var(--text-2)" }}>
                                    {(p.secret_scan?.patterns || []).map((pat, i) => (
                                      <li key={`s-${i}`}>命中高置信密钥模式：{pat}</li>
                                    ))}
                                    {(p.report_validation?.problems || []).map((prob, i) => (
                                      <li key={`v-${i}`}>{prob}</li>
                                    ))}
                                  </ul>
                                </details>
                              )}
                              {p.has_rejected_draft && (
                                <button
                                  type="button"
                                  className="btn"
                                  style={{ marginTop: 4, padding: "2px 8px", fontSize: 11 }}
                                  onClick={() => void handleViewRejected(p.project_id, p.project_name || p.project_id)}
                                >
                                  查看被拦截原始草稿
                                </button>
                              )}
                            </div>
                          ) : (
                            <div>
                              <span className="muted" style={{ fontSize: 12 }}>
                                {fmtSec(totalSec)}
                              </span>
                              {(p.report_validation?.problems?.length ?? 0) > 0 && (
                                <details style={{ marginTop: 4, fontSize: 12 }}>
                                  <summary style={{ cursor: "pointer", color: "var(--warning)" }}>
                                    格式提示（{p.report_validation!.problems!.length} 条，已落盘）
                                  </summary>
                                  <ul style={{ margin: "4px 0 0 16px", padding: 0, color: "var(--text-2)" }}>
                                    {p.report_validation!.problems!.map((prob, i) => (
                                      <li key={`w-${i}`}>{prob}</li>
                                    ))}
                                  </ul>
                                </details>
                              )}
                            </div>
                          )}
                        </td>
                        <td>
                          {p.status === "ok" ? (
                            <button
                              className="btn"
                              style={{ padding: "3px 9px", fontSize: 12 }}
                              onClick={() => onOpenReport(detail.date, detail.run_id, p.project_id)}
                            >
                              阅读报告
                            </button>
                          ) : (
                            <div style={{ display: "flex", gap: 5, flexWrap: "nowrap" }}>
                              {isAdmin && onFixProject && classifyError(p.error) === "Git 克隆 / 分支" && (
                                <button
                                  type="button"
                                  className="btn"
                                  style={{ padding: "3px 8px", fontSize: 12, borderColor: "var(--warning)", color: "var(--warning)" }}
                                  title="跳转到项目管理并自动探测可用分支"
                                  onClick={() => onFixProject(p.project_id)}
                                >
                                  <GitBranch size={12} /> 去修分支
                                </button>
                              )}
                              <button
                                className="btn"
                                style={{ padding: "3px 9px", fontSize: 12 }}
                                disabled={actionBusy}
                                onClick={() => void handleRetryProjects([p.project_id])}
                              >
                                <RotateCcw size={12} /> 重跑
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                  {filteredProjects.length === 0 && (
                    <tr>
                      <td colSpan={10} className="muted" style={{ textAlign: "center", padding: 24 }}>
                        没有符合筛选条件的项目
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {draftModal && (
        <DraftModal
          title={draftModal.title}
          markdown={draftModal.markdown}
          filenameBase={draftModal.filenameBase}
          onClose={() => setDraftModal(null)}
        />
      )}

      <ConfirmDialog
        open={Boolean(confirmKind && detail)}
        tone={confirmKind === "delete_run" ? "danger" : "info"}
        title={
          confirmKind === "delete_run"
            ? `确认永久删除批次 ${activeDate} / ${activeRunId}？`
            : `确认发送本批次审查结果邮件？`
        }
        description={
          confirmKind === "delete_run"
            ? "该批次的运行记录与项目报告将被彻底清除；若项目最新基线指向该批次，系统会自动回退到上一次有效扫描结果。"
            : `将按项目负责人聚合发送批次（${activeDate} / ${activeRunId}）的 HTML 报告与附件（并按配置抄送管理汇总）。`
        }
        confirmLabel={confirmKind === "delete_run" ? "永久删除批次" : "确认发送邮件"}
        error={confirmError}
        busy={actionBusy}
        onConfirm={() => void (confirmKind === "delete_run" ? confirmDeleteRun() : confirmSendBatchEmails())}
        onCancel={() => {
          setConfirmError(null);
          setConfirmKind(null);
        }}
      />
    </div>
  );
}
