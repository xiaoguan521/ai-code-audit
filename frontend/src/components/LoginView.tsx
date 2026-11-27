import { FormEvent, useEffect, useState } from "react";
import {
  ArrowRight,
  Cpu,
  Eye,
  EyeOff,
  Lock,
  ShieldCheck,
  Sparkles,
  Terminal,
  User,
} from "lucide-react";
import { NeuralBackdrop } from "./NeuralBackdrop";

interface Props {
  username: string;
  password: string;
  error: string | null;
  loggingIn: boolean;
  onUsernameChange: (v: string) => void;
  onPasswordChange: (v: string) => void;
  onSubmit: (e: FormEvent) => void;
}

const DIMENSIONS = [
  { label: "安全性", angle: -90 },
  { label: "健壮性", angle: -45 },
  { label: "性能", angle: 0 },
  { label: "可读性", angle: 45 },
  { label: "可维护", angle: 90 },
  { label: "可扩展", angle: 135 },
  { label: "可测试", angle: 180 },
  { label: "规范性", angle: 225 },
];

const AI_TELEMETRY_LINES = [
  { tag: "HARNESS", text: "DeepSeek Headless Agent 已就绪 · 64 项八性规则装载完成" },
  { tag: "AST-MAP", text: "构建仓库代码地图 · 识别核心入口 / 鉴权 / 数据访问链路" },
  { tag: "SEC-SCAN", text: "静态预扫描联动 Semgrep & 密钥防泄露门禁检查中..." },
  { tag: "SCORING", text: "八维加权模型收敛 · 自动输出 Markdown & Word 报告" },
  { tag: "RBAC", text: "多角色隔离通道激活 · 超管 / 管理员 / 负责人分级鉴权" },
];

