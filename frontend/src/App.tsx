import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HeatState, MainChart, buildHeatCanvas, fpLabel, paintHeat } from "./chart/MainChart";
import { Minimap } from "./chart/Minimap";
import { PanelCanvas, PanelModel, buildPanel, legendValues } from "./chart/Panel";
import { HoverBus, TimeScale } from "./chart/engine";
import { Alerts, Screener, SettingsDrawer } from "./Pages";
import { RightColumn } from "./Side";
import { BASES, ChartData, EXCHANGE_LABEL, Footprint, Instrument, Live, LiveMsg, MARKET_LABEL, Profile, RANGES, TF_MS,
  ago, api, dateTime, fmtCompact, fmtNum, fmtPrice, fmtSigned } from "./lib";
import { PANEL_INFO, Settings, TABS, loadLocal, loadRemote, merge, save } from "./state";

type View = "chart" | "screener" | "alerts";

const STATUS_TEXT: Record<string, string> = {
  LIVE: "LIVE", CONNECTING: "CONNECTING", STALE: "STALE", RESYNC: "RESYNC", DEMO: "DEMO", ERROR: "ERROR",
};

export default function App() {
  const [settings, setSettings] = useState<Settings>(() => loadLocal());
  const update = useCallback((patch: Partial<Settings>) => setSettings((s) => { const n = { ...s, ...patch }; save(n); return n; }), []);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [statuses, setStatuses] = useState<Record<string, any>>({});
  const [view, setView] = useState<View>(() => (location.hash === "#screener" ? "screener" : location.hash === "#alerts" ? "alerts" : "chart"));
  const [data, setData] = useState<ChartData | null>(null);
  const dataRef = useRef<ChartData | null>(null);
  const [version, setVersion] = useState(0);
  const [heat, setHeat] = useState<HeatState | null>(null);
  const [fp, setFp] = useState<Footprint | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [live, setLive] = useState<LiveMsg | null>(null);
  const [uiConn, setUiConn] = useState(false);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [yManual, setYManual] = useState<{ lo: number; hi: number } | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [mobileRight, setMobileRight] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [now, setNow] = useState(Date.now());
  const scale = useMemo(() => new TimeScale(), []);
  const hover = useMemo(() => new HoverBus(), []);
  const liveConn = useMemo(() => new Live(), []);
  const tipRef = useRef<HTMLDivElement>(null);
  const loadSeq = useRef(0);

  // ---- bootstrap -------------------------------------------------------------
  useEffect(() => {
    Promise.all([loadRemote(), api.meta().catch(() => null)]).then(([r, meta]) => {
      if (r) setSettings((s) => merge({ ...r, inst: s.inst || r.inst }));
      else if (meta?.demo) {
        // DEMO starts with a few hours of synthetic history: use a 4 h statistics base
        setSettings((s) => ({ ...s, params: { ...s.params, z_base: "4h" } }));
      }
    });
    const loadInst = () => api.instruments().then((d) => setInstruments(d.instruments)).catch((e) => setErr(`Сервер FlowLens недоступен: ${e.message}`));
    loadInst();
    const id = window.setInterval(loadInst, 10000);
    const id2 = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { window.clearInterval(id); window.clearInterval(id2); };
  }, []);
  useEffect(() => { const h = () => setView(location.hash === "#screener" ? "screener" : location.hash === "#alerts" ? "alerts" : "chart"); window.addEventListener("hashchange", h); return () => window.removeEventListener("hashchange", h); }, []);
  const go = (v: View) => { location.hash = v === "chart" ? "" : v; setView(v); };

  const inst = instruments.find((i) => i.key === settings.inst) || instruments.find((i) => i.ready) || null;
  useEffect(() => {
    if (inst && inst.key !== settings.inst) update({ inst: inst.key, openTabs: settings.openTabs.includes(inst.key) ? settings.openTabs : [...settings.openTabs, inst.key] });
  }, [inst?.key]);
  const openInst = (key: string) => {
    update({ inst: key, openTabs: settings.openTabs.includes(key) ? settings.openTabs : [...settings.openTabs, key] });
    setYManual(null); setMobileRight(false); go("chart");
  };
  const closeTab = (key: string) => {
    const tabs = settings.openTabs.filter((k) => k !== key);
    update({ openTabs: tabs, inst: key === settings.inst ? tabs[0] || settings.inst : settings.inst });
  };

  // ---- chart data ------------------------------------------------------------------
  const tf = settings.tf;
  const paramsKey = JSON.stringify(settings.params);
  const load = useCallback(async (quiet = false) => {
    if (!inst || !inst.ready) return;
    const seq = ++loadSeq.current;
    if (!quiet) setLoading(true);
    try {
      const from = Date.now() - rangeMs(settings);
      const d = await api.chart(inst.key, tf, from, settings.params);
      if (seq !== loadSeq.current) return;
      const fresh = !dataRef.current || dataRef.current.inst !== d.inst || dataRef.current.tf !== d.tf;
      dataRef.current = d;
      setData(d);
      setErr("");
      scale.tf = TF_MS[tf];
      if (fresh) {
        // fit the zoom to the history that actually exists
        const first = d.candles.c.findIndex((v) => v !== null);
        const n = first < 0 ? 0 : d.t.length - first;
        const fit = scale.plotW / (n + scale.rightPad + 3);
        scale.barW = n > 0 ? Math.max(2, Math.min(18, fit < 7 ? 7 : fit)) : 7;
        scale.follow = true;
      }
      scale.setData(d.t[0] ?? Date.now(), d.t[d.t.length - 1] ?? Date.now());
      setVersion((v) => v + 1);
    } catch (e: any) {
      if (seq === loadSeq.current) setErr(`Не удалось загрузить график: ${e.message}`);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, [inst?.key, inst?.ready, tf, settings.range, settings.customDays, paramsKey]);

  useEffect(() => { scale.setTf(TF_MS[tf]); setYManual(null); }, [tf, inst?.key]);
  useEffect(() => { dataRef.current = null; setData(null); setFp(null); setProfile(null); setHeat(null); load(); }, [load]);
  useEffect(() => { if (inst) liveConn.subscribe(inst.key, tf); }, [inst?.key, tf]);

  const loadHeat = useCallback(async () => {
    if (!inst || settings.bookLayer !== "heatmap") return;
    try {
      const from = Date.now() - rangeMs(settings);
      const m = await api.heatmap(inst.key, tf, from, settings.heatAgg);
      setHeat((prev) => buildHeatCanvas(m, settings, prev));
    } catch { setHeat(null); }
  }, [inst?.key, tf, settings.range, settings.customDays, settings.bookLayer, settings.heatAgg]);
  useEffect(() => { loadHeat(); }, [loadHeat]);
  useEffect(() => { setHeat((h) => (h ? paintHeat(h, settings) : h)); }, [settings.heatUnits, settings.heatScale, settings.heatMin, settings.heatMax]);

  // footprint and profile follow the visible range (debounced)
  const fpTimer = useRef<number>();
  const loadVisible = useCallback(() => {
    window.clearTimeout(fpTimer.current);
    fpTimer.current = window.setTimeout(async () => {
      if (!inst) return;
      const [a, b] = scale.visible();
      if (settings.chartMode === "footprint") {
        try { setFp(await api.footprint(inst.key, tf, Math.floor(a), Math.ceil(b) + TF_MS[tf], settings.fpStep || undefined)); } catch { setFp(null); }
      }
      if (settings.profile) {
        try { setProfile(await api.profile(inst.key, Math.floor(a), Math.ceil(b) + TF_MS[tf], settings.fpStep || undefined)); } catch { setProfile(null); }
      }
    }, 350);
  }, [inst?.key, tf, settings.chartMode, settings.profile, settings.fpStep]);
  useEffect(() => { loadVisible(); return scale.on(loadVisible); }, [loadVisible]);

  // ---- live stream -------------------------------------------------------------------
  const reloadTimer = useRef<number>();
  useEffect(() => { const off = liveConn.on((m: any) => {
    if (m.type === "ui_conn") { setUiConn(m.connected); return; }
    if (m.type === "statuses") { setStatuses(m.items); return; }
    if (m.type === "bar_closed") {
      window.clearTimeout(reloadTimer.current);
      reloadTimer.current = window.setTimeout(() => { load(true); loadHeat(); loadVisible(); }, 400);
      return;
    }
    if (m.type !== "live") return;
    const msg = m as LiveMsg;
    const d = dataRef.current;
    if (!d || msg.inst !== d.inst || msg.tf !== d.tf) return;
    setLive(msg);
    const c = msg.candle;
    if (c) {
      const n = d.t.length;
      if (n && c.t > d.t[n - 1]) {           // a new slot started: reload to get aligned series
        window.clearTimeout(reloadTimer.current);
        reloadTimer.current = window.setTimeout(() => load(true), 200);
        return;
      }
      const i = n - 1;
      if (i >= 0 && d.t[i] === c.t) {
        const cd = d.candles;
        cd.o[i] = c.o; cd.h[i] = c.h; cd.l[i] = c.l; cd.c[i] = c.c;
        cd.q_buy[i] = c.q_buy; cd.q_sell[i] = c.q_sell; cd.v_buy[i] = c.v_buy; cd.v_sell[i] = c.v_sell;
        const quote = d.indicators.units.trades === "quote";
        const b = quote ? c.q_buy : c.v_buy, s = quote ? c.q_sell : c.v_sell;
        const f = d.indicators.flow;
        f.buy[i] = b; f.sell[i] = s; f.delta[i] = b - s; f.total[i] = b + s;
        if (c.n_buy !== null && c.n_sell !== null) { f.trades_buy[i] = c.n_buy; f.trades_sell[i] = c.n_sell; f.trades_delta[i] = c.n_buy - c.n_sell; }
        d.live_index = i;
        scale.lastT = d.t[i];
        setVersion((v) => v + 1);
      }
    }
  }); return () => { off(); }; }, [load, loadHeat, loadVisible]);

  useEffect(() => { if (!drawer) return; const id = window.setInterval(() => api.health().then(setHealth).catch(() => undefined), 3000); api.health().then(setHealth).catch(() => undefined); return () => window.clearInterval(id); }, [drawer]);

  // ---- panels ----------------------------------------------------------------------
  const tab = TABS.find((t) => t.id === settings.tab) || TABS[0];
  const shownPanels = settings.panels.filter((p) => (tab.kinds ? tab.kinds.includes(p.kind) : p.enabled));
  const models = useMemo(() => {
    const out: Record<string, PanelModel | null> = {};
    if (!data) return out;
    for (const p of shownPanels) out[p.id] = buildPanel(p.kind, p.opts, data, settings, live);
    return out;
  }, [data, version, live, settings.panels, settings.tab]);

  // ---- tooltip -------------------------------------------------------------------------
  const onHover = useCallback((cx: number, cy: number, idx: number | null) => {
    setHoverIdx(idx);
    const el = tipRef.current;
    const d = dataRef.current;
    if (!el) return;
    if (idx === null || !d || (d.candles.c[idx] === null && !d.depth.samples[idx])) { el.hidden = true; return; }
    el.innerHTML = tooltipHtml(d, idx, inst);
    el.hidden = false;
    const w = el.offsetWidth, h = el.offsetHeight;
    let x = cx + 16, y = cy + 16;
    if (x + w > window.innerWidth - 8) x = cx - w - 16;
    if (y + h > window.innerHeight - 8) y = Math.max(8, cy - h - 16);
    el.style.left = `${x}px`; el.style.top = `${y}px`;
  }, [inst?.key]);

  // ---- resizing -----------------------------------------------------------------------------
  const areaRef = useRef<HTMLDivElement>(null);
  const startSplit = (e: React.PointerEvent) => {
    const area = areaRef.current!.getBoundingClientRect();
    const move = (ev: PointerEvent) => update({ mainFrac: Math.min(0.88, Math.max(0.25, (ev.clientY - area.top) / area.height)) });
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    e.preventDefault();
  };
  const startPanelResize = (e: React.PointerEvent, id: string) => {
    const p0 = settings.panels.find((p) => p.id === id)!;
    const y0 = e.clientY, h0 = p0.height;
    const move = (ev: PointerEvent) => setSettings((s) => ({ ...s, panels: s.panels.map((p) => (p.id === id ? { ...p, height: Math.max(56, Math.min(600, h0 + ev.clientY - y0)) } : p)) }));
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); setSettings((s) => { save(s); return s; }); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    e.preventDefault();
  };

  const st = live?.status;
  const status = inst ? (st?.status || statuses[inst.key]?.status || inst.status) : "CONNECTING";
  const isPerp = inst?.market === "perp";
  const fpStepOptions = inst?.symbol.startsWith("BTC") ? [10, 20, 50] : inst ? stepChoices(inst, data) : [];

  return (
    <div className="app">
      <header className="top">
        <div className="brand">FlowLens</div>
        <nav className="views" aria-label="Разделы">
          <button aria-pressed={view === "chart"} onClick={() => go("chart")}>График</button>
          <button aria-pressed={view === "screener"} onClick={() => go("screener")}>Скринер</button>
          <button aria-pressed={view === "alerts"} onClick={() => go("alerts")}>Алерты</button>
        </nav>
        <div className="tabs" aria-label="Открытые графики">
          {settings.openTabs.filter((k) => instruments.some((i) => i.key === k)).map((k) => {
            const i = instruments.find((x) => x.key === k)!;
            return (
              <span key={k} className={"tab" + (k === settings.inst ? " active" : "")}>
                <button onClick={() => openInst(k)}>{i.symbol}<small>{EXCHANGE_LABEL[i.exchange]} {i.market === "perp" ? "perp" : "spot"}</small></button>
                {settings.openTabs.length > 1 && <button className="x" aria-label="Закрыть вкладку" onClick={() => closeTab(k)}>×</button>}
              </span>
            );
          })}
        </div>
        <select id="inst-select" className="inst-select" value={inst?.key || ""} onChange={(e) => openInst(e.target.value)} aria-label="Инструмент">
          {instruments.map((i) => <option key={i.key} value={i.key}>{EXCHANGE_LABEL[i.exchange]} · {MARKET_LABEL[i.market]} · {i.symbol}</option>)}
        </select>
        <div className="status" title={st ? `стакан: ${st.book_status}, эпоха #${st.book_epoch}` : ""}>
          <span className={"badge s-" + status}>{STATUS_TEXT[status] || status}</span>
          {inst?.demo && <span className="demo-note">синтетические данные</span>}
          <span className="muted small">{st?.last_event ? ago(st.last_event, now) : "—"}</span>
          <span className="muted small" title="Задержка: время получения − время биржи (медиана)">
            {st?.latency_ms?.book !== null && st?.latency_ms?.book !== undefined ? `стакан ${Math.round(st.latency_ms.book)} мс` : ""}
            {st?.latency_ms?.trades !== null && st?.latency_ms?.trades !== undefined ? ` · сделки ${Math.round(st.latency_ms.trades)} мс` : ""}
          </span>
          {!uiConn && <span className="badge s-STALE" title="Нет WebSocket-соединения с сервером FlowLens">UI offline</span>}
        </div>
        <button className="icon mobile-only" onClick={() => setMobileRight(true)} aria-label="Инструменты и индикаторы">☰</button>
      </header>

      {view === "chart" && (
        <div className="mobile-bar mobile-only-flex">
          <select value={tf} onChange={(e) => update({ tf: e.target.value })} aria-label="Таймфрейм">{Object.keys(TF_MS).map((t) => <option key={t}>{t}</option>)}</select>
          <select value={settings.bookLayer} onChange={(e) => update({ bookLayer: e.target.value as Settings["bookLayer"] })} aria-label="Слой стакана (мобильный)">
            <option value="bands">Полосы</option><option value="heatmap">Карта глубины</option><option value="off">Без стакана</option>
          </select>
          <button aria-pressed={toolsOpen} onClick={() => setToolsOpen(!toolsOpen)}>Параметры графика</button>
          <button onClick={() => { scale.goLive(); setYManual(null); }}>К цене</button>
        </div>
      )}
      {view === "chart" && (
        <div className={"toolbar" + (toolsOpen ? " open" : "")}>
          <div className="seg" role="group" aria-label="Таймфрейм">
            {Object.keys(TF_MS).map((t) => <button key={t} aria-pressed={tf === t} onClick={() => update({ tf: t })}>{t}</button>)}
          </div>
          <div className="seg" role="group" aria-label="Режим цены">
            {(["candles", "line", "footprint"] as const).map((m) => <button key={m} aria-pressed={settings.chartMode === m} onClick={() => update({ chartMode: m })}>{{ candles: "Свечи", line: "Линия", footprint: "Футпринт" }[m]}</button>)}
          </div>
          <button aria-pressed={settings.profile} onClick={() => update({ profile: !settings.profile })}>Профиль</button>
          {settings.chartMode === "footprint" && (
            <select value={settings.fpMetric} onChange={(e) => update({ fpMetric: e.target.value as Settings["fpMetric"] })} aria-label="Метрика футпринта">
              {["volume", "delta", "buysell", "trades", "tdelta"].map((m) => <option key={m} value={m}>{fpLabel(m)}</option>)}
            </select>
          )}
          {(settings.chartMode === "footprint" || settings.profile) && (
            <label className="inline">Детализация
              <select value={settings.fpStep ?? ""} onChange={(e) => update({ fpStep: e.target.value ? Number(e.target.value) : null })} aria-label="Шаг цены">
                <option value="">{inst?.steps.footprint ? `база ${inst.steps.footprint}` : "база"}</option>
                {fpStepOptions.map((v) => <option key={v} value={v}>{v} {inst?.quote}</option>)}
              </select>
              <input className="step-input" type="number" min={0} placeholder="свой" title="Свой шаг: округляется до кратного базового шага хранения (кратного tick size)"
                onKeyDown={(e) => { if (e.key === "Enter") update({ fpStep: Number((e.target as HTMLInputElement).value) || null }); }} />
            </label>
          )}
          <select value={settings.bookLayer} onChange={(e) => update({ bookLayer: e.target.value as Settings["bookLayer"] })} aria-label="Слой стакана">
            <option value="bands">Полосы дисбаланса</option><option value="heatmap">Карта глубины</option><option value="off">Без слоя стакана</option>
          </select>
          <label className="inline">Диапазон<select value={settings.range} onChange={(e) => update({ range: e.target.value })}>
            {Object.keys(RANGES).map((r) => <option key={r} value={r}>{r}</option>)}<option value="custom">свой</option></select></label>
          {settings.range === "custom" && (
            <label className="inline">дней<input className="step-input" type="number" min={0.1} max={14} step={0.5} value={settings.customDays}
              onChange={(e) => update({ customDays: Math.max(0.1, Math.min(14, Number(e.target.value) || 1)) })} /></label>
          )}
          <label className="inline">База<select value={settings.params.z_base} onChange={(e) => update({ params: { ...settings.params, z_base: e.target.value } })}>
            {BASES.map((b) => <option key={b}>{b}</option>)}</select></label>
          <span className="spacer" />
          <button onClick={() => { update({ rightOpen: true }); setMobileRight(true); }}>Индикаторы</button>
          <button onClick={() => setDrawer(true)}>Настройки</button>
          <button onClick={() => { scale.goLive(); setYManual(null); }} title="Вернуться к последней свече и автомасштабу">К текущей цене</button>
          <button onClick={() => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.())?.catch?.(() => undefined)} title="Полноэкранный режим">⛶</button>
          <button className="icon desktop-only" onClick={() => update({ rightOpen: !settings.rightOpen })} aria-label="Правая колонка">{settings.rightOpen ? "⟩" : "⟨"}</button>
        </div>
      )}

      {err && <div className="banner warn">{err}</div>}
      {instruments.length > 0 && !instruments.some((i) => i.ready) && (
        <div className="banner warn">
          Коллектор пока не может получить данные бирж и повторяет попытки. Проверьте доступ к интернету и к Binance/Bybit
          (в некоторых странах они закрыты) или запустите DEMO: <code>run.sh --demo</code>.
          {instruments.filter((i) => i.last_error).slice(0, 3).map((i) => <div key={i.key} className="small">{i.key}: {i.last_error}</div>)}
        </div>
      )}
      {data && data.source.demo && view === "chart" && <div className="banner demo">DEMO: синтетический рынок для проверки интерфейса. Это не данные биржи.</div>}

      <div className="body">
        <main className="main" ref={areaRef}>
          {view === "screener" && <Screener settings={settings} update={update} open={openInst} />}
          {view === "alerts" && <Alerts instruments={instruments} />}
          {view === "chart" && (
            <>
              <div className="main-chart" style={{ flexBasis: `${settings.mainFrac * 100}%` }}>
                <div className="chart-head">
                  <b>{inst?.symbol}</b> <span className="muted">{inst ? `${EXCHANGE_LABEL[inst.exchange]} ${MARKET_LABEL[inst.market]}` : ""} · {tf}</span>
                  {data && hoverIdx === null && <LastLine d={data} tick={inst?.tickSize} />}
                  {data && <Availability d={data} />}
                  {loading && <span className="muted small">загрузка…</span>}
                  {!scale.follow && <span className="muted small">история · автопрокрутка выкл.</span>}
                </div>
                <MainChart scale={scale} hover={hover} data={data} version={version} heat={heat} fp={fp} profile={profile} live={live}
                  settings={settings} tick={inst?.tickSize || "0.01"} onHover={onHover} yManual={yManual} setYManual={setYManual} />
                <Minimap scale={scale} times={data?.t || []} closes={data?.candles.c || []} version={version} />
              </div>
              <div className="splitter" onPointerDown={startSplit} role="separator" aria-label="Изменить высоту панелей" />
              <div className="panels">
                <div className="panel-tabs" role="tablist">
                  {TABS.map((t) => <button key={t.id} role="tab" aria-selected={settings.tab === t.id} onClick={() => update({ tab: t.id })}>{t.title}</button>)}
                </div>
                <div className="panel-scroll">
                  {shownPanels.map((p) => {
                    const info = PANEL_INFO[p.kind];
                    const m = models[p.id] || null;
                    const isCol = collapsed.has(p.id);
                    const lv = legendValues(m, hoverIdx ?? (data ? data.t.length - 1 : null));
                    return (
                      <section key={p.id} className="panel" style={{ height: isCol ? 26 : p.height + 26 }}>
                        <div className="panel-head" onClick={() => { const n = new Set(collapsed); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); setCollapsed(n); }}>
                          <span className="ptitle">{info.title}</span>
                          {m && <span className="punit muted">{m.unit}</span>}
                          {lv.map((x) => <span key={x.label} className="legend"><i style={{ background: x.color }} />{x.label} <b>{x.value}</b></span>)}
                          {p.kind === "vdelta" && (
                            <span className="popts" onClick={(e) => e.stopPropagation()}>
                              <select value={p.opts.metric || "delta"} onChange={(e) => update({ panels: settings.panels.map((q) => (q.id === p.id ? { ...q, opts: { ...q.opts, metric: e.target.value } } : q)) })} aria-label="Метрика">
                                <option value="volume">Объём</option><option value="delta">Дельта объёма</option><option value="buysell">Покупки/продажи</option><option value="trades">Сделки</option><option value="tdelta">Дельта сделок</option>
                              </select>
                              <label className="check"><input type="checkbox" checked={!!p.opts.cumulative} onChange={(e) => update({ panels: settings.panels.map((q) => (q.id === p.id ? { ...q, opts: { ...q.opts, cumulative: e.target.checked } } : q)) })} />Накопительно</label>
                            </span>
                          )}
                          {p.kind === "obdelta" && (
                            <span className="popts" onClick={(e) => e.stopPropagation()}>
                              <select value={p.opts.depth || "0.01"} onChange={(e) => update({ panels: settings.panels.map((q) => (q.id === p.id ? { ...q, opts: { ...q.opts, depth: e.target.value } } : q)) })} aria-label="Глубина">
                                <option value="0.0025">0.25 %</option><option value="0.005">0.5 %</option><option value="0.01">1 %</option><option value="0.03">3 %</option><option value="0.05">5 %</option><option value="0.1">10 %</option>
                              </select>
                            </span>
                          )}
                          <span className="pcollapse">{isCol ? "▸" : "▾"}</span>
                        </div>
                        {!isCol && <div className="panel-body" style={{ height: p.height }}>
                          <PanelCanvas cfg={p} scale={scale} hover={hover} model={m} times={data?.t || []} version={version} liveIndex={data?.live_index ?? null} />
                        </div>}
                        {!isCol && <div className="panel-resize" onPointerDown={(e) => startPanelResize(e, p.id)} role="separator" aria-label="Изменить высоту панели" />}
                      </section>
                    );
                  })}
                  {!shownPanels.length && <div className="empty">Во вкладке «Все панели» нет включённых индикаторов — включите их в правой колонке.</div>}
                </div>
              </div>
            </>
          )}
        </main>
        {(settings.rightOpen || mobileRight) && (
          <div className={"right-wrap" + (mobileRight ? " mobile-open" : "")}>
            {mobileRight && <button className="drawer-close mobile-only" onClick={() => setMobileRight(false)} aria-label="Закрыть">×</button>}
            <RightColumn instruments={instruments} statuses={statuses} settings={settings} update={update} openInst={openInst} isPerp={isPerp} />
          </div>
        )}
      </div>
      {drawer && <SettingsDrawer settings={settings} update={update} close={() => setDrawer(false)} health={health} />}
      <div ref={tipRef} className="tooltip" hidden />
    </div>
  );
}

