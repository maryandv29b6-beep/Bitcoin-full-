// Canvas chart engine: shared time scale, crosshair bus, colours, axis helpers.
import { hhmm } from "../lib";

export const C = {
  bg: "#07090c",
  panel: "#0b0e13",
  grid: "#141922",
  gridStrong: "#1d2430",
  text: "#c5ccd6",
  muted: "#687384",
  faint: "#3a4350",
  bid: "#1fbf75",
  ask: "#e5484d",
  bidLow: "#f29d1e",   // statistically low bid volume (below base)
  askLow: "#27a9d8",   // statistically low ask volume (below base)
  up: "#2fd08a",
  down: "#ff5c61",
  delta: "#4c7dff",
  l025: "#e9d44c",
  l05: "#c5ccd6",
  l1: "#5b8cff",
  accent: "#e2a336",
  neutral: "#5c6573",
};

export function rgba(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

export type Listener = () => void;

export class Emitter {
  private ls = new Set<Listener>();
  on(fn: Listener) { this.ls.add(fn); return () => { this.ls.delete(fn); }; }
  emit() { this.ls.forEach((f) => f()); }
}

export const AXIS_W = 72;

/** Shared horizontal (time) scale for the main chart and all panels. */
export class TimeScale extends Emitter {
  tf = 300_000;
  barW = 7;
  rightT = 0;          // slot time at the right anchor
  follow = true;       // auto-scroll with the live candle
  width = 800;         // full canvas width (plot = width - AXIS_W)
  rightPad = 5;        // empty slots to the right of the last candle
  lastT = 0;
  firstT = 0;

  get plotW() { return Math.max(10, this.width - AXIS_W); }

  x(t: number): number {
    return this.plotW - (this.rightPad + (this.rightT - t) / this.tf) * this.barW - this.barW / 2;
  }

  tAt(x: number): number {
    return this.rightT - ((this.plotW - this.barW / 2 - x) / this.barW - this.rightPad) * this.tf;
  }

  slotAt(x: number): number {
    const t = this.tAt(x);
    return Math.round(t / this.tf) * this.tf;
  }

  visible(): [number, number] {
    return [this.tAt(0) - this.tf, this.tAt(this.plotW) + this.tf];
  }

  setData(firstT: number, lastT: number) {
    this.firstT = firstT;
    this.lastT = lastT;
    if (this.follow) this.rightT = lastT;
    this.emit();
  }

  setTf(tf: number) {
    this.tf = tf;
    this.follow = true;
    this.emit();
  }

  zoom(factor: number, anchorX: number) {
    const tA = this.tAt(anchorX);
    this.barW = Math.max(0.6, Math.min(160, this.barW * factor));
    // keep the time under the cursor fixed
    const tB = this.tAt(anchorX);
    if (!this.follow) this.rightT += tA - tB;
    this.emit();
  }

  pan(dxPx: number) {
    this.rightT -= (dxPx / this.barW) * this.tf;
    const minRight = this.firstT + 10 * this.tf;
    if (this.rightT < minRight) this.rightT = minRight;
    if (this.rightT >= this.lastT) { this.rightT = this.lastT; this.follow = true; } else this.follow = false;
    this.emit();
  }

  goLive() { this.follow = true; this.rightT = this.lastT; this.emit(); }
}

export interface Hover { t: number | null; x: number; y: number; src: string | null; price: number | null }
export class HoverBus extends Emitter { h: Hover = { t: null, x: 0, y: 0, src: null, price: null }; set(h: Partial<Hover>) { this.h = { ...this.h, ...h }; this.emit(); } }

export function setupCanvas(cv: HTMLCanvasElement): { g: CanvasRenderingContext2D; w: number; h: number } {
  const r = cv.getBoundingClientRect();
  const d = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.round(r.width * d));
  const H = Math.max(1, Math.round(r.height * d));
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const g = cv.getContext("2d")!;
  g.setTransform(d, 0, 0, d, 0, 0);
  g.clearRect(0, 0, r.width, r.height);
  return { g, w: r.width, h: r.height };
}

export function niceStep(range: number, count: number): number {
  if (!(range > 0)) return 1;
  const raw = range / count;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

export const FONT = '10.5px "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace';
export const FONT_B = '600 10.5px "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace';
export const FONT_UI = '11px "IBM Plex Sans Condensed", "Arial Narrow", system-ui, sans-serif';

export function drawYAxis(g: CanvasRenderingContext2D, w: number, top: number, bot: number, lo: number, hi: number,
  fmt: (v: number) => string, count = 4) {
  const st = niceStep(hi - lo, count);
  g.font = FONT;
  g.textAlign = "left";
  g.textBaseline = "middle";
  const x0 = w - AXIS_W;
  for (let v = Math.ceil(lo / st) * st; v <= hi + 1e-12; v += st) {
    const y = Math.round(bot - ((v - lo) / (hi - lo)) * (bot - top)) + 0.5;
    if (y < top - 1 || y > bot + 1) continue;
    g.strokeStyle = C.grid;
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, y); g.lineTo(x0, y); g.stroke();
    g.fillStyle = C.muted;
    g.fillText(fmt(Math.abs(v) < st * 1e-6 ? 0 : v), x0 + 6, y);
  }
  g.strokeStyle = C.gridStrong;
  g.beginPath(); g.moveTo(x0 + 0.5, 0); g.lineTo(x0 + 0.5, bot + 30); g.stroke();
}

const STEPS = [1, 5, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080].map((m) => m * 60_000);

export function drawTimeGrid(g: CanvasRenderingContext2D, s: TimeScale, h: number, labelsY: number | null) {
  const [t0, t1] = s.visible();
  const pxPerMs = s.barW / s.tf;
  const step = STEPS.find((st) => st * pxPerMs >= 90) || STEPS[STEPS.length - 1];
  const tz = new Date().getTimezoneOffset() * 60_000;
  g.font = FONT;
  g.textAlign = "center";
  g.textBaseline = "top";
  for (let t = Math.ceil((t0 - tz) / step) * step + tz; t <= t1; t += step) {
    const x = Math.round(s.x(t)) + 0.5;
    if (x < 0 || x > s.plotW) continue;
    g.strokeStyle = C.grid;
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    if (labelsY !== null) {
      const d = new Date(t);
      const midnight = d.getHours() === 0 && d.getMinutes() === 0;
      g.fillStyle = midnight ? C.text : C.muted;
      g.fillText(midnight ? `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}` : hhmm(t), x, labelsY);
    }
  }
}

export function drawCrosshairX(g: CanvasRenderingContext2D, s: TimeScale, hover: Hover, h: number) {
  if (hover.t === null) return;
  const x = Math.round(s.x(hover.t)) + 0.5;
  if (x < 0 || x > s.plotW) return;
  g.save();
  g.strokeStyle = "rgba(197,204,214,0.35)";
  g.setLineDash([3, 3]);
  g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
  g.restore();
}

export function indexOfTime(times: number[], t: number): number {
  // times are an evenly spaced grid
  if (!times.length) return -1;
  const tf = times.length > 1 ? times[1] - times[0] : 1;
  const i = Math.round((t - times[0]) / tf);
  return i >= 0 && i < times.length && times[i] === t ? i : -1;
}

export function visibleIdx(times: number[], s: TimeScale): [number, number] {
  if (!times.length) return [0, -1];
  const [a, b] = s.visible();
  const tf = times.length > 1 ? times[1] - times[0] : s.tf;
  const i0 = Math.max(0, Math.floor((a - times[0]) / tf));
  const i1 = Math.min(times.length - 1, Math.ceil((b - times[0]) / tf));
  return [i0, i1];
}
