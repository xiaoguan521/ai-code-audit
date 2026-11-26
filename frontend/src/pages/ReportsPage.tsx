import { useEffect, useMemo, useState } from "react";
import { Check, Download, Link2, Printer } from "lucide-react";
import { Marked, Renderer } from "marked";
import { api } from "../api";
import type { ReportResponse, RunDetailResponse, RunSummary } from "../types";

interface TocItem {
  id: string;
  depth: number;
  text: string;
}

function escapeHtml(raw: string): string {
  return raw.replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return ch;
    }
  });
}

function renderMarkdownWithToc(markdown: string): { html: string; toc: TocItem[] } {
  const toc: TocItem[] = [];
  const renderer = new Renderer();
  renderer.html = ({ text }) => escapeHtml(text);
  const origLink = renderer.link.bind(renderer);
  renderer.link = (token) => {
    const href = (token.href || "").trim();
    if (/^(javascript|vbscript|data):/i.test(href)) {
      return escapeHtml(token.text || href);
    }
    return origLink(token);
  };
  const origImage = renderer.image.bind(renderer);
  renderer.image = (token) => {
    const href = (token.href || "").trim();
    if (/^(javascript|vbscript|data):/i.test(href)) {
      return escapeHtml(token.text || href);
    }
    return origImage(token);
  };
  renderer.heading = function ({ tokens, depth, text }) {
    const inner = this.parser.parseInline(tokens);
    if (depth === 2 || depth === 3) {
      const id = `sec-${toc.length}`;
      toc.push({ id, depth, text: text.replace(/[`*_#]/g, "").trim() });
      return `<h${depth} id="${id}">${inner}</h${depth}>\n`;
    }
    return `<h${depth}>${inner}</h${depth}>\n`;
  };
  const instance = new Marked({ gfm: true, breaks: false, renderer });
  const html = instance.parse(markdown) as string;
  return { html, toc };
}

function downloadBlob(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function csvCell(v: string): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

interface Props {
  initialTarget: { date: string; runId: string; target: string } | null;
  onTargetChange?: (next: { date: string; runId: string; target: string }) => void;
}

export function ReportsPage({ initialTarget, onTargetChange }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [date, setDate] = useState(initialTarget?.date || "");
  const [runId, setRunId] = useState(initialTarget?.runId || "");
  const [target, setTarget] = useState(initialTarget?.target || "_summary");
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [levelFilter, setLevelFilter] = useState("");
  const [issueQuery, setIssueQuery] = useState("");
  const [tab, setTab] = useState<"markdown" | "issues">("markdown");

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
    api.runDetail(date, runId).then(setRunDetail).catch(() => setRunDetail(null));
  }, [date, runId]);

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
    return list.filter(
      (i) =>
        (!levelFilter || i.level === levelFilter) &&
        (!q || `${i.issue_id} ${i.item_id} ${i.title} ${i.evidence_text} ${i.suggestion}`.toLowerCase().includes(q))
    );
  }, [report?.issues, levelFilter, issueQuery]);

  const okProjects = (runDetail?.projects ?? []).filter((p) => p.status === "ok");

  const handleCopyLink = () => {
    const url = window.location.href;
    navigator.clipboard?.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    }).catch(() => {});
  };

  const handleDownloadMd = () => {
    if (!report?.markdown) return;
    const fname = `${date}_${target === "_summary" ? "summary" : target}.md`;
    downloadBlob(fname, report.markdown, "text/markdown;charset=utf-8");
  };

  const handleExportIssuesCsv = () => {
    if (!report?.issues) return;
    const header = ["ID", "等级", "检查项", "问题标题", "证据", "影响", "修复建议", "工作量"];
    const lines = [
      header.join(","),
      ...filteredIssues.map((i) =>
        [i.issue_id, i.level, i.item_id, i.title, i.evidence_text, i.impact, i.suggestion, i.effort]
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
              setTarget("_summary");
            }}
          >
            {runs.length === 0 && <option value="">暂无批次</option>}
            {runs.map((r) => (
              <option key={`${r.date}/${r.run_id}`} value={`${r.date}/${r.run_id}`}>
                {r.date} · {r.run_id} ({r.ok_count}/{r.total_projects})
              </option>
            ))}
          </select>

          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="_summary">跨项目汇总报告 (_summary.md)</option>
            {okProjects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.project_name || p.project_id} ({p.project_id})
              </option>
            ))}
          </select>

          <button className="btn" onClick={handleCopyLink} title="复制当前报告直达链接">
            {copied ? <Check size={14} /> : <Link2 size={14} />} {copied ? "已复制" : "复制链接"}
          </button>

          {report && (
            <>
              <button className="btn" onClick={handleDownloadMd}>
                <Download size={14} /> 下载 .md
              </button>
              {report.type === "project" && (report.issues?.length ?? 0) > 0 && (
                <button className="btn" onClick={handleExportIssuesCsv}>
                  <Download size={14} /> 导出问题 CSV
                </button>
              )}
              <button className="btn" onClick={() => window.print()}>
                <Printer size={14} /> 打印 / PDF
              </button>
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
              P0 {report.issue_counts?.P0 ?? 0} / P1 {report.issue_counts?.P1 ?? 0} / P2{" "}
              {report.issue_counts?.P2 ?? 0} / P3 {report.issue_counts?.P3 ?? 0}
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
          <div className="filters no-print">
            <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}>
              <option value="">全部等级</option>
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
            <input
              placeholder="搜索问题标题 / 检查项 / 文件路径 / 建议"
              value={issueQuery}
              onChange={(e) => setIssueQuery(e.target.value)}
              style={{ minWidth: 280 }}
            />
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>等级</th>
                  <th>检查项</th>
                  <th>问题标题</th>
                  <th>证据</th>
                  <th>修复建议</th>
                  <th>工作量</th>
                </tr>
              </thead>
              <tbody>
                {filteredIssues.map((i) => (
                  <tr key={`${i.issue_id}-${i.item_id}`}>
                    <td>{i.issue_id}</td>
                    <td>
                      <span className={`lv-badge lv-${i.level}`}>{i.level}</span>
                    </td>
                    <td>{i.item_id}</td>
                    <td style={{ whiteSpace: "normal", minWidth: 200 }}>{i.title}</td>
                    <td style={{ whiteSpace: "normal", minWidth: 180 }} className="sub">
                      {i.evidence_text || "—"}
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 240 }}>{i.suggestion || "—"}</td>
                    <td>{i.effort || "—"}</td>
                  </tr>
                ))}
                {filteredIssues.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted" style={{ textAlign: "center", padding: 24 }}>
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
              <div className="markdown-body" dangerouslySetInnerHTML={{ __html: html }} />
            </div>
          </div>
        )
      )}
    </div>
  );
}
