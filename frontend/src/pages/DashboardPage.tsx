import { useMemo, useState } from "react";
import { Download } from "lucide-react";
import type { OverviewResponse, ProjectRow } from "../types";

function csvCell(v: string | number | null | undefined): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

interface Props {
  data: OverviewResponse | null;
  loading: boolean;
  onOpenReport: (date: string, runId: string, projectId: string) => void;
}

const GRADE_COLOR: Record<string, string> = {
  良好: "var(--good)",
  基本合格需改进: "var(--warning)",
  存在明显风险: "var(--serious)",
  "高风险，建议专项整改": "var(--critical)",
  扫描失败: "var(--critical)",
};

const fmt1 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const fmtPct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);

export function DashboardPage({ data, loading, onOpenReport }: Props) {
  const [dept, setDept] = useState("");
  const [grade, setGrade] = useState("");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<keyof ProjectRow>("total");
  const [sortAsc, setSortAsc] = useState(true);
  const [viewMode, setViewMode] = useState<"projects" | "departments">("projects");
  const [tip, setTip] = useState<{ x: number; y: number; html: string } | null>(null);

  const rows = data?.rows ?? [];
  const depts = useMemo(() => [...new Set(rows.map((r) => r.department).filter(Boolean))], [rows]);
  const grades = useMemo(() => [...new Set(rows.map((r) => r.grade).filter(Boolean))], [rows]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows
      .filter(
        (r) =>
          (!dept || r.department === dept) &&
          (!grade || r.grade === grade) &&
          (!q || `${r.project_name} ${r.project_id} ${r.owner} ${r.tech_stack}`.toLowerCase().includes(q))
      )
      .sort((a, b) => {
        const av = a[sortKey];
        const bv = b[sortKey];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        return (av > bv ? 1 : av < bv ? -1 : 0) * (sortAsc ? 1 : -1);
      });
  }, [rows, dept, grade, search, sortKey, sortAsc]);

  const kpis = useMemo(() => {
    const scored = filtered.filter((r) => r.total != null);
    const secScored = filtered.filter((r) => r.security != null);
    const avg = (arr: number[]) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
    const sum = (k: "P0" | "P1" | "security_p0") => filtered.reduce((a, r) => a + (r[k] || 0), 0);
    const failCount = filtered.filter((r) => r.status === "error").length;
    return {
      totalCount: filtered.length,
      failCount,
      avgTotal: avg(scored.map((r) => r.total as number)),
      avgSec: avg(secScored.map((r) => r.security as number)),
      p0: sum("P0"),
      p1: sum("P1"),
      secP0: sum("security_p0"),
    };
  }, [filtered]);

  const deptSummary = useMemo(() => {
    const map = new Map<string, ProjectRow[]>();
    for (const r of rows) {
      const key = (r.department || "").trim() || "未分组";
      const list = map.get(key);
      if (list) list.push(r);
      else map.set(key, [r]);
    }
    return [...map.entries()]
      .map(([department, list]) => {
        const totals = list.map((x) => x.total).filter((v): v is number => v != null);
        const secs = list.map((x) => x.security).filter((v): v is number => v != null);
        return {
          department,
          count: list.length,
          scored: totals.length,
          avgTotal: totals.length ? totals.reduce((a, b) => a + b, 0) / totals.length : null,
          avgSec: secs.length ? secs.reduce((a, b) => a + b, 0) / secs.length : null,
          p0: list.reduce((a, x) => a + (x.P0 || 0), 0),
          p1: list.reduce((a, x) => a + (x.P1 || 0), 0),
          failed: list.filter((x) => x.status === "error").length,
        };
      })
      .sort((a, b) => {
        if (a.avgTotal == null && b.avgTotal == null) return b.count - a.count;
        if (a.avgTotal == null) return 1;
        if (b.avgTotal == null) return -1;
        return a.avgTotal - b.avgTotal;
      });
  }, [rows]);

  const handleExportCsv = () => {
    const header = [
      "项目ID", "项目名称", "部门", "负责人", "总评分", "较上次", "档位",
      "安全评分", "覆盖率", "P0", "P1", "P2", "P3", "最近日期", "提交",
    ];
    const lines = [
      header.join(","),
      ...filtered.map((r) =>
        [
          r.project_id, r.project_name, r.department, r.owner,
          r.total ?? "", r.delta_total ?? "", r.grade,
          r.security ?? "", r.coverage != null ? `${Math.round(r.coverage * 100)}%` : "",
          r.P0, r.P1, r.P2, r.P3, r.date || "", r.commit || "",
        ].map(csvCell).join(",")
      ),
    ];
    const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit_dashboard_${data?.latest_run?.date || "latest"}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const toggleSort = (k: keyof ProjectRow) => {
    if (sortKey === k) setSortAsc(!sortAsc);
    else {
      setSortKey(k);
      setSortAsc(k === "project_name" || k === "department");
    }
  };

  if (loading && !data) {
    return <div className="card muted">正在加载质量看板数据…</div>;
  }

  const trend = data?.trend ?? [];
  const dims = data?.dims ?? [];

  return (
    <div>
      <div className="tiles">
        <div className="card">
          <div className="tile-label">纳入项目数</div>
          <div className="tile-value">{kpis.totalCount}</div>
          <div className="tile-note">
            {kpis.failCount ? `其中最近扫描失败 ${kpis.failCount} 个` : "最近批次运行正常"}
          </div>
        </div>
        <div className="card">
          <div className="tile-label">平均总评分</div>
          <div className="tile-value">{fmt1(kpis.avgTotal)}</div>
          <div className="tile-note">0–10 分制，基于已评分维度</div>
        </div>
        <div className="card">
          <div className="tile-label">平均安全评分</div>
          <div className="tile-value">{fmt1(kpis.avgSec)}</div>
          <div className="tile-note">安全性维度均分</div>
        </div>
        <div className="card">
          <div className="tile-label">P0 致命问题</div>
          <div className="tile-value" style={{ color: kpis.p0 > 0 ? "var(--critical)" : undefined }}>
            {kpis.p0}
          </div>
          <div className="tile-note">其中安全类 {kpis.secP0} 个</div>
        </div>
        <div className="card">
          <div className="tile-label">P1 严重问题</div>
          <div className="tile-value" style={{ color: kpis.p1 > 0 ? "var(--serious)" : undefined }}>
            {kpis.p1}
          </div>
          <div className="tile-note">
            {data?.latest_run
              ? `最新批次 ${data.latest_run.date} (${data.latest_run.run_id})`
              : "暂无批次记录"}
          </div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-title">
            <span>平均分历史趋势（0–10）</span>
            <span className="sub">
              <span style={{ color: "var(--series-1)", marginRight: 12 }}>● 平均总评分</span>
              <span style={{ color: "var(--series-2)" }}>● 平均安全评分</span>
            </span>
          </div>
          {trend.length === 0 ? (
            <div className="muted" style={{ padding: "48px 0", textAlign: "center" }}>
              暂无历史评分趋势数据（完成扫描且报告通过校验后自动展示）
            </div>
          ) : (
            <svg width="100%" viewBox="0 0 600 210" style={{ display: "block" }}>
              {[0, 2, 4, 6, 8, 10].map((v) => {
                const y = 12 + ((10 - v) * 165) / 10;
                return (
                  <g key={v}>
                    <line x1={34} x2={560} y1={y} y2={y} stroke="var(--grid)" />
                    <text x={28} y={y + 4} textAnchor="end" fill="var(--text-3)" fontSize={11}>
                      {v}
                    </text>
                  </g>
                );
              })}
              {(() => {
                const xAt = (i: number) =>
                  34 + (trend.length === 1 ? 260 : (i * 526) / (trend.length - 1));
                const yAt = (v: number) => 12 + ((10 - v) * 165) / 10;
                const totalPts = trend
                  .map((p, i) => (p.total == null ? null : `${xAt(i)},${yAt(p.total)}`))
                  .filter(Boolean)
                  .join(" ");
                const secPts = trend
                  .map((p, i) => (p.security == null ? null : `${xAt(i)},${yAt(p.security)}`))
                  .filter(Boolean)
                  .join(" ");
                return (
                  <>
                    {totalPts && (
                      <polyline fill="none" stroke="var(--series-1)" strokeWidth={2.2} points={totalPts} />
                    )}
                    {secPts && (
                      <polyline fill="none" stroke="var(--series-2)" strokeWidth={2.2} points={secPts} />
                    )}
                    {(() => {
                      const labelStep = Math.max(1, Math.ceil(trend.length / 8));
                      return trend.map((p, i) => (
                        <g key={`${p.date}-${p.run_id}`}>
                          {(i % labelStep === 0 || i === trend.length - 1) && (
                            <text x={xAt(i)} y={198} textAnchor="middle" fill="var(--text-3)" fontSize={11}>
                              {p.date.slice(5)}
                            </text>
                          )}
                          {p.total != null && (
                            <circle
                              cx={xAt(i)}
                              cy={yAt(p.total)}
                              r={4}
                              fill="var(--series-1)"
                              style={{ cursor: "pointer" }}
                              onMouseMove={(e) =>
                                setTip({
                                  x: e.clientX,
                                  y: e.clientY,
                                  html: `${p.date} · 总分 ${fmt1(p.total)} · 安全 ${fmt1(p.security)}`,
                                })
                              }
                              onMouseLeave={() => setTip(null)}
                            />
                          )}
                        </g>
                      ));
                    })()}
                  </>
                );
              })()}
            </svg>
          )}
        </div>

        <div className="card">
          <div className="card-title">八个维度项目均分</div>
          {dims.map((d) => (
            <div className="dim-row" key={d.id}>
              <span title={d.name}>
                {d.id}. {d.name}
              </span>
              <div className="dim-bar-track">
                <div
                  className="dim-bar-fill"
                  style={{ width: `${d.avg == null ? 0 : Math.min(100, d.avg * 10)}%` }}
                />
              </div>
              <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {d.avg == null ? "—" : fmt1(d.avg)}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-title" style={{ flexWrap: "wrap" }}>
          <span>
            {viewMode === "projects"
              ? "项目质量排行（点击行查看报告）"
              : "部门质量汇总（点击行下钻该项目列表）"}
          </span>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <button
              className={`btn ${viewMode === "projects" ? "btn-primary" : ""}`}
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => setViewMode("projects")}
            >
              项目排行 ({filtered.length})
            </button>
            <button
              className={`btn ${viewMode === "departments" ? "btn-primary" : ""}`}
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => setViewMode("departments")}
            >
              按部门汇总 ({deptSummary.length})
            </button>
            <button
              className="btn"
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={handleExportCsv}
              disabled={filtered.length === 0}
            >
              <Download size={13} /> 导出 CSV
            </button>
          </div>
        </div>

        {viewMode === "departments" ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>部门</th>
                  <th className="num">项目数</th>
                  <th className="num">已评分</th>
                  <th className="num">平均总评分</th>
                  <th className="num">平均安全分</th>
                  <th className="num">P0</th>
                  <th className="num">P1</th>
                  <th className="num">最近失败</th>
                </tr>
              </thead>
              <tbody>
                {deptSummary.map((d) => (
                  <tr
                    key={d.department}
                    className="clickable"
                    onClick={() => {
                      setDept(d.department === "未分组" ? "" : d.department);
                      setViewMode("projects");
                    }}
                  >
                    <td>
                      <b>{d.department}</b>
                    </td>
                    <td className="num">{d.count}</td>
                    <td className="num">{d.scored}</td>
                    <td className="num">
                      <b>{fmt1(d.avgTotal)}</b>
                    </td>
                    <td className="num">
                      <b>{fmt1(d.avgSec)}</b>
                    </td>
                    <td className="num" style={{ color: d.p0 > 0 ? "var(--critical)" : undefined }}>
                      {d.p0}
                    </td>
                    <td className="num" style={{ color: d.p1 > 0 ? "var(--serious)" : undefined }}>
                      {d.p1}
                    </td>
                    <td className="num" style={{ color: d.failed > 0 ? "var(--critical)" : undefined }}>
                      {d.failed}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <>
            <div className="filters">
          <select value={dept} onChange={(e) => setDept(e.target.value)}>
            <option value="">全部部门</option>
            {depts.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <select value={grade} onChange={(e) => setGrade(e.target.value)}>
            <option value="">全部档位</option>
            {grades.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
          <input
            placeholder="搜索项目名 / ID / 负责人 / 技术栈"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ minWidth: 240 }}
          />
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="sortable" onClick={() => toggleSort("project_name")}>
                  项目
                </th>
                <th className="sortable" onClick={() => toggleSort("department")}>
                  部门
                </th>
                <th>负责人</th>
                <th className="num sortable" onClick={() => toggleSort("total")}>
                  总评分
                </th>
                <th className="num sortable" onClick={() => toggleSort("delta_total")}>
                  较上次
                </th>
                <th>档位</th>
                <th className="num sortable" onClick={() => toggleSort("security")}>
                  安全评分
                </th>
                <th className="num sortable" onClick={() => toggleSort("coverage")}>
                  覆盖率
                </th>
                <th className="num sortable" onClick={() => toggleSort("P0")}>
                  P0
                </th>
                <th className="num sortable" onClick={() => toggleSort("P1")}>
                  P1
                </th>
                <th className="num sortable" onClick={() => toggleSort("P2")}>
                  P2
                </th>
                <th className="num sortable" onClick={() => toggleSort("P3")}>
                  P3
                </th>
                <th>最近日期 / 提交</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => {
                const canOpen = Boolean(r.date && r.run_id && r.status === "ok");
                return (
                  <tr
                    key={r.project_id}
                    className={canOpen ? "clickable" : undefined}
                    onClick={() => canOpen && r.date && r.run_id && onOpenReport(r.date, r.run_id, r.project_id)}
                  >
                    <td>
                      <div style={{ fontWeight: 600 }}>{r.project_name}</div>
                      <div className="muted" style={{ fontSize: 12 }}>
                        {r.project_id}
                      </div>
                    </td>
                    <td>{r.department || "—"}</td>
                    <td>{r.owner || "—"}</td>
                    <td className="num">
                      <b>{fmt1(r.total)}</b>
                    </td>
                    <td className="num" title={r.prev_date ? `对比上次 ${r.prev_date}` : "暂无上次记录"}>
                      {r.delta_total == null ? (
                        <span className="muted">—</span>
                      ) : r.delta_total > 0 ? (
                        <span className="delta-up">▲ {r.delta_total.toFixed(1)}</span>
                      ) : r.delta_total < 0 ? (
                        <span className="delta-down">▼ {Math.abs(r.delta_total).toFixed(1)}</span>
                      ) : (
                        <span className="muted">0.0</span>
                      )}
                    </td>
                    <td>
                      <span className="chip">
                        <span
                          className="chip-dot"
                          style={{ background: GRADE_COLOR[r.grade] || "var(--neutral)" }}
                        />
                        {r.grade}
                      </span>
                    </td>
                    <td className="num">
                      <b>{fmt1(r.security)}</b>
                    </td>
                    <td className="num">{fmtPct(r.coverage)}</td>
                    <td className="num">{r.P0}</td>
                    <td className="num">{r.P1}</td>
                    <td className="num">{r.P2}</td>
                    <td className="num">{r.P3}</td>
                    <td>
                      <div>{r.date || "—"}</div>
                      <div className="muted" style={{ fontSize: 12 }}>
                        {r.commit || "—"}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={13} className="muted" style={{ textAlign: "center", padding: 24 }}>
                    没有符合条件的项目
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
          </>
        )}
      </div>

      {tip && (
        <div className="tooltip-float" style={{ left: tip.x + 12, top: tip.y + 12 }}>
          {tip.html}
        </div>
      )}
    </div>
  );
}
