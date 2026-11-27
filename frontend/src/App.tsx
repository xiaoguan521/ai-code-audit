import { FormEvent, useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  FileText,
  History,
  LogOut,
  Moon,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sun,
  UserCircle2,
  Users,
} from "lucide-react";
import { api, setUnauthorizedHandler } from "./api";
import { LoginView } from "./components/LoginView";
import { ProfileModal } from "./components/ProfileModal";
import { ConfigPage } from "./pages/ConfigPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ProgressPage } from "./pages/ProgressPage";
import { ReportsPage } from "./pages/ReportsPage";
import { RunsPage } from "./pages/RunsPage";
import { UsersPage } from "./pages/UsersPage";
import type { AuthUser, OverviewResponse } from "./types";

type Page = "dashboard" | "progress" | "runs" | "reports" | "config" | "users";
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
  else if (pathPart === "config") page = "config";
  else if (pathPart === "users") page = "users";

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
      if (page === "users" && currentUser.role === "user") {
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

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-badge">
              <ShieldCheck size={17} />
            </span>
            <span>八性代码质量审查平台</span>
          </div>

          <nav className="nav-tabs">
            <button
              className={`nav-tab ${page === "dashboard" ? "active" : ""}`}
              onClick={() => navigate("dashboard")}
            >
              <BarChart3 size={15} /> 质量看板
            </button>
            <button
              className={`nav-tab ${page === "progress" ? "active" : ""}`}
              onClick={() => navigate("progress")}
            >
              <Activity size={15} /> 实时进度
            </button>
            <button
              className={`nav-tab ${page === "runs" ? "active" : ""}`}
              onClick={() => navigate("runs")}
            >
              <History size={15} /> 批次记录
            </button>
            <button
              className={`nav-tab ${page === "reports" ? "active" : ""}`}
              onClick={() => navigate("reports")}
            >
              <FileText size={15} /> 报告阅读
            </button>
            <button
              className={`nav-tab ${page === "config" ? "active" : ""}`}
              onClick={() => navigate("config")}
            >
              <Settings size={15} /> {currentUser.role === "user" ? "我的项目扫描" : "扫描配置"}
            </button>
            {(currentUser.role === "superadmin" || currentUser.role === "admin") && (
              <button
                className={`nav-tab ${page === "users" ? "active" : ""}`}
                onClick={() => navigate("users")}
              >
                <Users size={15} /> 用户管理
              </button>
            )}
          </nav>

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
            <button className="btn" onClick={() => void loadOverview()} disabled={loading}>
              <RefreshCw size={14} /> 刷新
            </button>
            <button className="btn" onClick={() => setDark(!dark)} aria-label="切换主题">
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
        {page === "config" && (
          <ConfigPage currentUser={currentUser} onScanStarted={handleScanStarted} />
        )}
        {page === "users" && (currentUser.role === "superadmin" || currentUser.role === "admin") && (
          <UsersPage currentUser={currentUser} />
        )}
      </main>

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