export function LoginView({
  username,
  password,
  error,
  loggingIn,
  onUsernameChange,
  onPasswordChange,
  onSubmit,
}: Props) {
  const [showPwd, setShowPwd] = useState(false);
  const [activeDim, setActiveDim] = useState(0);
  const [logCursor, setLogCursor] = useState(0);

  // 轮播高亮八性维度节点与 AI 实时遥测流
  useEffect(() => {
    const dimTimer = window.setInterval(() => {
      setActiveDim((prev) => (prev + 1) % DIMENSIONS.length);
    }, 1400);
    const logTimer = window.setInterval(() => {
      setLogCursor((prev) => (prev + 1) % AI_TELEMETRY_LINES.length);
    }, 2800);
    return () => {
      window.clearInterval(dimTimer);
      window.clearInterval(logTimer);
    };
  }, []);

  const visibleLogs = [0, 1, 2].map(
    (offset) => AI_TELEMETRY_LINES[(logCursor + offset) % AI_TELEMETRY_LINES.length]
  );

  return (
    <div className="ai-login-stage">
      {/* 共享动态神经网络粒子 + 赛博网格 + 光晕 + 扫描光带 */}
      <NeuralBackdrop variant="login" dark />

      <div className="ai-login-layout">
        {/* 左侧：AI 审查核心视觉区 */}
        <section className="ai-login-hero">
          <div className="ai-status-pill">
            <span className="ai-pulse-dot" />
            <span>AI AUDIT ENGINE · ONLINE</span>
            <span className="ai-pill-sep">|</span>
            <span>64 CHECKS · 8 DIMENSIONS</span>
          </div>

          <h1 className="ai-hero-title">
            八性代码质量
            <span className="ai-hero-gradient">智能审查平台</span>
          </h1>
          <p className="ai-hero-sub">
            Autonomous AI Code Intelligence · 深度融合 DeepSeek Harness 自主代码推理、静态规则预扫描与分级权限管控，为研发团队构建全天候代码质量防线。
          </p>

          {/* 全息八维雷达环 */}
          <div className="ai-radar-shell" aria-hidden="true">
            <svg viewBox="0 0 280 280" className="ai-radar-svg">
              <defs>
                <radialGradient id="aiCoreGlow" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.45" />
                  <stop offset="60%" stopColor="#6366f1" stopOpacity="0.14" />
                  <stop offset="100%" stopColor="#6366f1" stopOpacity="0" />
                </radialGradient>
                <linearGradient id="aiSweepGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.55" />
                  <stop offset="100%" stopColor="#818cf8" stopOpacity="0" />
                </linearGradient>
              </defs>

              <circle cx="140" cy="140" r="108" fill="url(#aiCoreGlow)" />
              <circle cx="140" cy="140" r="106" fill="none" stroke="rgba(56,189,248,0.22)" strokeDasharray="4 6" className="ai-ring-cw" />
              <circle cx="140" cy="140" r="76" fill="none" stroke="rgba(129,140,248,0.28)" strokeDasharray="10 6" className="ai-ring-ccw" />
              <circle cx="140" cy="140" r="46" fill="none" stroke="rgba(56,189,248,0.35)" />

              {/* 雷达旋转扫描扇区 */}
              <g className="ai-radar-sweep">
                <path d="M140,140 L140,34 A106,106 0 0,1 215,65 Z" fill="url(#aiSweepGrad)" />
                <line x1="140" y1="140" x2="140" y2="34" stroke="#38bdf8" strokeWidth="1.5" strokeOpacity="0.9" />
              </g>

              {/* 八维骨架与脉冲节点 */}
              {DIMENSIONS.map((d, idx) => {
                const rad = (d.angle * Math.PI) / 180;
                const x = 140 + Math.cos(rad) * 106;
                const y = 140 + Math.sin(rad) * 106;
                const lx = 140 + Math.cos(rad) * 124;
                const ly = 140 + Math.sin(rad) * 124;
                const isHot = idx === activeDim;
                return (
                  <g key={d.label}>
                    <line
                      x1="140"
                      y1="140"
                      x2={x}
                      y2={y}
                      stroke={isHot ? "rgba(56,189,248,0.75)" : "rgba(148,163,184,0.18)"}
                      strokeWidth={isHot ? 1.4 : 0.8}
                    />
                    <circle
                      cx={x}
                      cy={y}
                      r={isHot ? 5.5 : 3}
                      fill={isHot ? "#38bdf8" : "#818cf8"}
                      opacity={isHot ? 1 : 0.65}
                    />
                    {isHot && (
                      <circle cx={x} cy={y} r="10" fill="none" stroke="#38bdf8" strokeWidth="1" opacity="0.55" />
                    )}
                    <text
                      x={lx}
                      y={ly}
                      textAnchor="middle"
                      dominantBaseline="central"
                      fontSize="10.5"
                      fontWeight={isHot ? 700 : 500}
                      fill={isHot ? "#e0f2fe" : "rgba(203,213,225,0.68)"}
                    >
                      {d.label}
                    </text>
                  </g>
                );
              })}

              {/* 中心 AI 核心图标 */}
              <circle cx="140" cy="140" r="24" fill="rgba(15, 23, 42, 0.88)" stroke="rgba(56, 189, 248, 0.7)" strokeWidth="1.5" />
              <text x="140" y="137" textAnchor="middle" fontSize="11" fontWeight="700" fill="#7dd3fc">
                AI
              </text>
              <text x="140" y="150" textAnchor="middle" fontSize="8" fill="#cbd5e1" letterSpacing="1">
                AUDIT
              </text>
            </svg>

            {/* 右侧微遥测终端 */}
            <div className="ai-telemetry-box">
              <div className="ai-telemetry-head">
                <Terminal size={13} />
                <span>NEURAL TELEMETRY STREAM</span>
                <span className="ai-telemetry-live">LIVE</span>
              </div>
              <div className="ai-telemetry-body">
                {visibleLogs.map((line, idx) => (
                  <div
                    key={`${line.tag}-${idx}`}
                    className={`ai-telemetry-line ${idx === 0 ? "active" : ""}`}
                  >
                    <span className="ai-telemetry-tag">[{line.tag}]</span>
                    <span>{line.text}</span>
                  </div>
                ))}
              </div>
              <div className="ai-telemetry-metrics">
                <div>
                  <span className="ai-metric-val">8 维</span>
                  <span className="ai-metric-lbl">加权评分体系</span>
                </div>
                <div>
                  <span className="ai-metric-val">64 项</span>
                  <span className="ai-metric-lbl">智能核验指标</span>
                </div>
                <div>
                  <span className="ai-metric-val">3 级</span>
                  <span className="ai-metric-lbl">RBAC 权限隔离</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* 右侧：赛博流光毛玻璃登录控制台 */}
        <section className="ai-login-card-wrap">
          <div className="ai-card-glow-border" aria-hidden="true" />
          <form className={`ai-login-card ${loggingIn ? "scanning" : ""}`} onSubmit={onSubmit}>
            <span className="ai-corner tl" aria-hidden="true" />
            <span className="ai-corner tr" aria-hidden="true" />
            <span className="ai-corner bl" aria-hidden="true" />
            <span className="ai-corner br" aria-hidden="true" />
            <div className="ai-card-laser" aria-hidden="true" />

            <div className="ai-card-header">
              <div className="ai-logo-chip">
                <ShieldCheck size={20} />
              </div>
              <div>
                <div className="ai-card-title">身份安全验证</div>
                <div className="ai-card-subtitle">
                  <Sparkles size={12} /> 超级管理员 / 管理员 / 项目负责人统一入口
                </div>
              </div>
            </div>

            {error && <div className="ai-login-error">{error}</div>}

            <div className="ai-field">
              <label htmlFor="login-username">账号 / 姓名全拼</label>
              <div className="ai-input-box">
                <User size={15} className="ai-input-icon" />
                <input
                  id="login-username"
                  type="text"
                  autoFocus
                  autoComplete="username"
                  placeholder="例如 admin 或 wangxiuli"
                  value={username}
                  onChange={(e) => onUsernameChange(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="ai-field">
              <label htmlFor="login-password">访问密码</label>
              <div className="ai-input-box">
                <Lock size={15} className="ai-input-icon" />
                <input
                  id="login-password"
                  type={showPwd ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="请输入登录密码"
                  value={password}
                  onChange={(e) => onPasswordChange(e.target.value)}
                  required
                />
                <button
                  type="button"
                  className="ai-pwd-toggle"
                  onClick={() => setShowPwd((v) => !v)}
                  aria-label={showPwd ? "隐藏密码" : "显示密码"}
                >
                  {showPwd ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              className="ai-submit-btn"
              disabled={loggingIn || !username.trim() || !password}
            >
              <span className="ai-submit-shine" aria-hidden="true" />
              {loggingIn ? (
                <>
                  <Cpu size={15} className="ai-spin" /> 正在校验身份密钥…
                </>
              ) : (
                <>
                  <span>进入审查控制台</span>
                  <ArrowRight size={15} />
                </>
              )}
            </button>

            <div className="ai-login-footnote">
              首次升级部署：默认超级管理员为 <code>admin</code>，初始密码沿用原{" "}
              <code>AUDIT_WEB_TOKEN</code>（未配置令牌请查看 <code>docker compose logs web</code>）。
            </div>
          </form>
        </section>
      </div>
    </div>
  );
}
