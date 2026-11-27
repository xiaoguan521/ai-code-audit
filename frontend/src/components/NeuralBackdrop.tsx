import { useEffect, useRef } from "react";

interface Props {
  variant: "login" | "console";
  dark?: boolean;
}

export function NeuralBackdrop({ variant, dark = false }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const isLogin = variant === "login";
  const isDarkPalette = isLogin || dark;

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

    const divisor = isLogin ? 18500 : 24000;
    const minN = isLogin ? 42 : 30;
    const maxN = isLogin ? 82 : 60;
    const speedScale = isLogin ? 0.42 : 0.28;
    const nodeCount = Math.max(minN, Math.min(maxN, Math.floor((width * height) / divisor)));

    const nodes = Array.from({ length: nodeCount }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * speedScale,
      vy: (Math.random() - 0.5) * speedScale,
      r: (isLogin ? 1.3 : 1.2) + Math.random() * (isLogin ? 1.9 : 1.5),
      phase: Math.random() * Math.PI * 2,
      alt: Math.random() > 0.45,
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
    const linkDist = isLogin ? 148 : 142;
    const linkDistSq = linkDist * linkDist;

    const lineRgb = isDarkPalette ? "99, 179, 255" : "37, 99, 235";
    const lineMaxAlpha = isLogin ? 0.28 : isDarkPalette ? 0.22 : 0.16;
    const mouseRgb = isDarkPalette ? "56, 189, 248" : "59, 130, 246";
    const mouseMaxAlpha = isLogin ? 0.45 : isDarkPalette ? 0.34 : 0.24;
    const nodePrimary = isDarkPalette ? "56, 189, 248" : "37, 99, 235";
    const nodeAlt = isDarkPalette ? "129, 140, 248" : "99, 102, 241";
    const pulseCore = isDarkPalette ? "rgba(125, 211, 252, 0.92)" : "rgba(37, 99, 235, 0.72)";
    const pulseHalo = isDarkPalette ? "rgba(99, 102, 241, 0.42)" : "rgba(56, 189, 248, 0.28)";

    const frame = (nowMs: number) => {
      ctx.clearRect(0, 0, width, height);

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
            if (d2 > 900 && d2 < 36000) {
              n.x += dx * (isLogin ? 0.0022 : 0.0012);
              n.y += dy * (isLogin ? 0.0022 : 0.0012);
            }
          }
        }
      }

      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 < linkDistSq) {
            const alpha = (1 - Math.sqrt(d2) / linkDist) * lineMaxAlpha;
            ctx.strokeStyle = `rgba(${lineRgb}, ${alpha.toFixed(3)})`;
            ctx.lineWidth = isLogin ? 0.85 : 0.75;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();

            if (!reducedMotion && pulses.length < (isLogin ? 14 : 9) && Math.random() < (isLogin ? 0.00055 : 0.00035)) {
              pulses.push({ from: i, to: j, t: 0, speed: 0.011 + Math.random() * 0.013 });
            }
          }
        }

        if (mouse.active) {
          const mdx = mouse.x - a.x;
          const mdy = mouse.y - a.y;
          const md2 = mdx * mdx + mdy * mdy;
          if (md2 < 28000) {
            const mAlpha = (1 - Math.sqrt(md2) / 168) * mouseMaxAlpha;
            ctx.strokeStyle = `rgba(${mouseRgb}, ${mAlpha.toFixed(3)})`;
            ctx.lineWidth = 0.9;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(mouse.x, mouse.y);
            ctx.stroke();
          }
        }
      }

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
        const rad = isLogin ? 7 : 5.5;
        const grad = ctx.createRadialGradient(px, py, 0, px, py, rad);
        grad.addColorStop(0, pulseCore);
        grad.addColorStop(0.5, pulseHalo);
        grad.addColorStop(1, "rgba(99, 102, 241, 0)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(px, py, rad, 0, Math.PI * 2);
        ctx.fill();
      }

      const timeSec = nowMs * 0.002;
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const baseGlow = isLogin ? 0.52 : isDarkPalette ? 0.42 : 0.34;
        const amp = isLogin ? 0.38 : 0.25;
        const glow = baseGlow + amp * Math.sin(timeSec + n.phase);
        ctx.fillStyle = `rgba(${n.alt ? nodePrimary : nodeAlt}, ${glow.toFixed(3)})`;
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
  }, [isLogin, isDarkPalette]);

  return (
    <div
      className={`ai-backdrop no-print ${isLogin ? "ai-backdrop-login" : "ai-backdrop-console"}`}
      aria-hidden="true"
    >
      <canvas ref={canvasRef} className="ai-login-canvas" />
      <div className={isLogin ? "ai-login-grid" : "ai-console-grid"} />
      <div className="ai-login-orb ai-login-orb-1" />
      <div className="ai-login-orb ai-login-orb-2" />
      <div className="ai-login-orb ai-login-orb-3" />
      {isLogin && <div className="ai-login-scanbeam" />}
    </div>
  );
}
