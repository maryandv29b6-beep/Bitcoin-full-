// Types, API client, live stream and formatting helpers.

export type Num = number | null;

export interface Instrument {
  key: string; exchange: string; market: string; symbol: string; base: string; quote: string;
  tickSize: string; quantityStep: string; demo: boolean; status: string; ready: boolean;
  last_price: Num; ticker: { last?: number; change_pct?: number; volume_quote?: number; volume_base?: number } | null;
  steps: { footprint: string | null; heatmap: string | null }; sync_rules: string;
  contractSpec: Record<string, unknown>;
}

export interface Belt { lo: number; hi: number; bid: { value: Num[]; z: Num[]; status: string[] }; ask: { value: Num[]; z: Num[]; status: string[] } }

export interface ChartData {
  inst: string; tf: string; from: number; to: number; status: string;
  source: { exchange: string; market: string; sync_rules: string; demo: boolean };
  units: { price: string; base: string; quote: string };
  availability: Record<string, Num | string>;
  quality: { t_start: number; kind: string; detail: string }[];
  t: number[];
  live_index: number | null;
  candles: {
    o: Num[]; h: Num[]; l: Num[]; c: Num[]; v_buy: Num[]; v_sell: Num[]; q_buy: Num[]; q_sell: Num[];
    n_buy: Num[]; n_sell: Num[]; n_total: Num[]; complete: boolean[]; minutes: number[]; source: string[][];
  };
  depth: { samples: number[]; time_cov: number[]; mid: Num[]; spread_bps: Num[]; lag_ms: Num[]; epoch_start: boolean[] };
  indicators: {
    params: Record<string, unknown>; params_hash: string; algorithm: string; units: { book: string; trades: string };
    flow: { buy: Num[]; sell: Num[]; delta: Num[]; total: Num[]; trades_delta: Num[]; trades_buy: Num[]; trades_sell: Num[]; trades_total: Num[]; cvd: Num[]; cvd_partial: boolean[] };
    anomaly: { buy: { ratio: Num[]; z: Num[]; status: string[] }; sell: { ratio: Num[]; z: Num[]; status: string[] };
      peaks: { t: number; t0: number; t1: number; side: string; sum: number; ratio: number; candles: number }[] };
    ob_delta: Record<string, { bid: Num[]; ask: Num[]; delta: Num[]; cov_bid: Num[]; cov_ask: Num[] }>;
    ob_delta_ext: Record<string, { bid: Num[]; ask: Num[]; delta: Num[]; cov_bid: Num[]; cov_ask: Num[] }>;
    dominance: { mode: string; lines: Record<string, Num[]>; range: [number, number]; neutral: number };
    dynamics: { lag: number; ema: number; lines: Record<string, Num[]>; unit: string };
    belts: { base: string; slots: number; threshold: number; items: Belt[] };
    rsi: { period: number; values: Num[]; live?: Num };
    macd: { macd: Num[]; signal: Num[]; hist: Num[]; live?: { macd: Num; signal: Num; hist: Num } };
    deriv?: { oi: Num[]; oi_value: Num[]; oi_change: Num[]; funding: Num[]; next_funding: Num[]; mark: Num[];
      liq_long: Num[]; liq_short: Num[]; liq_long_n: Num[]; liq_short_n: Num[] };
  };
}

export interface Heatmap {
  t: number[]; rows: number; cols: number; step: string; step_value: number; lo_price: number; units: string;
  bid?: string; ask?: string; empty?: boolean; agg: string;
}

export interface Footprint { step: string; step_value: number; candles: { t: number; partial: boolean; rows: number[][] }[] }
export interface Profile { buckets: number[][]; step_value: number; poc: Num; val: Num; vah: Num; va_share: Num; partial: boolean; units: string }

