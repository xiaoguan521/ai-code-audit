import { useEffect, useState } from "react";
import { FileText, RefreshCw } from "lucide-react";
import { api } from "../api";
import type { RunDetailResponse, RunSummary } from "../types";

interface Props {
  onOpenReport: (date: string, runId: string, target: string) => void;
}

const STATUS_COLOR: Record<string, string> = {
  complete: "var(--good)",
  ok: "var(--good)",
  partial_failed: "var(--warning)",
  running: "var(--accent)",
  failed: "var(--critical)",
  error: "var(--critical)",
};

export function RunsPage({ onOpenReport }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<{ date: string; run_id: string } | null>(null);
  const [detail, setDetail] = useState<RunDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
                onClick={() => setSelected({ date: r.date, run_id: r.run_id })}
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
            <div className="card-title">
              <span>
                批次详情：{detail.date} / {detail.run_id}
              </span>
              {detail.has_summary && (
                <button
                  className="btn btn-primary"
                  onClick={() => onOpenReport(detail.date, detail.run_id, "_summary")}
                >
                  <FileText size={14} /> 查看整体汇总 (_summary.md)
                </button>
              )}
            </div>
            <div className="sub" style={{ marginBottom: 14 }}>
              状态：<b>{detail.status}</b> · 模式：{detail.report_mode || "—"} · 开始：
              {detail.started_at?.slice(0, 19).replace("T", " ") || "—"} · 结束：
              {detail.finished_at?.slice(0, 19).replace("T", " ") || "—"} · 汇总状态：
              {detail.summary_status || "—"}
            </div>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>项目</th>
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
                  {detail.projects.map((p) => {
                    const stages = p.metrics?.stages_seconds || {};
                    const totalSec = Object.values(stages).reduce((a, b) => a + b, 0);
                    return (
                      <tr key={p.project_id}>
                        <td>
                          <b>{p.project_name || p.project_id}</b>
                          <div className="muted" style={{ fontSize: 12 }}>
                            {p.project_id}
                          </div>
                        </td>
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
                        <td style={{ whiteSpace: "normal", maxWidth: 260 }}>
                          {p.error ? (
                            <span style={{ color: "var(--critical)", fontSize: 12 }}>{p.error}</span>
                          ) : totalSec > 0 ? (
                            <span className="muted" style={{ fontSize: 12 }}>
                              {totalSec.toFixed(1)}s
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>
                          {p.status === "ok" && (
                            <button
                              className="btn"
                              onClick={() => onOpenReport(detail.date, detail.run_id, p.project_id)}
                            >
                              阅读报告
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
