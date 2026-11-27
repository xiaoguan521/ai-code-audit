import { FormEvent, useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw, Shield, Trash2, UserPlus, Users } from "lucide-react";
import { api } from "../api";
import type { AuditLogEntry, AuthUser, OwnerPreviewItem, UserRole } from "../types";

interface Props {
  currentUser: AuthUser;
}

const ROLE_LABELS: Record<UserRole, string> = {
  superadmin: "超级管理员（运维）",
  admin: "管理员",
  user: "普通用户（负责人）",
};

export function UsersPage({ currentUser }: Props) {
  const isSuper = currentUser.role === "superadmin";
  const [tab, setTab] = useState<"users" | "audit">("users");
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [auditLogs, setAuditLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 新建用户表单
  const [showCreate, setShowCreate] = useState(false);
  const [newUsername, setNewUsername] = useState("");
  const [newDisplay, setNewDisplay] = useState("");
  const [newRole, setNewRole] = useState<UserRole>("user");
  const [newEmail, setNewEmail] = useState("");
  const [newPass, setNewPass] = useState("Sy@123456");

  // 编辑用户弹窗
  const [editing, setEditing] = useState<AuthUser | null>(null);
  const [editDisplay, setEditDisplay] = useState("");
  const [editRole, setEditRole] = useState<UserRole>("user");
  const [editEmail, setEditEmail] = useState("");
  const [editActive, setEditActive] = useState(true);

  // 重置密码弹窗
  const [resetTarget, setResetTarget] = useState<AuthUser | null>(null);
  const [resetPass, setResetPass] = useState("Sy@123456");

  // 从项目负责人批量生成向导
  const [showBatch, setShowBatch] = useState(false);
  const [emailDomain, setEmailDomain] = useState("shineyue.com");
  const [batchPassword, setBatchPassword] = useState("Sy@123456");
  const [previewRows, setPreviewRows] = useState<Array<OwnerPreviewItem & { selected: boolean }>>([]);
  const [batchBusy, setBatchBusy] = useState(false);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const uRes = await api.users();
      setUsers(uRes.users);
      if (isSuper) {
        const aRes = await api.auditLogs(150);
        setAuditLogs(aRes.logs);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载用户列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const handleCreateUser = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await api.createUser({
        username: newUsername.trim(),
        display_name: newDisplay.trim() || newUsername.trim(),
        role: newRole,
        password: newPass,
        email: newEmail.trim() || `${newUsername.trim().toLowerCase()}@shineyue.com`,
        must_change_password: true,
      });
      setShowCreate(false);
      setNewUsername("");
      setNewDisplay("");
      setNewEmail("");
      setNotice("用户已创建");
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建用户失败");
    }
  };

  const openEdit = (u: AuthUser) => {
    setEditing(u);
    setEditDisplay(u.display_name);
    setEditRole(u.role);
    setEditEmail(u.email || "");
    setEditActive(u.is_active);
  };

  const handleSaveEdit = async (e: FormEvent) => {
    e.preventDefault();
    if (!editing) return;
    setError(null);
    setNotice(null);
    try {
      await api.updateUser(editing.id, {
        display_name: editDisplay.trim(),
        role: editRole,
        email: editEmail.trim(),
        is_active: editActive,
      });
      setEditing(null);
      setNotice("用户信息已更新");
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新失败");
    }
  };

  const handleResetPassword = async (e: FormEvent) => {
    e.preventDefault();
    if (!resetTarget) return;
    setError(null);
    setNotice(null);
    try {
      await api.resetUserPassword(resetTarget.id, resetPass, true);
      setResetTarget(null);
      setNotice(`已重置 ${resetTarget.username} 的密码，下次登录需强制改密`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重置密码失败");
    }
  };

  const handleDelete = async (u: AuthUser) => {
    if (!window.confirm(`确定删除用户 ${u.display_name}（${u.username}）吗？`)) return;
    setError(null);
    setNotice(null);
    try {
      await api.deleteUser(u.id);
      setNotice(`已删除用户 ${u.username}`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除用户失败");
    }
  };

  const loadBatchPreview = async (domain = emailDomain) => {
    setBatchBusy(true);
    setError(null);
    try {
      const res = await api.previewUsersFromProjects(domain);
      setPreviewRows(res.items.map((it) => ({ ...it, selected: !it.exists })));
      setShowBatch(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取项目负责人失败");
    } finally {
      setBatchBusy(false);
    }
  };

  const handleBatchCreate = async () => {
    const selected = previewRows.filter((r) => r.selected && !r.exists);
    if (selected.length === 0) {
      setError("请至少勾选一位待生成的负责人");
      return;
    }
    setBatchBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.batchCreateUsers(
        selected.map((r) => ({
          username: r.username.trim(),
          display_name: r.display_name.trim(),
          email: r.email.trim(),
          role: r.role,
        })),
        batchPassword
      );
      setShowBatch(false);
      setNotice(`批量生成完成：成功创建 ${res.created.length} 个账号，跳过 ${res.skipped.length} 个`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "批量生成失败");
    } finally {
      setBatchBusy(false);
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
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              type="button"
              className={`btn ${tab === "users" ? "btn-primary" : ""}`}
              onClick={() => setTab("users")}
            >
              <Users size={14} /> 用户与角色管理 ({users.length})
            </button>
            {isSuper && (
              <button
                type="button"
                className={`btn ${tab === "audit" ? "btn-primary" : ""}`}
                onClick={() => setTab("audit")}
              >
                <Shield size={14} /> 操作审计日志 ({auditLogs.length})
              </button>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {tab === "users" && (
              <>
                <button
                  type="button"
                  className="btn"
                  disabled={batchBusy}
                  onClick={() => void loadBatchPreview()}
                >
                  <UserPlus size={14} /> 从项目负责人批量生成
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => setShowCreate(true)}
                >
                  <Plus size={14} /> 新增用户
                </button>
              </>
            )}
            <button type="button" className="btn" onClick={() => void loadData()} disabled={loading}>
              <RefreshCw size={14} /> 刷新
            </button>
          </div>
        </div>

        {tab === "users" ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>姓名（匹配负责人）</th>
                  <th>角色</th>
                  <th>公司邮箱</th>
                  <th>状态</th>
                  <th>最近登录</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} style={{ opacity: u.is_active ? 1 : 0.55 }}>
                    <td>
                      <code>{u.username}</code>
                    </td>
                    <td style={{ fontWeight: 600 }}>{u.display_name}</td>
                    <td>
                      <span className="chip">{ROLE_LABELS[u.role]}</span>
                    </td>
                    <td>{u.email || "—"}</td>
                    <td>
                      <span className="chip">
                        <span
                          className="chip-dot"
                          style={{ background: u.is_active ? "var(--good)" : "var(--neutral)" }}
                        />
                        {u.is_active ? "启用" : "停用"}
                      </span>
                    </td>
                    <td className="sub">{u.last_login_at ? u.last_login_at.replace("T", " ") : "从未登录"}</td>
                    <td>
                      {isSuper || u.role === "user" ? (
                        <div style={{ display: "flex", gap: 6 }}>
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 8px", fontSize: 12 }}
                            onClick={() => openEdit(u)}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: "3px 8px", fontSize: 12 }}
                            onClick={() => {
                              setResetTarget(u);
                              setResetPass("Sy@123456");
                            }}
                          >
                            <KeyRound size={12} /> 重置密码
                          </button>
                          {u.id !== currentUser.id && (
                            <button
                              type="button"
                              className="btn btn-danger"
                              style={{ padding: "3px 8px", fontSize: 12 }}
                              onClick={() => void handleDelete(u)}
                            >
                              <Trash2 size={12} /> 删除
                            </button>
                          )}
                        </div>
                      ) : (
                        <span className="muted" style={{ fontSize: 12 }}>
                          仅超管可操作
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="table-wrap" style={{ maxHeight: 560, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>操作人</th>
                  <th>角色</th>
                  <th>动作</th>
                  <th>目标</th>
                  <th>详情</th>
                  <th>IP</th>
                </tr>
              </thead>
              <tbody>
                {auditLogs.map((log) => (
                  <tr key={log.id}>
                    <td className="sub">{log.created_at.replace("T", " ")}</td>
                    <td>
                      <code>{log.actor_username}</code>
                    </td>
                    <td>{log.actor_role}</td>
                    <td>
                      <b>{log.action}</b>
                    </td>
                    <td>{log.target || "—"}</td>
                    <td style={{ whiteSpace: "normal", maxWidth: 320 }} className="sub">
                      {log.detail || "—"}
                    </td>
                    <td className="muted">{log.client_ip}</td>
                  </tr>
                ))}
                {auditLogs.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted" style={{ textAlign: "center", padding: 24 }}>
                      暂无操作审计记录
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 新增用户弹窗 */}
      {showCreate && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.48)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setShowCreate(false)}
        >
          <form
            className="card"
            style={{ width: "min(460px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleCreateUser(e)}
          >
            <div className="card-title">
              <span>新增用户</span>
              <button type="button" className="btn" onClick={() => setShowCreate(false)}>
                关闭
              </button>
            </div>
            <input
              placeholder="用户名（建议姓名全拼，如 wangxiuli）"
              value={newUsername}
              onChange={(e) => {
                setNewUsername(e.target.value);
                if (!newEmail) {
                  const clean = e.target.value.trim().toLowerCase();
                  if (clean) setNewEmail(`${clean}@shineyue.com`);
                }
              }}
              required
            />
            <input
              placeholder="中文姓名（用于自动匹配项目负责人，如 王秀丽）"
              value={newDisplay}
              onChange={(e) => setNewDisplay(e.target.value)}
              required
            />
            <select value={newRole} onChange={(e) => setNewRole(e.target.value as UserRole)} disabled={!isSuper}>
              <option value="user">普通用户（负责人，仅可见及触发本人项目）</option>
              {isSuper && <option value="admin">管理员（管理项目/提示词/普通用户与全部扫描）</option>}
              {isSuper && <option value="superadmin">超级管理员（运维，全部权限含模型与角色管理）</option>}
            </select>
            <input
              type="email"
              placeholder="公司邮箱（姓名全拼@shineyue.com）"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
            />
            <input
              type="text"
              placeholder="初始密码（至少 6 位，首次登录需修改）"
              value={newPass}
              onChange={(e) => setNewPass(e.target.value)}
              required
            />
            <button type="submit" className="btn btn-primary" style={{ justifyContent: "center" }}>
              创建用户
            </button>
          </form>
        </div>
      )}

      {/* 编辑用户弹窗 */}
      {editing && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.48)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setEditing(null)}
        >
          <form
            className="card"
            style={{ width: "min(460px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleSaveEdit(e)}
          >
            <div className="card-title">
              <span>编辑用户：{editing.username}</span>
              <button type="button" className="btn" onClick={() => setEditing(null)}>
                关闭
              </button>
            </div>
            <input
              placeholder="中文姓名"
              value={editDisplay}
              onChange={(e) => setEditDisplay(e.target.value)}
              required
            />
            <select value={editRole} onChange={(e) => setEditRole(e.target.value as UserRole)} disabled={!isSuper}>
              <option value="user">普通用户（负责人）</option>
              {isSuper && <option value="admin">管理员</option>}
              {isSuper && <option value="superadmin">超级管理员（运维）</option>}
            </select>
            <input
              type="email"
              placeholder="公司邮箱"
              value={editEmail}
              onChange={(e) => setEditEmail(e.target.value)}
            />
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <input type="checkbox" checked={editActive} onChange={(e) => setEditActive(e.target.checked)} />
              账号启用中
            </label>
            <button type="submit" className="btn btn-primary" style={{ justifyContent: "center" }}>
              保存修改
            </button>
          </form>
        </div>
      )}

      {/* 重置密码弹窗 */}
      {resetTarget && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.48)",
            display: "grid",
            placeItems: "center",
            padding: 20,
            zIndex: 60,
          }}
          onClick={() => setResetTarget(null)}
        >
          <form
            className="card"
            style={{ width: "min(420px, 95vw)", display: "grid", gap: 10 }}
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => void handleResetPassword(e)}
          >
            <div className="card-title">
              <span>重置密码：{resetTarget.display_name}（{resetTarget.username}）</span>
              <button type="button" className="btn" onClick={() => setResetTarget(null)}>
                关闭
              </button>
            </div>
            <input
              type="text"
              placeholder="新初始密码（至少 6 位）"
              value={resetPass}
              onChange={(e) => setResetPass(e.target.value)}
              required
            />
            <button type="submit" className="btn btn-primary" style={{ justifyContent: "center" }}>
              确认重置（强制下线并要求下次改密）
            </button>
          </form>
        </div>
      )}

      {/* 从项目负责人批量生成向导 */}
      {showBatch && (
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
          onClick={() => setShowBatch(false)}
        >
          <div
            className="card"
            style={{ width: "min(860px, 95vw)", maxHeight: "88vh", display: "flex", flexDirection: "column" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="card-title" style={{ flexWrap: "wrap" }}>
              <span>从现有项目负责人批量生成普通用户（支持自定义全拼与邮箱）</span>
              <button type="button" className="btn" onClick={() => setShowBatch(false)}>
                关闭
              </button>
            </div>
            <div className="filters">
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                邮箱后缀
                <input
                  value={emailDomain}
                  onChange={(e) => setEmailDomain(e.target.value)}
                  style={{ width: 150 }}
                />
              </label>
              <button
                type="button"
                className="btn"
                style={{ fontSize: 12 }}
                disabled={batchBusy}
                onClick={() => void loadBatchPreview(emailDomain)}
              >
                按后缀重新生成预览
              </button>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, marginLeft: "auto" }}>
                统一初始密码
                <input
                  value={batchPassword}
                  onChange={(e) => setBatchPassword(e.target.value)}
                  style={{ width: 140 }}
                />
              </label>
            </div>

            <div className="table-wrap" style={{ maxHeight: "55vh", overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 36 }}>选</th>
                    <th>负责人姓名</th>
                    <th>登录用户名（可改）</th>
                    <th>公司邮箱（可改）</th>
                    <th>角色</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {previewRows.map((r, idx) => (
                    <tr key={`${r.display_name}-${idx}`} style={{ opacity: r.exists ? 0.55 : 1 }}>
                      <td>
                        <input
                          type="checkbox"
                          disabled={r.exists}
                          checked={r.selected && !r.exists}
                          onChange={(e) =>
                            setPreviewRows((prev) =>
                              prev.map((row, i) => (i === idx ? { ...row, selected: e.target.checked } : row))
                            )
                          }
                        />
                      </td>
                      <td style={{ fontWeight: 600 }}>{r.display_name}</td>
                      <td>
                        <input
                          disabled={r.exists}
                          value={r.username}
                          onChange={(e) => {
                            const val = e.target.value;
                            setPreviewRows((prev) =>
                              prev.map((row, i) =>
                                i === idx
                                  ? {
                                      ...row,
                                      username: val,
                                      email: val.trim() ? `${val.trim().toLowerCase()}@${emailDomain}` : row.email,
                                    }
                                  : row
                              )
                            );
                          }}
                          style={{ width: 150, padding: "4px 8px" }}
                        />
                      </td>
                      <td>
                        <input
                          disabled={r.exists}
                          value={r.email}
                          onChange={(e) =>
                            setPreviewRows((prev) =>
                              prev.map((row, i) => (i === idx ? { ...row, email: e.target.value } : row))
                            )
                          }
                          style={{ width: 230, padding: "4px 8px" }}
                        />
                      </td>
                      <td>
                        <select
                          disabled={r.exists || !isSuper}
                          value={isSuper ? r.role : "user"}
                          onChange={(e) =>
                            setPreviewRows((prev) =>
                              prev.map((row, i) =>
                                i === idx ? { ...row, role: e.target.value as UserRole } : row
                              )
                            )
                          }
                          style={{ padding: "4px 6px", fontSize: 12 }}
                        >
                          <option value="user">普通用户</option>
                          {isSuper && <option value="admin">管理员</option>}
                        </select>
                      </td>
                      <td>
                        <span className="chip">{r.exists ? "已存在" : "待创建"}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                已勾选 {previewRows.filter((r) => r.selected && !r.exists).length} 个待生成账号（首次登录强制修改密码）
              </span>
              <button
                type="button"
                className="btn btn-primary"
                disabled={batchBusy || previewRows.filter((r) => r.selected && !r.exists).length === 0}
                onClick={() => void handleBatchCreate()}
              >
                {batchBusy ? "创建中…" : "确认批量创建"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
