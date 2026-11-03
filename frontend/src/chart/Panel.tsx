import { useEffect, useRef } from "react";
import { ChartData, LiveMsg, Num, fmtCompact, fmtNum, fmtSigned } from "../lib";
import { PANEL_INFO, PanelCfg, Settings } from "../state";
import { AXIS_W, C, FONT, FONT_B, FONT_UI, HoverBus, TimeScale, drawCrosshairX, drawTimeGrid, drawYAxis, rgba, setupCanvas, visibleIdx } from "./engine";

export interface Series {
  type: "line" | "hist" | "step";
  values: Num[];
  color: string | ((v: number, i: number) => string);
  label: string;
  dash?: number[];
  width?: number;
  fmt?: (v: number) => string;
}

export interface PanelModel {
  series: Series[];
  range?: [number, number];
  zero?: boolean;
  levels?: { v: number; color: string; label?: string }[];
  labels?: { t: number; v: number; text: string; sub?: string; color: string; up: boolean }[];
  unit: string;
  fmt: (v: number) => string;
  message?: string;
}

const pct = (v: number) => fmtNum(v, 1);

export function buildPanel(kind: PanelCfg["kind"], opts: Record<string, any>, d: ChartData, settings: Settings, live: LiveMsg | null): PanelModel {
  const I = d.indicators;
  const unitQ = d.units.quote;
  const unitB = d.units.base;
  const flowUnit = I.units.trades === "quote" ? unitQ : unitB;
  const bookUnit = I.units.book === "quote" ? unitQ : unitB;
  const perp = d.source.market === "perp";
  const liveI = d.live_index;
  switch (kind) {
    case "vdelta": {
      const m = opts.metric || "delta";
      if (opts.cumulative) {
        return { series: [{ type: "line", values: I.flow.cvd, color: C.delta, label: "CVD" }], zero: true, unit: flowUnit, fmt: (v) => fmtCompact(v) };
      }
      if (m === "volume") return { series: [{ type: "hist", values: I.flow.total, color: rgba(C.text, 0.45), label: "объём" }], unit: flowUnit, fmt: (v) => fmtCompact(v) };
      if (m === "buysell") return { series: [
        { type: "hist", values: I.flow.buy, color: rgba(C.bid, 0.75), label: "покупки" },
        { type: "hist", values: I.flow.sell.map((v) => (v === null ? null : -v)), color: rgba(C.ask, 0.75), label: "продажи" }], zero: true, unit: flowUnit, fmt: (v) => fmtCompact(v) };
      if (m === "trades") return { series: [{ type: "hist", values: I.flow.trades_total, color: rgba(C.text, 0.45), label: "сделки" }], unit: "сделок", fmt: (v) => fmtCompact(v, 0) };
      if (m === "tdelta") return { series: [{ type: "hist", values: I.flow.trades_delta, color: (v) => (v >= 0 ? C.bid : C.ask), label: "дельта сделок" }], zero: true, unit: "сделок", fmt: (v) => fmtSigned(v, 0),
        message: I.flow.trades_delta.every((v) => v === null) ? "Нет разбивки сделок по стороне (история из свечей биржи)" : undefined };
      return { series: [{ type: "hist", values: I.flow.delta, color: (v) => (v >= 0 ? C.bid : C.ask), label: "дельта" }], zero: true, unit: flowUnit, fmt: (v) => fmtSigned(v),
        message: I.flow.delta.every((v) => v === null) ? "Нет разбивки на покупки/продажи для этого источника" : undefined };
    }
    case "cvd":
      return { series: [{ type: "line", values: I.flow.cvd, color: (v, i) => (I.flow.cvd_partial[i] ? rgba(C.delta, 0.45) : C.delta), label: `CVD (${String(I.params.cvd_reset)})` }],
        zero: true, unit: flowUnit, fmt: (v) => fmtCompact(v) };
    case "anomaly": {
      const buy = I.flow.buy, sell = I.flow.sell, rb = I.anomaly.buy.ratio, rs = I.anomaly.sell.ratio;
      const thr = Number(I.params.anom_threshold);
      const labels = I.anomaly.peaks.map((pk) => ({
        t: pk.t, v: pk.side === "buy" ? (buy[d.t.indexOf(pk.t)] || 0) : -(sell[d.t.indexOf(pk.t)] || 0),
        text: `${fmtCompact(pk.sum)} ×${pk.ratio.toFixed(1)}`, sub: pk.side === "buy" ? "покупки" : "продажи",
        color: pk.side === "buy" ? C.bid : C.ask, up: pk.side === "buy",
      }));
      const warm = I.anomaly.buy.status.every((s) => s !== "ok");
      return {
        series: [
          { type: "hist", values: buy, color: (_, i) => (rb[i] !== null && (rb[i] as number) >= thr ? C.l025 : rgba(C.bid, 0.55)), label: "покупки" },
          { type: "hist", values: sell.map((v) => (v === null ? null : -v)), color: (_, i) => (rs[i] !== null && (rs[i] as number) >= thr ? "#c04ee8" : rgba(C.ask, 0.55)), label: "продажи" },
        ],
        zero: true, labels, unit: flowUnit, fmt: (v) => fmtCompact(v),
        message: warm ? `Прогрев: нужно ${I.params.anom_window} завершённых свечей` : undefined,
      };
    }
    case "obdelta": {
      const k = opts.depth || "0.01";
      const src = I.ob_delta[k] || I.ob_delta_ext[k];
      if (!src) return { series: [], unit: bookUnit, fmt: (v) => fmtCompact(v), message: "Нет данных" };
      const noCov = src.bid.every((v) => v === null);
      const covNote = noCov ? (src.cov_bid.some((c) => c !== null && c < 1) ? `Глубина ${(Number(k) * 100).toFixed(2)}% не покрыта стаканом источника (null, а не 0)` : "История стакана копится с момента запуска") : undefined;
      if (opts.style === "hist") return { series: [{ type: "hist", values: src.delta, color: (v) => (v >= 0 ? C.bid : C.ask), label: "B−A" }], zero: true, unit: bookUnit, fmt: (v) => fmtSigned(v), message: covNote };
      return { series: [
        { type: "line", values: src.bid, color: C.bid, label: "B" },
        { type: "line", values: src.ask, color: C.ask, label: "A" },
        { type: "line", values: src.delta, color: C.delta, label: "B−A" }], zero: true, unit: bookUnit, fmt: (v) => fmtCompact(v), message: covNote };
    }
    case "dominance": {
      const L = I.dominance.lines;
      const imb = I.dominance.mode === "imbalance";
      const liveVals = live?.book?.bands;
      const withLive = (k: string) => {
        const v = [...L[k]];
        if (liveI !== null && liveVals && liveVals[k]) {
          const b = liveVals[k];
          v[liveI] = imb ? b.imbalance : b.bid !== null && b.ask !== null && b.bid + b.ask > 0 ? (100 * b.bid) / (b.bid + b.ask) : null;
        }
        return v;
      };
      const none = Object.values(L).every((arr) => arr.every((v) => v === null));
      return {
        series: [
          { type: "line", values: withLive("0.0025"), color: C.l025, label: "0.25%" },
          { type: "line", values: withLive("0.005"), color: C.l05, label: "0.5%" },
          { type: "line", values: withLive("0.01"), color: C.l1, label: "1%", dash: [3, 2] },
        ],
        range: I.dominance.range as [number, number], levels: [{ v: I.dominance.neutral, color: C.faint }],
        unit: "%", fmt: pct, message: none ? "История стакана копится с момента запуска" : undefined,
      };
    }
    case "dynamics": {
      const L = I.dynamics.lines;
      return { series: [
        { type: "line", values: L["0.0025"], color: C.l025, label: "0.25%" },
        { type: "line", values: L["0.005"], color: C.l05, label: "0.5%" },
        { type: "line", values: L["0.01"], color: C.l1, label: "1%", dash: [3, 2] }], zero: true, unit: "п.п.", fmt: (v) => fmtNum(v, 1) };
    }
    case "rsi": {
      const v = [...I.rsi.values];
      if (liveI !== null && I.rsi.live !== undefined) v[liveI] = I.rsi.live ?? null;
      return { series: [{ type: "line", values: v, color: C.l025, label: `RSI ${I.rsi.period}` }], range: [0, 100],
        levels: [{ v: 30, color: C.faint, label: "30" }, { v: 70, color: C.faint, label: "70" }], unit: "", fmt: (x) => fmtNum(x, 0) };
    }
    case "macd":
      return { series: [
        { type: "hist", values: I.macd.hist, color: (x) => (x >= 0 ? rgba(C.bid, 0.6) : rgba(C.ask, 0.6)), label: "hist" },
        { type: "line", values: I.macd.macd, color: C.delta, label: "MACD" },
        { type: "line", values: I.macd.signal, color: C.accent, label: "signal" }], zero: true, unit: "", fmt: (x) => fmtNum(x, 2) };
    case "oi":
      if (!perp || !I.deriv) return { series: [], unit: "", fmt: (v) => fmtCompact(v), message: "Неприменимо: открытый интерес есть только у фьючерсов" };
      return { series: [
        { type: "hist", values: I.deriv.oi_change, color: (x) => (x >= 0 ? rgba(C.bid, 0.5) : rgba(C.ask, 0.5)), label: "ΔOI" },
        { type: "step", values: I.deriv.oi, color: C.accent, label: "OI" }], unit: unitB, fmt: (v) => fmtCompact(v),
        message: I.deriv.oi.every((v) => v === null) ? "Нет данных OI за период" : undefined };
    case "funding":
      if (!perp || !I.deriv) return { series: [], unit: "", fmt: (v) => fmtNum(v, 4), message: "Неприменимо на споте" };
      return { series: [{ type: "step", values: I.deriv.funding.map((v) => (v === null ? null : v * 100)), color: (x) => (x >= 0 ? C.bid : C.ask), label: "funding, % за период" }],
        zero: true, unit: "%", fmt: (v) => fmtNum(v, 4), message: I.deriv.funding.every((v) => v === null) ? "Нет данных funding за период" : undefined };
    case "liq":
      if (!perp || !I.deriv) return { series: [], unit: "", fmt: (v) => fmtCompact(v), message: "Неприменимо на споте" };
      return { series: [
        { type: "hist", values: I.deriv.liq_short, color: rgba(C.bid, 0.8), label: "short ликв." },
        { type: "hist", values: I.deriv.liq_long.map((v) => (v === null ? null : -v)), color: rgba(C.ask, 0.8), label: "long ликв." }],
        zero: true, unit: flowUnit, fmt: (v) => fmtCompact(v),
        message: d.source.exchange === "binance" ? "Binance forceOrder: не более 1 ликвидации в секунду на символ — поток неполный" : undefined };
  }
  return { series: [], unit: "", fmt: String };
}