function rangeMs(s: Settings): number {
  return s.range === "custom" ? s.customDays * 86_400_000 : RANGES[s.range] ?? RANGES["1d"];
}

function stepChoices(inst: Instrument, d: ChartData | null): number[] {
  const base = Number(inst.steps.footprint || 0);
  if (!base) return [];
  return [2, 4, 10].map((k) => +(base * k).toPrecision(6));
}

function LastLine({ d, tick }: { d: ChartData; tick?: string }) {
  const i = d.t.length - 1;
  const c = d.candles;
  if (i < 0 || c.c[i] === null) return null;
  const f = d.indicators.flow;
  return (
    <span className="ohlc">
      <span>O {fmtPrice(c.o[i], tick)}</span><span>H {fmtPrice(c.h[i], tick)}</span><span>L {fmtPrice(c.l[i], tick)}</span><span>C {fmtPrice(c.c[i], tick)}</span>
      <span className={(f.delta[i] ?? 0) >= 0 ? "up" : "dn"}>Δ {fmtSigned(f.delta[i])}</span>
    </span>
  );
}

function Availability({ d }: { d: ChartData }) {
  const bf = d.availability.book_from as number | null;
  if (!bf) return <span className="avail">Стакан: данных ещё нет — история собирается с момента запуска</span>;
  if (bf > d.from) return <span className="avail">Стакан: данные доступны с {dateTime(bf)}</span>;
  return null;
}

