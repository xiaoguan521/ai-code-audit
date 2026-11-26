import type {
  CatalogProject,
  DoctorReport,
  OverviewResponse,
  ReportResponse,
  RunDetailResponse,
  RunSummary,
  ScanTask,
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
  report: (date: string, runId: string, target: string) =>
    request<ReportResponse>(
      `/api/reports/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/${encodeURIComponent(target)}`
    ),
  projects: () => request<{ projects: CatalogProject[]; scan_statuses?: string[] }>("/api/projects"),
  doctor: () => request<DoctorReport>("/api/doctor"),
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
