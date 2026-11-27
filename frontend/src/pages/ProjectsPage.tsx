import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  FolderGit2,
  GitBranch,
  Info,
  MoreHorizontal,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  Sliders,
  Trash2,
  Upload,
  Users,
  X,
} from "lucide-react";
import { api } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type { AuthUser, CatalogProject, GitProbeResult, StaleThresholds } from "../types";

interface Props {
  currentUser: AuthUser;
  focusProjectId?: string | null;
  onFocusConsumed?: () => void;
  onOpenReport?: (date: string, runId: string, target: string) => void;
  onScanStarted?: (taskId: string) => void;
}

type SortKey = "default" | "name" | "loc" | "stale";
type SortDir = "asc" | "desc";

interface ToastItem {
  id: number;
  tone: "success" | "error" | "info";
  text: string;
}

interface ConfirmState {
  tone: "danger" | "warning" | "info";
  title: string;
  description: string;
  previewItems?: string[];
  footnote?: string;
  confirmLabel: string;
  onConfirm: () => Promise<void>;
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

/** 从 Git / SVN 地址自动推导合法的 project_id 和默认项目名 */
function deriveProjectSlugFromUrl(rawUrl: string): { id: string; name: string } | null {
  const trimmed = (rawUrl || "").trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  let pathStr = "";
  if (trimmed.startsWith("git@") && trimmed.includes(":")) {
    pathStr = trimmed.split(":", 2)[1] || "";
  } else {
    try {
      pathStr = new URL(trimmed).pathname;
    } catch {
      return null;
    }
  }
  const segs = pathStr
    .split("/")
    .map((s) => decodeURIComponent(s).trim())
    .filter(Boolean);
  if (segs.length === 0) return null;
  let last = segs[segs.length - 1].replace(/\.git$/i, "");
  // 针对形如 .../SJPT/4.Src 这种通用目录名，合并上一级目录更有辨识度
  if (/^(?:src|4\.src|trunk|code|source)$/i.test(last) && segs.length >= 2) {
    last = `${segs[segs.length - 2]}-${last}`;
  }
  const slug = last
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+/g, "-")
    .replace(/-$/, "")
    .slice(0, 64);
  if (!slug) return null;
  return { id: slug, name: last.slice(0, 80) };
}

