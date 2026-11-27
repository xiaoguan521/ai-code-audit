import { FormEvent, useEffect, useState } from "react";
import {
  Check,
  Cpu,
  KeyRound,
  Mail,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  SlidersHorizontal,
  Trash2,
  Zap,
} from "lucide-react";
import { api } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type {
  EmailLogEntry,
  GitCredentialItem,
  ModelTestResult,
  OpsConfigResponse,
  OpsRuntimeConfig,
  SmtpConfig,
} from "../types";

export function OpsConfigPage() {
  const [tab, setTab] = useState<"model" | "runtime" | "git" | "smtp">("model");
  const [cfg, setCfg] = useState<OpsConfigResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 模型配置表单
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [contextWindow, setContextWindow] = useState("131072");
  const [maxOutputTokens, setMaxOutputTokens] = useState("8192");
  const [dsBaseUrl, setDsBaseUrl] = useState("");
  const [dsModel, setDsModel] = useState("");
  const [dsApiKey, setDsApiKey] = useState("");
  const [testingModel, setTestingModel] = useState(false);
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null);

  // 运行时环境变量表单
  const [runtime, setRuntime] = useState<OpsRuntimeConfig | null>(null);

  // Git / SVN 账号弹窗
  const [gitModal, setGitModal] = useState<{ mode: "create" | "edit"; item?: GitCredentialItem } | null>(null);
  const [gitLabel, setGitLabel] = useState("");
  const [gitProtocol, setGitProtocol] = useState<"https" | "http" | "svn">("https");
  const [gitHost, setGitHost] = useState("");
  const [gitPathPrefix, setGitPathPrefix] = useState("");
  const [gitUsername, setGitUsername] = useState("");
  const [gitSecret, setGitSecret] = useState("");
  const [gitActive, setGitActive] = useState(true);
  const [deleteGitTarget, setDeleteGitTarget] = useState<GitCredentialItem | null>(null);

  // SMTP 邮件投递配置与日志
  const [smtp, setSmtp] = useState<SmtpConfig | null>(null);
  const [smtpPassword, setSmtpPassword] = useState("");
  const [testToEmail, setTestToEmail] = useState("");
  const [testingSmtp, setTestingSmtp] = useState(false);
  const [emailLogs, setEmailLogs] = useState<EmailLogEntry[]>([]);

  const hydrate = (res: OpsConfigResponse) => {
    setCfg(res);
    setBaseUrl(res.model.OPENAI_BASE_URL || "");
    setModelName(res.model.OPENAI_MODEL || "");
    setApiKey("");
    setContextWindow(res.model.OPENAI_CONTEXT_WINDOW || "131072");
    setMaxOutputTokens(res.model.OPENAI_MAX_OUTPUT_TOKENS || "8192");
    setDsBaseUrl(res.model.DEEPSEEK_BASE_URL || "");
    setDsModel(res.model.DEEPSEEK_MODEL || "");
    setDsApiKey("");
    setRuntime(res.runtime);
  };

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [res, smtpRes, logRes] = await Promise.all([
        api.opsConfig(),
        api.smtpConfig(),
        api.emailLogs(100),
      ]);
      hydrate(res);
      setSmtp(smtpRes.smtp);
      setSmtpPassword("");
      setEmailLogs(logRes.logs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载运维配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const handleSaveModel = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveOpsModel({
        OPENAI_BASE_URL: baseUrl.trim(),
        OPENAI_MODEL: modelName.trim(),
        OPENAI_API_KEY: apiKey.trim(),
        OPENAI_CONTEXT_WINDOW: contextWindow.trim(),
        OPENAI_MAX_OUTPUT_TOKENS: maxOutputTokens.trim(),
        DEEPSEEK_BASE_URL: dsBaseUrl.trim(),
        DEEPSEEK_MODEL: dsModel.trim(),
        DEEPSEEK_API_KEY: dsApiKey.trim(),
      });
      hydrate(res);
      setNotice("模型环境变量已保存至 model.runtime.env，下一次扫描即时热生效（免重启容器）");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存模型配置失败");
    } finally {
      setBusy(false);
    }
  };

  const handleTestModel = async () => {
    setTestingModel(true);
    setTestResult(null);
    setError(null);
    try {
      const res = await api.testOpsModel({
        OPENAI_BASE_URL: baseUrl.trim(),
        OPENAI_MODEL: modelName.trim(),
        OPENAI_API_KEY: apiKey.trim(),
      });
      setTestResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "模型连通测试失败");
    } finally {
      setTestingModel(false);
    }
  };

  const handleSaveRuntime = async (e: FormEvent) => {
    e.preventDefault();
    if (!runtime) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveOpsRuntime(runtime);
      hydrate(res);
      setNotice("运行参数与环境变量已热保存（runtime_overrides.json），后续触发的扫描将自动应用");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存运行参数失败");
    } finally {
      setBusy(false);
    }
  };

  const openGitCreate = () => {
    setGitLabel("");
    setGitProtocol("https");
    setGitHost("");
    setGitPathPrefix("");
    setGitUsername("");
    setGitSecret("");
    setGitActive(true);
    setGitModal({ mode: "create" });
  };

  const openGitEdit = (item: GitCredentialItem) => {
    setGitLabel(item.label || "");
    setGitProtocol(item.protocol);
    setGitHost(item.host);
    setGitPathPrefix(item.path_prefix || "");
    setGitUsername(item.username);
    setGitSecret("");
    setGitActive(item.is_active);
    setGitModal({ mode: "edit", item });
  };

  const handleSaveGit = async (e: FormEvent) => {
    e.preventDefault();
    if (!gitModal) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (gitModal.mode === "create") {
        await api.createGitCredential({
          label: gitLabel.trim(),
          protocol: gitProtocol,
          host: gitHost.trim(),
          path_prefix: gitPathPrefix.trim(),
          username: gitUsername.trim(),
          secret: gitSecret,
          is_active: gitActive,
        });
        setNotice("Git 仓库账号已新增并同步至运行时凭据文件（权限 0600）");
      } else if (gitModal.item) {
        await api.updateGitCredential(gitModal.item.id, {
          label: gitLabel.trim(),
          protocol: gitProtocol,
          host: gitHost.trim(),
          path_prefix: gitPathPrefix.trim(),
          username: gitUsername.trim(),
          secret: gitSecret,
          is_active: gitActive,
        });
        setNotice("Git 仓库账号已更新并即时生效");
      }
      setGitModal(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存 Git 账号失败");
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteGit = (item: GitCredentialItem) => {
    setDeleteGitTarget(item);
  };

  const confirmDeleteGit = async () => {
    if (!deleteGitTarget) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.deleteGitCredential(deleteGitTarget.id);
      setNotice("已删除该仓库凭据");
      setDeleteGitTarget(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusy(false);
    }
  };

  const handleSaveSmtp = async (e: FormEvent) => {
    e.preventDefault();
    if (!smtp) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveSmtpConfig({ ...smtp, password: smtpPassword.trim() });
      setSmtp(res.smtp);
      setSmtpPassword("");
      setNotice("SMTP 邮件投递配置已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存 SMTP 配置失败");
    } finally {
      setBusy(false);
    }
  };

  const handleTestSmtp = async () => {
    if (!smtp || !testToEmail.trim()) {
      setError("请先填写测试收件人邮箱");
      return;
    }
    setTestingSmtp(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.testSmtpEmail({
        ...smtp,
        password: smtpPassword.trim(),
        to_email: testToEmail.trim(),
      });
      if (res.ok) {
        setNotice(`测试邮件（含示例 Word 附件）已发送至 ${testToEmail.trim()}，耗时 ${res.elapsed_ms} ms`);
      } else {
        setError(`测试邮件发送失败：${res.error}`);
      }
      const logs = await api.emailLogs(100);
      setEmailLogs(logs.logs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "测试邮件失败");
    } finally {
      setTestingSmtp(false);
    }
  };

  const handleRetryEmail = async (logId: number) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.retryEmailLog(logId);
      if (res.ok && res.sent > 0) {
        setNotice("邮件重发成功");
      } else {
        setError(`邮件重发失败：${res.skipped_reason || "请查看最新日志"}`);
      }
      const logs = await api.emailLogs(100);
      setEmailLogs(logs.logs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "重发失败");
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

      <div className="card">
        <div className="card-title" style={{ flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className={`btn ${tab === "model" ? "btn-primary" : ""}`}
              onClick={() => setTab("model")}
            >
              <Cpu size={14} /> 模型接口配置
            </button>
            <button
              type="button"
              className={`btn ${tab === "runtime" ? "btn-primary" : ""}`}
              onClick={() => setTab("runtime")}
            >
              <SlidersHorizontal size={14} /> 并发 / 超时与环境变量
            </button>
            <button
              type="button"
              className={`btn ${tab === "git" ? "btn-primary" : ""}`}
              onClick={() => setTab("git")}
            >
              <KeyRound size={14} /> Git / SVN 仓库凭据 ({cfg?.git_credentials.length ?? 0})
            </button>
            <button
              type="button"
              className={`btn ${tab === "smtp" ? "btn-primary" : ""}`}
              onClick={() => setTab("smtp")}
            >
              <Mail size={14} /> 邮件投递 (SMTP)
            </button>
          </div>
          <button type="button" className="btn" disabled={loading} onClick={() => void loadData()}>
            <RefreshCw size={14} /> 刷新
          </button>
        </div>

        {/* 1. 模型接口配置 */}
        {tab === "model" && (
          <form onSubmit={(e) => void handleSaveModel(e)} style={{ display: "grid", gap: 12, maxWidth: 760 }}>
            <div className="sub">
              当前来源：<b>{cfg?.model.source === "runtime_override" ? "Web 热配置覆盖 (model.runtime.env)" : "宿主机挂载 (model.env)"}</b>
              {" · "}所有密钥均落盘 <code>0600</code>，页面仅脱敏展示，不写入日志与报告。
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">OPENAI_BASE_URL（接口根路径，含 /v1）</span>
                <input
                  value={baseUrl}
                  placeholder="http://qwen-server:8000/v1"
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">OPENAI_MODEL（服务端模型 ID）</span>
                <input
                  value={modelName}
                  placeholder="qwen3.8"
                  onChange={(e) => setModelName(e.target.value)}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">
                  OPENAI_API_KEY（{cfg?.model.OPENAI_API_KEY_SET ? `已配置：${cfg.model.OPENAI_API_KEY_MASKED}，留空保持不变` : "未配置"}）
                </span>
                <input
                  type="password"
                  value={apiKey}
                  placeholder="留空保持原密钥不变；无鉴权可填 EMPTY"
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </label>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">上下文窗口</span>
                  <input value={contextWindow} onChange={(e) => setContextWindow(e.target.value)} />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">最大输出 Tokens</span>
                  <input value={maxOutputTokens} onChange={(e) => setMaxOutputTokens(e.target.value)} />
                </label>
              </div>
            </div>

            <details style={{ marginTop: 4 }}>
              <summary style={{ cursor: "pointer", fontSize: 12.5, color: "var(--text-2)" }}>
                可选：DeepSeek 官方接口参数（使用自部署 OpenAI 兼容服务时留空即可）
              </summary>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginTop: 10 }}>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">DEEPSEEK_BASE_URL</span>
                  <input value={dsBaseUrl} onChange={(e) => setDsBaseUrl(e.target.value)} />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">DEEPSEEK_MODEL</span>
                  <input value={dsModel} onChange={(e) => setDsModel(e.target.value)} />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">
                    DEEPSEEK_API_KEY（{cfg?.model.DEEPSEEK_API_KEY_SET ? cfg.model.DEEPSEEK_API_KEY_MASKED : "未配置"}）
                  </span>
                  <input
                    type="password"
                    placeholder="留空保持不变"
                    value={dsApiKey}
                    onChange={(e) => setDsApiKey(e.target.value)}
                  />
                </label>
              </div>
            </details>

            {testResult && (
              <div
                style={{
                  padding: "10px 12px",
                  borderRadius: 8,
                  fontSize: 12.5,
                  border: `1px solid ${testResult.ok ? "var(--good)" : "var(--critical)"}`,
                  color: testResult.ok ? "var(--good)" : "var(--critical)",
                }}
              >
                {testResult.ok ? (
                  <div>
                    <b>模型连通正常！</b> 耗时 {testResult.elapsed_ms} ms · 返回模型：<code>{testResult.model}</code>
                    {" · "}响应预览：<code>{testResult.reply_preview || "(空)"}</code>
                    {testResult.has_thinking && (
                      <span style={{ color: "var(--warning)", marginLeft: 8 }}>
                        提示：检测到返回思考链（reasoning），如追求高吞吐建议在 vLLM 关闭默认 thinking
                      </span>
                    )}
                  </div>
                ) : (
                  <div>
                    <b>模型测试失败：</b> {testResult.error}（耗时 {testResult.elapsed_ms} ms）
                  </div>
                )}
              </div>
            )}

            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                <Check size={14} /> {busy ? "保存中…" : "保存模型配置（热生效）"}
              </button>
              <button type="button" className="btn" disabled={testingModel} onClick={() => void handleTestModel()}>
                <Zap size={14} /> {testingModel ? "探测中…" : "测试模型连通性"}
              </button>
            </div>
          </form>
        )}

        {/* 2. 并发 / 超时与环境变量 */}
        {tab === "runtime" && runtime && (
          <form onSubmit={(e) => void handleSaveRuntime(e)} style={{ display: "grid", gap: 12, maxWidth: 860 }}>
            <div className="sub">
              状态：<b>{cfg?.runtime_overridden ? "已启用 Web 运行参数覆盖" : "当前沿用容器 .env 环境变量"}</b>
              {" · "}保存后将覆盖调度器并发、超时、镜像与校验策略，下一次扫描直接生效。
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 12 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">流水线总并发 (AUDIT_MAX_PARALLEL)</span>
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={runtime.max_parallel}
                  onChange={(e) => setRuntime({ ...runtime, max_parallel: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">模型并发上限 (MAX_MODEL_CONCURRENCY)</span>
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={runtime.max_model_concurrency}
                  onChange={(e) => setRuntime({ ...runtime, max_model_concurrency: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">批次总时限（秒，默认 129600 = 36h）</span>
                <input
                  type="number"
                  min={300}
                  max={604800}
                  value={runtime.batch_timeout_sec}
                  onChange={(e) => setRuntime({ ...runtime, batch_timeout_sec: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">小型项目超时（秒）</span>
                <input
                  type="number"
                  min={60}
                  max={28800}
                  value={runtime.timeout_small_sec}
                  onChange={(e) => setRuntime({ ...runtime, timeout_small_sec: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">中型项目超时（秒）</span>
                <input
                  type="number"
                  min={60}
                  max={28800}
                  value={runtime.timeout_medium_sec}
                  onChange={(e) => setRuntime({ ...runtime, timeout_medium_sec: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">大型项目超时（秒）</span>
                <input
                  type="number"
                  min={60}
                  max={28800}
                  value={runtime.timeout_large_sec}
                  onChange={(e) => setRuntime({ ...runtime, timeout_large_sec: Number(e.target.value) })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">报告校验模式 (VALIDATION_MODE)</span>
                <select
                  value={runtime.validation_mode}
                  onChange={(e) =>
                    setRuntime({ ...runtime, validation_mode: e.target.value as "lenient" | "strict" })
                  }
                >
                  <option value="lenient">lenient（宽容落盘，推荐）</option>
                  <option value="strict">strict（严格拦截）</option>
                </select>
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">Docker 模型网络 (MODEL_NETWORK)</span>
                <input
                  value={runtime.model_network}
                  onChange={(e) => setRuntime({ ...runtime, model_network: e.target.value })}
                />
              </label>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">扫描容器镜像 (AUDIT_SCANNER_IMAGE)</span>
                <input
                  value={runtime.scanner_image}
                  onChange={(e) => setRuntime({ ...runtime, scanner_image: e.target.value })}
                />
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">Semgrep 离线规则目录（容器内绝对路径，可选）</span>
                <input
                  value={runtime.semgrep_rules_dir}
                  placeholder="/opt/semgrep-rules"
                  onChange={(e) => setRuntime({ ...runtime, semgrep_rules_dir: e.target.value })}
                />
              </label>
            </div>

            <div style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 13 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={runtime.fast_mode}
                  onChange={(e) => setRuntime({ ...runtime, fast_mode: e.target.checked })}
                />
                默认启用快速巡检 (fast_mode)
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={runtime.fast_skip_lizard}
                  onChange={(e) => setRuntime({ ...runtime, fast_skip_lizard: e.target.checked })}
                />
                快速模式跳过 Lizard 复杂度预扫描
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={runtime.adaptive_timeout}
                  onChange={(e) => setRuntime({ ...runtime, adaptive_timeout: e.target.checked })}
                />
                按项目规模动态分配超时
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={runtime.summary_enabled}
                  onChange={(e) => setRuntime({ ...runtime, summary_enabled: e.target.checked })}
                />
                批次结束启用 AI 深度汇总
              </label>
            </div>

            <div>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                <Check size={14} /> {busy ? "保存中…" : "保存运行配置（热生效）"}
              </button>
            </div>
          </form>
        )}

        {/* 3. Git 仓库账号凭据（仅超管） */}
        {tab === "git" && (
          <div style={{ display: "grid", gap: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
              <div className="sub">
                此处配置的 HTTPS/HTTP Git 只读账号会自动合并导出为 <code>0600</code> 运行时凭据文件，直接用于项目连通探测（<code>git ls-remote</code>）与扫描克隆。
                {cfg?.base_git_creds_present && "（已检测到宿主机只读挂载凭据，未冲突条目将自动保留）"}
              </div>
              <button type="button" className="btn btn-primary" onClick={openGitCreate}>
                <Plus size={14} /> 新增 Git 账号
              </button>
            </div>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>备注</th>
                    <th>匹配域名与路径</th>
                    <th>用户名</th>
                    <th>密码 / Token</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {(cfg?.git_credentials || []).map((c) => (
                    <tr key={c.id} style={{ opacity: c.is_active ? 1 : 0.55 }}>
                      <td style={{ fontWeight: 600 }}>{c.label || "—"}</td>
                      <td>
                        <code>
                          {c.protocol}://{c.host}
                          {c.path_prefix}
                        </code>
                      </td>
                      <td>
                        <code>{c.username}</code>
                      </td>
                      <td className="muted">
                        <code>{c.secret_masked || "****"}</code>
                      </td>
                      <td>
                        <span className="chip">
                          <span
                            className="chip-dot"
                            style={{ background: c.is_active ? "var(--good)" : "var(--neutral)" }}
                          />
                          {c.is_active ? "启用" : "停用"}
                        </span>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6 }}>
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 8px", fontSize: 12 }}
                            onClick={() => openGitEdit(c)}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="btn btn-danger"
                            style={{ padding: "3px 8px", fontSize: 12 }}
                            disabled={busy}
                            onClick={() => void handleDeleteGit(c)}
                          >
                            <Trash2 size={12} /> 删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {(cfg?.git_credentials || []).length === 0 && (
                    <tr>
                      <td colSpan={6} className="muted" style={{ textAlign: "center", padding: 24 }}>
                        尚未在 Web 端配置 Git 账号（当前使用宿主机 secrets/git-credentials 或 SSH 密钥）
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* 4. SMTP 邮件投递配置与投递日志 */}
        {tab === "smtp" && smtp && (
          <div style={{ display: "grid", gap: 18 }}>
            <form onSubmit={(e) => void handleSaveSmtp(e)} style={{ display: "grid", gap: 12, maxWidth: 860 }}>
              <div style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 13 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
                  <input
                    type="checkbox"
                    checked={smtp.enabled}
                    onChange={(e) => setSmtp({ ...smtp, enabled: e.target.checked })}
                  />
                  启用邮件投递功能
                </label>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={smtp.auto_send_on_batch}
                    onChange={(e) => setSmtp({ ...smtp, auto_send_on_batch: e.target.checked })}
                  />
                  批次扫描结束后自动按负责人聚合发邮件
                </label>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "2fr 100px 140px", gap: 10 }}>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">SMTP 服务器地址</span>
                  <input
                    value={smtp.host}
                    placeholder="例如 smtp.exmail.qq.com 或 smtp.shineyue.com"
                    onChange={(e) => setSmtp({ ...smtp, host: e.target.value })}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">端口</span>
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={smtp.port}
                    onChange={(e) => setSmtp({ ...smtp, port: Number(e.target.value) })}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">传输加密</span>
                  <select
                    value={smtp.security}
                    onChange={(e) => setSmtp({ ...smtp, security: e.target.value as SmtpConfig["security"] })}
                  >
                    <option value="ssl">SSL/TLS (465)</option>
                    <option value="starttls">STARTTLS (587)</option>
                    <option value="none">不加密 (25)</option>
                  </select>
                </label>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">SMTP 登录账号</span>
                  <input
                    value={smtp.username}
                    placeholder="例如 code-audit@shineyue.com"
                    onChange={(e) => setSmtp({ ...smtp, username: e.target.value })}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">
                    SMTP 密码 / 授权码（{smtp.password_set ? `已配置：${smtp.password_masked}，留空不变` : "未配置"}）
                  </span>
                  <input
                    type="password"
                    value={smtpPassword}
                    placeholder="留空保持原密码不变"
                    onChange={(e) => setSmtpPassword(e.target.value)}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">发件人邮箱（留空同登录账号）</span>
                  <input
                    type="email"
                    value={smtp.from_email}
                    placeholder="code-audit@shineyue.com"
                    onChange={(e) => setSmtp({ ...smtp, from_email: e.target.value })}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">发件人显示名</span>
                  <input
                    value={smtp.from_name}
                    onChange={(e) => setSmtp({ ...smtp, from_name: e.target.value })}
                  />
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">附件格式（超过 6 个自动打包 ZIP）</span>
                  <select
                    value={smtp.attachment_format}
                    onChange={(e) =>
                      setSmtp({ ...smtp, attachment_format: e.target.value as SmtpConfig["attachment_format"] })
                    }
                  >
                    <option value="docx">Word 文档 (.docx，推荐)</option>
                    <option value="md">Markdown 原文 (.md)</option>
                    <option value="both">同时附带 .docx 与 .md</option>
                  </select>
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                  <span className="muted">平台外链地址（用于邮件内直达链接）</span>
                  <input
                    value={smtp.portal_url}
                    placeholder="http://10.0.87.179:8787"
                    onChange={(e) => setSmtp({ ...smtp, portal_url: e.target.value })}
                  />
                </label>
              </div>

              <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
                <span className="muted">管理层汇总 Word 报告抄送邮箱（可选，多个用逗号分隔，每批次发送整体汇总 .docx）</span>
                <input
                  value={smtp.summary_cc_emails}
                  placeholder="leader1@shineyue.com, qa@shineyue.com"
                  onChange={(e) => setSmtp({ ...smtp, summary_cc_emails: e.target.value })}
                />
              </label>

              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  <Check size={14} /> {busy ? "保存中…" : "保存 SMTP 配置"}
                </button>
                <input
                  type="email"
                  placeholder="输入测试收件邮箱"
                  value={testToEmail}
                  onChange={(e) => setTestToEmail(e.target.value)}
                  style={{ width: 230 }}
                />
                <button type="button" className="btn" disabled={testingSmtp} onClick={() => void handleTestSmtp()}>
                  <Send size={13} /> {testingSmtp ? "发送中…" : "发送测试邮件（含示例 Word 附件）"}
                </button>
              </div>
            </form>

            <div>
              <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 8 }}>
                最近邮件投递记录 ({emailLogs.length})
              </div>
              <div className="table-wrap" style={{ maxHeight: 380, overflowY: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>批次</th>
                      <th>类型</th>
                      <th>收件人</th>
                      <th>附件</th>
                      <th>状态</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {emailLogs.map((log) => (
                      <tr key={log.id}>
                        <td className="sub">{log.created_at.replace("T", " ")}</td>
                        <td className="sub">{log.run_date ? `${log.run_date} / ${log.run_id}` : log.run_id}</td>
                        <td>
                          <span className="chip">
                            {log.recipient_type === "owner"
                              ? "负责人"
                              : log.recipient_type === "summary"
                              ? "管理层汇总"
                              : "测试"}
                          </span>
                        </td>
                        <td>
                          <div>{log.recipient_name || "—"}</div>
                          <div className="muted" style={{ fontSize: 11.5 }}>
                            {log.recipient_email}
                          </div>
                        </td>
                        <td style={{ whiteSpace: "normal", maxWidth: 220 }} className="sub">
                          {log.attachment_names.length > 0 ? log.attachment_names.join("、") : "—"}
                        </td>
                        <td style={{ whiteSpace: "normal", maxWidth: 240 }}>
                          <span
                            className="chip"
                            style={{
                              borderColor: log.status === "sent" ? "var(--good)" : "var(--critical)",
                              color: log.status === "sent" ? "var(--good)" : "var(--critical)",
                            }}
                          >
                            {log.status === "sent" ? "已发送" : "失败"}
                          </span>
                          {log.error && (
                            <div style={{ fontSize: 11, color: "var(--critical)", marginTop: 2 }}>{log.error}</div>
                          )}
                        </td>
                        <td>
                          {log.status !== "sent" && (
                            <button
                              type="button"
                              className="btn"
                              style={{ padding: "3px 8px", fontSize: 12 }}
                              disabled={busy}
                              onClick={() => void handleRetryEmail(log.id)}
                            >
                              <RotateCcw size={12} /> 重发
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {emailLogs.length === 0 && (
                      <tr>
                        <td colSpan={7} className="muted" style={{ textAlign: "center", padding: 20 }}>
                          暂无邮件投递记录
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 新增 / 编辑 Git 凭据弹窗 */}
      {gitModal && (
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
          onClick={() => setGitModal(null)}
        >
          <form
            className="card"
            style={{ width: "min(480px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleSaveGit(e)}
          >
            <div className="card-title">
              <span>{gitModal.mode === "create" ? "新增 Git / SVN 仓库账号" : "编辑 Git / SVN 仓库账号"}</span>
              <button type="button" className="btn" onClick={() => setGitModal(null)}>
                关闭
              </button>
            </div>
            <input
              placeholder="备注名称（例如：公司内网 GitLab / SVN 只读账号）"
              value={gitLabel}
              onChange={(e) => setGitLabel(e.target.value)}
            />
            <div style={{ display: "grid", gridTemplateColumns: "110px 1fr", gap: 8 }}>
              <select value={gitProtocol} onChange={(e) => setGitProtocol(e.target.value as "https" | "http" | "svn")}>
                <option value="https">https</option>
                <option value="http">http</option>
                <option value="svn">svn</option>
              </select>
              <input
                placeholder="域名或 IP:端口（如 git.sjgjj.cn 或 svn.sjgjj.cn）"
                value={gitHost}
                onChange={(e) => setGitHost(e.target.value)}
                required
              />
            </div>
            <input
              placeholder="路径前缀（可选，留空匹配该域名下全部仓库；或填 /svn/CustomerProject）"
              value={gitPathPrefix}
              onChange={(e) => setGitPathPrefix(e.target.value)}
            />
            <input
              placeholder="仓库用户名（Git 账号/oauth2 或 SVN 用户名）"
              value={gitUsername}
              onChange={(e) => setGitUsername(e.target.value)}
              required
            />
            <input
              type="password"
              placeholder={gitModal.mode === "edit" ? "密码 / Access Token（留空保持原凭据不变）" : "密码 / 只读 Access Token"}
              value={gitSecret}
              onChange={(e) => setGitSecret(e.target.value)}
              required={gitModal.mode === "create"}
            />
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={gitActive} onChange={(e) => setGitActive(e.target.checked)} />
              启用该凭据
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy} style={{ justifyContent: "center" }}>
              {busy ? "保存中…" : "保存凭据"}
            </button>
          </form>
        </div>
      )}
      <ConfirmDialog
        open={Boolean(deleteGitTarget)}
        tone="danger"
        title={deleteGitTarget ? `确认删除凭据「${deleteGitTarget.label || deleteGitTarget.host}」？` : ""}
        description="删除后会立即同步更新运行时凭据文件，依赖该账号拉取的仓库可能认证失败。"
        previewItems={
          deleteGitTarget
            ? [`${deleteGitTarget.protocol}://${deleteGitTarget.host}${deleteGitTarget.path_prefix || ""} (${deleteGitTarget.username})`]
            : undefined
        }
        confirmLabel="确认删除"
        busy={busy}
        onConfirm={() => void confirmDeleteGit()}
        onCancel={() => setDeleteGitTarget(null)}
      />
    </div>
  );
}
