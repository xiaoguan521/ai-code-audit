import type {
  AuditLogEntry,
  AuthUser,
  CatalogProject,
  DoctorReport,
  EmailDispatchResult,
  EmailLogEntry,
  GitCredentialItem,
  GitProbeResult,
  LiveProgressResponse,
  ModelTestResult,
  OpsConfigResponse,
  OpsRuntimeConfig,
  OverviewResponse,
  OwnerPreviewItem,
  PauseState,
  PromptItem,
  ReportResponse,
  RunDetailResponse,
  RunSummary,
  ScanTask,
  ScheduleResponse,
  SmtpConfig,
  StaleThresholds,
  UserRole,
} from "./types";

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin", ...init });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err?.error) msg = err.error;
    } catch {
      // ignore
    }
    if (res.status === 401 && url !== "/api/auth/login" && unauthorizedHandler) {
      unauthorizedHandler();
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean; auth_required: boolean; has_frontend: boolean }>("/api/health"),
  authStatus: () =>
    request<{ auth_required: boolean; authenticated: boolean; user?: AuthUser | null }>("/api/auth/status"),
  login: (username: string, password: string) =>
    request<{ authenticated: boolean; auth_required: boolean; user: AuthUser }>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ authenticated: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => request<{ user: AuthUser }>("/api/me"),
  updateProfile: (payload: { display_name?: string; email?: string; email_notify?: "batch" | "risk_only" | "off" }) =>
    request<{ user: AuthUser }>("/api/me/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  changePassword: (old_password: string, new_password: string) =>
    request<{ ok: boolean; user: AuthUser }>("/api/me/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password, new_password }),
    }),
  users: () => request<{ users: AuthUser[] }>("/api/users"),
  createUser: (payload: {
    username: string;
    display_name: string;
    role: UserRole;
    password: string;
    email: string;
    must_change_password?: boolean;
  }) =>
    request<{ user: AuthUser }>("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateUser: (
    userId: number,
    payload: {
      display_name?: string;
      role?: UserRole;
      email?: string;
      is_active?: boolean;
      email_notify?: "batch" | "risk_only" | "off";
    }
  ) =>
    request<{ user: AuthUser }>(`/api/users/${userId}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  resetUserPassword: (userId: number, new_password: string, must_change_password = true) =>
    request<{ ok: boolean }>(`/api/users/${userId}/reset-password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password, must_change_password }),
    }),
  deleteUser: (userId: number) =>
    request<{ ok: boolean }>(`/api/users/${userId}/delete`, { method: "POST" }),
  previewUsersFromProjects: (email_domain = "shineyue.com") =>
    request<{ items: OwnerPreviewItem[]; email_domain: string }>("/api/users/preview-from-projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email_domain }),
    }),
  batchCreateUsers: (
    items: Array<{ username: string; display_name: string; email: string; role: UserRole }>,
    default_password: string
  ) =>
    request<{ created: AuthUser[]; skipped: Array<{ username?: string; display_name?: string; reason: string }> }>(
      "/api/users/batch-create",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, default_password }),
      }
    ),
  auditLogs: (limit = 150) => request<{ logs: AuditLogEntry[] }>(`/api/audit-logs?limit=${limit}`),
  overview: () => request<OverviewResponse>("/api/overview"),
  runs: () => request<{ runs: RunSummary[] }>("/api/runs"),
  runDetail: (date: string, runId: string) =>
    request<RunDetailResponse>(`/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}`),
  markRunAborted: (date: string, runId: string) =>
    request<{ ok: boolean; status: string }>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/mark-aborted`,
      { method: "POST" }
    ),
  deleteRun: (date: string, runId: string) =>
    request<{ ok: boolean; date: string; run_id: string; rebuilt_states: string[]; cleared_states: string[] }>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/delete`,
      { method: "POST" }
    ),
  generateSummary: (date: string, runId: string) =>
    request<{ ok: boolean; date: string; run_id: string; ok_count: number; markdown: string }>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/summary`,
      { method: "POST" }
    ),
  downloadReportDocx: async (date: string, runId: string, target: string, refresh = false) => {
    const qs = refresh ? "?refresh=1" : "";
    const url = `/api/reports/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/${encodeURIComponent(target)}/docx${qs}`;
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const err = await res.json();
        if (err?.error) msg = err.error;
      } catch {
        // ignore
      }
      if (res.status === 401 && unauthorizedHandler) unauthorizedHandler();
      throw new Error(msg);
    }
    const blob = await res.blob();
    const fname =
      target === "_summary" ? `代码检查分析报告汇总_${date}_${runId}.docx` : `${date}_${target}.docx`;
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
  },
  rejectedDraft: (date: string, runId: string, projectId: string) =>
    request<{ date: string; run_id: string; project_id: string; markdown: string }>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/${encodeURIComponent(projectId)}/rejected`
    ),
  report: (date: string, runId: string, target: string) =>
    request<ReportResponse>(
      `/api/reports/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/${encodeURIComponent(target)}`
    ),
  projects: () =>
    request<{ projects: CatalogProject[]; stale_thresholds?: StaleThresholds; scan_statuses?: string[] }>(
      "/api/projects"
    ),
  ownersList: () =>
    request<{ users: Array<{ id: number; username: string; display_name: string; role: UserRole; email: string }> }>(
      "/api/users?scope=owners"
    ),
  createProject: (payload: Partial<CatalogProject>) =>
    request<{ project: CatalogProject }>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateProject: (projectId: string, payload: Partial<CatalogProject>) =>
    request<{ project: CatalogProject }>(`/api/projects/${encodeURIComponent(projectId)}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteProject: (projectId: string) =>
    request<{ ok: boolean; project_id: string }>(`/api/projects/${encodeURIComponent(projectId)}/delete`, {
      method: "POST",
    }),
  probeGit: (git_url: string, branch = "", project_id = "", vcs_type = "") =>
    request<GitProbeResult>("/api/projects/probe-git", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        git_url,
        branch,
        ...(project_id ? { project_id } : {}),
        ...(vcs_type ? { vcs_type } : {}),
      }),
    }),
  probeAllProjectsGit: (only_active = true) =>
    request<{
      total: number;
      ok: number;
      failed: number;
      failed_items: Array<{ project_id: string; project_name: string; branch: string; error: string }>;
    }>("/api/projects/probe-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ only_active }),
    }),
  importProjectsCsv: (csv_text: string, mode: "upsert" | "replace" = "upsert") =>
    request<{ created: number; updated: number; errors: Array<{ line: number; project_id: string; reason: string }> }>(
      "/api/projects/import",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv_text, mode }),
      }
    ),
  downloadProjectsCsv: async () => {
    const res = await fetch("/api/projects/export", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`导出失败 (HTTP ${res.status})`);
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = "projects.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
  },
  refreshProjectMetrics: (project_ids?: string[]) =>
    request<{ scanned: number; updated: number; missing_repo: number }>("/api/projects/refresh-metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_ids }),
    }),
  saveStaleThresholds: (warn_days: number, critical_days: number) =>
    request<{ stale_thresholds: StaleThresholds }>("/api/projects/stale-thresholds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ warn_days, critical_days }),
    }),
  pauseScan: (reason = "") =>
    request<PauseState>("/api/scans/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),
  resumeScan: () =>
    request<PauseState>("/api/scans/resume", {
      method: "POST",
    }),
  prompts: () => request<{ prompts: PromptItem[] }>("/api/prompts"),
  savePrompt: (key: string, content: string, note = "") =>
    request<{ prompts: PromptItem[] }>(`/api/prompts/${encodeURIComponent(key)}/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, note }),
    }),
  rollbackPrompt: (key: string, version_id: number) =>
    request<{ prompts: PromptItem[] }>(`/api/prompts/${encodeURIComponent(key)}/rollback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version_id }),
    }),
  resetPrompt: (key: string) =>
    request<{ prompts: PromptItem[] }>(`/api/prompts/${encodeURIComponent(key)}/reset`, {
      method: "POST",
    }),
  opsConfig: () => request<OpsConfigResponse>("/api/ops/config"),
  saveOpsModel: (payload: Record<string, string | boolean>) =>
    request<OpsConfigResponse>("/api/ops/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  testOpsModel: (payload: { OPENAI_BASE_URL?: string; OPENAI_MODEL?: string; OPENAI_API_KEY?: string }) =>
    request<ModelTestResult>("/api/ops/test-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  saveOpsRuntime: (payload: Partial<OpsRuntimeConfig>) =>
    request<OpsConfigResponse>("/api/ops/runtime", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  createGitCredential: (payload: {
    label?: string;
    protocol: "https" | "http" | "svn";
    host: string;
    path_prefix?: string;
    username: string;
    secret: string;
    is_active?: boolean;
  }) =>
    request<{ credential: GitCredentialItem; git_credentials: GitCredentialItem[] }>("/api/ops/git-credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateGitCredential: (
    id: number,
    payload: {
      label?: string;
      protocol: "https" | "http" | "svn";
      host: string;
      path_prefix?: string;
      username: string;
      secret?: string;
      is_active?: boolean;
    }
  ) =>
    request<{ credential: GitCredentialItem; git_credentials: GitCredentialItem[] }>(
      `/api/ops/git-credentials/${id}/update`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    ),
  deleteGitCredential: (id: number) =>
    request<{ ok: boolean; git_credentials: GitCredentialItem[] }>(`/api/ops/git-credentials/${id}/delete`, {
      method: "POST",
    }),
  smtpConfig: () => request<{ smtp: SmtpConfig }>("/api/ops/smtp"),
  saveSmtpConfig: (payload: Partial<SmtpConfig> & { password?: string; clear_password?: boolean }) =>
    request<{ smtp: SmtpConfig }>("/api/ops/smtp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  testSmtpEmail: (payload: { to_email: string } & Partial<SmtpConfig> & { password?: string }) =>
    request<{ ok: boolean; elapsed_ms: number; error: string }>("/api/ops/smtp/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  emailLogs: (limit = 100) => request<{ logs: EmailLogEntry[] }>(`/api/email-logs?limit=${limit}`),
  retryEmailLog: (logId: number) =>
    request<EmailDispatchResult>(`/api/email-logs/${logId}/retry`, { method: "POST" }),
  sendRunEmails: (date: string, runId: string, recipient_email?: string) =>
    request<EmailDispatchResult>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/send-emails`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(recipient_email ? { recipient_email } : {}),
      }
    ),
  doctor: () => request<DoctorReport>("/api/doctor"),
  schedule: () => request<ScheduleResponse>("/api/schedule"),
  saveSchedule: (payload: {
    enabled: boolean;
    time: string;
    weekdays: number[];
    fast: boolean;
    resume: boolean;
    force_full: boolean;
    only: string[];
  }) =>
    request<ScheduleResponse>("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  scanProgress: () => request<LiveProgressResponse>("/api/scans/progress"),
  scans: () => request<{ tasks: ScanTask[] }>("/api/scans"),
  scanDetail: (taskId: string) => request<ScanTask>(`/api/scans/${encodeURIComponent(taskId)}`),
  cancelScan: (taskId: string) =>
    request<{ task_id: string; status: string }>(`/api/scans/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
    }),
  startScan: (payload: {
    only?: string[];
    fast?: boolean;
    force_full?: boolean;
    resume?: boolean;
    dry_run?: boolean;
    summary_only?: boolean;
    snapshot_at?: string;
  }) =>
    request<{ task_id: string; status: string; command: string }>("/api/scans", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
};
