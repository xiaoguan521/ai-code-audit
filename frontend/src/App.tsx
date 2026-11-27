import { FormEvent, ReactNode, useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  FileText,
  FolderGit2,
  History,
  LogOut,
  Menu,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Sun,
  UserCircle2,
  Users,
  X,
} from "lucide-react";
import { api, setUnauthorizedHandler } from "./api";
import { LoginView } from "./components/LoginView";
import { NeuralBackdrop } from "./components/NeuralBackdrop";
import { ProfileModal } from "./components/ProfileModal";
import { ConfigPage } from "./pages/ConfigPage";
import { DashboardPage } from "./pages/DashboardPage";
import { OpsConfigPage } from "./pages/OpsConfigPage";
import { ProgressPage } from "./pages/ProgressPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { PromptsPage } from "./pages/PromptsPage";
import { ReportsPage } from "./pages/ReportsPage";
import { RunsPage } from "./pages/RunsPage";
import { UsersPage } from "./pages/UsersPage";
import type { AuthUser, OverviewResponse } from "./types";

type Page = "dashboard" | "progress" | "runs" | "reports" | "projects" | "prompts" | "config" | "users" | "ops";
type ReportTarget = { date: string; runId: string; target: string };

const ROLE_BADGE: Record<AuthUser["role"], string> = {
  superadmin: "超管",
  admin: "管理员",
  user: "负责人",
};

function parseHash(): { page: Page; reportTarget: ReportTarget | null } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  let page: Page = "dashboard";
  if (pathPart === "progress" || pathPart === "scans") page = "progress";
  else if (pathPart === "runs") page = "runs";
  else if (pathPart === "reports") page = "reports";
  else if (pathPart === "projects") page = "projects";
  else if (pathPart === "prompts") page = "prompts";
  else if (pathPart === "config") page = "config";
  else if (pathPart === "users") page = "users";
  else if (pathPart === "ops") page = "ops";

  if (page === "reports" && queryPart) {
    const params = new URLSearchParams(queryPart);
    const date = params.get("date") || "";
    const runId = params.get("run") || "";
    const target = params.get("target") || "_summary";
    if (date && runId) {
      return { page, reportTarget: { date, runId, target } };
    }
  }
  return { page, reportTarget: null };
}

function buildHash(page: Page, target: ReportTarget | null): string {
  if (page === "reports" && target?.date && target?.runId) {
    const params = new URLSearchParams({
      date: target.date,
      run: target.runId,
      target: target.target || "_summary",
    });
    return `#/reports?${params.toString()}`;
  }
  return `#/${page}`;
}

