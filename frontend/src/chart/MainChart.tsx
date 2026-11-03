import { useEffect, useRef } from "react";
import { ChartData, Footprint, Heatmap, LiveMsg, Profile, decodeF32, fmtCompact, fmtPrice, fmtSigned } from "../lib";
import { Settings } from "../state";
import { AXIS_W, C, FONT, FONT_B, FONT_UI, HoverBus, TimeScale, drawCrosshairX, drawTimeGrid, drawYAxis, rgba, setupCanvas, visibleIdx } from "./engine";

export interface HeatState {
  meta: Heatmap;
  bid: Float32Array;
  ask: Float32Array;
  canvas: HTMLCanvasElement;   // one pixel per cell: x = column, y = row (top = highest price)
  vmax: number;
  vmin: number;
  key: string;
}

interface Props {
  scale: TimeScale;
  hover: HoverBus;
  data: ChartData | null;
  version: number;
  heat: HeatState | null;
  fp: Footprint | null;
  profile: Profile | null;
  live: LiveMsg | null;
  settings: Settings;
  tick: string;
  onHover: (clientX: number, clientY: number, idx: number | null) => void;
  yManual: { lo: number; hi: number } | null;
  setYManual: (v: { lo: number; hi: number } | null) => void;
}

export function buildHeatCanvas(meta: Heatmap, settings: Settings, prev?: HeatState | null): HeatState | null {
  if (!meta || meta.empty || !meta.bid || !meta.ask) return null;
  const bid = decodeF32(meta.bid);
  const ask = decodeF32(meta.ask);
  return paintHeat({ meta, bid, ask, canvas: prev?.canvas || document.createElement("canvas"), vmax: 0, vmin: 0, key: "" }, settings);
}

export function paintHeat(hs: HeatState, settings: Settings): HeatState {
  const { meta, bid, ask } = hs;
  const rows = meta.rows, cols = meta.cols;
  const quote = settings.heatUnits === "quote";
  const val = (i: number, v: number) => (quote ? v * (meta.lo_price + ((i % rows) + 0.5) * meta.step_value) : v);
  // auto contrast: typical liquidity (median of non-empty cells) is dim, the top 0.5 % saturates,
  // so resting walls stand out; manual mode uses the user's min/max
  let vmax = settings.heatMax;
  let vmin = settings.heatMin;
  if (settings.heatScale === "auto" || !(vmax > 0)) {
    const sample: number[] = [];
    const stride = Math.max(1, Math.floor((rows * cols) / 40000));
    for (let i = 0; i < rows * cols; i += stride) {
      const b = bid[i], a = ask[i];
      const v = Math.max(b === b ? b : 0, a === a ? a : 0);
      if (v > 0) sample.push(val(i, v));
    }
    sample.sort((x, y) => x - y);
    vmax = sample.length ? sample[Math.floor(sample.length * 0.995)] : 1;
    vmin = sample.length ? sample[Math.floor(sample.length * 0.5)] : 0;
  }
  const cv = hs.canvas;
  cv.width = Math.max(1, cols);
  cv.height = Math.max(1, rows);
  const g = cv.getContext("2d")!;
  const img = g.createImageData(cols, rows);
  // log-linear contrast between vmin (typical liquidity, dim) and vmax (walls, saturated)
  const llo = Math.log(Math.max(1e-12, vmin > 0 ? vmin : vmax * 1e-3));
  const lhi = Math.log(Math.max(1e-12, vmax));
  const gb = [31, 191, 117], ga = [229, 72, 77];
  for (let c = 0; c < cols; c++) {
    for (let r = 0; r < rows; r++) {
      const i = c * rows + r;
      const b = bid[i], a = ask[i];
      if (b !== b && a !== a) continue; // NaN: not covered -> transparent
      const vb = b === b ? val(i, b) : 0;
      const va = a === a ? val(i, a) : 0;
      const isBid = vb >= va;
      const v = Math.max(vb, va);
      if (v <= 0) continue;
      const k = Math.max(0, Math.min(1, (Math.log(v) - llo) / (lhi - llo || 1)));
      const p = ((rows - 1 - r) * cols + c) * 4;
      const col = isBid ? gb : ga;
      img.data[p] = col[0]; img.data[p + 1] = col[1]; img.data[p + 2] = col[2];
      img.data[p + 3] = Math.round(255 * (0.06 + 0.9 * Math.pow(k, 1.4)));
    }
  }
  g.putImageData(img, 0, 0);
  return { ...hs, vmax, vmin, key: `${meta.t[0]}-${cols}-${rows}` };
}

