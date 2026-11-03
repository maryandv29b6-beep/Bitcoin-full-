// Workspace settings, panel catalogue and persistence.
import { api } from "./lib";

export type PanelKind = "vdelta" | "obdelta" | "dominance" | "dynamics" | "anomaly" | "cvd" | "rsi" | "macd" | "oi" | "funding" | "liq";

export interface PanelCfg { id: string; kind: PanelKind; enabled: boolean; height: number; opts: Record<string, any> }

export const PANEL_INFO: Record<PanelKind, { title: string; desc: string; perp?: boolean; group: string }> = {
  vdelta: { title: "Дельта объёма", desc: "агрессивные покупки − продажи по сделкам", group: "Поток" },
  anomaly: { title: "Аномальный поток", desc: "объём агрессора к среднему 100 свечей, ×3", group: "Поток" },
  cvd: { title: "CVD", desc: "накопленная дельта сделок со сбросом", group: "Поток" },
  obdelta: { title: "Дельта стакана", desc: "B_r, A_r и B−A, стоящие заявки", group: "Стакан" },
  dominance: { title: "Bid Dominance", desc: "100·(B−A)/(B+A) на 0.25/0.5/1 %", group: "Стакан" },
  dynamics: { title: "Imbalance Dynamics", desc: "изменение дисбаланса за Δt, п.п.", group: "Стакан" },
  rsi: { title: "RSI", desc: "Уайлдер, 14, уровни 30/70", group: "Классика" },
  macd: { title: "MACD", desc: "EMA 12/26, сигнал 9", group: "Классика" },
  oi: { title: "Открытый интерес", desc: "OI и его изменение", perp: true, group: "Деривативы" },
  funding: { title: "Funding", desc: "ставка финансирования", perp: true, group: "Деривативы" },
  liq: { title: "Ликвидации", desc: "long / short по потоку биржи", perp: true, group: "Деривативы" },
};

export const UNAVAILABLE = [
  { name: "DMI", desc: "перегрев потока на референсе" },
  { name: "SMF", desc: "«поток умных денег» на референсе" },
  { name: "BGW", desc: "пики с суммами и множителями на референсе" },
];

export const TABS: { id: string; title: string; kinds: PanelKind[] | null }[] = [
  { id: "overview", title: "Обзор", kinds: ["vdelta", "obdelta", "dominance"] },
  { id: "anomaly", title: "Аномальный поток", kinds: ["anomaly", "vdelta"] },
  { id: "book", title: "Стакан Δ", kinds: ["obdelta", "dominance", "dynamics"] },
  { id: "cvd", title: "CVD", kinds: ["cvd", "vdelta"] },
  { id: "oi", title: "OI", kinds: ["oi", "funding"] },
  { id: "liq", title: "Ликвидации", kinds: ["liq"] },
  { id: "all", title: "Все панели", kinds: null },
];

export interface Settings {
  inst: string;
  openTabs: string[];
  favorites: string[];
  tf: string;
  range: string;
  chartMode: "candles" | "line" | "footprint";
  profile: boolean;
  bookLayer: "bands" | "heatmap" | "off";
  fpMetric: "volume" | "delta" | "buysell" | "trades" | "tdelta";
  fpStep: number | null;
  heatAgg: "mean" | "max";
  heatUnits: "quote" | "base";
  heatScale: "auto" | "manual";
  heatMin: number;
  heatMax: number;
  bandAlpha: number;
  bandPalette: "reference" | "mono";
  panels: PanelCfg[];
  tab: string;
  mainFrac: number;
  rightOpen: boolean;
  params: Record<string, any>;
}

let pid = 0;
const P = (kind: PanelKind, enabled = true, opts: Record<string, any> = {}): PanelCfg => ({ id: `${kind}-${++pid}`, kind, enabled, height: 120, opts });

export const DEFAULT_SETTINGS: Settings = {
  inst: "",
  openTabs: [],
  favorites: [],
  tf: "5m",
  range: "1d",
  chartMode: "candles",
  profile: true,
  bookLayer: "bands",
  fpMetric: "delta",
  fpStep: null,
  heatAgg: "mean",
  heatUnits: "quote",
  heatScale: "auto",
  heatMin: 0,
  heatMax: 0,
  bandAlpha: 1,
  bandPalette: "reference",
  panels: [
    P("vdelta", true, { metric: "delta", cumulative: false }),
    P("obdelta", true, { depth: "0.01", style: "lines" }),
    P("dominance", true),
    P("dynamics", true),
    P("anomaly", true),
    P("cvd", true),
    P("rsi", false),
    P("macd", false),
    P("oi", true),
    P("funding", true),
    P("liq", true),
  ],
  tab: "overview",
  mainFrac: 0.62,
  rightOpen: true,
  params: {
    units: "quote", z_base: "1d", z_threshold: 3, dyn_lag: 1, dyn_ema: 0, dom_mode: "imbalance",
    cvd_reset: "utc_day", cvd_session_start: 0, anom_window: 100, anom_threshold: 3, rsi_period: 14,
    macd_fast: 12, macd_slow: 26, macd_signal: 9, book_min_time_cov: 0.5, z_min_n: 30, z_min_cov: 0.9,
  },
};

const LS_KEY = "flowlens.workspace.v1";

export function loadLocal(): Settings {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) return merge(JSON.parse(raw));
  } catch { /* storage may be unavailable */ }
  return structuredClone(DEFAULT_SETTINGS);
}

export function merge(s: Partial<Settings>): Settings {
  const d = structuredClone(DEFAULT_SETTINGS);
  const out = { ...d, ...s, params: { ...d.params, ...(s.params || {}) } } as Settings;
  // keep panel catalogue complete when new panel kinds are added
  const have = new Set((out.panels || []).map((p) => p.kind));
  out.panels = [...(out.panels || []), ...d.panels.filter((p) => !have.has(p.kind))];
  return out;
}

let saveTimer: number | undefined;
export function save(s: Settings) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(s)); } catch { /* ignore */ }
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => { api.putSetting("workspace", s).catch(() => undefined); }, 800);
}

export async function loadRemote(): Promise<Settings | null> {
  try {
    const r = await api.getSetting("workspace");
    return r.value ? merge(r.value as Partial<Settings>) : null;
  } catch {
    return null;
  }
}