export function App() {
  const initial = parseHash();
  const [page, setPage] = useState<Page>(initial.page);
  const [dark, setDark] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [usernameInput, setUsernameInput] = useState("admin");
  const [passwordInput, setPasswordInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const [showProfile, setShowProfile] = useState(false);

  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reportTarget, setReportTarget] = useState<ReportTarget | null>(initial.reportTarget);
  const [focusTaskId, setFocusTaskId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);

  useEffect(() => {
    const onHashChange = () => {
      const next = parseHash();
      setPage(next.page);
      if (next.reportTarget) setReportTarget(next.reportTarget);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    const nextHash = buildHash(page, reportTarget);
    if (window.location.hash !== nextHash) {
      window.history.replaceState(null, "", nextHash);
    }
  }, [page, reportTarget]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCurrentUser(null);
      setAuthError("会话已过期，请重新登录");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const loadOverview = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.overview();
      setOverview(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法连接后端 API 服务");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    api
      .authStatus()
      .then((st) => {
        setCurrentUser(st.authenticated && st.user ? st.user : null);
        setAuthChecked(true);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "无法连接后端 API 服务");
        setAuthChecked(true);
      });
  }, []);

  useEffect(() => {
    if (authChecked && currentUser) {
      if ((page === "users" || page === "prompts") && currentUser.role === "user") {
        setPage("dashboard");
      } else if (page === "ops" && currentUser.role !== "superadmin") {
        setPage("dashboard");
      }
      void loadOverview();
    }
  }, [authChecked, currentUser?.id, currentUser?.role]);

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    setLoggingIn(true);
    setAuthError(null);
    try {
      const res = await api.login(usernameInput.trim(), passwordInput);
      setPasswordInput("");
      setCurrentUser(res.user);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
    } finally {
      setOverview(null);
      setCurrentUser(null);
      setShowProfile(false);
    }
  };

  const openReport = (date: string, runId: string, target: string) => {
    const next = { date, runId, target };
    setReportTarget(next);
    setPage("reports");
    window.location.hash = buildHash("reports", next);
  };

  const navigate = (nextPage: Page) => {
    setPage(nextPage);
    setMobileMenuOpen(false);
    window.location.hash = buildHash(nextPage, nextPage === "reports" ? reportTarget : null);
  };

  const handleScanStarted = (taskId: string) => {
    setFocusTaskId(taskId);
    navigate("progress");
  };

  if (!authChecked) {
    return (
      <div className="app-shell" style={{ alignItems: "center", justifyContent: "center" }}>
        <div className="muted">正在检查访问权限…</div>
      </div>
    );
  }

  if (!currentUser) {
    return (
      <LoginView
        username={usernameInput}
        password={passwordInput}
        error={authError}
        loggingIn={loggingIn}
        onUsernameChange={setUsernameInput}
        onPasswordChange={setPasswordInput}
        onSubmit={(e) => void handleLogin(e)}
      />
    );
  }

  const isAdmin = currentUser.role === "superadmin" || currentUser.role === "admin";
  const isSuper = currentUser.role === "superadmin";

  type NavItem = { id: Page; label: string; desc: string; icon: ReactNode };
  type NavGroup = { title: string; items: NavItem[] };

  const navGroups: NavGroup[] = [
    {
      title: "审查监控",
      items: [
        { id: "dashboard", label: "质量看板", desc: "八维评分趋势与长期未维护预警", icon: <BarChart3 size={16} /> },
        { id: "progress", label: "实时进度", desc: "扫描队列监控、暂停与继续控制", icon: <Activity size={16} /> },
        { id: "runs", label: "批次记录", desc: "历史批次清册、导出与结果邮件通知", icon: <History size={16} /> },
        { id: "reports", label: "报告阅读", desc: "单项目八性报告与管理层汇总阅读", icon: <FileText size={16} /> },
      ],
    },
    {
      title: "资产与配置",
      items: [
        {
          id: "projects",
          label: isAdmin ? "项目管理" : "我的项目",
          desc: isAdmin ? "Git 仓库维护、分支探测与代码活跃度" : "名下项目代码量与维护活跃度",
          icon: <FolderGit2 size={16} />,
        },
        {
          id: "config",
          label: isAdmin ? "扫描配置" : "触发扫描",
          desc: isAdmin ? "手动扫描、定时计划与环境自检" : "手动触发本人负责项目审查",
          icon: <Settings size={16} />,
        },
        ...(isAdmin
          ? [{ id: "prompts" as Page, label: "提示词配置", desc: "SKILL.md 与汇总提示词在线热更新与回滚", icon: <Sparkles size={16} /> }]
          : []),
      ],
    },
    ...(isAdmin
      ? [
          {
            title: "系统管理",
            items: [
              { id: "users" as Page, label: "用户管理", desc: "账号角色授权、拼音邮箱批量生成与审计", icon: <Users size={16} /> },
              ...(isSuper
                ? [
                    {
                      id: "ops" as Page,
                      label: "运维配置",
                      desc: "模型接口、并发环境变量、Git 凭据与 SMTP 邮件",
                      icon: <SlidersHorizontal size={16} />,
                    },
                  ]
                : []),
            ],
          },
        ]
      : []),
  ];

  const allNavItems = navGroups.flatMap((g) => g.items);
  const currentNav = allNavItems.find((it) => it.id === page) || allNavItems[0];

  return (
    <div className={`console-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <NeuralBackdrop variant="console" dark={dark} />
      {mobileMenuOpen && (
        <div
          className="sidebar-backdrop no-print"
          onClick={() => setMobileMenuOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside className={`sidebar no-print ${mobileMenuOpen ? "open" : ""}`}>
        <div className="sidebar-brand">
          <span className="brand-badge">
            <ShieldCheck size={17} />
          </span>
          {!sidebarCollapsed && (
            <div className="sidebar-brand-text">
              <div className="sidebar-brand-title">八性代码审查</div>
              <div className="sidebar-brand-sub">AI Audit Console</div>
            </div>
          )}
          <button
            type="button"
            className="sidebar-mobile-close"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="关闭菜单"
          >
            <X size={16} />
          </button>
        </div>

        <nav className="sidebar-nav">
          {navGroups.map((group) => (
            <div key={group.title} className="sidebar-group">
              {!sidebarCollapsed && <div className="sidebar-group-title">{group.title}</div>}
              {group.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  title={sidebarCollapsed ? item.label : undefined}
                  className={`sidebar-item ${page === item.id ? "active" : ""}`}
                  onClick={() => navigate(item.id)}
                >
                  <span className="sidebar-item-icon">{item.icon}</span>
                  {!sidebarCollapsed && <span className="sidebar-item-label">{item.label}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button
            type="button"
            className="sidebar-collapse-btn"
            onClick={() => setSidebarCollapsed((v) => !v)}
            title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
          >
            {sidebarCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
            {!sidebarCollapsed && <span>收起菜单</span>}
          </button>
        </div>
      </aside>

      <div className="console-body">
        <header className="topbar no-print">
          <div className="topbar-console-inner">
            <div className="topbar-left">
              <button
                type="button"
                className="btn mobile-menu-btn"
                onClick={() => setMobileMenuOpen(true)}
                aria-label="打开菜单"
              >
                <Menu size={16} />
              </button>
              <div className="page-heading">
                <span className="page-heading-title">{currentNav.label}</span>
                <span className="page-heading-sep">/</span>
                <span className="page-heading-desc">{currentNav.desc}</span>
              </div>
            </div>

            <div className="topbar-actions">
              <button
                className="btn"
                onClick={() => setShowProfile(true)}
                title="个人资料、公司邮箱与密码设置"
              >
                <UserCircle2 size={15} />
                <span>{currentUser.display_name}</span>
                <span className="chip" style={{ padding: "0 6px", fontSize: 11 }}>
                  {ROLE_BADGE[currentUser.role]}
                </span>
              </button>
              <button className="btn" onClick={() => void loadOverview()} disabled={loading} title="刷新看板数据">
                <RefreshCw size={14} /> 刷新
              </button>
              <button className="btn" onClick={() => setDark(!dark)} aria-label="切换主题" title="切换明暗主题">
                {dark ? <Sun size={15} /> : <Moon size={15} />}
              </button>
              <button className="btn" onClick={() => void handleLogout()} title="退出登录">
                <LogOut size={14} /> 退出
              </button>
            </div>
          </div>
        </header>

        <main className="main-wrap">
          {error && (
            <div className="card no-print" style={{ marginBottom: 14, borderColor: "var(--critical)" }}>
              <b style={{ color: "var(--critical)" }}>API 连接提示：</b>
              <span>{error}</span>
            </div>
          )}
          {page === "dashboard" && (
            <DashboardPage data={overview} loading={loading} onOpenReport={openReport} />
          )}
          {page === "progress" && (
            <ProgressPage
              currentUser={currentUser}
              initialTaskId={focusTaskId}
              onOpenReport={openReport}
              onScanStarted={handleScanStarted}
            />
          )}
          {page === "runs" && (
            <RunsPage
              currentUser={currentUser}
              onOpenReport={openReport}
              onScanStarted={handleScanStarted}
            />
          )}
          {page === "reports" && (
            <ReportsPage
              currentUser={currentUser}
              initialTarget={reportTarget}
              onTargetChange={setReportTarget}
            />
          )}
          {page === "projects" && <ProjectsPage currentUser={currentUser} />}
          {page === "prompts" && isAdmin && <PromptsPage />}
          {page === "config" && (
            <ConfigPage currentUser={currentUser} onScanStarted={handleScanStarted} />
          )}
          {page === "users" && isAdmin && <UsersPage currentUser={currentUser} />}
          {page === "ops" && isSuper && <OpsConfigPage />}
        </main>
      </div>

      {(showProfile || currentUser.must_change_password) && (
        <ProfileModal
          user={currentUser}
          forcePasswordChange={currentUser.must_change_password}
          onUpdated={(next) => {
            setCurrentUser(next);
            if (!next.must_change_password) setShowProfile(false);
          }}
          onClose={() => setShowProfile(false)}
        />
      )}
    </div>
  );
}
