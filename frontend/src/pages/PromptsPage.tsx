import { useEffect, useState } from "react";
import { Check, History, RefreshCw, RotateCcw, Sparkles } from "lucide-react";
import { api } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type { PromptItem } from "../types";

export function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptItem[]>([]);
  const [activeKey, setActiveKey] = useState<PromptItem["key"]>("skill_md");
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<
    | { kind: "reset" }
    | { kind: "rollback"; versionId: number; verNum: number }
    | null
  >(null);

  const loadData = async (keepKey?: PromptItem["key"]) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.prompts();
      setPrompts(res.prompts);
      const targetKey = keepKey || activeKey;
      const cur = res.prompts.find((p) => p.key === targetKey) || res.prompts[0];
      if (cur) {
        setActiveKey(cur.key);
        setDraft(cur.content);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载提示词配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const current = prompts.find((p) => p.key === activeKey) || null;
  const dirty = Boolean(current && draft !== current.content);

  const selectPrompt = (item: PromptItem) => {
    setActiveKey(item.key);
    setDraft(item.content);
    setNote("");
    setNotice(null);
    setError(null);
  };

  const handleSave = async () => {
    if (!current) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.savePrompt(current.key, draft, note.trim());
      setPrompts(res.prompts);
      const updated = res.prompts.find((p) => p.key === current.key);
      if (updated) setDraft(updated.content);
      setNote("");
      setNotice(`已保存「${current.title}」新版本（v${updated?.active_version || ""}），下一次扫描自动热挂载生效`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存提示词失败");
    } finally {
      setBusy(false);
    }
  };

  const handleReset = () => {
    if (!current) return;
    setConfirmAction({ kind: "reset" });
  };

  const handleRollback = (versionId: number, verNum: number) => {
    if (!current) return;
    setConfirmAction({ kind: "rollback", versionId, verNum });
  };

  const executeConfirm = async () => {
    if (!current || !confirmAction) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (confirmAction.kind === "reset") {
        const res = await api.resetPrompt(current.key);
        setPrompts(res.prompts);
        const updated = res.prompts.find((p) => p.key === current.key);
        if (updated) setDraft(updated.content);
        setNotice(`已恢复「${current.title}」为内置默认版本`);
      } else {
        const res = await api.rollbackPrompt(current.key, confirmAction.versionId);
        setPrompts(res.prompts);
        const updated = res.prompts.find((p) => p.key === current.key);
        if (updated) setDraft(updated.content);
        setNotice(`已回滚至 v${confirmAction.verNum} 并热生效`);
      }
      setConfirmAction(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : confirmAction.kind === "reset" ? "恢复默认失败" : "回滚失败"
      );
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

      <div className="grid-sidebar">
        {/* 左侧模板列表 */}
        <div className="card">
          <div className="card-title">
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Sparkles size={15} /> 提示词模板
            </span>
            <button type="button" className="btn" disabled={loading} onClick={() => void loadData(activeKey)}>
              <RefreshCw size={13} /> 刷新
            </button>
          </div>
          <div style={{ display: "grid", gap: 8 }}>
            {prompts.map((item) => (
              <div
                key={item.key}
                className={`list-item ${item.key === activeKey ? "active" : ""}`}
                onClick={() => selectPrompt(item)}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                  <b style={{ fontSize: 13 }}>{item.title}</b>
                  <span
                    className="chip"
                    style={
                      item.is_custom
                        ? { borderColor: "var(--accent)", color: "var(--accent)" }
                        : undefined
                    }
                  >
                    {item.is_custom ? `自定义 v${item.active_version}` : "内置默认"}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: 11.5, marginTop: 4, lineHeight: 1.45 }}>
                  {item.description}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* 右侧在线编辑器与版本历史 */}
        <div style={{ display: "grid", gap: 14 }}>
          {current && (
            <>
              <div className="card">
                <div className="card-title" style={{ flexWrap: "wrap" }}>
                  <div>
                    <span>{current.title}</span>
                    <span className="sub" style={{ fontWeight: 400, marginLeft: 8 }}>
                      {draft.length.toLocaleString()} 字符 ·{" "}
                      {current.is_custom
                        ? `当前生效：自定义 v${current.active_version}（${current.updated_by}）`
                        : "当前生效：内置默认"}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy || draft === current.default_content}
                      onClick={() => setDraft(current.default_content)}
                      title="仅载入编辑器预览，未点击保存不会生效"
                    >
                      载入内置默认草稿
                    </button>
                    {current.is_custom && (
                      <button type="button" className="btn" disabled={busy} onClick={() => void handleReset()}>
                        <RotateCcw size={13} /> 恢复内置默认
                      </button>
                    )}
                  </div>
                </div>

                <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
                  {current.description}
                </div>

                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={current.key === "skill_md" ? 22 : 12}
                  placeholder={
                    current.key === "dispatch_extra"
                      ? "例如：重点核查接口越权、硬编码内网 IP、MyBatis ${} 注入与事务回滚缺失…"
                      : "请输入提示词内容…"
                  }
                  style={{
                    width: "100%",
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                    fontSize: 12.5,
                    lineHeight: 1.6,
                    padding: 12,
                    borderRadius: 8,
                    border: "1px solid var(--border)",
                    background: "var(--bg-2)",
                    color: "var(--text-1)",
                    resize: "vertical",
                  }}
                />

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    marginTop: 12,
                    flexWrap: "wrap",
                  }}
                >
                  <input
                    style={{ flex: 1, minWidth: 220 }}
                    placeholder="本次变更说明（可选，例如：加强 SQL 注入与脱敏要求）"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busy || (!dirty && current.is_custom)}
                    onClick={() => void handleSave()}
                  >
                    <Check size={14} /> {busy ? "保存中…" : "保存并热生效"}
                  </button>
                </div>
              </div>

              <div className="card">
                <div className="card-title">
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <History size={14} /> 历史版本记录 ({current.versions.length})
                  </span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>版本</th>
                        <th>变更说明</th>
                        <th>修改人</th>
                        <th>字符数</th>
                        <th>时间</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {current.versions.map((v) => (
                        <tr key={v.id}>
                          <td>
                            <b>v{v.version}</b>
                            {v.is_active === 1 && (
                              <span
                                className="chip"
                                style={{ marginLeft: 6, borderColor: "var(--good)", color: "var(--good)" }}
                              >
                                当前生效
                              </span>
                            )}
                          </td>
                          <td>{v.note || "—"}</td>
                          <td>
                            <code>{v.updated_by || "—"}</code>
                          </td>
                          <td>{v.char_count.toLocaleString()}</td>
                          <td className="sub">{v.created_at.replace("T", " ")}</td>
                          <td>
                            {v.is_active === 1 ? (
                              <span className="muted" style={{ fontSize: 12 }}>
                                已生效
                              </span>
                            ) : (
                              <button
                                type="button"
                                className="btn"
                                style={{ padding: "3px 8px", fontSize: 12 }}
                                disabled={busy}
                                onClick={() => void handleRollback(v.id, v.version)}
                              >
                                <RotateCcw size={12} /> 回滚到此版本
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                      {current.versions.length === 0 && (
                        <tr>
                          <td colSpan={6} className="muted" style={{ textAlign: "center", padding: 20 }}>
                            尚未保存过自定义版本，当前使用镜像内置默认提示词
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={Boolean(confirmAction && current)}
        tone="warning"
        title={
          confirmAction?.kind === "reset"
            ? `确认将「${current?.title || ""}」恢复为内置默认版本？`
            : `确认回滚「${current?.title || ""}」到历史版本 v${confirmAction?.kind === "rollback" ? confirmAction.verNum : ""}？`
        }
        description={
          confirmAction?.kind === "reset"
            ? "恢复后下一次扫描将使用镜像内置的默认提示词，已保存的历史版本记录仍会保留，可随时切回。"
            : "回滚后将立即热生效并在下一次扫描中自动挂载该历史版本。"
        }
        confirmLabel={confirmAction?.kind === "reset" ? "确认恢复默认" : "确认回滚"}
        busy={busy}
        onConfirm={() => void executeConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  );
}
