import { FormEvent, useState } from "react";
import { Check, KeyRound, Mail, User } from "lucide-react";
import { api } from "../api";
import type { AuthUser } from "../types";

interface Props {
  user: AuthUser;
  forcePasswordChange?: boolean;
  onUpdated: (next: AuthUser) => void;
  onClose?: () => void;
}

const ROLE_LABEL: Record<AuthUser["role"], string> = {
  superadmin: "超级管理员（运维）",
  admin: "管理员",
  user: "普通用户（负责人）",
};

export function ProfileModal({ user, forcePasswordChange, onUpdated, onClose }: Props) {
  const [displayName, setDisplayName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email || "");
  const [emailNotify, setEmailNotify] = useState<AuthUser["email_notify"]>(user.email_notify || "batch");
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileMsg, setProfileMsg] = useState<string | null>(null);

  const [oldPass, setOldPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [confirmPass, setConfirmPass] = useState("");
  const [savingPass, setSavingPass] = useState(false);
  const [passMsg, setPassMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSaveProfile = async (e: FormEvent) => {
    e.preventDefault();
    setSavingProfile(true);
    setError(null);
    setProfileMsg(null);
    try {
      const res = await api.updateProfile({
        display_name: displayName.trim(),
        email: email.trim(),
        email_notify: emailNotify,
      });
      onUpdated(res.user);
      setProfileMsg("个人资料已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSavingProfile(false);
    }
  };

  const handleChangePassword = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setPassMsg(null);
    if (newPass.length < 6) {
      setError("新密码长度不能少于 6 位");
      return;
    }
    if (newPass !== confirmPass) {
      setError("两次输入的新密码不一致");
      return;
    }
    setSavingPass(true);
    try {
      const res = await api.changePassword(oldPass, newPass);
      setOldPass("");
      setNewPass("");
      setConfirmPass("");
      setPassMsg("密码已更新");
      onUpdated(res.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改密码失败");
    } finally {
      setSavingPass(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "grid",
        placeItems: "center",
        padding: 20,
        zIndex: 70,
      }}
      onClick={() => {
        if (!forcePasswordChange && onClose) onClose();
      }}
    >
      <div
        className="card"
        style={{ width: "min(540px, 95vw)", maxHeight: "90vh", overflowY: "auto" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-title">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <User size={16} />
            {forcePasswordChange ? "首次登录请先修改初始密码" : "个人设置与邮箱配置"}
          </span>
          {!forcePasswordChange && onClose && (
            <button type="button" className="btn" onClick={onClose}>
              关闭
            </button>
          )}
        </div>

        <div className="sub" style={{ marginBottom: 12 }}>
          账号：<b>{user.username}</b> · 角色：<b>{ROLE_LABEL[user.role]}</b>
        </div>

        {error && (
          <div style={{ color: "var(--critical)", fontSize: 13, marginBottom: 10 }}>{error}</div>
        )}

        {!forcePasswordChange && (
          <form
            onSubmit={(e) => void handleSaveProfile(e)}
            style={{
              display: "grid",
              gap: 10,
              paddingBottom: 16,
              marginBottom: 16,
              borderBottom: "1px solid var(--border)",
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
              <Mail size={14} /> 姓名与公司邮箱（用于匹配负责项目及接收扫描结果邮件）
            </div>
            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">
                显示姓名{user.role === "user" ? "（由管理员维护，用于绑定名下项目）" : ""}
              </span>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={user.role === "user"}
              />
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">公司邮箱（例如 wangxiuli@shineyue.com）</span>
              <input
                type="email"
                placeholder="姓名全拼@shineyue.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
              <span className="muted">邮件通知偏好（邮件投递启用后生效）</span>
              <select
                value={emailNotify}
                onChange={(e) => setEmailNotify(e.target.value as AuthUser["email_notify"])}
              >
                <option value="batch">每次批次完成发送本人项目汇总（含附件）</option>
                <option value="risk_only">仅在出现高风险 / P0 / 扫描失败时发送</option>
                <option value="off">暂不接收自动邮件</option>
              </select>
            </label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button type="submit" className="btn btn-primary" disabled={savingProfile}>
                <Check size={14} /> {savingProfile ? "保存中…" : "保存资料"}
              </button>
              {profileMsg && <span style={{ color: "var(--good)", fontSize: 12 }}>{profileMsg}</span>}
            </div>
          </form>
        )}

        <form onSubmit={(e) => void handleChangePassword(e)} style={{ display: "grid", gap: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <KeyRound size={14} /> 修改登录密码
          </div>
          <input
            type="password"
            placeholder="当前密码"
            value={oldPass}
            onChange={(e) => setOldPass(e.target.value)}
            required
          />
          <input
            type="password"
            placeholder="新密码（至少 6 位）"
            value={newPass}
            onChange={(e) => setNewPass(e.target.value)}
            required
          />
          <input
            type="password"
            placeholder="确认新密码"
            value={confirmPass}
            onChange={(e) => setConfirmPass(e.target.value)}
            required
          />
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button type="submit" className="btn btn-primary" disabled={savingPass}>
              {savingPass ? "提交中…" : "更新密码"}
            </button>
            {passMsg && <span style={{ color: "var(--good)", fontSize: 12 }}>{passMsg}</span>}
          </div>
        </form>
      </div>
    </div>
  );
}