function beltColor(side: "bid" | "ask", z: number | null, st: string, thr: number, palette: string): string | null {
  if (st === "nodata") return null;
  if (z === null) return rgba(C.neutral, 0.08);           // warm-up / zero variance: neutral fill
  const base = side === "bid" ? C.bid : C.ask;
  const low = palette === "mono" ? C.neutral : side === "bid" ? C.bidLow : C.askLow;
  if (z <= -thr) return rgba(low, 0.5);                    // statistically low ("ниже базы")
  if (z >= thr) return rgba(base, 0.62);                   // statistically high ("выше базы")
  const k = (z + thr) / (2 * thr);                         // 0..1 inside the normal range
  return rgba(base, 0.06 + 0.3 * k);
}

export function MainChart(p: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const propsRef = useRef(p);
  propsRef.current = p;
  const raf = useRef(0);
  const drag = useRef<{ x: number; y: number; axis: boolean; lo: number; hi: number; moved: boolean } | null>(null);
  const yr = useRef<{ lo: number; hi: number; top: number; bot: number }>({ lo: 0, hi: 1, top: 0, bot: 1 });

  const schedule = () => {
    cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(draw);
  };

  useEffect(() => {
    const offs = [p.scale.on(schedule), p.hover.on(schedule)];
    const ro = new ResizeObserver(() => { if (ref.current) { p.scale.width = ref.current.getBoundingClientRect().width; } schedule(); });
    if (ref.current) ro.observe(ref.current);
    return () => { offs.forEach((f) => f()); ro.disconnect(); cancelAnimationFrame(raf.current); };
  }, []);
  useEffect(schedule);

  function draw() {
    const cv = ref.current;
    const P = propsRef.current;
    if (!cv) return;
    const { g, w, h } = setupCanvas(cv);
    const s = P.scale;
    s.width = w;
    const d = P.data;
    g.fillStyle = C.bg;
    g.fillRect(0, 0, w, h);
    const top = 8, bot = h - 20;
    drawTimeGrid(g, s, bot, bot + 4);
    if (!d || !d.t.length) {
      g.fillStyle = C.muted; g.font = FONT_UI; g.textAlign = "center";
      g.fillText(d ? "Нет данных за выбранный период" : "Загрузка…", (w - AXIS_W) / 2, h / 2);
      return;
    }
    const t = d.t, cd = d.candles;
    const [i0, i1] = visibleIdx(t, s);
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i <= i1; i++) {
      const hh = cd.h[i], ll = cd.l[i];
      if (hh !== null && ll !== null) { if (hh > hi) hi = hh; if (ll < lo) lo = ll; }
    }
    if (!isFinite(lo)) { const last = cd.c.filter((x) => x !== null).pop() as number | undefined; lo = (last || 1) * 0.99; hi = (last || 1) * 1.01; }
    const pad = (hi - lo) * 0.08 || hi * 0.002;
    lo -= pad; hi += pad;
    if (P.yManual) { lo = P.yManual.lo; hi = P.yManual.hi; }
    yr.current = { lo, hi, top, bot };
    const y = (v: number) => bot - ((v - lo) / (hi - lo)) * (bot - top);
    const plotW = w - AXIS_W;
    g.save();
    g.beginPath(); g.rect(0, 0, plotW, bot); g.clip();

    // ---- book layer (under candles)
    if (P.settings.bookLayer === "heatmap" && P.heat) {
      const hm = P.heat.meta;
      if (hm.t.length) {
        g.imageSmoothingEnabled = false;
        const x0 = s.x(hm.t[0]) - s.barW / 2;
        const yTop = y(hm.lo_price + hm.rows * hm.step_value);
        const yBot = y(hm.lo_price);
        g.drawImage(P.heat.canvas, x0, yTop, hm.cols * s.barW * ((hm.t[1] ?? hm.t[0] + s.tf) - hm.t[0]) / s.tf, yBot - yTop);
      }
    } else if (P.settings.bookLayer === "bands") {
      const belts = d.indicators.belts;
      const mid = d.depth.mid;
      const thr = belts.threshold;
      const bw = Math.max(1, s.barW);
      for (let i = i0; i <= i1; i++) {
        const m = mid[i];
        if (m === null) continue;
        const x = s.x(t[i]) - bw / 2;
        for (const b of belts.items) {
          const cb = beltColor("bid", b.bid.z[i], b.bid.status[i], thr, P.settings.bandPalette);
          if (cb) {
            const ya = y(m * (1 - b.lo)), yb = y(m * (1 - b.hi));
            g.fillStyle = cb; g.fillRect(x, ya, bw + 0.5, yb - ya);
          }
          const ca = beltColor("ask", b.ask.z[i], b.ask.status[i], thr, P.settings.bandPalette);
          if (ca) {
            const ya = y(m * (1 + b.hi)), yb = y(m * (1 + b.lo));
            g.fillStyle = ca; g.fillRect(x, ya, bw + 0.5, yb - ya);
          }
        }
      }
    }

    // ---- volume profile (left)
    if (P.settings.profile && P.profile && P.profile.buckets.length) {
      const pr = P.profile;
      const maxW = Math.min(170, plotW * 0.18);
      let vmax = 0;
      for (const b of pr.buckets) vmax = Math.max(vmax, b[1] + b[2]);
      const step = pr.step_value;
      if (pr.val !== null && pr.vah !== null) {
        g.fillStyle = "rgba(226,163,54,0.05)";
        g.fillRect(0, y(pr.vah), maxW, y(pr.val) - y(pr.vah));
      }
      for (const [price, buy, sell] of pr.buckets) {
        const ya = y(price + step), yb = y(price);
        const hgt = Math.max(1, yb - ya - (yb - ya > 3 ? 1 : 0));
        const wb = (buy / vmax) * maxW, ws = (sell / vmax) * maxW;
        g.fillStyle = rgba(C.bid, 0.38); g.fillRect(0, ya, wb, hgt);
        g.fillStyle = rgba(C.ask, 0.38); g.fillRect(wb, ya, ws, hgt);
      }
      if (pr.poc !== null) {
        const yp = Math.round(y(pr.poc + step / 2)) + 0.5;
        g.strokeStyle = rgba(C.accent, 0.8); g.setLineDash([4, 3]);
        g.beginPath(); g.moveTo(0, yp); g.lineTo(maxW + 30, yp); g.stroke(); g.setLineDash([]);
        g.fillStyle = C.accent; g.font = FONT; g.textAlign = "left"; g.textBaseline = "bottom";
        g.fillText("POC", 2, yp - 2);
      }
    }

    // ---- price: candles / line / footprint
    const mode = P.settings.chartMode;
    const bw = s.barW;
    const body = Math.max(1, bw * 0.7);
    if (mode === "line" || bw < 1.6) {
      g.strokeStyle = C.text; g.lineWidth = 1.3; g.beginPath();
      let started = false;
      for (let i = i0; i <= i1; i++) {
        const c = cd.c[i];
        if (c === null) { started = false; continue; }
        const X = s.x(t[i]), Y = y(c);
        if (!started) { g.moveTo(X, Y); started = true; } else g.lineTo(X, Y);
      }
      g.stroke(); g.lineWidth = 1;
    } else {
      const fpMap = new Map<number, number[][]>();
      if (mode === "footprint" && P.fp) for (const fc of P.fp.candles) fpMap.set(fc.t, fc.rows);
      const fstep = P.fp?.step_value || 0;
      let fmax = 0;
      if (mode === "footprint") {
        for (let i = i0; i <= i1; i++) {
          const rows = fpMap.get(t[i]); if (!rows) continue;
          for (const r of rows) fmax = Math.max(fmax, fpVal(P.settings.fpMetric, r, true));
        }
      }
      for (let i = i0; i <= i1; i++) {
        const o = cd.o[i], c = cd.c[i], hh = cd.h[i], ll = cd.l[i];
        if (o === null || c === null || hh === null || ll === null) continue;
        const X = s.x(t[i]);
        const upc = c >= o;
        const partial = !cd.complete[i] && i !== d.live_index;
        const col = upc ? C.up : C.down;
        const rows = mode === "footprint" ? fpMap.get(t[i]) : undefined;
        if (rows && fstep > 0) {
          const cw = bw * 0.92;
          const showText = cw >= 34 && Math.abs(y(0) - y(fstep)) >= 10;
          for (const r of rows) {
            const ya = y(r[0] + fstep), yb = y(r[0]);
            const v = fpVal(P.settings.fpMetric, r, false);
            const mag = Math.min(1, Math.abs(fpVal(P.settings.fpMetric, r, true)) / (fmax || 1));
            const isDelta = P.settings.fpMetric === "delta" || P.settings.fpMetric === "tdelta" || P.settings.fpMetric === "buysell";
            const base = isDelta ? ((r[1] - r[2]) >= 0 ? C.bid : C.ask) : C.delta;
            g.fillStyle = rgba(base, 0.12 + 0.6 * mag);
            g.fillRect(X - cw / 2, ya, cw, Math.max(1, yb - ya - 0.5));
            if (showText) {
              g.fillStyle = C.text; g.font = FONT; g.textAlign = "center"; g.textBaseline = "middle";
              const txt = P.settings.fpMetric === "buysell" ? `${fmtCompact(r[2], 1)}×${fmtCompact(r[1], 1)}`
                : P.settings.fpMetric === "trades" || P.settings.fpMetric === "tdelta" ? String(Math.round(v)) : fmtCompact(v, 1);
              g.fillText(txt, X, (ya + yb) / 2, cw - 2);
            }
          }
          g.strokeStyle = rgba(col, 0.9);
          g.beginPath(); g.moveTo(X - cw / 2 - 1.5, y(o)); g.lineTo(X - cw / 2 - 1.5, y(c)); g.stroke();
          continue;
        }
        g.globalAlpha = partial ? 0.45 : 1;
        g.strokeStyle = col; g.fillStyle = col;
        const xx = Math.round(X) + 0.5;
        g.beginPath(); g.moveTo(xx, y(hh)); g.lineTo(xx, y(ll)); g.stroke();
        const yo = y(o), yc = y(c);
        g.fillRect(X - body / 2, Math.min(yo, yc), body, Math.max(1, Math.abs(yc - yo)));
        g.globalAlpha = 1;
      }
    }

    // ---- quality markers: start of a new valid book sequence
    g.fillStyle = rgba(C.accent, 0.85);
    for (let i = i0; i <= i1; i++) {
      if (d.depth.epoch_start[i]) {
        const X = s.x(t[i]);
        g.beginPath(); g.moveTo(X - 4, top); g.lineTo(X + 4, top); g.lineTo(X, top + 6); g.fill();
      }
    }
    g.restore();

    // ---- axes, last price, crosshair
    drawYAxis(g, w, top, bot, lo, hi, (v) => fmtPrice(v, P.tick), 6);
    let last: number | null = null;
    for (let i = t.length - 1; i >= 0; i--) if (cd.c[i] !== null) { last = cd.c[i]; break; }
    if (last !== null) {
      const ly = Math.round(y(last)) + 0.5;
      const up = d.live_index !== null && cd.o[d.live_index] !== null ? last >= (cd.o[d.live_index] as number) : true;
      g.strokeStyle = rgba(up ? C.up : C.down, 0.6); g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(0, ly); g.lineTo(plotW, ly); g.stroke(); g.setLineDash([]);
      g.fillStyle = up ? "#13734b" : "#8e2a2e";
      g.fillRect(plotW + 1, ly - 9, AXIS_W - 1, 18);
      g.fillStyle = "#fff"; g.font = FONT_B; g.textAlign = "left"; g.textBaseline = "middle";
      g.fillText(fmtPrice(last, P.tick), plotW + 6, ly);
    }
    const hv = P.hover.h;
    drawCrosshairX(g, s, hv, bot);
    if (hv.src === "main" && hv.y > top && hv.y < bot) {
      const py = Math.round(hv.y) + 0.5;
      g.save(); g.strokeStyle = "rgba(197,204,214,0.35)"; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(0, py); g.lineTo(plotW, py); g.stroke(); g.restore();
      const price = lo + ((bot - hv.y) / (bot - top)) * (hi - lo);
      g.fillStyle = "#2a3140"; g.fillRect(plotW + 1, py - 9, AXIS_W - 1, 18);
      g.fillStyle = C.text; g.font = FONT; g.textAlign = "left"; g.textBaseline = "middle";
      g.fillText(fmtPrice(price, P.tick), plotW + 6, py);
    }
    // legend for the book layer
    g.font = FONT_UI; g.textAlign = "left"; g.textBaseline = "top";
    if (P.settings.bookLayer === "bands") {
      const items: [string, string][] = [[rgba(C.ask, 0.6), "аски выше базы"], [rgba(C.askLow, 0.6), "аски ниже базы"],
        [rgba(C.bid, 0.6), "биды выше базы"], [rgba(C.bidLow, 0.6), "биды ниже базы"]];
      let lx = 8;
      const ly = 6;
      g.fillStyle = "rgba(7,9,12,0.7)"; g.fillRect(4, 2, 470, 16);
      for (const [c, lbl] of items) { g.fillStyle = c; g.fillRect(lx, ly + 2, 9, 9); g.fillStyle = C.muted; g.fillText(lbl, lx + 13, ly); lx += g.measureText(lbl).width + 26; }
      g.fillStyle = C.faint;
      g.fillText(`|z| ≥ ${d.indicators.belts.threshold}, база ${d.indicators.belts.base}`, lx, ly);
    } else if (P.settings.bookLayer === "heatmap" && P.heat) {
      const u = P.settings.heatUnits === "quote" ? d.units.quote : d.units.base;
      g.fillStyle = "rgba(7,9,12,0.7)"; g.fillRect(4, 2, 330, 16);
      const grd = g.createLinearGradient(8, 0, 108, 0);
      grd.addColorStop(0, "rgba(31,191,117,0.05)"); grd.addColorStop(1, "rgba(31,191,117,0.95)");
      g.fillStyle = grd; g.fillRect(8, 7, 100, 8);
      g.fillStyle = C.muted;
      g.fillText(`${fmtCompact(P.heat.vmin)} … ${fmtCompact(P.heat.vmax)} ${u} на бакет ${P.heat.meta.step} (log)`, 114, 5);
    }
  }

  function onMove(e: React.PointerEvent) {
    const cv = ref.current!;
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, yy = e.clientY - r.top;
    const P = propsRef.current;
    const dd = drag.current;
    if (dd) {
      if (Math.abs(e.clientX - dd.x) + Math.abs(e.clientY - dd.y) > 2) dd.moved = true;
      if (dd.axis) {
        const k = Math.exp((e.clientY - dd.y) / 150);
        const c = (dd.lo + dd.hi) / 2, half = ((dd.hi - dd.lo) / 2) * k;
        P.setYManual({ lo: c - half, hi: c + half });
      } else {
        P.scale.pan(e.clientX - dd.x);
        dd.x = e.clientX;
        if (P.yManual) {
          const yv = yr.current;
          const dv = ((e.clientY - dd.y) / (yv.bot - yv.top)) * (yv.hi - yv.lo);
          dd.y = e.clientY;
          P.setYManual({ lo: yv.lo + dv, hi: yv.hi + dv });
        }
      }
    }
    const slot = P.scale.slotAt(x);
    const d = P.data;
    let idx: number | null = null;
    if (d && d.t.length) {
      const i = Math.round((slot - d.t[0]) / (d.t[1] - d.t[0] || P.scale.tf));
      idx = i >= 0 && i < d.t.length ? i : null;
    }
    P.hover.set({ t: x < P.scale.plotW ? slot : null, x, y: yy, src: "main" });
    P.onHover(e.clientX, e.clientY, x < P.scale.plotW ? idx : null);
  }

  return (
    <canvas
      ref={ref}
      className="chart-canvas"
      onPointerDown={(e) => {
        const r = ref.current!.getBoundingClientRect();
        const axis = e.clientX - r.left > p.scale.plotW;
        drag.current = { x: e.clientX, y: e.clientY, axis, lo: yr.current.lo, hi: yr.current.hi, moved: false };
        (e.target as Element).setPointerCapture(e.pointerId);
      }}
      onPointerUp={() => { drag.current = null; }}
      onPointerMove={onMove}
      onPointerLeave={() => { p.hover.set({ t: null, src: null }); p.onHover(0, 0, null); }}
      onDoubleClick={(e) => {
        const r = ref.current!.getBoundingClientRect();
        if (e.clientX - r.left > p.scale.plotW) p.setYManual(null);
      }}
      onWheel={(e) => {
        const r = ref.current!.getBoundingClientRect();
        p.scale.zoom(e.deltaY > 0 ? 1 / 1.12 : 1.12, e.clientX - r.left);
      }}
    />
  );
}

export function fpVal(metric: string, r: number[], magnitude: boolean): number {
  // r = [price_lo, buy_qty, sell_qty, buy_trades, sell_trades]
  let v = 0;
  switch (metric) {
    case "volume": v = r[1] + r[2]; break;
    case "delta": v = r[1] - r[2]; break;
    case "buysell": v = r[1] + r[2]; break;
    case "trades": v = r[3] + r[4]; break;
    case "tdelta": v = r[3] - r[4]; break;
  }
  return magnitude ? Math.abs(v) : v;
}

export function fpLabel(metric: string): string {
  return { volume: "объём", delta: "дельта объёма", buysell: "продажи×покупки", trades: "сделки", tdelta: "дельта сделок" }[metric] || metric;
}

export { fmtSigned };
