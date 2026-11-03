import { useEffect, useRef } from "react";
import { Num } from "../lib";
import { AXIS_W, C, TimeScale, rgba, setupCanvas } from "./engine";

/** History navigator: whole loaded range with the visible window; click or drag to move. */
export function Minimap({ scale, times, closes, version }: { scale: TimeScale; times: number[]; closes: Num[]; version: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const pr = useRef({ times, closes });
  pr.current = { times, closes };
  const raf = useRef(0);
  const schedule = () => { cancelAnimationFrame(raf.current); raf.current = requestAnimationFrame(draw); };
  useEffect(() => { const off = scale.on(schedule); const ro = new ResizeObserver(schedule); if (ref.current) ro.observe(ref.current); return () => { off(); ro.disconnect(); }; }, []);
  useEffect(schedule);

  function geom(w: number) {
    const { times: t } = pr.current;
    const t0 = t[0], t1 = t[t.length - 1] + scale.tf;
    const pw = w - AXIS_W;
    return { t0, t1, pw, x: (tt: number) => ((tt - t0) / (t1 - t0)) * pw, tOf: (x: number) => t0 + (x / pw) * (t1 - t0) };
  }

  function draw() {
    const cv = ref.current;
    if (!cv) return;
    const { g, w, h } = setupCanvas(cv);
    g.fillStyle = C.panel; g.fillRect(0, 0, w, h);
    const { times: t, closes: c } = pr.current;
    if (t.length < 2) return;
    const G = geom(w);
    let lo = Infinity, hi = -Infinity;
    for (const v of c) if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (!isFinite(lo)) return;
    const y = (v: number) => h - 3 - ((v - lo) / (hi - lo || 1)) * (h - 6);
    g.strokeStyle = rgba(C.text, 0.45); g.beginPath();
    let st = false;
    for (let i = 0; i < t.length; i++) {
      const v = c[i];
      if (v === null) { st = false; continue; }
      const X = G.x(t[i]);
      if (!st) { g.moveTo(X, y(v)); st = true; } else g.lineTo(X, y(v));
    }
    g.stroke();
    const [a, b] = scale.visible();
    const xa = Math.max(0, G.x(a)), xb = Math.min(G.pw, G.x(b));
    g.fillStyle = rgba(C.accent, 0.12); g.fillRect(xa, 0, xb - xa, h);
    g.strokeStyle = rgba(C.accent, 0.7); g.strokeRect(xa + 0.5, 0.5, Math.max(2, xb - xa - 1), h - 1);
  }

  const moveTo = (clientX: number) => {
    const cv = ref.current!;
    const r = cv.getBoundingClientRect();
    const G = geom(r.width);
    const [a, b] = scale.visible();
    const half = (b - a) / 2;
    const center = G.tOf(clientX - r.left);
    const right = center + half - scale.tf * (scale.rightPad + 1);
    scale.pan(((scale.rightT - right) / scale.tf) * scale.barW);
  };

  return <canvas ref={ref} className="minimap" onPointerDown={(e) => { (e.target as Element).setPointerCapture(e.pointerId); moveTo(e.clientX); }}
    onPointerMove={(e) => { if (e.buttons) moveTo(e.clientX); }} />;
}
