import type {
  CatalogProject,
  DoctorReport,
  LiveProgressResponse,
  OverviewResponse,
  ReportResponse,
  RunDetailResponse,
  RunSummary,
  ScanTask,
  ScheduleResponse,
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
  authStatus: () => request<{ auth_required: boolean; authenticated: boolean }>("/api/auth/status"),
  login: (token: string) =>
    request<{ authenticated: boolean; auth_required: boolean }>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }),
  logout: () => request<{ authenticated: boolean }>("/api/auth/logout", { method: "POST" }),
  overview: () => request<OverviewResponse>("/api/overview"),
  runs: () => request<{ runs: RunSummary[] }>("/api/runs"),
  runDetail: (date: string, runId: string) =>
    request<RunDetailResponse>(`/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}`),
  markRunAborted: (date: string, runId: string) =>
    request<{ ok: boolean; status: string }>(
      `/api/runs/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/mark-aborted`,
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
  projects: () => request<{ projects: CatalogProject[]; scan_statuses?: string[] }>("/api/projects"),
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