interface Props {
  cfg: PanelCfg;
  scale: TimeScale;
  hover: HoverBus;
  model: PanelModel | null;
  times: number[];
  version: number;
  liveIndex: number | null;
}

export function PanelCanvas(p: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const pr = useRef(p);
  pr.current = p;
  const raf = useRef(0);
  const schedule = () => { cancelAnimationFrame(raf.current); raf.current = requestAnimationFrame(draw); };
  useEffect(() => {
    const offs = [p.scale.on(schedule), p.hover.on(schedule)];
    const ro = new ResizeObserver(schedule);
    if (ref.current) ro.observe(ref.current);
    return () => { offs.forEach((f) => f()); ro.disconnect(); cancelAnimationFrame(raf.current); };
  }, []);
  useEffect(schedule);

  function draw() {
    const cv = ref.current;
    if (!cv) return;
    const P = pr.current;
    const { g, w, h } = setupCanvas(cv);
    g.fillStyle = C.bg; g.fillRect(0, 0, w, h);
    const s = P.scale, m = P.model, t = P.times;
    const top = 6, bot = h - 4;
    drawTimeGrid(g, s, h, null);
    if (!m || !t.length) return;
    const [i0, i1] = visibleIdx(t, s);
    let lo = Infinity, hi = -Infinity;
    if (m.range) { [lo, hi] = m.range; } else {
      for (const se of m.series) for (let i = i0; i <= i1; i++) {
        const v = se.values[i];
        if (v === null || v === undefined || !isFinite(v)) continue;
        if (v < lo) lo = v; if (v > hi) hi = v;
      }
      if (m.zero || m.series.some((x) => x.type === "hist")) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (!isFinite(lo)) { lo = -1; hi = 1; }
      if (hi === lo) { hi += 1; lo -= 1; }
      const pad = (hi - lo) * 0.1; hi += pad; if (lo < 0 || !m.zero) lo -= pad;
    }
    const y = (v: number) => bot - ((v - lo) / (hi - lo)) * (bot - top);
    const plotW = w - AXIS_W;
    drawYAxis(g, w, top, bot, lo, hi, m.fmt, Math.max(2, Math.floor((bot - top) / 28)));
    g.save(); g.beginPath(); g.rect(0, 0, plotW, h); g.clip();
    if (m.zero && lo < 0 && hi > 0) { g.strokeStyle = C.gridStrong; g.beginPath(); g.moveTo(0, Math.round(y(0)) + 0.5); g.lineTo(plotW, Math.round(y(0)) + 0.5); g.stroke(); }
    for (const lv of m.levels || []) {
      g.strokeStyle = lv.color; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(0, Math.round(y(lv.v)) + 0.5); g.lineTo(plotW, Math.round(y(lv.v)) + 0.5); g.stroke(); g.setLineDash([]);
    }
    const bw = Math.max(1, s.barW * 0.72);
    for (const se of m.series) {
      const col = (v: number, i: number) => (typeof se.color === "function" ? se.color(v, i) : se.color);
      if (se.type === "hist") {
        const y0 = y(Math.max(lo, Math.min(hi, 0)));
        for (let i = i0; i <= i1; i++) {
          const v = se.values[i];
          if (v === null || v === undefined) continue;
          const X = s.x(t[i]) - bw / 2, Y = y(v);
          g.globalAlpha = i === P.liveIndex ? 0.55 : 1;
          g.fillStyle = col(v, i);
          g.fillRect(X, Math.min(Y, y0), bw, Math.max(1, Math.abs(y0 - Y)));
        }
        g.globalAlpha = 1;
      } else {
        g.lineWidth = se.width || 1.3;
        g.setLineDash(se.dash || []);
        let prev: [number, number] | null = null;
        for (let i = i0; i <= i1; i++) {
          const v = se.values[i];
          if (v === null || v === undefined || !isFinite(v)) { prev = null; continue; }
          const X = s.x(t[i]), Y = y(v);
          if (prev) {
            g.strokeStyle = col(v, i);
            g.beginPath(); g.moveTo(prev[0], prev[1]);
            if (se.type === "step") { g.lineTo(X, prev[1]); }
            g.lineTo(X, Y); g.stroke();
          }
          prev = [X, Y];
        }
        g.setLineDash([]); g.lineWidth = 1;
      }
    }
    // labels for anomaly peaks (avoid overlaps)
    if (m.labels && m.labels.length) {
      const placed: { x: number; y: number }[] = [];
      g.textAlign = "center";
      for (const lb of m.labels) {
        const i = Math.round((lb.t - t[0]) / (t[1] - t[0] || s.tf));
        if (i < i0 || i > i1) continue;
        const X = s.x(lb.t);
        let Y = lb.up ? Math.max(top + 20, y(lb.v) - 6) : Math.min(bot - 4, y(lb.v) + 24);
        let guard = 0;
        while (placed.some((q) => Math.abs(q.x - X) < 78 && Math.abs(q.y - Y) < 22) && guard++ < 4) Y += lb.up ? -20 : 20;
        if (Y < top + 18 || Y > bot + 2) continue;
        placed.push({ x: X, y: Y });
        g.fillStyle = lb.color; g.font = FONT_B; g.textBaseline = "alphabetic";
        g.fillText(lb.text, X, Y - 10);
        g.font = FONT_UI; g.fillStyle = rgba(lb.color, 0.8);
        g.fillText(lb.sub || "", X, Y + 1);
      }
    }
    g.restore();
    drawCrosshairX(g, s, P.hover.h, h);
    if (m.message) {
      g.font = FONT_UI; g.fillStyle = C.muted; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillStyle = "rgba(7,9,12,0.75)";
      const tw = g.measureText(m.message).width + 16;
      g.fillRect(plotW / 2 - tw / 2, h / 2 - 10, tw, 20);
      g.fillStyle = C.muted;
      g.fillText(m.message, plotW / 2, h / 2);
    }
  }

  return (
    <canvas ref={ref} className="chart-canvas"
      onPointerMove={(e) => {
        const r = ref.current!.getBoundingClientRect();
        const x = e.clientX - r.left;
        p.hover.set({ t: x < p.scale.plotW ? p.scale.slotAt(x) : null, x, y: e.clientY - r.top, src: p.cfg.id });
      }}
      onPointerLeave={() => p.hover.set({ t: null, src: null })}
      onWheel={(e) => { const r = ref.current!.getBoundingClientRect(); p.scale.zoom(e.deltaY > 0 ? 1 / 1.12 : 1.12, e.clientX - r.left); }}
    />
  );
}

export function legendValues(m: PanelModel | null, idx: number | null): { label: string; value: string; color: string }[] {
  if (!m) return [];
  return m.series.map((se) => {
    const v = idx !== null ? se.values[idx] : null;
    const col = typeof se.color === "function" ? (v !== null && v !== undefined ? se.color(v, idx || 0) : C.muted) : se.color;
    return { label: se.label, value: v === null || v === undefined ? "—" : (se.fmt || m.fmt)(v), color: col };
  });
}

export { PANEL_INFO };
