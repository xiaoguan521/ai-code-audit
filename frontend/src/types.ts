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

export interface StaleThresholds {
  warn_days: number;
  critical_days: number;
}

export interface PauseState {
  paused: boolean;
  paused_at?: string | null;
  paused_by?: string;
  reason?: string;
}

export interface GitProbeResult {
  ok: boolean;
  vcs_type?: "git" | "svn";
  branches: string[];
  default_branch: string;
  branch_exists: boolean;
  revision?: string;
  error: string;
}

export interface ProjectRow {
  project_id: string;
  project_name: string;
  department: string;
  owner: string;
  tech_stack: string;
  branch?: string;
  catalog_status?: string;
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
  loc_total?: number | null;
  file_count?: number | null;
  languages?: Record<string, number>;
  last_commit_at?: string | null;
  last_commit_author?: string;
  last_commit_subject?: string;
  inactive_days?: number | null;
  stale_level?: "active" | "warning" | "critical" | "unknown";
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
  stale_thresholds?: StaleThresholds;
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
  vcs_type?: "git" | "svn";
  git_url: string;
  branch: string;
  status: string;
  tech_stack: string;
  project_type?: string;
  core_flows?: string;
  scope_out?: string;
  materials_dir?: string;
  weights?: string;
  notes?: string;
  loc_total?: number | null;
  file_count?: number | null;
  languages?: Record<string, number>;
  last_commit_at?: string | null;
  last_commit_author?: string;
  last_commit_subject?: string;
  inactive_days?: number | null;
  stale_level?: "active" | "warning" | "critical" | "unknown";
  git_health?: "ok" | "error" | "unknown";
  git_health_reason?: string;
  git_checked_at?: string | null;
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
  pause_state?: PauseState;
}

export interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "error";
  detail: string;
  fix?: string;
}

export interface PromptVersionEntry {
  id: number;
  prompt_key: string;
  version: number;
  note: string;
  updated_by: string;
  is_active: number;
  created_at: string;
  char_count: number;
}

export interface PromptItem {
  key: "skill_md" | "dispatch_extra" | "summary_prompt";
  title: string;
  description: string;
  is_custom: boolean;
  active_version: number;
  updated_by: string;
  updated_at: string | null;
  content: string;
  default_content: string;
  versions: PromptVersionEntry[];
}

export interface OpsModelConfig {
  source: string;
  OPENAI_BASE_URL: string;
  OPENAI_MODEL: string;
  OPENAI_API_KEY_SET: boolean;
  OPENAI_API_KEY_MASKED: string;
  OPENAI_CONTEXT_WINDOW: string;
  OPENAI_MAX_OUTPUT_TOKENS: string;
  DEEPSEEK_BASE_URL: string;
  DEEPSEEK_MODEL: string;
  DEEPSEEK_API_KEY_SET: boolean;
  DEEPSEEK_API_KEY_MASKED: string;
  extra_env: Record<string, string>;
}

export interface OpsRuntimeConfig {
  max_parallel: number;
  max_model_concurrency: number;
  per_project_timeout_sec: number;
  timeout_small_sec: number;
  timeout_medium_sec: number;
  timeout_large_sec: number;
  batch_timeout_sec: number;
  fast_mode: boolean;
  fast_skip_lizard: boolean;
  adaptive_timeout: boolean;
  validation_mode: "lenient" | "strict";
  summary_enabled: boolean;
  scanner_image: string;
  model_network: string;
  semgrep_rules_dir: string;
}

export interface GitCredentialItem {
  id: number;
  label: string;
  protocol: "https" | "http" | "svn";
  host: string;
  path_prefix: string;
  username: string;
  has_secret: boolean;
  secret_masked: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface OpsConfigResponse {
  model: OpsModelConfig;
  runtime: OpsRuntimeConfig;
  runtime_overridden: boolean;
  git_credentials: GitCredentialItem[];
  base_git_creds_present: boolean;
}

export interface ModelTestResult {
  ok: boolean;
  status_code: number;
  elapsed_ms: number;
  model: string;
  reply_preview: string;
  has_thinking: boolean;
  error: string;
}

export interface SmtpConfig {
  enabled: boolean;
  host: string;
  port: number;
  security: "ssl" | "starttls" | "none";
  username: string;
  password_set: boolean;
  password_masked: string;
  from_email: string;
  from_name: string;
  portal_url: string;
  attachment_format: "docx" | "md" | "both";
  max_attach_mb: number;
  auto_send_on_batch: boolean;
  summary_cc_emails: string;
}

export interface EmailLogEntry {
  id: number;
  run_date: string;
  run_id: string;
  recipient_email: string;
  recipient_name: string;
  recipient_type: "owner" | "summary" | "test" | string;
  project_ids: string[];
  subject: string;
  attachment_names: string[];
  status: "sent" | "failed" | "skipped" | string;
  error: string;
  created_at: string;
}

export interface EmailDispatchResult {
  ok: boolean;
  skipped_reason?: string;
  sent: number;
  failed: number;
  skipped: number;
  details: EmailLogEntry[];
}

export interface DoctorReport {
  verdict: "ok" | "warn" | "error";
  errors: number;
  warnings: number;
  checks: DoctorCheck[];
}
