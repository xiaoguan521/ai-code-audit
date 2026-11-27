import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Download,
  FolderGit2,
  GitBranch,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  Sliders,
  Trash2,
  Upload,
} from "lucide-react";
import { api } from "../api";
import type { AuthUser, CatalogProject, GitProbeResult, StaleThresholds } from "../types";

interface Props {
  currentUser: AuthUser;
}

const EMPTY_FORM: Partial<CatalogProject> = {
  project_id: "",
  project_name: "",
  department: "",
  owner: "",
  vcs_type: "git",
  git_url: "",
  branch: "main",
  status: "在研",
  tech_stack: "",
  project_type: "",
  core_flows: "",
  scope_out: "",
  materials_dir: "",
  weights: "",
  notes: "",
};

function looksLikeSvnUrl(raw: string): boolean {
  const url = (raw || "").trim().toLowerCase();
  if (url.startsWith("svn://") || url.startsWith("svn+ssh://")) return true;
  if (url.startsWith("http://") || url.startsWith("https://")) {
    try {
      const u = new URL(url);
      if (u.hostname.startsWith("svn.") || u.pathname.includes("/svn/") || u.pathname === "/svn") return true;
    } catch {
      return false;
    }
  }
  return false;
}

function formatLoc(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万行`;
  return `${n.toLocaleString()} 行`;
}

function topLangs(langs?: Record<string, number>): string {
  if (!langs) return "";
  const entries = Object.entries(langs)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  return entries.map(([k]) => k).join(" / ");
}

export function ProjectsPage({ currentUser }: Props) {
  const isAdmin = currentUser.role === "superadmin" || currentUser.role === "admin";
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [thresholds, setThresholds] = useState<StaleThresholds>({ warn_days: 90, critical_days: 180 });
  const [owners, setOwners] = useState<Array<{ display_name: string; username: string; email: string }>>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 筛选条件
  const [statusFilter, setStatusFilter] = useState("");
  const [deptFilter, setDeptFilter] = useState("");
  const [staleFilter, setStaleFilter] = useState("");
  const [gitHealthFilter, setGitHealthFilter] = useState("");
  const [query, setQuery] = useState("");

  // 新增/编辑弹窗
  const [modalMode, setModalMode] = useState<"create" | "edit" | null>(null);
  const [form, setForm] = useState<Partial<CatalogProject>>(EMPTY_FORM);
  const [probing, setProbing] = useState(false);
  const [probingAll, setProbingAll] = useState(false);
  const [probeRes, setProbeRes] = useState<GitProbeResult | null>(null);
  const [branchDropdownOpen, setBranchDropdownOpen] = useState(false);
  const [branchSearch, setBranchSearch] = useState("");

  // CSV 导入弹窗
  const [showImport, setShowImport] = useState(false);
  const [csvText, setCsvText] = useState("");
  const [importMode, setImportMode] = useState<"upsert" | "replace">("upsert");

  // 预警阈值弹窗
  const [showThresholdModal, setShowThresholdModal] = useState(false);
  const [warnDaysInput, setWarnDaysInput] = useState("90");
  const [critDaysInput, setCritDaysInput] = useState("180");

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.projects();
      setProjects(res.projects);
      if (res.stale_thresholds) {
        setThresholds(res.stale_thresholds);
        setWarnDaysInput(String(res.stale_thresholds.warn_days));
        setCritDaysInput(String(res.stale_thresholds.critical_days));
      }
      if (isAdmin) {
        const own = await api.ownersList().catch(() => ({ users: [] }));
        setOwners(own.users);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载项目清单失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const departments = useMemo(
    () => [...new Set(projects.map((p) => (p.department || "").trim()).filter(Boolean))].sort(),
    [projects]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return projects.filter((p) => {
      if (statusFilter && p.status !== statusFilter) return false;
      if (deptFilter && (p.department || "").trim() !== deptFilter) return false;
      if (staleFilter === "stale" && p.stale_level !== "warning" && p.stale_level !== "critical") return false;
      if (staleFilter && staleFilter !== "stale" && p.stale_level !== staleFilter) return false;
      if (gitHealthFilter && (p.git_health || "unknown") !== gitHealthFilter) return false;
      if (
        q &&
        !`${p.project_id} ${p.project_name} ${p.department} ${p.owner} ${p.git_url} ${p.branch} ${p.tech_stack} ${p.git_health_reason || ""}`
          .toLowerCase()
          .includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [projects, statusFilter, deptFilter, staleFilter, gitHealthFilter, query]);

  const stats = useMemo(() => {
    const activeScan = projects.filter((p) => p.status === "在研").length;
    const totalLoc = projects.reduce((acc, p) => acc + (p.loc_total || 0), 0);
    const critStale = projects.filter((p) => p.stale_level === "critical").length;
    const warnStale = projects.filter((p) => p.stale_level === "warning").length;
    const gitErrorCount = projects.filter((p) => p.git_health === "error").length;
    return { total: projects.length, activeScan, totalLoc, critStale, warnStale, gitErrorCount };
  }, [projects]);

  const filteredRemoteBranches = useMemo(() => {
    const all = probeRes?.ok ? probeRes.branches : [];
    const q = branchSearch.trim().toLowerCase();
    if (!q) return all;
    return all.filter((b) => b.toLowerCase().includes(q));
  }, [probeRes, branchSearch]);

  const openCreate = () => {
    setForm({ ...EMPTY_FORM });
    setProbeRes(null);
    setBranchDropdownOpen(false);
    setBranchSearch("");
    setModalMode("create");
  };

  const openEdit = (p: CatalogProject) => {
    setForm({ ...p });
    setProbeRes(null);
    setBranchDropdownOpen(false);
    setBranchSearch("");
    setModalMode("edit");
  };

  const handleProbeGit = async () => {
    if (!form.git_url?.trim()) {
      setError("请先填写仓库地址");
      return;
    }
    setProbing(true);
    setError(null);
    try {
      const res = await api.probeGit(
        form.git_url.trim(),
        (form.branch || "").trim(),
        modalMode === "edit" ? (form.project_id || "") : "",
        form.vcs_type || "git"
      );
      setProbeRes(res);
      setBranchSearch("");
      if (res.ok && res.branches.length > 0) {
        setBranchDropdownOpen(true);
        if (!form.branch?.trim() && res.default_branch && res.vcs_type !== "svn") {
          setForm((prev) => ({ ...prev, branch: res.default_branch }));
        }
      }
      if (modalMode === "edit") {
        void loadData();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "仓库探测失败");
    } finally {
      setProbing(false);
    }
  };

  const handleProbeAllGit = async () => {
    setProbingAll(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.probeAllProjectsGit(true);
      await loadData();
      if (res.failed > 0) {
        setGitHealthFilter("error");
        setNotice(`批量 Git 校验完成（共 ${res.total} 个在研项目）：正常 ${res.ok} 个，发现异常 ${res.failed} 个，已自动筛选出异常项目`);
      } else {
        setNotice(`批量 Git 校验完成（共 ${res.total} 个在研项目）：全部 ${res.ok} 个地址与分支均正常`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "批量校验 Git 失败");
    } finally {
      setProbingAll(false);
    }
  };

  const handleSaveProject = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (modalMode === "create") {
        await api.createProject(form);
        setNotice(`已新增项目 ${form.project_id}`);
      } else if (modalMode === "edit" && form.project_id) {
        await api.updateProject(form.project_id, form);
        setNotice(`已更新项目 ${form.project_id}`);
      }
      setModalMode(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存项目失败");
    } finally {
      setBusy(false);
    }
  };

  const handleToggleStatus = async (p: CatalogProject) => {
    const nextStatus = p.status === "在研" ? "停用" : "在研";
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.updateProject(p.project_id, { status: nextStatus });
      setNotice(`项目 ${p.project_name} 已切换为「${nextStatus}」`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新状态失败");
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (p: CatalogProject) => {
    if (!window.confirm(`确定移除项目 ${p.project_name}（${p.project_id}）吗？\n移除后将不再参与后续扫描（历史批次报告保留）。`)) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.deleteProject(p.project_id);
      setNotice(`已移除项目 ${p.project_id}`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "移除项目失败");
    } finally {
      setBusy(false);
    }
  };

  const handleRefreshMetrics = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.refreshProjectMetrics();
      setNotice(`代码量与最后提交刷新完成：更新 ${res.updated}/${res.scanned} 个已克隆仓库（未克隆 ${res.missing_repo} 个）`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "刷新统计失败");
    } finally {
      setBusy(false);
    }
  };

  const handleImportCsv = async (e: FormEvent) => {
    e.preventDefault();
    if (!csvText.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.importProjectsCsv(csvText, importMode);
      setShowImport(false);
      setCsvText("");
      setNotice(`CSV 导入完成：新增 ${res.created} 个、更新 ${res.updated} 个${res.errors.length ? `，失败 ${res.errors.length} 行` : ""}`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "CSV 导入失败");
    } finally {
      setBusy(false);
    }
  };

  const handleSaveThresholds = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveStaleThresholds(Number(warnDaysInput), Number(critDaysInput));
      setThresholds(res.stale_thresholds);
      setShowThresholdModal(false);
      setNotice(`预警阈值已更新：黄色 ≥ ${res.stale_thresholds.warn_days} 天，红色 ≥ ${res.stale_thresholds.critical_days} 天`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存阈值失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {error && (
        <div className="card" style={{ borderColor: "var(--critical)", color: "var(--critical)" }}>
          {error}
        </div>
      )}
      {notice && (
        <div className="card" style={{ borderColor: "var(--good)", color: "var(--good)" }}>
          {notice}
        </div>
      )}

      {/* 顶部统计指标 */}
      <div className="tiles">
        <div className="card">
          <div className="tile-label">{isAdmin ? "纳入管理项目" : "我负责的项目"}</div>
          <div className="tile-value">{stats.total}</div>
          <div className="tile-note">其中在研参与扫描 {stats.activeScan} 个</div>
        </div>
        <div className="card">
          <div className="tile-label">已统计代码总量</div>
          <div className="tile-value">{stats.totalLoc > 0 ? formatLoc(stats.totalLoc) : "—"}</div>
          <div className="tile-note">来自扫描与本地仓库代码行数统计</div>
        </div>
        <div className="card">
          <div className="tile-label">严重长期未维护（≥{thresholds.critical_days}天）</div>
          <div className="tile-value" style={{ color: stats.critStale > 0 ? "var(--critical)" : undefined }}>
            {stats.critStale}
          </div>
          <div className="tile-note">建议评估归档或排查是否无人维护</div>
        </div>
        <div className="card">
          <div className="tile-label">久未更新预警（≥{thresholds.warn_days}天）</div>
          <div className="tile-value" style={{ color: stats.warnStale > 0 ? "var(--warning)" : undefined }}>
            {stats.warnStale}
          </div>
          <div className="tile-note">按 Git 最近一次代码提交时间自动判定</div>
        </div>
        <div
          className="card"
          style={{
            cursor: "pointer",
            borderColor: gitHealthFilter === "error" ? "var(--critical)" : undefined,
          }}
          title="点击筛选地址或分支异常的项目"
          onClick={() => setGitHealthFilter((v) => (v === "error" ? "" : "error"))}
        >
          <div className="tile-label">地址 / 分支异常</div>
          <div className="tile-value" style={{ color: stats.gitErrorCount > 0 ? "var(--critical)" : undefined }}>
            {stats.gitErrorCount}
          </div>
          <div className="tile-note">点击一键筛选 · 支持下方批量校验</div>
        </div>
      </div>

      {/* 主表 */}
      <div className="card">
        <div className="card-title" style={{ flexWrap: "wrap" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <FolderGit2 size={15} /> {isAdmin ? "扫描项目管理与代码活跃度" : "我的项目代码量与活跃度"} ({filtered.length}/{projects.length})
          </span>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {isAdmin && (
              <>
                <button type="button" className="btn btn-primary" onClick={openCreate}>
                  <Plus size={14} /> 新增项目
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={probingAll || busy}
                  title="并发执行 git ls-remote 批量检查所有在研项目的仓库连通性与目标分支是否存在"
                  onClick={() => void handleProbeAllGit()}
                >
                  <ShieldAlert size={14} /> {probingAll ? "批量校验中…" : "批量校验 Git"}
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => void handleRefreshMetrics()}>
                  <RefreshCw size={14} /> 刷新代码量与提交时间
                </button>
                <button type="button" className="btn" onClick={() => setShowThresholdModal(true)}>
                  <Sliders size={14} /> 预警阈值 ({thresholds.warn_days}/{thresholds.critical_days}天)
                </button>
                <button type="button" className="btn" onClick={() => setShowImport(true)}>
                  <Upload size={14} /> 导入 CSV
                </button>
                <button type="button" className="btn" onClick={() => void api.downloadProjectsCsv()}>
                  <Download size={14} /> 导出 CSV
                </button>
              </>
            )}
            <button type="button" className="btn" disabled={loading} onClick={() => void loadData()}>
              <RefreshCw size={14} /> 刷新
            </button>
          </div>
        </div>

        <div className="filters">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">全部状态</option>
            <option value="在研">在研（参与扫描）</option>
            <option value="停用">停用</option>
            <option value="归档">归档</option>
          </select>
          <select value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
            <option value="">全部部门</option>
            {departments.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <select value={staleFilter} onChange={(e) => setStaleFilter(e.target.value)}>
            <option value="">全部维护活跃度</option>
            <option value="stale">全部长期未维护预警</option>
            <option value="critical">严重停滞（≥{thresholds.critical_days}天）</option>
            <option value="warning">久未更新（≥{thresholds.warn_days}天）</option>
            <option value="active">维护活跃</option>
          </select>
          <select value={gitHealthFilter} onChange={(e) => setGitHealthFilter(e.target.value)}>
            <option value="">全部 Git 状态</option>
            <option value="error">地址 / 分支异常 ({stats.gitErrorCount})</option>
            <option value="ok">Git 地址与分支正常</option>
            <option value="unknown">尚未校验</option>
          </select>
          <input
            placeholder="搜索项目 ID / 名称 / 负责人 / Git 地址 / 分支 / 异常原因"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ minWidth: 240, flex: "1 1 240px", maxWidth: 360 }}
          />
        </div>

        <div className="table-wrap">
          <table className="projects-table">
            <thead>
              <tr>
                <th>项目</th>
                <th>部门 / 负责人</th>
                <th>仓库地址与分支</th>
                <th>状态</th>
                <th className="num">代码量 (LOC)</th>
                <th>最后代码提交 / 活跃度</th>
                {isAdmin && <th className="sticky-action-col">操作</th>}
              </tr>
            </thead>
            <tbody>
              {filtered.map((p) => {
                const langs = topLangs(p.languages);
                return (
                  <tr key={p.project_id} style={{ opacity: p.status === "在研" ? 1 : 0.6 }}>
                    <td style={{ whiteSpace: "normal", minWidth: 180, maxWidth: 240, wordBreak: "break-word" }}>
                      <div style={{ fontWeight: 600, lineHeight: 1.35 }}>{p.project_name}</div>
                      <div className="muted" style={{ fontSize: 11.5, marginTop: 2, wordBreak: "break-all" }}>
                        <code>{p.project_id}</code>
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 92, maxWidth: 130 }}>
                      <div>{p.department || "—"}</div>
                      <div className="muted" style={{ fontSize: 12 }}>
                        {p.owner || "未指定"}
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 190, maxWidth: 260 }}>
                      <div style={{ fontSize: 12, wordBreak: "break-all", lineHeight: 1.35 }}>{p.git_url || "—"}</div>
                      <div
                        style={{
                          fontSize: 11.5,
                          marginTop: 3,
                          display: "flex",
                          alignItems: "center",
                          flexWrap: "wrap",
                          gap: 6,
                        }}
                      >
                        {p.vcs_type === "svn" && (
                          <span
                            className="chip"
                            style={{
                              borderColor: "var(--accent)",
                              color: "var(--accent)",
                              fontSize: 10.5,
                              padding: "0 5px",
                            }}
                          >
                            SVN
                          </span>
                        )}
                        <span className="muted" style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                          <GitBranch size={11} /> {p.vcs_type === "svn" ? (p.branch || "直接目录") : (p.branch || "main")}
                        </span>
                        {p.git_health === "error" && (
                          <span
                            className="chip"
                            title={p.git_health_reason || "地址或分支异常"}
                            style={{
                              borderColor: "var(--critical)",
                              color: "var(--critical)",
                              fontSize: 11,
                              padding: "1px 6px",
                            }}
                          >
                            <AlertTriangle size={10} /> 地址/分支异常
                          </span>
                        )}
                      </div>
                      {p.git_health === "error" && p.git_health_reason && (
                        <div
                          style={{
                            fontSize: 11,
                            color: "var(--critical)",
                            marginTop: 2,
                            lineHeight: 1.35,
                            wordBreak: "break-word",
                          }}
                        >
                          {p.git_health_reason.slice(0, 90)}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className="chip">
                        <span
                          className="chip-dot"
                          style={{ background: p.status === "在研" ? "var(--good)" : "var(--neutral)" }}
                        />
                        {p.status}
                      </span>
                    </td>
                    <td className="num">
                      <div style={{ fontWeight: 600 }}>{formatLoc(p.loc_total)}</div>
                      {(p.file_count != null || langs) && (
                        <div className="muted" style={{ fontSize: 11 }}>
                          {p.file_count != null ? `${p.file_count} 文件` : ""}
                          {p.file_count != null && langs ? " · " : ""}
                          {langs}
                        </div>
                      )}
                    </td>
                    <td
                      title={
                        p.last_commit_subject
                          ? `${p.last_commit_author || ""}：${p.last_commit_subject}`
                          : undefined
                      }
                    >
                      {p.last_commit_at ? (
                        <div>
                          <div>{p.last_commit_at.slice(0, 10)}</div>
                          <div style={{ marginTop: 2 }}>
                            {p.stale_level === "critical" ? (
                              <span className="chip" style={{ borderColor: "var(--critical)", color: "var(--critical)" }}>
                                <AlertTriangle size={11} /> 停滞 {p.inactive_days} 天
                              </span>
                            ) : p.stale_level === "warning" ? (
                              <span className="chip" style={{ borderColor: "var(--warning)", color: "var(--warning)" }}>
                                <AlertTriangle size={11} /> {p.inactive_days} 天未更新
                              </span>
                            ) : (
                              <span className="muted" style={{ fontSize: 11.5 }}>
                                活跃（{p.inactive_days ?? 0} 天前）
                              </span>
                            )}
                          </div>
                        </div>
                      ) : (
                        <span className="muted">待采集</span>
                      )}
                    </td>
                    {isAdmin && (
                      <td className="sticky-action-col">
                        <div style={{ display: "flex", gap: 5, flexWrap: "nowrap" }}>
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 7px", fontSize: 12 }}
                            onClick={() => openEdit(p)}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 7px", fontSize: 12 }}
                            disabled={busy}
                            onClick={() => void handleToggleStatus(p)}
                          >
                            {p.status === "在研" ? "停用" : "启用"}
                          </button>
                          <button
                            type="button"
                            className="btn btn-danger"
                            style={{ padding: "3px 7px", fontSize: 12 }}
                            disabled={busy}
                            onClick={() => void handleDelete(p)}
                          >
                            <Trash2 size={12} /> 移除
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={isAdmin ? 7 : 6} className="muted" style={{ textAlign: "center", padding: 24 }}>
                    没有符合条件的项目
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 新增 / 编辑项目弹窗 */}
      {modalMode && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setModalMode(null)}
        >
          <form
            className="card"
            style={{ width: "min(640px, 96vw)", maxHeight: "90vh", overflowY: "auto", display: "grid", gap: 10 }}
            onClick={(e) => {
              e.stopPropagation();
              setBranchDropdownOpen(false);
            }}
            onSubmit={(e) => void handleSaveProject(e)}
          >
            <div className="card-title">
              <span>{modalMode === "create" ? "新增扫描项目" : `编辑项目：${form.project_id}`}</span>
              <button type="button" className="btn" onClick={() => setModalMode(null)}>
                关闭
              </button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">项目 ID（唯一英文标识）*</span>
                <input
                  value={form.project_id || ""}
                  disabled={modalMode === "edit"}
                  placeholder="例如 order-service"
                  onChange={(e) => setForm({ ...form, project_id: e.target.value })}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">项目名称 *</span>
                <input
                  value={form.project_name || ""}
                  placeholder="例如 订单中心"
                  onChange={(e) => setForm({ ...form, project_name: e.target.value })}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">所属部门</span>
                <input
                  value={form.department || ""}
                  placeholder="例如 研发一部"
                  onChange={(e) => setForm({ ...form, department: e.target.value })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">负责人（多负责人用顿号、分隔）</span>
                <input
                  list="owner-suggestions"
                  value={form.owner || ""}
                  placeholder="例如 王秀丽"
                  onChange={(e) => setForm({ ...form, owner: e.target.value })}
                />
                <datalist id="owner-suggestions">
                  {owners.map((u) => (
                    <option key={u.username} value={u.display_name}>
                      {u.username} ({u.email})
                    </option>
                  ))}
                </datalist>
              </label>
            </div>

            <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">
                {form.vcs_type === "svn"
                  ? "SVN 仓库地址（支持直接填源码目录，如 https://svn.sjgjj.cn/svn/.../4.Src）*"
                  : "Git / SVN 仓库地址（粘贴 SVN 地址将自动识别为 SVN）*"}
              </span>
              <div style={{ display: "flex", gap: 8 }}>
                <select
                  value={form.vcs_type || "git"}
                  style={{ width: 92, flexShrink: 0 }}
                  onChange={(e) => {
                    const nextVcs = e.target.value as "git" | "svn";
                    setProbeRes(null);
                    setBranchDropdownOpen(false);
                    setForm((prev) => ({
                      ...prev,
                      vcs_type: nextVcs,
                      branch:
                        nextVcs === "svn"
                          ? prev.branch === "main"
                            ? ""
                            : prev.branch
                          : prev.branch || "main",
                    }));
                  }}
                >
                  <option value="git">Git</option>
                  <option value="svn">SVN</option>
                </select>
                <input
                  style={{ flex: 1 }}
                  value={form.git_url || ""}
                  placeholder={
                    form.vcs_type === "svn"
                      ? "https://svn.sjgjj.cn/svn/CustomerProject/.../4.Src"
                      : "https://git.example.com/group/repo.git"
                  }
                  onChange={(e) => {
                    const val = e.target.value;
                    setForm((prev) => {
                      const autoSvn = looksLikeSvnUrl(val);
                      if (autoSvn && prev.vcs_type !== "svn") {
                        return {
                          ...prev,
                          git_url: val,
                          vcs_type: "svn",
                          branch: prev.branch === "main" ? "" : prev.branch,
                        };
                      }
                      return { ...prev, git_url: val };
                    });
                  }}
                  required
                />
                <button type="button" className="btn" disabled={probing} onClick={() => void handleProbeGit()}>
                  <GitBranch size={13} /> {probing ? "探测中…" : form.vcs_type === "svn" ? "测试连通" : "测试连通 & 分支"}
                </button>
              </div>
            </div>

            {probeRes && (
              <div
                style={{
                  fontSize: 12,
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: `1px solid ${probeRes.ok ? "var(--good)" : "var(--critical)"}`,
                  color: probeRes.ok ? "var(--good)" : "var(--critical)",
                }}
              >
                {probeRes.ok ? (
                  probeRes.vcs_type === "svn" ? (
                    <div>
                      SVN 连通正常！
                      {probeRes.revision ? <> 最新修订版 <code>{probeRes.revision}</code></> : null}
                      {probeRes.branches.length > 0 ? (
                        <> · 检测到 <b>{probeRes.branches.length}</b> 个标准分支子路径，可在下方下拉选择</>
                      ) : (
                        <> · 将直接检出该目录进行扫描</>
                      )}
                    </div>
                  ) : (
                    <div>
                      连通正常！检测到 <b>{probeRes.branches.length}</b> 个远端分支（默认：<code>{probeRes.default_branch || "—"}</code>）
                      {form.branch && !probeRes.branches.includes(form.branch.trim()) ? (
                        <span style={{ color: "var(--warning)", marginLeft: 8 }}>
                          注意：当前所填分支「{form.branch}」不在远端分支列表中，可在下方下拉搜索选择
                        </span>
                      ) : form.branch ? (
                        <span style={{ marginLeft: 8 }}>· 当前分支「{form.branch}」存在 ✓</span>
                      ) : null}
                    </div>
                  )
                ) : (
                  <div>连通失败：{probeRes.error}</div>
                )}
              </div>
            )}

            <div style={{ display: "grid", gridTemplateColumns: "1.35fr 1fr 1fr", gap: 10 }}>
              <div
                style={{ display: "grid", gap: 4, fontSize: 12, position: "relative" }}
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setBranchDropdownOpen(false);
                }}
              >
                <span className="muted">
                  {form.vcs_type === "svn" ? "SVN 子路径（可选，留空直接检出上方目录）" : "扫描分支 *"}
                  {probeRes?.ok && probeRes.branches.length > 0 ? `（支持下拉/模糊查，共 ${probeRes.branches.length} 个）` : ""}
                </span>
                <div style={{ display: "flex", position: "relative", alignItems: "center" }}>
                  <input
                    style={{ width: "100%", paddingRight: probeRes?.ok && probeRes.branches.length > 0 ? 30 : 10 }}
                    value={form.branch || ""}
                    placeholder={form.vcs_type === "svn" ? "留空直接检出上方地址，或填 trunk / branches/xxx" : "输入或选择扫描分支"}
                    onFocus={() => {
                      if (probeRes?.ok && probeRes.branches.length > 0) setBranchDropdownOpen(true);
                    }}
                    onChange={(e) => {
                      const val = e.target.value;
                      setForm({ ...form, branch: val });
                      if (probeRes?.ok && probeRes.branches.length > 0) {
                        setBranchSearch(val);
                        setBranchDropdownOpen(true);
                      }
                    }}
                    required={form.vcs_type !== "svn"}
                  />
                  {probeRes?.ok && probeRes.branches.length > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setBranchSearch("");
                        setBranchDropdownOpen((v) => !v);
                      }}
                      title="展开/收起远端分支列表"
                      style={{
                        position: "absolute",
                        right: 4,
                        border: "none",
                        background: "transparent",
                        padding: 4,
                        display: "grid",
                        placeItems: "center",
                        cursor: "pointer",
                        color: "var(--text-2)",
                      }}
                    >
                      <ChevronDown size={14} />
                    </button>
                  )}
                </div>

                {branchDropdownOpen && probeRes?.ok && probeRes.branches.length > 0 && (
                  <div className="branch-combo-panel">
                    <div className="branch-combo-search">
                      <Search size={12} className="muted" />
                      <input
                        type="text"
                        placeholder="输入关键词模糊筛选分支…"
                        value={branchSearch}
                        onChange={(e) => setBranchSearch(e.target.value)}
                      />
                      <span className="muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
                        {filteredRemoteBranches.length}/{probeRes.branches.length}
                      </span>
                      <button
                        type="button"
                        onClick={() => setBranchDropdownOpen(false)}
                        style={{
                          border: "none",
                          background: "transparent",
                          color: "var(--text-2)",
                          fontSize: 11,
                          cursor: "pointer",
                          padding: "0 2px",
                          whiteSpace: "nowrap",
                        }}
                      >
                        收起
                      </button>
                    </div>
                    <div className="branch-combo-list">
                      {filteredRemoteBranches.map((b) => {
                        const selected = (form.branch || "").trim() === b;
                        const isDef = probeRes.default_branch === b;
                        return (
                          <button
                            key={b}
                            type="button"
                            className={`branch-combo-item ${selected ? "active" : ""}`}
                            onClick={() => {
                              setForm({ ...form, branch: b });
                              setBranchSearch("");
                              setBranchDropdownOpen(false);
                            }}
                          >
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                              <GitBranch size={12} />
                              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{b}</span>
                            </span>
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
                              {isDef && <span className="chip" style={{ fontSize: 10, padding: "0 5px" }}>默认</span>}
                              {selected && <Check size={12} />}
                            </span>
                          </button>
                        );
                      })}
                      {filteredRemoteBranches.length === 0 && (
                        <div className="muted" style={{ padding: "10px 12px", textAlign: "center", fontSize: 12 }}>
                          未匹配到包含「{branchSearch}」的远端分支
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">状态</span>
                <select value={form.status || "在研"} onChange={(e) => setForm({ ...form, status: e.target.value })}>
                  <option value="在研">在研（纳入扫描）</option>
                  <option value="停用">停用（暂停扫描）</option>
                  <option value="归档">归档</option>
                </select>
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">技术栈</span>
                <input
                  value={form.tech_stack || ""}
                  placeholder="Java 17 / Vue 3"
                  onChange={(e) => setForm({ ...form, tech_stack: e.target.value })}
                />
              </label>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">排除路径（分号分隔）</span>
                <input
                  value={form.scope_out || ""}
                  placeholder="generated/;docs/legacy/"
                  onChange={(e) => setForm({ ...form, scope_out: e.target.value })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">自定义维度权重（可选）</span>
                <input
                  value={form.weights || ""}
                  placeholder="留空按默认权重"
                  onChange={(e) => setForm({ ...form, weights: e.target.value })}
                />
              </label>
            </div>

            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">备注</span>
              <input value={form.notes || ""} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
            </label>

            <button type="submit" className="btn btn-primary" disabled={busy} style={{ justifyContent: "center" }}>
              {busy ? "保存中…" : modalMode === "create" ? "确认新增项目" : "保存修改"}
            </button>
          </form>
        </div>
      )}

      {/* CSV 批量导入弹窗 */}
      {showImport && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setShowImport(false)}
        >
          <form
            className="card"
            style={{ width: "min(640px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleImportCsv(e)}
          >
            <div className="card-title">
              <span>CSV 批量导入项目清单</span>
              <button type="button" className="btn" onClick={() => setShowImport(false)}>
                关闭
              </button>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  file.text().then(setCsvText).catch(() => {});
                }}
              />
              <select value={importMode} onChange={(e) => setImportMode(e.target.value as "upsert" | "replace")}>
                <option value="upsert">按 project_id 增量新增/更新（推荐）</option>
                <option value="replace">全量覆盖现有项目清单</option>
              </select>
            </div>
            <textarea
              rows={10}
              placeholder="粘贴 CSV 内容（需包含 project_id,project_name,department,owner,git_url,branch,status 等表头）"
              value={csvText}
              onChange={(e) => setCsvText(e.target.value)}
              style={{
                width: "100%",
                fontFamily: "monospace",
                fontSize: 12,
                padding: 10,
                borderRadius: 8,
                border: "1px solid var(--border)",
                background: "var(--bg-2)",
                color: "var(--text-1)",
              }}
              required
            />
            <button type="submit" className="btn btn-primary" disabled={busy} style={{ justifyContent: "center" }}>
              {busy ? "导入中…" : "执行导入并同步扫描清册"}
            </button>
          </form>
        </div>
      )}

      {/* 长期未维护预警阈值弹窗 */}
      {showThresholdModal && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setShowThresholdModal(false)}
        >
          <form
            className="card"
            style={{ width: "min(420px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleSaveThresholds(e)}
          >
            <div className="card-title">
              <span>长期未维护预警阈值</span>
              <button type="button" className="btn" onClick={() => setShowThresholdModal(false)}>
                关闭
              </button>
            </div>
            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">黄色预警天数（久未更新）</span>
              <input
                type="number"
                min={7}
                max={3649}
                value={warnDaysInput}
                onChange={(e) => setWarnDaysInput(e.target.value)}
                required
              />
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">红色预警天数（严重停滞）</span>
              <input
                type="number"
                min={8}
                max={3650}
                value={critDaysInput}
                onChange={(e) => setCritDaysInput(e.target.value)}
                required
              />
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy} style={{ justifyContent: "center" }}>
              保存阈值
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
