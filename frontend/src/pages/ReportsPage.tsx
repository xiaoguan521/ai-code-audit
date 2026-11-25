import { useEffect, useMemo, useState } from "react";
import { Marked, Renderer } from "marked";
import { api } from "../api";
import type { ReportResponse, RunDetailResponse, RunSummary } from "../types";

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

function createSafeMarked(): Marked {
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
  return new Marked({ gfm: true, breaks: false, renderer });
}

const safeMarked = createSafeMarked();

interface Props {
  initialTarget: { date: string; runId: string; target: string } | null;
}

export function ReportsPage({ initialTarget }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [date, setDate] = useState(initialTarget?.date || "");
  const [runId, setRunId] = useState(initialTarget?.runId || "");
  const [target, setTarget] = useState(initialTarget?.target || "_summary");
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [levelFilter, setLevelFilter] = useState("");
  const [issueQuery, setIssueQuery] = useState("");
  const [tab, setTab] = useState<"markdown" | "issues">("markdown");

  useEffect(() => {
    if (initialTarget) {
      setDate(initialTarget.date);
      setRunId(initialTarget.runId);
      setTarget(initialTarget.target);
    }
  }, [initialTarget]);

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

  const html = useMemo(() => {
    if (!report?.markdown) return "";
    return safeMarked.parse(report.markdown) as string;
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

  return (
    <div>
      <div className="card" style={{ marginBottom: 14 }}>
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
          <div className="filters">
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
          <div className="card">
            <div className="markdown-body" dangerouslySetInnerHTML={{ __html: html }} />
          </div>
        )
      )}
    </div>
  );
}
