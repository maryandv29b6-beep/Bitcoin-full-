import { useMemo, useState } from "react";
import { EXCHANGE_LABEL, Instrument, MARKET_LABEL, fmtCompact, fmtNum, fmtPrice } from "./lib";
import { PANEL_INFO, PanelCfg, Settings, UNAVAILABLE } from "./state";

interface Props {
  instruments: Instrument[];
  statuses: Record<string, { status: string; last: number | null; change_pct: number | null; volume_quote: number | null }>;
  settings: Settings;
  update: (patch: Partial<Settings>) => void;
  openInst: (key: string) => void;
  isPerp: boolean;
}

export function RightColumn(p: Props) {
  const [q, setQ] = useState("");
  const [tab, setTab] = useState<"inst" | "ind">("inst");
  const groups = useMemo(() => {
    const m = new Map<string, Instrument[]>();
    for (const i of p.instruments) {
      if (q && !i.symbol.toLowerCase().includes(q.toLowerCase()) && !i.exchange.includes(q.toLowerCase())) continue;
      const g = `${EXCHANGE_LABEL[i.exchange] || i.exchange} · ${MARKET_LABEL[i.market] || i.market}`;
      if (!m.has(g)) m.set(g, []);
      m.get(g)!.push(i);
    }
    return [...m.entries()];
  }, [p.instruments, q]);
  const fav = new Set(p.settings.favorites);
  const toggleFav = (k: string) => p.update({ favorites: fav.has(k) ? p.settings.favorites.filter((x) => x !== k) : [...p.settings.favorites, k] });
  const favList = p.instruments.filter((i) => fav.has(i.key));

  const row = (i: Instrument) => {
    const st = p.statuses[i.key];
    const last = st?.last ?? i.last_price ?? i.ticker?.last ?? null;
    const ch = st?.change_pct ?? i.ticker?.change_pct ?? null;
    const vol = st?.volume_quote ?? i.ticker?.volume_quote ?? null;
    return (
      <div key={i.key} className={"inst-row" + (p.settings.inst === i.key ? " active" : "")} onClick={() => p.openInst(i.key)} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter") p.openInst(i.key); }}>
        <button className={"star" + (fav.has(i.key) ? " on" : "")} title="Избранное" onClick={(e) => { e.stopPropagation(); toggleFav(i.key); }}>★</button>
        <span className="sym">{i.symbol}<span className={"dot s-" + (st?.status || i.status)} title={st?.status || i.status} /></span>
        <span className="num">{fmtPrice(last, i.tickSize)}</span>
        <span className={"num " + (ch === null ? "" : ch >= 0 ? "up" : "dn")}>{ch === null ? "—" : `${ch >= 0 ? "+" : ""}${fmtNum(ch, 2)}%`}</span>
        <span className="num muted" title={`Объём за 24 ч, ${i.quote}`}>{vol === null ? "—" : `${fmtCompact(vol)} ${i.quote}`}</span>
      </div>
    );
  };

  return (
    <aside className="right">
      <div className="seg">
        <button aria-pressed={tab === "inst"} onClick={() => setTab("inst")}>Инструменты</button>
        <button aria-pressed={tab === "ind"} onClick={() => setTab("ind")}>Индикаторы</button>
      </div>
      {tab === "inst" ? (
        <div className="inst-list">
          <input id="inst-search" className="search" placeholder="Найти инструмент" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="inst-head"><span /><span>Символ</span><span>Цена</span><span>24 ч</span><span>Объём 24 ч</span></div>
          {favList.length > 0 && <><div className="group">Избранное</div>{favList.map(row)}</>}
          {groups.map(([g, items]) => (<div key={g}><div className="group">{g}</div>{items.map(row)}</div>))}
          {!p.instruments.length && <div className="empty">Коллектор запускается…</div>}
          <p className="hint">Инструменты задаются в <code>flowlens.config.json</code>: для каждого коллектор держит локальный стакан и копит историю.</p>
        </div>
      ) : (
        <IndicatorManager settings={p.settings} update={p.update} isPerp={p.isPerp} />
      )}
    </aside>
  );
}

