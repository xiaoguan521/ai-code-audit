import { FormEvent, useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Download, FileText, Link2, Printer, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "../api";
import type { AuthUser, IssueTriageStatus, ReportIssue, ReportResponse, RunDetailResponse, RunSummary } from "../types";
import {
  downloadBlob,
  downloadMarkdown,
  escapeHtml,
  exportHtmlToPdf,
  renderMarkdownWithToc,
  type TocItem,
} from "../utils/markdown";

const TRIAGE_META: Record<IssueTriageStatus, { label: string; color: string }> = {
  open: { label: "待整改", color: "var(--critical)" },
  planned: { label: "已排期", color: "var(--accent)" },
  fixed: { label: "已修复待复核", color: "var(--good)" },
  exempted: { label: "已豁免 / 误报", color: "var(--text-3)" },
};

function csvCell(v: string): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

interface Props {
  currentUser?: AuthUser | null;
  initialTarget: { date: string; runId: string; target: string } | null;
  onTargetChange?: (next: { date: string; runId: string; target: string }) => void;
}

export function ReportsPage({ currentUser, initialTarget, onTargetChange }: Props) {
  const isAdmin = !currentUser || currentUser.role === "superadmin" || currentUser.role === "admin";
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [date, setDate] = useState(initialTarget?.date || "");
  const [runId, setRunId] = useState(initialTarget?.runId || "");
  const [target, setTarget] = useState(
    initialTarget?.target || (isAdmin ? "_summary" : "")
  );
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);

  const [levelFilter, setLevelFilter] = useState("");
  const [triageFilter, setTriageFilter] = useState("");
  const [issueQuery, setIssueQuery] = useState("");
  const [projectSearch, setProjectSearch] = useState("");
  const [tab, setTab] = useState<"markdown" | "issues">("markdown");

  // 问题处置 / 误报豁免弹窗
  const [triageTarget, setTriageTarget] = useState<ReportIssue | null>(null);
  const [triageStatusInput, setTriageStatusInput] = useState<IssueTriageStatus>("planned");
  const [triageReasonInput, setTriageReasonInput] = useState("");
  const [triageBusy, setTriageBusy] = useState(false);

  useEffect(() => {
    if (initialTarget) {
      setDate(initialTarget.date);
      setRunId(initialTarget.runId);
      setTarget(initialTarget.target);
    }
  }, [initialTarget?.date, initialTarget?.runId, initialTarget?.target]);

  useEffect(() => {
    api
      .runs()
      .then((res) => {
        setRuns(res.runs);
        if (!date && !runId && res.runs.length > 0) {
          setDate(res.runs[0].date);
          setRunId(res.runs[0].run_id);
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : "加载批次列表失败"));
  }, []);

  useEffect(() => {
    if (date && runId && onTargetChange) {
      onTargetChange({ date, runId, target });
    }
  }, [date, runId, target]);

  useEffect(() => {
    if (!date || !runId) return;
    api
      .runDetail(date, runId)
      .then((d) => {
        setRunDetail(d);
        if (!isAdmin && (!target || target === "_summary")) {
          const firstOk = (d.projects || []).find((p) => p.status === "ok");
          if (firstOk) setTarget(firstOk.project_id);
        }
      })
      .catch(() => setRunDetail(null));
  }, [date, runId, isAdmin]);

  useEffect(() => {
    if (!date || !runId || !target) return;
    setError(null);
    api
      .report(date, runId, target)
      .then(setReport)
      .catch((err) => {
        setReport(null);
        setError(err instanceof Error ? err.message : "加载报告失败");
      });
  }, [date, runId, target]);

  const { html, toc } = useMemo(() => {
    if (!report?.markdown) return { html: "", toc: [] as TocItem[] };
    return renderMarkdownWithToc(report.markdown);
  }, [report?.markdown]);

  const filteredIssues = useMemo(() => {
    const list = report?.issues ?? [];
    const q = issueQuery.trim().toLowerCase();
    return list.filter((i) => {
      const st = i.triage_status || "open";
      if (levelFilter && i.level !== levelFilter) return false;
      if (triageFilter === "active" && st === "exempted") return false;
      if (triageFilter && triageFilter !== "active" && st !== triageFilter) return false;
      if (
        q &&
        !`${i.issue_id} ${i.item_id} ${i.title} ${i.evidence_text} ${i.suggestion} ${i.triage_reason || ""}`
          .toLowerCase()
          .includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [report?.issues, levelFilter, triageFilter, issueQuery]);

  const openTriageModal = (issue: ReportIssue) => {
    setTriageTarget(issue);
    setTriageStatusInput(issue.triage_status || "planned");
    setTriageReasonInput(issue.triage_reason || "");
  };

  const handleSaveTriage = async (e: FormEvent) => {
    e.preventDefault();
    if (!triageTarget || !report?.project_id) return;
    setTriageBusy(true);
    setError(null);
    try {
      await api.triageIssue({
        project_id: report.project_id,
        fingerprint: triageTarget.fingerprint,
        item_id: triageTarget.item_id,
        level: triageTarget.level,
        title: triageTarget.title,
        evidence_text: triageTarget.evidence_text,
        status: triageStatusInput,
        reason: triageReasonInput.trim(),
      });
      setTriageTarget(null);
      const refreshed = await api.report(date, runId, target);
      setReport(refreshed);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存问题处置状态失败");
    } finally {
      setTriageBusy(false);
    }
  };

  const okProjects = (runDetail?.projects ?? []).filter((p) => p.status === "ok");
  const visibleProjects = useMemo(() => {
    const q = projectSearch.trim().toLowerCase();
    if (!q) return okProjects;
    return okProjects.filter((p) =>
      `${p.project_name || ""} ${p.project_id} ${p.department || ""}`.toLowerCase().includes(q)
    );
  }, [okProjects, projectSearch]);

  const targetOrder = useMemo(
    () => (isAdmin ? ["_summary", ...visibleProjects.map((p) => p.project_id)] : visibleProjects.map((p) => p.project_id)),
    [visibleProjects, isAdmin]
  );
  const currentIdx = targetOrder.indexOf(target);

  const handleCopyLink = () => {
    const url = window.location.href;
    navigator.clipboard?.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    }).catch(() => { });
  };

  const handleDownloadMd = () => {
    if (!report?.markdown) return;
    downloadMarkdown(`${date}_${target === "_summary" ? "summary" : target}`, report.markdown);
  };

  const handleGenerateSummary = async () => {
    if (!date || !runId) return;
    setExportBusy(true);
    setError(null);
    try {
      const res = await api.generateSummary(date, runId);
      setTarget("_summary");
      setReport({ type: "summary", date, run_id: runId, markdown: res.markdown });
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成汇总报告失败");
    } finally {
      setExportBusy(false);
    }
  };

  const handleDownloadDocx = async (refresh = false) => {
    if (!date || !runId || !target) return;
    setExportBusy(true);
    setError(null);
    try {
      await api.downloadReportDocx(date, runId, target, refresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出 Word 失败");
    } finally {
      setExportBusy(false);
    }
  };

  const handleExportPdf = () => {
    if (!report?.markdown) return;
    const base = `${date}_${target === "_summary" ? "summary" : target}`;
    let banner = "";
    if (report.type === "project") {
      const totalStr = report.scores?.total != null ? report.scores.total.toFixed(1) : "暂不评分";
      const secStr = report.scores?.security_score != null ? report.scores.security_score.toFixed(1) : "—";
      const gradeStr = report.scores?.grade || "—";
      banner = `<div class="pdf-meta-banner"><b>${escapeHtml(report.project_id || target)}</b> · 批次 ${escapeHtml(
        `${date} / ${runId}`
      )} · 总评分 ${escapeHtml(totalStr)}（${escapeHtml(gradeStr)}） · 安全评分 ${escapeHtml(secStr)}</div>`;
    }
    exportHtmlToPdf(base, banner + html);
  };

  const handleExportIssuesCsv = () => {
    if (!report?.issues) return;
    const header = ["ID", "等级", "检查项", "处置状态", "处置说明", "问题标题", "证据", "影响", "修复建议", "工作量"];
    const lines = [
      header.join(","),
      ...filteredIssues.map((i) =>
        [
          i.issue_id,
          i.level,
          i.item_id,
          TRIAGE_META[i.triage_status || "open"].label,
          i.triage_reason || "",
          i.title,
          i.evidence_text,
          i.impact,
          i.suggestion,
          i.effort,
        ]
          .map(csvCell)
          .join(",")
      ),
    ];
    downloadBlob(`${date}_${target}_issues.csv`, "\ufeff" + lines.join("\n"), "text/csv;charset=utf-8");
  };

  return (
    <div>
      <div className="card no-print" style={{ marginBottom: 14 }}>
        <div className="filters" style={{ marginBottom: 0 }}>
          <select
            value={date && runId ? `${date}/${runId}` : ""}
            onChange={(e) => {
              const [d, r] = e.target.value.split("/");
              setDate(d || "");
              setRunId(r || "");
              setTarget(isAdmin ? "_summary" : "");
            }}
          >
            {runs.length === 0 && <option value="">暂无批次</option>}
            {runs.map((r) => (
              <option key={`${r.date}/${r.run_id}`} value={`${r.date}/${r.run_id}`}>
                {r.date} · {r.run_id} ({r.ok_count}/{r.total_projects})
              </option>
            ))}
          </select>

          <input
            placeholder="筛选报告项目…"
            value={projectSearch}
            onChange={(e) => setProjectSearch(e.target.value)}
            style={{ width: 150 }}
          />

          <select value={target} onChange={(e) => setTarget(e.target.value)} style={{ maxWidth: 280 }}>
            {isAdmin && <option value="_summary">跨项目汇总报告 (_summary.md)</option>}
            {!isAdmin && visibleProjects.length === 0 && <option value="">暂无可阅读报告</option>}
            {visibleProjects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.project_name || p.project_id} ({p.project_id})
              </option>
            ))}
          </select>

          <button
            className="btn"
            style={{ padding: "6px 8px" }}
            disabled={currentIdx <= 0}
            title="上一份报告"
            onClick={() => currentIdx > 0 && setTarget(targetOrder[currentIdx - 1])}
          >
            <ChevronLeft size={14} />
          </button>
          <button
            className="btn"
            style={{ padding: "6px 8px" }}
            disabled={currentIdx < 0 || currentIdx >= targetOrder.length - 1}
            title="下一份报告"
            onClick={() =>
              currentIdx >= 0 && currentIdx < targetOrder.length - 1 && setTarget(targetOrder[currentIdx + 1])
            }
          >
            <ChevronRight size={14} />
          </button>

          <button className="btn" onClick={handleCopyLink} title="复制当前报告直达链接">
            {copied ? <Check size={14} /> : <Link2 size={14} />} {copied ? "已复制" : "复制链接"}
          </button>

          {isAdmin && target === "_summary" && date && runId && (
            <button
              className="btn btn-primary"
              disabled={exportBusy}
              title="按 Word 汇总模板即时生成/刷新当前批次汇总报告"
              onClick={() => void handleGenerateSummary()}
            >
              <RefreshCw size={14} /> {exportBusy ? "生成中…" : report ? "刷新汇总" : "生成汇总报告"}
            </button>
          )}

          {report && (
            <>
              <button
                className="btn"
                disabled={exportBusy}
                onClick={() => void handleDownloadDocx(false)}
                title="导出为 Word (.docx) 文档"
              >
                <FileText size={14} /> 下载 Word (.docx)
              </button>
              <button className="btn" onClick={handleDownloadMd}>
                <Download size={14} /> 下载 Markdown
              </button>
              <button className="btn" onClick={handleExportPdf}>
                <Printer size={14} /> 导出 PDF
              </button>
              {report.type === "project" && (report.issues?.length ?? 0) > 0 && (
                <button className="btn" onClick={handleExportIssuesCsv}>
                  <Download size={14} /> 导出问题 CSV
                </button>
              )}
            </>
          )}

          {report?.type === "project" && (
            <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              <button
                className={`btn ${tab === "markdown" ? "btn-primary" : ""}`}
                onClick={() => setTab("markdown")}
              >
                Markdown 全文
              </button>
              <button
                className={`btn ${tab === "issues" ? "btn-primary" : ""}`}
                onClick={() => setTab("issues")}
              >
                风险问题筛选 ({report.issues?.length ?? 0})
              </button>
            </div>
          )}
        </div>
      </div>

      {error && <div className="card muted">{error}</div>}

      {report && report.type === "project" && report.dimensions && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title">
            <span>
              {report.project_id} · 总评分{" "}
              {report.scores?.total != null ? report.scores.total.toFixed(1) : "暂不评分"}（
              {report.scores?.grade || "—"}） · 安全评分{" "}
              {report.scores?.security_score != null ? report.scores.security_score.toFixed(1) : "—"}
            </span>
            <span className="sub">
              有效问题：P0 {report.effective_issue_counts?.P0 ?? report.issue_counts?.P0 ?? 0} / P1{" "}
              {report.effective_issue_counts?.P1 ?? report.issue_counts?.P1 ?? 0} / P2{" "}
              {report.effective_issue_counts?.P2 ?? report.issue_counts?.P2 ?? 0} / P3{" "}
              {report.effective_issue_counts?.P3 ?? report.issue_counts?.P3 ?? 0}
              {(report.triage_summary?.exempted ?? 0) > 0 && (
                <> · 已豁免 {report.triage_summary!.exempted} 条</>
              )}
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "4px 20px" }}>
            {report.dimensions.map((d) => (
              <div className="dim-row" key={d.id}>
                <span>
                  {d.id}. {d.name}
                </span>
                <div className="dim-bar-track">
                  <div
                    className="dim-bar-fill"
                    style={{ width: `${d.score == null ? 0 : Math.min(100, d.score * 10)}%` }}
                  />
                </div>
                <span style={{ textAlign: "right" }}>
                  {d.score == null ? "暂不评分" : d.score.toFixed(1)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {report && tab === "issues" && report.type === "project" ? (
        <div className="card">
          <div className="filters no-print" style={{ alignItems: "center" }}>
            <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}>
              <option value="">全部等级</option>
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
            <select value={triageFilter} onChange={(e) => setTriageFilter(e.target.value)}>
              <option value="">全部处置状态</option>
              <option value="active">排除已豁免（仅看有效问题）</option>
              <option value="open">待整改 ({report.triage_summary?.open ?? 0})</option>
              <option value="planned">已排期 ({report.triage_summary?.planned ?? 0})</option>
              <option value="fixed">已修复待复核 ({report.triage_summary?.fixed ?? 0})</option>
              <option value="exempted">已豁免 / 误报 ({report.triage_summary?.exempted ?? 0})</option>
            </select>
            <input
              placeholder="搜索问题标题 / 检查项 / 文件路径 / 建议 / 豁免说明"
              value={issueQuery}
              onChange={(e) => setIssueQuery(e.target.value)}
              style={{ minWidth: 260, flex: "1 1 240px" }}
            />
            {report.triage_summary && (
              <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
                待整改 <b>{report.triage_summary.open}</b> · 已排期 <b>{report.triage_summary.planned}</b> · 已修复{" "}
                <b>{report.triage_summary.fixed}</b> · 已豁免 <b>{report.triage_summary.exempted}</b>
              </span>
            )}
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>等级</th>
                  <th>检查项</th>
                  <th>处置状态</th>
                  <th>问题标题</th>
                  <th>证据</th>
                  <th>修复建议</th>
                  <th>工作量</th>
                  <th className="no-print">操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredIssues.map((i) => {
                  const st = i.triage_status || "open";
                  const meta = TRIAGE_META[st];
                  return (
                    <tr
                      key={`${i.issue_id}-${i.item_id}-${i.fingerprint || ""}`}
                      style={{ opacity: st === "exempted" ? 0.62 : 1 }}
                    >
                      <td>{i.issue_id}</td>
                      <td>
                        <span className={`lv-badge lv-${i.level}`}>{i.level}</span>
                      </td>
                      <td>{i.item_id}</td>
                      <td style={{ whiteSpace: "normal", minWidth: 118 }}>
                        <span
                          className="chip"
                          style={{ borderColor: meta.color, color: meta.color, fontSize: 11.5 }}
                          title={i.triage_reason || undefined}
                        >
                          {meta.label}
                        </span>
                        {i.triage_reason && (
                          <div className="muted" style={{ fontSize: 11, marginTop: 3, lineHeight: 1.35 }}>
                            {i.triage_reason.slice(0, 50)}
                            {i.triage_reason.length > 50 ? "…" : ""}
                          </div>
                        )}
                      </td>
                      <td style={{ whiteSpace: "normal", minWidth: 200 }}>{i.title}</td>
                      <td style={{ whiteSpace: "normal", minWidth: 180 }} className="sub">
                        {i.evidence_text || "—"}
                      </td>
                      <td style={{ whiteSpace: "normal", minWidth: 230 }}>{i.suggestion || "—"}</td>
                      <td>{i.effort || "—"}</td>
                      <td className="no-print">
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: "3px 8px", fontSize: 12 }}
                          onClick={() => openTriageModal(i)}
                        >
                          <ShieldCheck size={12} /> 处置
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {filteredIssues.length === 0 && (
                  <tr>
                    <td colSpan={9} className="muted" style={{ textAlign: "center", padding: 24 }}>
                      没有匹配的风险问题
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        report && (
          <div className={toc.length > 0 ? "report-layout" : undefined}>
            {toc.length > 0 && (
              <aside className="card toc-panel no-print">
                <div className="card-title" style={{ marginBottom: 8 }}>
                  <span>目录导航</span>
                </div>
                {toc.map((item) => (
                  <button
                    key={item.id}
                    className={`toc-link level-${item.depth}`}
                    title={item.text}
                    onClick={() =>
                      document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" })
                    }
                  >
                    {item.text}
                  </button>
                ))}
              </aside>
            )}
            <div className="card">
              <div
                className="markdown-body"
                dangerouslySetInnerHTML={{ __html: html }}
                onClick={(e) => {
                  const anchor = (e.target as HTMLElement).closest("a");
                  const href = anchor?.getAttribute("href") || "";
                  const m = href.match(/^([A-Za-z0-9._-]+)\.md$/);
                  if (m) {
                    e.preventDefault();
                    setTarget(m[1]);
                  }
                }}
              />
            </div>
          </div>
        )
      )}
      {triageTarget && (
        <div className="modal-backdrop" onClick={() => setTriageTarget(null)}>
          <form
            className="modal-dialog"
            style={{ width: "min(520px, 95vw)", display: "grid", gap: 12 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleSaveTriage(e)}
          >
            <div className="card-title">
              <span>问题整改追踪 / 风险豁免</span>
              <button type="button" className="btn" onClick={() => setTriageTarget(null)}>
                关闭
              </button>
            </div>
            <div style={{ fontSize: 12.5, padding: "8px 10px", borderRadius: 8, background: "var(--bg-2)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <span className={`lv-badge lv-${triageTarget.level}`}>{triageTarget.level}</span>
                <b>{triageTarget.item_id}</b>
                <span>{triageTarget.title}</span>
              </div>
              {triageTarget.evidence_text && (
                <div className="muted" style={{ fontSize: 11.5 }}>
                  证据：{triageTarget.evidence_text}
                </div>
              )}
            </div>

            <div style={{ display: "grid", gap: 6 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                处置状态（系统基于问题指纹跨批次自动记忆，代码行号小幅偏移仍可命中）
              </span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {(["open", "planned", "fixed", "exempted"] as IssueTriageStatus[]).map((st) => (
                  <button
                    key={st}
                    type="button"
                    className={`btn ${triageStatusInput === st ? "btn-primary" : ""}`}
                    style={{ padding: "5px 10px", fontSize: 12.5 }}
                    onClick={() => setTriageStatusInput(st)}
                  >
                    {TRIAGE_META[st].label}
                  </button>
                ))}
              </div>
            </div>

            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">
                处置说明 / 豁免原因{triageStatusInput === "exempted" ? "（豁免必填）*" : "（可选）"}
              </span>
              <textarea
                rows={3}
                value={triageReasonInput}
                onChange={(e) => setTriageReasonInput(e.target.value)}
                required={triageStatusInput === "exempted"}
                placeholder={
                  triageStatusInput === "exempted"
                    ? "例如：该接口仅内网定时任务调用且由网关统一鉴权，业务评估为可接受风险"
                    : "例如：已纳入下周迭代修复 / 已在提交 abc123 修复待复核"
                }
                style={{
                  width: "100%",
                  padding: 8,
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg-2)",
                  color: "var(--text-1)",
                  fontSize: 12.5,
                }}
              />
            </label>

            {triageTarget.triage_updated_by && (
              <div className="muted" style={{ fontSize: 11.5 }}>
                上次更新：{triageTarget.triage_updated_by} · {(triageTarget.triage_updated_at || "").replace("T", " ").slice(0, 19)}
              </div>
            )}

            <div className="confirm-footer">
              <button type="button" className="btn" disabled={triageBusy} onClick={() => setTriageTarget(null)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={triageBusy}>
                {triageBusy ? "保存中…" : "保存处置状态"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