function tooltipHtml(d: ChartData, i: number, inst: Instrument | null): string {
  const c = d.candles, I = d.indicators, dep = d.depth;
  const tick = inst?.tickSize;
  const q = I.units.trades === "quote" ? d.units.quote : d.units.base;
  const bu = I.units.book === "quote" ? d.units.quote : d.units.base;
  const rows: string[] = [];
  const src = c.source[i]?.join("+") || "нет данных";
  const live = d.live_index === i;
  rows.push(`<b>${dateTime(d.t[i])}</b> · ${d.tf} · ${live ? "формируется" : c.complete[i] ? "полная" : "неполная"} · ${src}`);
  if (c.o[i] !== null) rows.push(`O ${fmtPrice(c.o[i], tick)}  H ${fmtPrice(c.h[i], tick)}  L ${fmtPrice(c.l[i], tick)}  C ${fmtPrice(c.c[i], tick)}`);
  const f = I.flow;
  rows.push(`Объём ${fmtCompact(f.total[i])} ${q} · покупки ${fmtCompact(f.buy[i])} · продажи ${fmtCompact(f.sell[i])} · Δ <span class="${(f.delta[i] ?? 0) >= 0 ? "up" : "dn"}">${fmtSigned(f.delta[i])}</span>`);
  rows.push(`Сделки ${f.trades_total[i] ?? "—"} · Δ сделок ${f.trades_delta[i] === null ? "— (нет разбивки)" : fmtSigned(f.trades_delta[i], 0)}`);
  const ob = I.ob_delta["0.01"];
  if (ob) rows.push(`Стакан 1%: B ${fmtCompact(ob.bid[i])} · A ${fmtCompact(ob.ask[i])} ${bu} · B−A ${fmtSigned(ob.delta[i])} · покрытие ${pctOrDash(ob.cov_bid[i])}/${pctOrDash(ob.cov_ask[i])}`);
  const L = I.dominance.lines;
  rows.push(`${I.dominance.mode === "imbalance" ? "Imbalance" : "Bid Share"}: 0.25% ${fmtNum(L["0.0025"][i], 1)} · 0.5% ${fmtNum(L["0.005"][i], 1)} · 1% ${fmtNum(L["0.01"][i], 1)}`);
  const dz = I.belts.items.map((b) => `${(b.lo * 100).toFixed(2)}–${(b.hi * 100).toFixed(2)}%: bid ${zTxt(b.bid.z[i], b.bid.status[i])} / ask ${zTxt(b.ask.z[i], b.ask.status[i])}`);
  rows.push(`z (база ${I.belts.base}): ${dz.join(" · ")}`);
  rows.push(`Качество стакана: время ${pctOrDash(dep.time_cov[i])}, спред ${fmtNum(dep.spread_bps[i], 2)} bps, задержка ${dep.lag_ms[i] === null ? "—" : Math.round(dep.lag_ms[i] as number) + " мс"}${dep.epoch_start[i] ? " · ресинхронизация" : ""}`);
  if (I.deriv) rows.push(`OI ${fmtCompact(I.deriv.oi[i])} · funding ${I.deriv.funding[i] === null ? "—" : fmtNum((I.deriv.funding[i] as number) * 100, 4) + "%"} · ликв. long ${fmtCompact(I.deriv.liq_long[i])} / short ${fmtCompact(I.deriv.liq_short[i])}`);
  return rows.map((r) => `<div>${r}</div>`).join("");
}

function pctOrDash(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
}

function zTxt(z: number | null, st: string): string {
  if (z !== null) return fmtNum(z, 1);
  return { warmup: "прогрев", zero_var: "σ=0", nodata: "—" }[st] || "—";
}