function splitOwners(raw?: string): string[] {
  return (raw || "")
    .split(/[、,，/;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
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

export function ProjectsPage({ currentUser, focusProjectId, onFocusConsumed, onOpenReport, onScanStarted }: Props) {
  const isAdmin = currentUser.role === "superadmin" || currentUser.role === "admin";
  const [projects, setProjects] = useState<CatalogProject[]>([]);
  const [thresholds, setThresholds] = useState<StaleThresholds>({ warn_days: 90, critical_days: 180 });
  const [owners, setOwners] = useState<Array<{ display_name: string; username: string; email: string }>>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  // 悬浮 Toast 提示队列
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastIdRef = useRef(1);
  const pushToast = useCallback((tone: ToastItem["tone"], text: string) => {
    const id = toastIdRef.current++;
    setToasts((prev) => [...prev.slice(-3), { id, tone, text }]);
    const ttl = tone === "error" ? 8000 : 4500;
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, ttl);
  }, []);
  const dismissToast = (id: number) => setToasts((prev) => prev.filter((t) => t.id !== id));

  // 自定义确认弹窗
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  // 筛选条件
  const [statusFilter, setStatusFilter] = useState("");
  const [deptFilter, setDeptFilter] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [staleFilter, setStaleFilter] = useState("");
  const [gitHealthFilter, setGitHealthFilter] = useState("");
  const [query, setQuery] = useState("");

  // 排序 & 分页
  const [sortKey, setSortKey] = useState<SortKey>("default");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(50);

  // 多选批量操作
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // 工具栏「更多工具」下拉
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const moreMenuRef = useRef<HTMLDivElement | null>(null);

  // 新增/编辑弹窗
  const [modalMode, setModalMode] = useState<"create" | "edit" | null>(null);
  const [form, setForm] = useState<Partial<CatalogProject>>(EMPTY_FORM);
  const [editingHealthHint, setEditingHealthHint] = useState<string>("");
  const [idTouched, setIdTouched] = useState(false);
  const [nameTouched, setNameTouched] = useState(false);
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

  // 批量修改负责人 / 部门弹窗
  const [showBatchMeta, setShowBatchMeta] = useState(false);
  const [batchDeptEnabled, setBatchDeptEnabled] = useState(false);
  const [batchDeptVal, setBatchDeptVal] = useState("");
  const [batchOwnerEnabled, setBatchOwnerEnabled] = useState(true);
  const [batchOwnerMode, setBatchOwnerMode] = useState<"replace" | "append" | "remove">("replace");
  const [batchOwnerVal, setBatchOwnerVal] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
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
      pushToast("error", err instanceof Error ? err.message : "加载项目清单失败");
    } finally {
      setLoading(false);
    }
  }, [isAdmin, pushToast]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  // 点击外部关闭「更多工具」菜单
  useEffect(() => {
    if (!moreMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (moreMenuRef.current && !moreMenuRef.current.contains(e.target as Node)) {
        setMoreMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMoreMenuOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [moreMenuOpen]);

  // 当项目列表刷新时清理不存在的已选项
  useEffect(() => {
    const valid = new Set(projects.map((p) => p.project_id));
    setSelectedIds((prev) => {
      let changed = false;
      const next = new Set<string>();
      prev.forEach((id) => {
        if (valid.has(id)) next.add(id);
        else changed = true;
      });
      return changed ? next : prev;
    });
  }, [projects]);

  const departments = useMemo(
    () => [...new Set(projects.map((p) => (p.department || "").trim()).filter(Boolean))].sort(),
    [projects]
  );

  const uniqueOwners = useMemo(() => {
    const s = new Set<string>();
    projects.forEach((p) => splitOwners(p.owner).forEach((o) => s.add(o)));
    return [...s].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [projects]);

  const hasActiveFilters = Boolean(
    statusFilter || deptFilter || ownerFilter || staleFilter || gitHealthFilter || query.trim()
  );

  const resetFilters = () => {
    setStatusFilter("");
    setDeptFilter("");
    setOwnerFilter("");
    setStaleFilter("");
    setGitHealthFilter("");
    setQuery("");
    setPage(1);
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = projects.filter((p) => {
      if (statusFilter && p.status !== statusFilter) return false;
      if (deptFilter && (p.department || "").trim() !== deptFilter) return false;
      if (ownerFilter && !splitOwners(p.owner).includes(ownerFilter)) return false;
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

    if (sortKey === "default") return list;
    const factor = sortDir === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      if (sortKey === "name") {
        return factor * (a.project_name || a.project_id).localeCompare(b.project_name || b.project_id, "zh-Hans-CN");
      }
      if (sortKey === "loc") {
        const av = a.loc_total ?? -1;
        const bv = b.loc_total ?? -1;
        return factor * (av - bv);
      }
      if (sortKey === "stale") {
        const av = a.inactive_days ?? -1;
        const bv = b.inactive_days ?? -1;
        return factor * (av - bv);
      }
      return 0;
    });
  }, [projects, statusFilter, deptFilter, ownerFilter, staleFilter, gitHealthFilter, query, sortKey, sortDir]);

  // 筛选或排序变化时回到第 1 页
  useEffect(() => {
    setPage(1);
  }, [statusFilter, deptFilter, ownerFilter, staleFilter, gitHealthFilter, query, sortKey, sortDir, pageSize]);

  const totalPages = pageSize <= 0 ? 1 : Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pagedProjects = useMemo(() => {
    if (pageSize <= 0) return filtered;
    const start = (safePage - 1) * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, safePage, pageSize]);

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

  const toggleSort = (key: Exclude<SortKey, "default">) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    } else if (sortDir === "desc") {
      setSortDir("asc");
    } else {
      setSortKey("default");
    }
  };

  const renderSortIcon = (key: Exclude<SortKey, "default">) => {
    if (sortKey !== key) return <ArrowUpDown size={12} className="muted" />;
    return sortDir === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />;
  };

  // 多选逻辑
  const allOnPageSelected =
    pagedProjects.length > 0 && pagedProjects.every((p) => selectedIds.has(p.project_id));
  const someOnPageSelected =
    !allOnPageSelected && pagedProjects.some((p) => selectedIds.has(p.project_id));

  const toggleSelectOne = (pid: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  };

  const toggleSelectPage = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allOnPageSelected) {
        pagedProjects.forEach((p) => next.delete(p.project_id));
      } else {
        pagedProjects.forEach((p) => next.add(p.project_id));
      }
      return next;
    });
  };

  const selectAllFiltered = () => {
    setSelectedIds(new Set(filtered.map((p) => p.project_id)));
  };

  const selectedProjects = useMemo(
    () => projects.filter((p) => selectedIds.has(p.project_id)),
    [projects, selectedIds]
  );

  const runProbeFor = useCallback(
    async (gitUrl: string, branch: string, projectId: string, vcsType: string, autoOpen: boolean) => {
      if (!gitUrl.trim()) {
        pushToast("error", "请先填写仓库地址");
        return;
      }
      setProbing(true);
      try {
        const res = await api.probeGit(gitUrl.trim(), branch.trim(), projectId, vcsType || "git");
        setProbeRes(res);
        setBranchSearch("");
        if (res.ok && res.branches.length > 0) {
          if (autoOpen) setBranchDropdownOpen(true);
          if (!branch.trim() && res.default_branch && res.vcs_type !== "svn") {
            setForm((prev) => ({ ...prev, branch: res.default_branch }));
          }
        }
        if (projectId) {
          void loadData();
        }
      } catch (err) {
        pushToast("error", err instanceof Error ? err.message : "仓库探测失败");
      } finally {
        setProbing(false);
      }
    },
    [loadData, pushToast]
  );

  const openCreate = () => {
    setForm({ ...EMPTY_FORM });
    setEditingHealthHint("");
    setIdTouched(false);
    setNameTouched(false);
    setProbeRes(null);
    setBranchDropdownOpen(false);
    setBranchSearch("");
    setModalMode("create");
  };

  const openEdit = useCallback(
    (p: CatalogProject, forceProbe = false) => {
      setForm({ ...p });
      setEditingHealthHint(p.git_health === "error" ? p.git_health_reason || "地址或分支校验异常" : "");
      setIdTouched(true);
      setNameTouched(true);
      setProbeRes(null);
      setBranchDropdownOpen(false);
      setBranchSearch("");
      setModalMode("edit");
      // 若该项目当前标记为异常，或从批次页「去修分支」跳转而来，自动探测远端分支
      if ((forceProbe || p.git_health === "error") && p.git_url?.trim()) {
        void runProbeFor(p.git_url, p.branch || "", p.project_id, p.vcs_type || "git", true);
      }
    },
    [runProbeFor]
  );

  // 从「批次记录」点击「去修分支」跳转过来时，自动定位并打开编辑弹窗 + 探测远端分支
  useEffect(() => {
    if (!focusProjectId || projects.length === 0) return;
    const target = projects.find((p) => p.project_id === focusProjectId);
    if (target) {
      setQuery(target.project_id);
      openEdit(target, true);
    } else {
      pushToast("error", `未在项目清册中找到 ${focusProjectId}`);
    }
    if (onFocusConsumed) onFocusConsumed();
  }, [focusProjectId, projects, openEdit, onFocusConsumed, pushToast]);

  const handleProbeGit = async () => {
    await runProbeFor(
      form.git_url || "",
      form.branch || "",
      modalMode === "edit" ? form.project_id || "" : "",
      form.vcs_type || "git",
      true
    );
  };

  const handleProbeAllGit = async (targetIds?: string[]) => {
    setProbingAll(true);
    const scopeLabel = targetIds && targetIds.length > 0 ? `选中的 ${targetIds.length} 个项目` : "全部在研项目";
    pushToast("info", `正在并发校验${scopeLabel}的仓库连通性与分支，请稍候…`);
    try {
      const res = await api.probeAllProjectsGit(targetIds && targetIds.length > 0 ? false : true, targetIds);
      await loadData();
      if (res.failed > 0) {
        setGitHealthFilter("error");
        pushToast(
          "error",
          `批量校验完成（共 ${res.total} 个）：正常 ${res.ok} 个，异常 ${res.failed} 个，已自动筛选出异常项目`
        );
      } else {
        pushToast("success", `批量校验完成（共 ${res.total} 个）：全部 ${res.ok} 个仓库地址与分支均正常`);
      }
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "批量校验仓库失败");
    } finally {
      setProbingAll(false);
    }
  };

  const handleSaveProject = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (modalMode === "create") {
        await api.createProject(form);
        pushToast("success", `已新增项目 ${form.project_name || form.project_id}`);
      } else if (modalMode === "edit" && form.project_id) {
        await api.updateProject(form.project_id, form);
        pushToast("success", `已保存项目 ${form.project_name || form.project_id} 的修改`);
      }
      setModalMode(null);
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "保存项目失败");
    } finally {
      setBusy(false);
    }
  };

  const handleToggleStatus = async (p: CatalogProject) => {
    const nextStatus = p.status === "在研" ? "停用" : "在研";
    setBusy(true);
    try {
      await api.updateProject(p.project_id, { status: nextStatus });
      pushToast("success", `项目「${p.project_name}」已切换为「${nextStatus}」`);
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "更新状态失败");
    } finally {
      setBusy(false);
    }
  };

  const requestDeleteProject = (p: CatalogProject) => {
    setConfirmState({
      tone: "danger",
      title: `确认移除项目「${p.project_name}」？`,
      description: "移除后该项目将不再参与后续定时或手动扫描，也不会出现在项目清册中。",
      previewItems: [`${p.project_name} (${p.project_id})`],
      footnote: "说明：该操作不会删除历史已生成的审查报告与批次记录。",
      confirmLabel: "确认移除",
      onConfirm: async () => {
        await api.deleteProject(p.project_id);
        pushToast("success", `已移除项目 ${p.project_id}`);
        await loadData();
      },
    });
  };

  const requestBatchStatus = (nextStatus: "在研" | "停用" | "归档") => {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    const previews = selectedProjects.map((p) => p.project_name || p.project_id);
    setConfirmState({
      tone: nextStatus === "在研" ? "info" : "warning",
      title: `批量将 ${ids.length} 个项目设为「${nextStatus}」？`,
      description:
        nextStatus === "在研"
          ? "设为「在研」后，这些项目将重新纳入定时与全量扫描队列。"
          : `设为「${nextStatus}」后，这些项目将暂停参与后续全量扫描（历史报告保留）。`,
      previewItems: previews,
      confirmLabel: `批量设为${nextStatus}`,
      onConfirm: async () => {
        const res = await api.batchUpdateProjectStatus(ids, nextStatus);
        setSelectedIds(new Set());
        pushToast("success", `已批量将 ${res.updated} 个项目切换为「${res.status}」`);
        await loadData();
      },
    });
  };

  const requestQuickScan = (targets: CatalogProject[]) => {
    if (targets.length === 0) return;
    const ids = targets.map((p) => p.project_id);
    const isSingle = targets.length === 1;
    setConfirmState({
      tone: "info",
      title: isSingle ? `立即试扫「${targets[0].project_name}」？` : `立即扫描选中的 ${targets.length} 个项目？`,
      description: "将启动一次仅包含目标项目的扫描任务，适合修改分支或接入新 SVN/Git 仓库后快速验证。",
      previewItems: targets.map((p) => `${p.project_name} (${p.vcs_type === "svn" ? "SVN" : p.branch || "main"})`),
      footnote: "触发后将自动跳转至「实时进度」页面查看拉取与扫描日志。",
      confirmLabel: "立即开始扫描",
      onConfirm: async () => {
        const res = await api.startScan({ only: ids, resume: false });
        setSelectedIds(new Set());
        pushToast("success", `已启动扫描任务（${ids.length} 个项目），正在跳转实时进度…`);
        if (onScanStarted) {
          onScanStarted(res.task_id);
        }
      },
    });
  };

  const handleConfirmExecute = async () => {
    if (!confirmState) return;
    setConfirmBusy(true);
    try {
      await confirmState.onConfirm();
      setConfirmState(null);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "操作执行失败");
    } finally {
      setConfirmBusy(false);
    }
  };

  const handleRefreshMetrics = async () => {
    setBusy(true);
    setMoreMenuOpen(false);
    pushToast("info", "正在刷新已克隆仓库的代码量与最后提交时间…");
    try {
      const res = await api.refreshProjectMetrics();
      pushToast("success", `代码量刷新完成：更新 ${res.updated}/${res.scanned} 个已克隆仓库（未克隆 ${res.missing_repo} 个）`);
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "刷新统计失败");
    } finally {
      setBusy(false);
    }
  };

  const handleImportCsv = async (e: FormEvent) => {
    e.preventDefault();
    if (!csvText.trim()) return;
    setBusy(true);
    try {
      const res = await api.importProjectsCsv(csvText, importMode);
      setShowImport(false);
      setCsvText("");
      pushToast(
        res.errors.length ? "info" : "success",
        `CSV 导入完成：新增 ${res.created} 个、更新 ${res.updated} 个${res.errors.length ? `，跳过失败 ${res.errors.length} 行` : ""}`
      );
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "CSV 导入失败");
    } finally {
      setBusy(false);
    }
  };

  const handleBatchMetaSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    if (!batchDeptEnabled && !batchOwnerEnabled) {
      pushToast("error", "请至少勾选一项要修改的字段（部门或负责人）");
      return;
    }
    setBusy(true);
    try {
      const res = await api.batchUpdateProjectsMeta({
        project_ids: ids,
        ...(batchDeptEnabled ? { department: batchDeptVal.trim() } : {}),
        ...(batchOwnerEnabled ? { owner: batchOwnerVal.trim(), owner_mode: batchOwnerMode } : {}),
      });
      setShowBatchMeta(false);
      setSelectedIds(new Set());
      pushToast("success", `已批量更新 ${res.updated} 个项目的负责人 / 部门信息`);
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "批量修改失败");
    } finally {
      setBusy(false);
    }
  };

  const handleSaveThresholds = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await api.saveStaleThresholds(Number(warnDaysInput), Number(critDaysInput));
      setThresholds(res.stale_thresholds);
      setShowThresholdModal(false);
      pushToast(
        "success",
        `预警阈值已更新：黄色 ≥ ${res.stale_thresholds.warn_days} 天，红色 ≥ ${res.stale_thresholds.critical_days} 天`
      );
      await loadData();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "保存阈值失败");
    } finally {
      setBusy(false);
    }
  };

  const rangeStart = filtered.length === 0 ? 0 : pageSize <= 0 ? 1 : (safePage - 1) * pageSize + 1;
  const rangeEnd = pageSize <= 0 ? filtered.length : Math.min(filtered.length, safePage * pageSize);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {/* 顶部统计指标（全卡片可点击联动筛选/排序） */}
      <div className="tiles">
        <div
          className={`card kpi-clickable ${statusFilter === "在研" ? "active" : ""}`}
          title="点击筛选在研（参与扫描）项目"
          onClick={() => setStatusFilter((v) => (v === "在研" ? "" : "在研"))}
        >
          <div className="tile-label">{isAdmin ? "纳入管理项目" : "我负责的项目"}</div>
          <div className="tile-value">{stats.total}</div>
          <div className="tile-note">在研参与扫描 {stats.activeScan} 个 · 点击筛选在研</div>
        </div>
        <div
          className={`card kpi-clickable ${sortKey === "loc" ? "active" : ""}`}
          title="点击按代码量从大到小排序"
          onClick={() => toggleSort("loc")}
        >
          <div className="tile-label">已统计代码总量</div>
          <div className="tile-value">{stats.totalLoc > 0 ? formatLoc(stats.totalLoc) : "—"}</div>
          <div className="tile-note">点击按代码量排序定位大型仓库</div>
        </div>
        <div
          className={`card kpi-clickable tone-critical ${staleFilter === "critical" ? "active" : ""}`}
          title="点击筛选严重停滞项目"
          onClick={() => setStaleFilter((v) => (v === "critical" ? "" : "critical"))}
        >
          <div className="tile-label">严重长期未维护（≥{thresholds.critical_days}天）</div>
          <div className="tile-value" style={{ color: stats.critStale > 0 ? "var(--critical)" : undefined }}>
            {stats.critStale}
          </div>
          <div className="tile-note">点击一键筛选 · 建议评估停用或归档</div>
        </div>
        <div
          className={`card kpi-clickable tone-warning ${staleFilter === "warning" ? "active" : ""}`}
          title="点击筛选久未更新预警项目"
          onClick={() => setStaleFilter((v) => (v === "warning" ? "" : "warning"))}
        >
          <div className="tile-label">久未更新预警（≥{thresholds.warn_days}天）</div>
          <div className="tile-value" style={{ color: stats.warnStale > 0 ? "var(--warning)" : undefined }}>
            {stats.warnStale}
          </div>
          <div className="tile-note">点击一键筛选 · 按最近提交时间判定</div>
        </div>
        <div
          className={`card kpi-clickable tone-critical ${gitHealthFilter === "error" ? "active" : ""}`}
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

      {/* 主表卡片 */}
      <div className="card">
        <div className="card-title" style={{ flexWrap: "wrap" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <FolderGit2 size={15} /> {isAdmin ? "扫描项目管理与代码活跃度" : "我的项目代码量与活跃度"} ({filtered.length}/{projects.length})
          </span>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {isAdmin && (
              <>
                <button type="button" className="btn btn-primary" onClick={openCreate}>
                  <Plus size={14} /> 新增项目
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={probingAll || busy}
                  title="并发校验所有在研 Git/SVN 项目的仓库连通性与分支是否存在"
                  onClick={() => void handleProbeAllGit()}
                >
                  <ShieldAlert size={14} /> {probingAll ? "批量校验中…" : "批量校验仓库"}
                </button>
                <div className="toolbar-dropdown-wrap" ref={moreMenuRef}>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setMoreMenuOpen((v) => !v)}
                    aria-expanded={moreMenuOpen}
                  >
                    <MoreHorizontal size={14} /> 更多工具 <ChevronDown size={13} />
                  </button>
                  {moreMenuOpen && (
                    <div className="toolbar-dropdown-menu">
                      <button
                        type="button"
                        className="toolbar-dropdown-item"
                        disabled={busy}
                        onClick={() => void handleRefreshMetrics()}
                      >
                        <RefreshCw size={13} /> 刷新代码量与提交时间
                      </button>
                      <button
                        type="button"
                        className="toolbar-dropdown-item"
                        onClick={() => {
                          setMoreMenuOpen(false);
                          setShowThresholdModal(true);
                        }}
                      >
                        <Sliders size={13} /> 预警阈值 ({thresholds.warn_days}/{thresholds.critical_days}天)
                      </button>
                      <div className="toolbar-dropdown-divider" />
                      <button
                        type="button"
                        className="toolbar-dropdown-item"
                        onClick={() => {
                          setMoreMenuOpen(false);
                          setShowImport(true);
                        }}
                      >
                        <Upload size={13} /> 导入 CSV 清册
                      </button>
                      <button
                        type="button"
                        className="toolbar-dropdown-item"
                        onClick={() => {
                          setMoreMenuOpen(false);
                          void api.downloadProjectsCsv();
                        }}
                      >
                        <Download size={13} /> 导出 CSV 清册
                      </button>
                    </div>
                  )}
                </div>
              </>
            )}
            <button type="button" className="btn" disabled={loading} onClick={() => void loadData()}>
              <RefreshCw size={14} /> 刷新
            </button>
          </div>
        </div>

        {/* 筛选条 */}
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
          {isAdmin && (
            <select value={ownerFilter} onChange={(e) => setOwnerFilter(e.target.value)}>
              <option value="">全部负责人</option>
              {uniqueOwners.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          )}
          <select value={staleFilter} onChange={(e) => setStaleFilter(e.target.value)}>
            <option value="">全部维护活跃度</option>
            <option value="stale">全部长期未维护预警</option>
            <option value="critical">严重停滞（≥{thresholds.critical_days}天）</option>
            <option value="warning">久未更新（≥{thresholds.warn_days}天）</option>
            <option value="active">维护活跃</option>
          </select>
          <select value={gitHealthFilter} onChange={(e) => setGitHealthFilter(e.target.value)}>
            <option value="">全部仓库状态</option>
            <option value="error">地址 / 分支异常 ({stats.gitErrorCount})</option>
            <option value="ok">地址与分支正常</option>
            <option value="unknown">尚未校验</option>
          </select>
          <input
            placeholder="搜索项目 ID / 名称 / 负责人 / 仓库地址 / 分支 / 异常原因"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ minWidth: 220, flex: "1 1 220px", maxWidth: 340 }}
          />
          {hasActiveFilters && (
            <button type="button" className="btn" onClick={resetFilters} title="清空全部筛选条件">
              <RotateCcw size={13} /> 清空筛选
            </button>
          )}
        </div>

        {/* 批量选择操作条 */}
        {isAdmin && selectedIds.size > 0 && (
          <div className="bulk-action-bar">
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span>
                已选择 <b>{selectedIds.size}</b> 个项目
              </span>
              {selectedIds.size < filtered.length && (
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "2px 8px", fontSize: 12 }}
                  onClick={selectAllFiltered}
                >
                  全选当前筛选结果 ({filtered.length})
                </button>
              )}
              <button
                type="button"
                className="btn"
                style={{ padding: "2px 8px", fontSize: 12 }}
                onClick={() => setSelectedIds(new Set())}
              >
                取消选择
              </button>
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={probingAll || busy}
                onClick={() => void handleProbeAllGit([...selectedIds])}
              >
                <ShieldAlert size={12} /> 校验选中
              </button>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={busy}
                onClick={() => requestQuickScan(selectedProjects)}
              >
                <Play size={12} /> 扫描选中
              </button>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={busy}
                onClick={() => {
                  setBatchDeptEnabled(false);
                  setBatchDeptVal("");
                  setBatchOwnerEnabled(true);
                  setBatchOwnerMode("replace");
                  setBatchOwnerVal("");
                  setShowBatchMeta(true);
                }}
              >
                <Users size={12} /> 改负责人/部门
              </button>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={busy}
                onClick={() => requestBatchStatus("在研")}
              >
                批量启用
              </button>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={busy}
                onClick={() => requestBatchStatus("停用")}
              >
                批量停用
              </button>
              <button
                type="button"
                className="btn"
                style={{ padding: "4px 9px", fontSize: 12 }}
                disabled={busy}
                onClick={() => requestBatchStatus("归档")}
              >
                批量归档
              </button>
            </div>
          </div>
        )}

        <div className="table-wrap">
          <table className="projects-table">
            <thead>
              <tr>
                {isAdmin && (
                  <th style={{ width: 34, textAlign: "center" }}>
                    <input
                      type="checkbox"
                      aria-label="全选当前页"
                      checked={allOnPageSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = someOnPageSelected;
                      }}
                      onChange={toggleSelectPage}
                    />
                  </th>
                )}
                <th>
                  <button type="button" className="sortable-th" onClick={() => toggleSort("name")}>
                    项目 {renderSortIcon("name")}
                  </button>
                </th>
                <th>部门 / 负责人</th>
                <th>仓库地址与分支</th>
                <th>状态</th>
                <th className="num">
                  <button type="button" className="sortable-th" onClick={() => toggleSort("loc")}>
                    代码量 (LOC) {renderSortIcon("loc")}
                  </button>
                </th>
                <th>
                  <button type="button" className="sortable-th" onClick={() => toggleSort("stale")}>
                    最后代码提交 / 活跃度 {renderSortIcon("stale")}
                  </button>
                </th>
                <th className="sticky-action-col">操作</th>
              </tr>
            </thead>
            <tbody>
              {pagedProjects.map((p) => {
                const langs = topLangs(p.languages);
                const checked = selectedIds.has(p.project_id);
                return (
                  <tr key={p.project_id} style={{ opacity: p.status === "在研" ? 1 : 0.65 }}>
                    {isAdmin && (
                      <td style={{ textAlign: "center" }}>
                        <input
                          type="checkbox"
                          aria-label={`选择 ${p.project_name}`}
                          checked={checked}
                          onChange={() => toggleSelectOne(p.project_id)}
                        />
                      </td>
                    )}
                    <td style={{ whiteSpace: "normal", minWidth: 175, maxWidth: 235, wordBreak: "break-word" }}>
                      <div style={{ fontWeight: 600, lineHeight: 1.35 }}>{p.project_name}</div>
                      <div className="muted" style={{ fontSize: 11.5, marginTop: 2, wordBreak: "break-all" }}>
                        <code>{p.project_id}</code>
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 90, maxWidth: 128 }}>
                      <div>{p.department || "—"}</div>
                      <div className="muted" style={{ fontSize: 12 }}>
                        {p.owner || "未指定"}
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 185, maxWidth: 255 }}>
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
                          <GitBranch size={11} /> {p.vcs_type === "svn" ? p.branch || "直接目录" : p.branch || "main"}
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
                    <td className="sticky-action-col">
                      <div style={{ display: "flex", gap: 5, flexWrap: "nowrap", alignItems: "center" }}>
                        {isAdmin && (
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 7px", fontSize: 12 }}
                            onClick={() => openEdit(p)}
                          >
                            编辑
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: "3px 7px", fontSize: 12 }}
                          disabled={busy}
                          title="立即单独试扫该项目并跳转实时进度"
                          onClick={() => requestQuickScan([p])}
                        >
                          <Play size={11} /> 试扫
                        </button>
                        {p.latest_report && onOpenReport && (
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 7px", fontSize: 12 }}
                            title={`查看最新审查报告（${p.latest_report.date}）`}
                            onClick={() =>
                              onOpenReport(p.latest_report!.date, p.latest_report!.run_id, p.project_id)
                            }
                          >
                            <FileText size={11} /> 报告
                          </button>
                        )}
                        {isAdmin && (
                          <>
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
                              title="从清册移除该项目"
                              onClick={() => requestDeleteProject(p)}
                            >
                              <Trash2 size={12} />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {pagedProjects.length === 0 && (
                <tr>
                  <td colSpan={isAdmin ? 8 : 7} className="muted" style={{ textAlign: "center", padding: 28 }}>
                    没有符合条件的项目
                    {hasActiveFilters && (
                      <button
                        type="button"
                        className="btn"
                        style={{ marginLeft: 10, padding: "3px 9px", fontSize: 12 }}
                        onClick={resetFilters}
                      >
                        清空筛选
                      </button>
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* 分页条 */}
        {filtered.length > 0 && (
          <div className="pagination-bar">
            <span>
              显示第 <b>{rangeStart}</b>–<b>{rangeEnd}</b> 条，共 <b>{filtered.length}</b> 个项目
            </span>
            <div className="pagination-controls">
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                <span className="muted">每页</span>
                <select
                  value={pageSize}
                  onChange={(e) => setPageSize(Number(e.target.value))}
                  style={{ padding: "3px 8px", fontSize: 12 }}
                >
                  <option value={20}>20 条</option>
                  <option value={50}>50 条</option>
                  <option value={100}>100 条</option>
                  <option value={0}>全部</option>
                </select>
              </label>
              {pageSize > 0 && totalPages > 1 && (
                <>
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "3px 8px", fontSize: 12 }}
                    disabled={safePage <= 1}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                  >
                    <ChevronLeft size={13} /> 上一页
                  </button>
                  <span style={{ fontSize: 12, padding: "0 4px" }}>
                    {safePage} / {totalPages}
                  </span>
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "3px 8px", fontSize: 12 }}
                    disabled={safePage >= totalPages}
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  >
                    下一页 <ChevronRight size={13} />
                  </button>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 新增 / 编辑项目弹窗 */}
      {modalMode && (
        <div className="modal-backdrop" onClick={() => setModalMode(null)}>
          <form
            className="modal-dialog"
            style={{ width: "min(650px, 96vw)", maxHeight: "90vh", overflowY: "auto", display: "grid", gap: 10 }}
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

            {modalMode === "edit" && editingHealthHint && !probeRes?.ok && (
              <div
                style={{
                  fontSize: 12,
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: "1px solid var(--critical)",
                  background: "color-mix(in srgb, var(--critical) 8%, transparent)",
                  color: "var(--critical)",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <AlertTriangle size={14} style={{ flexShrink: 0 }} />
                <span>上次检测异常：{editingHealthHint}</span>
              </div>
            )}

            <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">
                {form.vcs_type === "svn"
                  ? "SVN 仓库地址（支持直接填源码目录，如 https://svn.sjgjj.cn/svn/.../4.Src）*"
                  : "Git / SVN 仓库地址（粘贴 SVN 地址自动识别；新增时自动推导项目 ID）*"}
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
                      const next: Partial<CatalogProject> = { ...prev, git_url: val };
                      if (looksLikeSvnUrl(val) && prev.vcs_type !== "svn") {
                        next.vcs_type = "svn";
                        if (prev.branch === "main") next.branch = "";
                      }
                      if (modalMode === "create" && (!idTouched || !nameTouched)) {
                        const derived = deriveProjectSlugFromUrl(val);
                        if (derived) {
                          if (!idTouched && !prev.project_id) next.project_id = derived.id;
                          if (!nameTouched && !prev.project_name) next.project_name = derived.name;
                        }
                      }
                      return next;
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
                      {probeRes.revision ? (
                        <>
                          {" "}
                          最新修订版 <code>{probeRes.revision}</code>
                        </>
                      ) : null}
                      {probeRes.branches.length > 0 ? (
                        <> · 检测到 <b>{probeRes.branches.length}</b> 个标准分支子路径，可在下方下拉选择</>
                      ) : (
                        <> · 将直接检出该目录进行扫描</>
                      )}
                    </div>
                  ) : (
                    <div>
                      连通正常！检测到 <b>{probeRes.branches.length}</b> 个远端分支（默认：
                      <code>{probeRes.default_branch || "—"}</code>）
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

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">项目 ID（唯一英文标识）*</span>
                <input
                  value={form.project_id || ""}
                  disabled={modalMode === "edit"}
                  placeholder="粘贴地址后可自动推导，例如 order-service"
                  onChange={(e) => {
                    setIdTouched(true);
                    setForm({ ...form, project_id: e.target.value });
                  }}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">项目名称 *</span>
                <input
                  value={form.project_name || ""}
                  placeholder="例如 订单中心"
                  onChange={(e) => {
                    setNameTouched(true);
                    setForm({ ...form, project_name: e.target.value });
                  }}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">所属部门</span>
                <input
                  list="dept-suggestions"
                  value={form.department || ""}
                  placeholder="例如 研发一部"
                  onChange={(e) => setForm({ ...form, department: e.target.value })}
                />
                <datalist id="dept-suggestions">
                  {departments.map((d) => (
                    <option key={d} value={d} />
                  ))}
                </datalist>
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
                    placeholder={
                      form.vcs_type === "svn" ? "留空直接检出上方地址，或填 trunk / branches/xxx" : "输入或选择扫描分支"
                    }
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
                              {isDef && (
                                <span className="chip" style={{ fontSize: 10, padding: "0 5px" }}>
                                  默认
                                </span>
                              )}
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

            <div className="confirm-footer">
              <button type="button" className="btn" disabled={busy} onClick={() => setModalMode(null)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? "保存中…" : modalMode === "create" ? "确认新增项目" : "保存修改"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* CSV 批量导入弹窗 */}
      {showImport && (
        <div className="modal-backdrop" onClick={() => setShowImport(false)}>
          <form
            className="modal-dialog"
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
              placeholder="粘贴 CSV 内容（需包含 project_id,project_name,department,owner,vcs_type,git_url,branch,status 等表头）"
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
            <div className="confirm-footer">
              <button type="button" className="btn" disabled={busy} onClick={() => setShowImport(false)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? "导入中…" : "执行导入并同步扫描清册"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 长期未维护预警阈值弹窗 */}
      {showThresholdModal && (
        <div className="modal-backdrop" onClick={() => setShowThresholdModal(false)}>
          <form
            className="modal-dialog"
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
            <div className="confirm-footer">
              <button type="button" className="btn" disabled={busy} onClick={() => setShowThresholdModal(false)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? "保存中…" : "保存阈值"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 批量修改负责人 / 部门弹窗 */}
      {showBatchMeta && (
        <div className="modal-backdrop" onClick={() => setShowBatchMeta(false)}>
          <form
            className="modal-dialog"
            style={{ width: "min(500px, 95vw)", display: "grid", gap: 12 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleBatchMetaSubmit(e)}
          >
            <div className="card-title">
              <span>批量修改负责人 / 部门（已选 {selectedIds.size} 个项目）</span>
              <button type="button" className="btn" onClick={() => setShowBatchMeta(false)}>
                关闭
              </button>
            </div>

            <div className="confirm-preview-list">
              {selectedProjects.slice(0, 6).map((p) => (
                <span key={p.project_id} className="confirm-preview-chip">
                  {p.project_name}
                </span>
              ))}
              {selectedProjects.length > 6 && (
                <span className="confirm-preview-chip muted">等共 {selectedProjects.length} 个项目</span>
              )}
            </div>

            <div style={{ display: "grid", gap: 8 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600 }}>
                <input
                  type="checkbox"
                  checked={batchOwnerEnabled}
                  onChange={(e) => setBatchOwnerEnabled(e.target.checked)}
                />
                调整负责人（适合离职交接或新增协作者）
              </label>
              {batchOwnerEnabled && (
                <div style={{ display: "grid", gridTemplateColumns: "130px 1fr", gap: 8 }}>
                  <select
                    value={batchOwnerMode}
                    onChange={(e) => setBatchOwnerMode(e.target.value as "replace" | "append" | "remove")}
                  >
                    <option value="replace">替换为新负责人</option>
                    <option value="append">追加负责人</option>
                    <option value="remove">移除指定负责人</option>
                  </select>
                  <input
                    list="batch-owner-suggestions"
                    placeholder="填写姓名（多人用顿号、分隔）"
                    value={batchOwnerVal}
                    onChange={(e) => setBatchOwnerVal(e.target.value)}
                    required={batchOwnerMode !== "replace"}
                  />
                  <datalist id="batch-owner-suggestions">
                    {owners.map((u) => (
                      <option key={u.username} value={u.display_name}>
                        {u.username}
                      </option>
                    ))}
                  </datalist>
                </div>
              )}
            </div>

            <div style={{ display: "grid", gap: 8 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600 }}>
                <input
                  type="checkbox"
                  checked={batchDeptEnabled}
                  onChange={(e) => setBatchDeptEnabled(e.target.checked)}
                />
                调整所属部门
              </label>
              {batchDeptEnabled && (
                <>
                  <input
                    list="batch-dept-suggestions"
                    placeholder="填写新部门名称（例如 研发一部）"
                    value={batchDeptVal}
                    onChange={(e) => setBatchDeptVal(e.target.value)}
                  />
                  <datalist id="batch-dept-suggestions">
                    {departments.map((d) => (
                      <option key={d} value={d} />
                    ))}
                  </datalist>
                </>
              )}
            </div>

            <div className="confirm-footer">
              <button type="button" className="btn" disabled={busy} onClick={() => setShowBatchMeta(false)}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? "更新中…" : `应用到 ${selectedIds.size} 个项目`}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 自定义高颜值确认弹窗 */}
      <ConfirmDialog
        open={Boolean(confirmState)}
        tone={confirmState?.tone || "warning"}
        title={confirmState?.title || ""}
        description={confirmState?.description || ""}
        previewItems={confirmState?.previewItems}
        footnote={confirmState?.footnote}
        confirmLabel={confirmState?.confirmLabel || "确认"}
        busy={confirmBusy}
        onConfirm={() => void handleConfirmExecute()}
        onCancel={() => setConfirmState(null)}
      />

      {/* 右下角悬浮 Toast 提示 */}
      {toasts.length > 0 && (
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast-item ${t.tone}`}>
              <span
                style={{
                  color:
                    t.tone === "success"
                      ? "var(--good)"
                      : t.tone === "error"
                        ? "var(--critical)"
                        : "var(--accent)",
                  marginTop: 1,
                  flexShrink: 0,
                }}
              >
                {t.tone === "success" ? (
                  <CheckCircle2 size={16} />
                ) : t.tone === "error" ? (
                  <AlertTriangle size={16} />
                ) : (
                  <Info size={16} />
                )}
              </span>
              <div style={{ flex: 1, minWidth: 0, wordBreak: "break-word" }}>{t.text}</div>
              <button
                type="button"
                className="confirm-close-btn"
                aria-label="关闭提示"
                onClick={() => dismissToast(t.id)}
              >
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
