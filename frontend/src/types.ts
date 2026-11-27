export type UserRole = "superadmin" | "admin" | "user";

export interface AuthUser {
  id: number;
  username: string;
  display_name: string;
  role: UserRole;
  email: string;
  must_change_password: boolean;
  email_notify: "batch" | "risk_only" | "off";
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
  last_login_at?: string | null;
}

export interface OwnerPreviewItem {
  display_name: string;
  username: string;
  email: string;
  role: UserRole;
  exists: boolean;
}

export interface AuditLogEntry {
  id: number;
  actor_username: string;
  actor_role: string;
  action: string;
  target: string;
  detail: string;
  client_ip: string;
  created_at: string;
}

export interface RunSummary {
  date: string;
  run_id: string;
  status: string;
  report_mode?: string;
  triggered_by?: string;
  trigger?: string;
  can_delete?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  snapshot_at?: string | null;
  summary_status?: string | null;
  has_summary: boolean;
  total_projects: number;
  ok_count: number;
  error_count: number;
  pending_count: number;
}

export interface ProjectRow {
  project_id: string;
  project_name: string;
  department: string;
  owner: string;
  tech_stack: string;
  status: string;
  error?: string | null;
  scan_type?: string;
  report_mode?: string;
  commit?: string;
  date?: string;
  run_id?: string | null;
  total: number | null;
  delta_total?: number | null;
  delta_security?: number | null;
  prev_date?: string | null;
  grade: string;
  security: number | null;
  coverage: number | null;
  scored_dimensions?: number | null;
  security_p0: number;
  security_p1: number;
  P0: number;
  P1: number;
  P2: number;
  P3: number;
  dim_scores?: Record<string, number | null>;
}

export interface TrendPoint {
  date: string;
  run_id: string;
  total: number | null;
  security: number | null;
  count: number;
}

export interface DimAvg {
  id: string;
  name: string;
  avg: number | null;
  count: number;
}

export interface OverviewResponse {
  latest_run: RunSummary | null;
  runs_count: number;
  rows: ProjectRow[];
  trend: TrendPoint[];
  dims: DimAvg[];
}

export interface RunProjectRecord {
  project_id: string;
  project_name?: string;
  department?: string;
  owner?: string;
  status: string;
  scan_type?: string;
  report_mode?: string;
  commit?: string;
  error?: string;
  report_validation?: {
    ok?: boolean;
    problems?: string[];
  };
  secret_scan?: {
    blocked?: boolean;
    patterns?: string[];
  };
  has_rejected_draft?: boolean;
  report_scores?: {
    total?: number | null;
    grade?: string;
    security_score?: number | null;
    coverage?: number | null;
    scored_dimensions?: number | null;
    not_evaluated?: number | null;
    security_p0?: number;
    security_p1?: number;
  };
  metrics?: {
    stages_seconds?: Record<string, number>;
  };
}

export interface RunDetailResponse {
  date: string;
  run_id: string;
  status: string;
  report_mode?: string;
  triggered_by?: string;
  trigger?: string;
  can_delete?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  snapshot_at?: string | null;
  summary_status?: string | null;
  has_summary: boolean;
  projects: RunProjectRecord[];
  pending_projects?: string[];
}

export interface ReportDimension {
  id: string;
  name: string;
  score: number | null;
  coverage: number | null;
  counts?: Record<string, number> | null;
  top_risk?: string;
}

export interface ReportIssue {
  issue_id: string;
  item_id: string;
  level: string;
  title: string;
  evidence_text: string;
  impact: string;
  suggestion: string;
  effort: string;
}

export interface ReportResponse {
  type: "project" | "summary";
  date: string;
  run_id: string;
  project_id?: string;
  markdown: string;
  dimensions?: ReportDimension[];
  issues?: ReportIssue[];
  issue_counts?: Record<string, number>;
  scores?: {
    total?: number | null;
    grade?: string;
    security_score?: number | null;
    coverage?: number | null;
    scored_dimensions?: number | null;
  } | null;
}

export interface CatalogProject {
  project_id: string;
  project_name: string;
  department: string;
  owner: string;
  git_url: string;
  branch: string;
  status: string;
  tech_stack: string;
  project_type?: string;
  notes?: string;
  state?: {
    last_scan_date?: string;
    last_commit?: string;
    report_mode?: string;
    scores?: {
      total?: number | null;
      grade?: string;
      security_score?: number | null;
    };
  };
}

export interface ScanTask {
  task_id: string;
  trigger?: "manual" | "schedule";
  command: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  started_at: string;
  finished_at: string | null;
  returncode: number | null;
  latest_log?: string;
  logs?: string[];
}

export interface ScheduleConfig {
  enabled: boolean;
  time: string;
  weekdays: number[];
  fast: boolean;
  resume: boolean;
  force_full: boolean;
  only: string[];
  last_triggered_slot: string | null;
  last_triggered_at: string | null;
  last_task_id: string | null;
  last_error: string | null;
}

export interface ScheduleResponse {
  schedule: ScheduleConfig;
  next_run_at: string | null;
  server_time: string;
}

export interface RunProgressProjectItem {
  project_id: string;
  project_name: string;
  department: string;
  owner: string;
  state: "ok" | "error" | "scanning" | "queued";
  status: string;
  total: number | null;
  error?: string | null;
  validation_problems?: string[];
  secret_patterns?: string[];
  has_rejected_draft?: boolean;
  duration_seconds: number | null;
  elapsed_seconds: number | null;
  eta_seconds: number | null;
  timeout_remaining_seconds: number | null;
}

export interface RunProgressItem {
  date: string;
  run_id: string;
  status: string;
  report_mode: string;
  started_at: string | null;
  finished_at: string | null;
  snapshot_at?: string | null;
  summary_status: string | null;
  elapsed_seconds?: number | null;
  avg_project_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  parallelism?: number;
  total_count: number;
  completed_count: number;
  ok_count: number;
  error_count: number;
  scanning_count?: number;
  queued_count?: number;
  pending_count: number;
  percent: number;
  items?: RunProgressProjectItem[];
  completed_projects: {
    project_id: string;
    project_name: string;
    status: string;
    total: number | null;
    error?: string | null;
  }[];
  pending_projects: string[];
}

export interface LiveProgressResponse {
  active_runs: RunProgressItem[];
  latest_run: RunProgressItem | null;
  running_task: ScanTask | null;
  tasks: ScanTask[];
}

export interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "error";
  detail: string;
  fix?: string;
}

export interface DoctorReport {
  verdict: "ok" | "warn" | "error";
  errors: number;
  warnings: number;
  checks: DoctorCheck[];
}
