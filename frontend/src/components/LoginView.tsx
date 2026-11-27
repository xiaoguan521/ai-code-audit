import { FormEvent, useEffect, useRef, useState } from "react";
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
  { tag: "SCORING", text: "八维加权模型收敛完成 · 自动生成 Markdown & Word 报告" },
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
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
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

  // 交互式神经网络粒子 + 脉冲连线 Canvas 背景
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    let width = window.innerWidth;
    let height = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();

    const nodeCount = Math.max(42, Math.min(82, Math.floor((width * height) / 18500)));
    const nodes = Array.from({ length: nodeCount }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.42,
      vy: (Math.random() - 0.5) * 0.42,
      r: 1.3 + Math.random() * 1.9,
      phase: Math.random() * Math.PI * 2,
      hue: Math.random() > 0.45 ? "56, 189, 248" : "129, 140, 248",
    }));

    const pulses: Array<{ from: number; to: number; t: number; speed: number }> = [];
    const mouse = { x: -9999, y: -9999, active: false };

    const onMove = (ev: MouseEvent) => {
      mouse.x = ev.clientX;
      mouse.y = ev.clientY;
      mouse.active = true;
    };
    const onLeave = () => {
      mouse.active = false;
    };
    window.addEventListener("resize", resize);
    window.addEventListener("mousemove", onMove, { passive: true });
    window.addEventListener("mouseleave", onLeave);

    let rafId = 0;
    const linkDist = 148;
    const linkDistSq = linkDist * linkDist;

    const frame = (nowMs: number) => {
      ctx.clearRect(0, 0, width, height);

      // 更新节点位置
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        if (!reducedMotion) {
          n.x += n.vx;
          n.y += n.vy;
          if (n.x < -20) n.x = width + 20;
          if (n.x > width + 20) n.x = -20;
          if (n.y < -20) n.y = height + 20;
          if (n.y > height + 20) n.y = -20;

          if (mouse.active) {
            const dx = mouse.x - n.x;
            const dy = mouse.y - n.y;
            const d2 = dx * dx + dy * dy;
            if (d2 > 900 && d2 < 38000) {
              n.x += dx * 0.0022;
              n.y += dy * 0.0022;
            }
          }
        }
      }

      // 绘制神经连线
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 < linkDistSq) {
            const alpha = (1 - Math.sqrt(d2) / linkDist) * 0.28;
            ctx.strokeStyle = `rgba(99, 179, 255, ${alpha.toFixed(3)})`;
            ctx.lineWidth = 0.85;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();

            if (!reducedMotion && pulses.length < 14 && Math.random() < 0.00055) {
              pulses.push({ from: i, to: j, t: 0, speed: 0.012 + Math.random() * 0.014 });
            }
          }
        }

        // 鼠标星芒连接
        if (mouse.active) {
          const mdx = mouse.x - a.x;
          const mdy = mouse.y - a.y;
          const md2 = mdx * mdx + mdy * mdy;
          if (md2 < 28000) {
            const mAlpha = (1 - Math.sqrt(md2) / 168) * 0.45;
            ctx.strokeStyle = `rgba(56, 189, 248, ${mAlpha.toFixed(3)})`;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(mouse.x, mouse.y);
            ctx.stroke();
          }
        }
      }

      // 绘制光脉冲数据包
      for (let k = pulses.length - 1; k >= 0; k--) {
        const p = pulses[k];
        p.t += p.speed;
        const a = nodes[p.from];
        const b = nodes[p.to];
        if (!a || !b || p.t >= 1) {
          pulses.splice(k, 1);
          continue;
        }
        const px = a.x + (b.x - a.x) * p.t;
        const py = a.y + (b.y - a.y) * p.t;
        const grad = ctx.createRadialGradient(px, py, 0, px, py, 7);
        grad.addColorStop(0, "rgba(125, 211, 252, 0.95)");
        grad.addColorStop(0.5, "rgba(99, 102, 241, 0.45)");
        grad.addColorStop(1, "rgba(99, 102, 241, 0)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(px, py, 7, 0, Math.PI * 2);
        ctx.fill();
      }

      // 绘制呼吸发光节点
      const timeSec = nowMs * 0.0022;
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const glow = 0.52 + 0.38 * Math.sin(timeSec + n.phase);
        ctx.fillStyle = `rgba(${n.hue}, ${glow.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fill();
      }

      if (!reducedMotion) {
        rafId = window.requestAnimationFrame(frame);
      }
    };

    rafId = window.requestAnimationFrame(frame);
    return () => {
      window.cancelAnimationFrame(rafId);
      window.removeEventListener("resize", resize);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseleave", onLeave);
    };
  }, []);

  const visibleLogs = [0, 1, 2].map(
    (offset) => AI_TELEMETRY_LINES[(logCursor + offset) % AI_TELEMETRY_LINES.length]
  );

  return (
    <div className="ai-login-stage">
      {/* 动态神经网络粒子背景 */}
      <canvas ref={canvasRef} className="ai-login-canvas" aria-hidden="true" />
      {/* 赛博网格 + 光晕 + 垂直扫描光带 */}
      <div className="ai-login-grid" aria-hidden="true" />
      <div className="ai-login-orb ai-login-orb-1" aria-hidden="true" />
      <div className="ai-login-orb ai-login-orb-2" aria-hidden="true" />
      <div className="ai-login-orb ai-login-orb-3" aria-hidden="true" />
      <div className="ai-login-scanbeam" aria-hidden="true" />

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
