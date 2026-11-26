import { FormEvent, useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  FileText,
  History,
  Lock,
  LogOut,
  Moon,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { api, setUnauthorizedHandler } from "./api";
import { ConfigPage } from "./pages/ConfigPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ProgressPage } from "./pages/ProgressPage";
import { ReportsPage } from "./pages/ReportsPage";
import { RunsPage } from "./pages/RunsPage";
import type { OverviewResponse } from "./types";

type Page = "dashboard" | "progress" | "runs" | "reports" | "config";
type ReportTarget = { date: string; runId: string; target: string };

function parseHash(): { page: Page; reportTarget: ReportTarget | null } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  let page: Page = "dashboard";
  if (pathPart === "progress" || pathPart === "scans") page = "progress";
  else if (pathPart === "runs") page = "runs";
  else if (pathPart === "reports") page = "reports";
  else if (pathPart === "config") page = "config";

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
  const [authRequired, setAuthRequired] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);

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
      setAuthenticated(false);
      setAuthRequired(true);
      setAuthError("会话已过期，请重新输入访问令牌");
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
        setAuthRequired(st.auth_required);
        setAuthenticated(st.authenticated);
        setAuthChecked(true);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "无法连接后端 API 服务");
        setAuthenticated(true);
        setAuthChecked(true);
      });
  }, []);

  useEffect(() => {
    if (authChecked && authenticated) {
      void loadOverview();
    }
  }, [authChecked, authenticated]);

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    setLoggingIn(true);
    setAuthError(null);
    try {
      await api.login(tokenInput.trim());
      setTokenInput("");
      setAuthenticated(true);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "认证失败");
    } finally {
      setLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
    } finally {
      setOverview(null);
      setAuthenticated(false);
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

  if (authRequired && !authenticated) {
    return (
      <div className="app-shell" style={{ alignItems: "center", justifyContent: "center", padding: 20 }}>
        <form className="card" onSubmit={(e) => void handleLogin(e)} style={{ width: "100%", maxWidth: 380 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
            <span className="brand-badge">
              <Lock size={16} />
            </span>
            <div>
              <div style={{ fontWeight: 700, fontSize: 16 }}>八性代码质量审查平台</div>
              <div className="muted" style={{ fontSize: 12 }}>
                请输入访问令牌（AUDIT_WEB_TOKEN）
              </div>
            </div>
          </div>
          {authError && (
            <div style={{ color: "var(--critical)", fontSize: 13, marginBottom: 10 }}>{authError}</div>
          )}
          <input
            type="password"
            autoFocus
            placeholder="输入访问令牌"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            style={{ width: "100%", marginBottom: 12, padding: "9px 12px" }}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={loggingIn || !tokenInput.trim()}
            style={{ width: "100%", justifyContent: "center", padding: "9px 12px" }}
          >
            {loggingIn ? "验证中…" : "登录"}
          </button>
        </form>
      </div>
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
              <Settings size={15} /> 扫描配置
            </button>
          </nav>

          <div className="topbar-actions">
            <button className="btn" onClick={() => void loadOverview()} disabled={loading}>
              <RefreshCw size={14} /> 刷新
            </button>
            <button className="btn" onClick={() => setDark(!dark)} aria-label="切换主题">
              {dark ? <Sun size={15} /> : <Moon size={15} />}
            </button>
            {authRequired && (
              <button className="btn" onClick={() => void handleLogout()} title="退出登录">
                <LogOut size={14} /> 退出
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="main-wrap">
        {error && (
          <div className="card no-print" style={{ marginBottom: 14, borderColor: "var(--critical)" }}>
            <b style={{ color: "var(--critical)" }}>API 连接提示：</b>
            <span>{error}（请确认已启动 python3 runner/web_server.py）</span>
          </div>
        )}
        {page === "dashboard" && (
          <DashboardPage data={overview} loading={loading} onOpenReport={openReport} />
        )}
        {page === "progress" && (
          <ProgressPage
            initialTaskId={focusTaskId}
            onOpenReport={openReport}
            onScanStarted={handleScanStarted}
          />
        )}
        {page === "runs" && (
          <RunsPage onOpenReport={openReport} onScanStarted={handleScanStarted} />
        )}
        {page === "reports" && (
          <ReportsPage initialTarget={reportTarget} onTargetChange={setReportTarget} />
        )}
        {page === "config" && <ConfigPage onScanStarted={handleScanStarted} />}
      </main>
    </div>
  );
}