function IndicatorManager({ settings, update, isPerp }: { settings: Settings; update: (p: Partial<Settings>) => void; isPerp: boolean }) {
  const panels = settings.panels;
  const set = (i: number, patch: Partial<PanelCfg>) => update({ panels: panels.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const move = (i: number, dir: number) => {
    const j = i + dir;
    if (j < 0 || j >= panels.length) return;
    const arr = [...panels];
    [arr[i], arr[j]] = [arr[j], arr[i]];
    update({ panels: arr });
  };
  const [open, setOpen] = useState<string | null>(null);
  const prm = settings.params;
  const setP = (k: string, v: unknown) => update({ params: { ...prm, [k]: v } });
  return (
    <div className="ind-list">
      <div className="group">На графике</div>
      <label className="ind-row"><input type="checkbox" checked={settings.profile} onChange={(e) => update({ profile: e.target.checked })} />
        <span>Профиль объёма<small>реальные исполнения по цене, POC и Value Area 70 %</small></span></label>
      <label className="ind-row"><input type="checkbox" checked={settings.bookLayer !== "off"} onChange={(e) => update({ bookLayer: e.target.checked ? "bands" : "off" })} />
        <span>Слой стакана<small>полосы дисбаланса или карта глубины под ценой</small></span></label>
      <div className="group">Панели (порядок, настройки, удаление)</div>
      {panels.map((pc, i) => {
        const info = PANEL_INFO[pc.kind];
        const na = info.perp && !isPerp;
        return (
          <div key={pc.id} className="ind-item">
            <div className="ind-row">
              <input type="checkbox" checked={pc.enabled} onChange={(e) => set(i, { enabled: e.target.checked })} aria-label={info.title} />
              <span className={na ? "muted" : ""}>{info.title}<small>{na ? "неприменимо на споте" : info.desc}</small></span>
              <div className="ind-btns">
                <button title="Выше" onClick={() => move(i, -1)}>↑</button>
                <button title="Ниже" onClick={() => move(i, 1)}>↓</button>
                <button title="Настройки" aria-pressed={open === pc.id} onClick={() => setOpen(open === pc.id ? null : pc.id)}>⚙</button>
                <button title="Удалить с экрана" onClick={() => set(i, { enabled: false })}>×</button>
              </div>
            </div>
            {open === pc.id && (
              <div className="ind-opts">
                {pc.kind === "dominance" && (<label>Формула<select value={prm.dom_mode} onChange={(e) => setP("dom_mode", e.target.value)}>
                  <option value="imbalance">Imbalance = 100·(B−A)/(B+A), −100…100</option><option value="bid_share">Bid Share = 100·B/(B+A), 0…100</option></select></label>)}
                {pc.kind === "dynamics" && (<>
                  <label>Δt, свечей<input type="number" min={1} max={100} value={prm.dyn_lag} onChange={(e) => setP("dyn_lag", Number(e.target.value))} /></label>
                  <label>EMA, период (0 — выкл.)<input type="number" min={0} max={200} value={prm.dyn_ema} onChange={(e) => setP("dyn_ema", Number(e.target.value))} /></label></>)}
                {pc.kind === "anomaly" && (<>
                  <label>Окно, завершённых свечей<input type="number" min={10} max={1000} value={prm.anom_window} onChange={(e) => setP("anom_window", Number(e.target.value))} /></label>
                  <label>Порог, ×<input type="number" step={0.5} min={1} max={20} value={prm.anom_threshold} onChange={(e) => setP("anom_threshold", Number(e.target.value))} /></label></>)}
                {pc.kind === "cvd" && (<>
                  <label>Сброс<select value={prm.cvd_reset} onChange={(e) => setP("cvd_reset", e.target.value)}>
                    <option value="utc_day">UTC-день</option><option value="session">сессия</option><option value="history">начало загруженной истории</option></select></label>
                  {prm.cvd_reset === "session" && <label>Начало сессии, мин после 00:00 UTC<input type="number" min={0} max={1439} value={prm.cvd_session_start} onChange={(e) => setP("cvd_session_start", Number(e.target.value))} /></label>}</>)}
                {pc.kind === "rsi" && <label>Период<input type="number" min={2} max={100} value={prm.rsi_period} onChange={(e) => setP("rsi_period", Number(e.target.value))} /></label>}
                {pc.kind === "macd" && (<>
                  <label>Быстрая EMA<input type="number" min={2} value={prm.macd_fast} onChange={(e) => setP("macd_fast", Number(e.target.value))} /></label>
                  <label>Медленная EMA<input type="number" min={3} value={prm.macd_slow} onChange={(e) => setP("macd_slow", Number(e.target.value))} /></label>
                  <label>Сигнальная<input type="number" min={2} value={prm.macd_signal} onChange={(e) => setP("macd_signal", Number(e.target.value))} /></label></>)}
                {pc.kind === "obdelta" && (<>
                  <label>Глубина<select value={pc.opts.depth || "0.01"} onChange={(e) => set(i, { opts: { ...pc.opts, depth: e.target.value } })}>
                    <option value="0.0025">0.25 %</option><option value="0.005">0.5 %</option><option value="0.01">1 %</option>
                    <option value="0.03">3 % (если покрыто)</option><option value="0.05">5 % (если покрыто)</option><option value="0.1">10 % (если покрыто)</option></select></label>
                  <label>Вид<select value={pc.opts.style || "lines"} onChange={(e) => set(i, { opts: { ...pc.opts, style: e.target.value } })}>
                    <option value="lines">линии B, A, B−A</option><option value="hist">гистограмма B−A</option></select></label></>)}
                {pc.kind === "vdelta" && (<>
                  <label>Метрика<select value={pc.opts.metric || "delta"} onChange={(e) => set(i, { opts: { ...pc.opts, metric: e.target.value } })}>
                    <option value="volume">Объём</option><option value="delta">Дельта объёма</option><option value="buysell">Покупки/продажи</option>
                    <option value="trades">Сделки</option><option value="tdelta">Дельта сделок</option></select></label>
                  <label className="check"><input type="checkbox" checked={!!pc.opts.cumulative} onChange={(e) => set(i, { opts: { ...pc.opts, cumulative: e.target.checked } })} />Накопительно (CVD)</label></>)}
                {["oi", "funding", "liq", "vdelta", "obdelta", "rsi", "macd", "cvd", "anomaly", "dominance", "dynamics"].includes(pc.kind) && (
                  <label>Высота, px<input type="number" min={60} max={600} value={pc.height} onChange={(e) => set(i, { height: Number(e.target.value) })} /></label>)}
              </div>
            )}
          </div>
        );
      })}
      <div className="group">Недоступно</div>
      {UNAVAILABLE.map((u) => (
        <div key={u.name} className="ind-row disabled" title="Формула закрытого модуля неизвестна">
          <input type="checkbox" disabled aria-label={u.name} />
          <span>{u.name}<small>{u.desc} — нужна спецификация алгоритма</small></span>
        </div>
      ))}
    </div>
  );
}