export interface LiveMsg {
  type: "live"; inst: string; tf: string; now: number;
  status: { status: string; book_status: string; last_event: Num; latency_ms: { book: Num; trades: Num }; generating: boolean; book_epoch: number };
  candle: { t: number; o: number; h: number; l: number; c: number; q_buy: number; q_sell: number; v_buy: number; v_sell: number; n_buy: Num; n_sell: Num } | null;
  book: { mid: number; best_bid: number; best_ask: number; spread_bps: number;
    bands: Record<string, { bid: Num; ask: Num; bid_qty: Num; ask_qty: Num; cov_bid: number; cov_ask: number; imbalance: Num; delta: Num }> } | null;
  heat?: { t: number; origin: number; step: number; bid: number[]; ask: number[] };
}

export const TF_MS: Record<string, number> = { "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000 };
export const RANGES: Record<string, number> = { "1d": 86_400_000, "2d": 172_800_000, "3d": 259_200_000, "7d": 604_800_000, "14d": 1_209_600_000 };
export const BASES = ["1h", "4h", "12h", "1d", "3d", "7d", "14d"];

async function getJSON<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const q = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)])).toString() : "";
  const r = await fetch(path + q);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error || msg; } catch { /* keep */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

async function sendJSON<T>(method: string, path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { error?: string }).error || `HTTP ${r.status}`);
  return data as T;
}

export const api = {
  meta: () => getJSON<{ demo: boolean; version: string; defaults: Record<string, unknown>; alert_metrics: Record<string, string>; alert_ops: string[] }>("/api/meta"),
  instruments: () => getJSON<{ instruments: Instrument[] }>("/api/instruments"),
  chart: (inst: string, tf: string, from: number, params: Record<string, unknown>) =>
    getJSON<ChartData>("/api/chart", { inst, tf, from, params: JSON.stringify(params) }),
  heatmap: (inst: string, tf: string, from: number, agg: string, step?: number) =>
    getJSON<Heatmap>("/api/heatmap", { inst, tf, from, agg, step }),
  footprint: (inst: string, tf: string, from: number, to: number, step?: number) =>
    getJSON<Footprint>("/api/footprint", { inst, tf, from, to, step }),
  profile: (inst: string, from: number, to: number, step?: number) => getJSON<Profile>("/api/profile", { inst, from, to, step }),
  screener: () => getJSON<{ rows: ScreenerRow[]; disclaimer: string }>("/api/screener"),
  health: () => getJSON<Record<string, unknown>>("/api/health"),
  getSetting: (key: string) => getJSON<{ value: unknown }>(`/api/settings/${key}`),
  putSetting: (key: string, value: unknown) => sendJSON("PUT", `/api/settings/${key}`, { value }),
  alerts: () => getJSON<{ rules: AlertRule[]; metrics: Record<string, string>; ops: string[] }>("/api/alerts"),
  addAlert: (instrument: string, rule: Record<string, unknown>) => sendJSON("POST", "/api/alerts", { instrument, rule }),
  patchAlert: (id: number, enabled: boolean) => sendJSON("PATCH", `/api/alerts/${id}`, { enabled }),
  deleteAlert: (id: number) => fetch(`/api/alerts/${id}`, { method: "DELETE" }),
  alertLog: () => getJSON<{ log: { id: number; rule_id: number; t: number; message: string; data: Record<string, unknown>; delivery: string }[] }>("/api/alerts/log"),
  telegram: () => getJSON<{ enabled: boolean; chat_id: string; token_set: boolean; token_hint: string }>("/api/telegram"),
  putTelegram: (body: Record<string, unknown>) => sendJSON<{ enabled: boolean; chat_id: string; token_set: boolean; token_hint: string }>("PUT", "/api/telegram", body),
  testTelegram: () => sendJSON<{ result: string }>("POST", "/api/telegram/test", {}),
};

export interface AlertRule { id: number; created: number; enabled: boolean; instrument: string; last_fired: number | null;
  rule: { metric: string; op: string; value: number; confirm_s: number; cooldown_s: number; note: string; telegram: boolean } }

export interface ScreenerRow {
  key: string; exchange: string; market: string; symbol: string; base: string; quote: string; status: string; demo: boolean;
  price: Num; change_24h_pct: Num; volume_24h_quote: Num; volume_unit: string; spread_bps: Num;
  bands: Record<string, { bid: Num; ask: Num; imbalance: Num; cov_bid: number; cov_ask: number }> | null;
  dynamics_5m: Record<string, Num> | null; delta_5m?: Num; delta_1h?: Num; delta_partial?: boolean;
  cvd_day?: Num; cvd_day_partial?: boolean; z_bid_1?: Num; z_ask_1?: Num; z_bid_1_status?: string; z_ask_1_status?: string;
  freshness_s: Num; coverage_1: [number, number] | null; oi?: Num; oi_value?: Num; funding?: Num; next_funding?: Num; funding_interval_h?: Num;
}

// ---- live stream -------------------------------------------------------------------
export class Live {
  ws: WebSocket | null = null;
  sub: { inst: string; tf: string } | null = null;
  handlers = new Set<(m: any) => void>();
  connected = false;
  private retry = 1000;
  private closed = false;

  constructor() { this.open(); }

  private open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/stream`);
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true; this.retry = 1000;
      if (this.sub) ws.send(JSON.stringify({ op: "sub", ...this.sub }));
      this.emit({ type: "ui_conn", connected: true });
    };
    ws.onmessage = (ev) => { try { this.emit(JSON.parse(ev.data)); } catch { /* ignore */ } };
    ws.onclose = () => {
      this.connected = false;
      this.emit({ type: "ui_conn", connected: false });
      if (!this.closed) setTimeout(() => this.open(), this.retry);
      this.retry = Math.min(this.retry * 2, 15000);
    };
  }

  subscribe(inst: string, tf: string) {
    this.sub = { inst, tf };
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ op: "sub", inst, tf }));
  }

  on(fn: (m: any) => void) { this.handlers.add(fn); return () => this.handlers.delete(fn); }
  private emit(m: any) { this.handlers.forEach((h) => h(m)); }
}

// ---- formatting ----------------------------------------------------------------------
export function fmtNum(v: Num | undefined, digits = 2): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  return v.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtCompact(v: Num | undefined, digits = 1): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const s = v < 0 ? "−" : "";
  if (a >= 1e9) return s + (a / 1e9).toFixed(digits) + " B";
  if (a >= 1e6) return s + (a / 1e6).toFixed(digits) + " M";
  if (a >= 1e3) return s + (a / 1e3).toFixed(digits) + " K";
  if (a >= 100) return s + a.toFixed(0);
  if (a >= 1) return s + a.toFixed(2);
  return s + a.toPrecision(3);
}

export function fmtSigned(v: Num | undefined, digits = 1): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  return (v > 0 ? "+" : "") + fmtCompact(v, digits);
}

export function fmtPrice(v: Num | undefined, tick?: string): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const d = tick ? Math.max(0, (tick.split(".")[1] || "").replace(/0+$/, "").length) : v >= 1000 ? 1 : v >= 1 ? 3 : 5;
  return v.toLocaleString("ru-RU", { minimumFractionDigits: Math.min(d, 6), maximumFractionDigits: Math.min(d, 6) });
}

export function hhmm(t: number, utc = false): string {
  const d = new Date(t);
  const h = utc ? d.getUTCHours() : d.getHours();
  const m = utc ? d.getUTCMinutes() : d.getMinutes();
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function dateTime(t: number): string {
  const d = new Date(t);
  return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${hhmm(t)}`;
}

export function ago(t: Num, now = Date.now()): string {
  if (!t) return "нет событий";
  const s = Math.max(0, (now - t) / 1000);
  if (s < 1) return "только что";
  if (s < 60) return `${Math.round(s)} с назад`;
  if (s < 3600) return `${Math.round(s / 60)} мин назад`;
  return `${Math.round(s / 3600)} ч назад`;
}

export function decodeF32(b64: string): Float32Array {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new Float32Array(buf);
}

export const MARKET_LABEL: Record<string, string> = { spot: "спот", perp: "бессрочный фьючерс" };
export const EXCHANGE_LABEL: Record<string, string> = { binance: "Binance", bybit: "Bybit", demo: "DEMO" };
