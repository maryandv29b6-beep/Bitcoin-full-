import { useEffect, useMemo, useState } from "react";
import { AlertRule, BASES, Instrument, SNAPSHOT, ScreenerRow, api, dateTime, fmtCompact, fmtNum, fmtPrice, fmtSigned } from "./lib";
import { Settings } from "./state";

// ---- screener (extension) ------------------------------------------------------
type Col = { key: string; title: string; get: (r: ScreenerRow) => number | string | null; fmt?: (v: any, r: ScreenerRow) => string; title2?: string };

const band = (r: ScreenerRow, k: string, f: "bid" | "ask" | "imbalance") => (r.bands && r.bands[k] ? (r.bands[k][f] as number | null) : null);

const COLS: Col[] = [
  { key: "exchange", title: "Биржа", get: (r) => r.exchange },
  { key: "market", title: "Рынок", get: (r) => r.market },
  { key: "symbol", title: "Тикер", get: (r) => r.symbol },
  { key: "price", title: "Цена", get: (r) => r.price, fmt: (v) => fmtPrice(v) },
  { key: "chg", title: "24 ч, %", get: (r) => r.change_24h_pct, fmt: (v) => (v === null ? "—" : `${v >= 0 ? "+" : ""}${fmtNum(v, 2)}`) },
  { key: "vol", title: "Объём 24 ч", get: (r) => r.volume_24h_quote, fmt: (v, r) => (v === null ? "—" : `${fmtCompact(v)} ${r.volume_unit}`) },
  { key: "spread", title: "Спред, bps", get: (r) => r.spread_bps, fmt: (v) => fmtNum(v, 2) },
  { key: "b1", title: "B 1 %", get: (r) => band(r, "0.01", "bid"), fmt: (v) => fmtCompact(v) },
  { key: "a1", title: "A 1 %", get: (r) => band(r, "0.01", "ask"), fmt: (v) => fmtCompact(v) },
  { key: "i025", title: "Дисб. 0.25 %", get: (r) => band(r, "0.0025", "imbalance"), fmt: (v) => fmtNum(v, 1) },
  { key: "i05", title: "Дисб. 0.5 %", get: (r) => band(r, "0.005", "imbalance"), fmt: (v) => fmtNum(v, 1) },
  { key: "i1", title: "Дисб. 1 %", get: (r) => band(r, "0.01", "imbalance"), fmt: (v) => fmtNum(v, 1) },
  { key: "dyn", title: "Динамика 1 %, 5 мин", get: (r) => (r.dynamics_5m ? r.dynamics_5m["0.01"] : null), fmt: (v) => fmtNum(v, 1) },
  { key: "d5", title: "Δ сделок 5 мин", get: (r) => r.delta_5m ?? null, fmt: (v, r) => fmtSigned(v) + (r.delta_partial ? "*" : "") },
  { key: "cvd", title: "CVD UTC-день", get: (r) => r.cvd_day ?? null, fmt: (v, r) => fmtSigned(v) + (r.cvd_day_partial ? "*" : "") },
  { key: "zb", title: "z бидов 1 %", get: (r) => r.z_bid_1 ?? null, fmt: (v, r) => (v === null ? (r.z_bid_1_status === "warmup" ? "прогрев" : "—") : fmtNum(v, 2)) },
  { key: "za", title: "z асков 1 %", get: (r) => r.z_ask_1 ?? null, fmt: (v, r) => (v === null ? (r.z_ask_1_status === "warmup" ? "прогрев" : "—") : fmtNum(v, 2)) },
  { key: "oi", title: "OI", get: (r) => r.oi ?? null, fmt: (v, r) => (r.market !== "perp" ? "н/п" : `${fmtCompact(v)} ${r.base}`) },
  { key: "fund", title: "Funding, %", get: (r) => (r.funding === null || r.funding === undefined ? null : r.funding * 100), fmt: (v, r) => (r.market !== "perp" ? "н/п" : fmtNum(v, 4)) },
  { key: "fresh", title: "Свежесть, с", get: (r) => r.freshness_s, fmt: (v) => fmtNum(v, 1) },
  { key: "cov", title: "Покрытие 1 %", get: (r) => (r.coverage_1 ? Math.min(r.coverage_1[0], r.coverage_1[1]) : null), fmt: (v) => (v === null ? "—" : `${Math.round(v * 100)}%`) },
];

export function Screener({ settings, update, open }: { settings: Settings; update: (p: Partial<Settings>) => void; open: (k: string) => void }) {
  const [rows, setRows] = useState<ScreenerRow[]>([]);
  const [err, setErr] = useState("");
  const [sort, setSort] = useState<{ key: string; dir: number }>({ key: "vol", dir: -1 });
  const [f, setF] = useState({ minVol: 0, maxSpread: 0, imb: 0, z: 0, favOnly: false });
  useEffect(() => {
    let stop = false;
    const load = () => api.screener().then((d) => { if (!stop) { setRows(d.rows); setErr(""); } }).catch((e) => setErr(String(e.message || e)));
    load();
    const id = window.setInterval(load, 3000);
    return () => { stop = true; window.clearInterval(id); };
  }, []);
  const fav = new Set(settings.favorites);
  const shown = useMemo(() => {
    const col = COLS.find((c) => c.key === sort.key)!;
    return rows.filter((r) => {
      if (f.favOnly && !fav.has(r.key)) return false;
      if (f.minVol && !((r.volume_24h_quote ?? 0) >= f.minVol)) return false;
      if (f.maxSpread && !(r.spread_bps !== null && r.spread_bps <= f.maxSpread)) return false;
      const im = band(r, "0.01", "imbalance");
      if (f.imb && !(im !== null && Math.abs(im) > f.imb)) return false;
      if (f.z && !(Math.max(Math.abs(r.z_bid_1 ?? 0), Math.abs(r.z_ask_1 ?? 0)) > f.z)) return false;
      return true;
    }).sort((a, b) => {
      const va = col.get(a), vb = col.get(b);
      if (va === null) return 1;
      if (vb === null) return -1;
      return (va > vb ? 1 : va < vb ? -1 : 0) * sort.dir;
    });
  }, [rows, sort, f, settings.favorites]);
  return (
    <div className="page">
      <div className="page-head">
        <h2>Скринер</h2>
        <p className="muted">Расширение. Метрики по инструментам, которые собирает коллектор. Исследовательские фильтры, не обещание прибыльной сделки. «*» — данные за период неполные.</p>
      </div>
      <div className="filters">
        <label>Объём 24 ч от<input type="number" value={f.minVol || ""} placeholder="0" onChange={(e) => setF({ ...f, minVol: Number(e.target.value) })} /></label>
        <label>Спред до, bps<input type="number" value={f.maxSpread || ""} placeholder="—" onChange={(e) => setF({ ...f, maxSpread: Number(e.target.value) })} /></label>
        <label>|Дисб. 1 %| &gt;<input type="number" value={f.imb || ""} placeholder="40" onChange={(e) => setF({ ...f, imb: Number(e.target.value) })} /></label>
        <label>|z| &gt;<input type="number" value={f.z || ""} placeholder="3" onChange={(e) => setF({ ...f, z: Number(e.target.value) })} /></label>
        <label className="check"><input type="checkbox" checked={f.favOnly} onChange={(e) => setF({ ...f, favOnly: e.target.checked })} />только избранное</label>
      </div>
      {err && <div className="banner warn">Скринер недоступен: {err}</div>}
      <div className="table-wrap">
        <table className="grid-table">
          <thead><tr><th />{COLS.map((c) => (
            <th key={c.key} onClick={() => setSort({ key: c.key, dir: sort.key === c.key ? -sort.dir : -1 })} aria-sort={sort.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : "none"}>
              {c.title}{sort.key === c.key ? (sort.dir > 0 ? " ▲" : " ▼") : ""}</th>))}</tr></thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.key} onClick={() => open(r.key)} className={r.demo ? "demo" : ""}>
                <td><button className={"star" + (fav.has(r.key) ? " on" : "")} onClick={(e) => { e.stopPropagation(); update({ favorites: fav.has(r.key) ? settings.favorites.filter((x) => x !== r.key) : [...settings.favorites, r.key] }); }}>★</button></td>
                {COLS.map((c) => {
                  const v = c.get(r);
                  const txt = c.fmt ? c.fmt(v, r) : v === null ? "—" : String(v);
                  const cls = typeof v === "number" && ["chg", "i025", "i05", "i1", "dyn", "d5", "cvd"].includes(c.key) ? (v >= 0 ? "up" : "dn") : "";
                  return <td key={c.key} className={cls + (typeof v === "number" ? " num" : "")}>{c.key === "symbol" ? <b>{txt}{r.demo ? " · DEMO" : ""}</b> : txt}</td>;
                })}
              </tr>
            ))}
            {!shown.length && <tr><td colSpan={COLS.length + 1} className="empty">Нет инструментов под фильтры</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---- alerts (extension) ------------------------------------------------------------
export function Alerts({ instruments }: { instruments: Instrument[] }) {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [metrics, setMetrics] = useState<Record<string, string>>({});
  const [ops, setOps] = useState<string[]>([]);
  const [log, setLog] = useState<{ id: number; t: number; message: string; data: Record<string, unknown>; delivery: string }[]>([]);
  const [form, setForm] = useState({ instrument: "", metric: "imbalance_0.01", op: ">", value: 40, confirm_s: 10, cooldown_s: 300, telegram: false, note: "" });
  const [tg, setTg] = useState<{ enabled: boolean; chat_id: string; token_set: boolean; token_hint: string } | null>(null);
  const [tgForm, setTgForm] = useState({ token: "", chat_id: "" });
  const [msg, setMsg] = useState("");
  const reload = () => {
    api.alerts().then((d) => { setRules(d.rules); setMetrics(d.metrics); setOps(d.ops); }).catch(() => undefined);
    api.alertLog().then((d) => setLog(d.log)).catch(() => undefined);
    api.telegram().then((d) => { setTg(d); setTgForm((f) => ({ ...f, chat_id: f.chat_id || d.chat_id })); }).catch(() => undefined);
  };
  useEffect(() => { reload(); const id = window.setInterval(() => api.alertLog().then((d) => setLog(d.log)).catch(() => undefined), 5000); return () => window.clearInterval(id); }, []);
  useEffect(() => { if (!form.instrument && instruments.length) setForm((f) => ({ ...f, instrument: instruments[0].key })); }, [instruments]);
  const create = async () => {
    try {
      await api.addAlert(form.instrument, { metric: form.metric, op: form.op, value: form.value, confirm_s: form.confirm_s, cooldown_s: form.cooldown_s, telegram: form.telegram, note: form.note });
      setMsg("Правило создано"); reload();
    } catch (e: any) { setMsg("Ошибка: " + e.message); }
  };
  return (
    <div className="page">
      <div className="page-head">
        <h2>Алерты</h2>
        <p className="muted">Расширение. Правило создаётся только вами. На невалидных данных (STALE, RESYNC, нет покрытия) условие не срабатывает. Свечные метрики считаются по закрытым минутам.</p>
      </div>
      <div className="alert-form">
        <label>Инструмент<select value={form.instrument} onChange={(e) => setForm({ ...form, instrument: e.target.value })}>
          {instruments.map((i) => <option key={i.key} value={i.key}>{i.key}</option>)}</select></label>
        <label>Метрика<select value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
          {Object.entries(metrics).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>Условие<select value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
          {ops.map((o) => <option key={o} value={o}>{{ cross_up: "пересекает вверх", cross_down: "пересекает вниз" }[o] || o}</option>)}</select></label>
        <label>Значение<input type="number" value={form.value} onChange={(e) => setForm({ ...form, value: Number(e.target.value) })} /></label>
        <label>Подтверждение, с<input type="number" min={0} value={form.confirm_s} onChange={(e) => setForm({ ...form, confirm_s: Number(e.target.value) })} /></label>
        <label>Cooldown, с<input type="number" min={10} value={form.cooldown_s} onChange={(e) => setForm({ ...form, cooldown_s: Number(e.target.value) })} /></label>
        <label className="check"><input type="checkbox" checked={form.telegram} onChange={(e) => setForm({ ...form, telegram: e.target.checked })} />в Telegram</label>
        <label className="wide">Заметка<input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} /></label>
        <button className="primary" onClick={create} disabled={!form.instrument}>Создать правило</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
      <h3>Правила</h3>
      <div className="table-wrap"><table className="grid-table"><thead><tr><th>Вкл.</th><th>Инструмент</th><th>Условие</th><th>Подтв.</th><th>Cooldown</th><th>Telegram</th><th>Последнее срабатывание</th><th /></tr></thead>
        <tbody>{rules.map((r) => (
          <tr key={r.id}>
            <td><input type="checkbox" checked={r.enabled} onChange={(e) => api.patchAlert(r.id, e.target.checked).then(reload)} aria-label="включено" /></td>
            <td>{r.instrument}</td><td>{metrics[r.rule.metric] || r.rule.metric} {r.rule.op} {r.rule.value}</td>
            <td className="num">{r.rule.confirm_s} с</td><td className="num">{r.rule.cooldown_s} с</td><td>{r.rule.telegram ? "да" : "нет"}</td>
            <td>{r.last_fired ? dateTime(r.last_fired) : "—"}</td>
            <td><button onClick={() => api.deleteAlert(r.id).then(reload)}>Удалить</button></td>
          </tr>))}
          {!rules.length && <tr><td colSpan={8} className="empty">Правил пока нет</td></tr>}</tbody></table></div>
      <h3>Журнал</h3>
      <div className="table-wrap"><table className="grid-table"><thead><tr><th>Время</th><th>Сообщение</th><th>Значения и параметры</th><th>Доставка</th></tr></thead>
        <tbody>{log.map((l) => (<tr key={l.id}><td>{dateTime(l.t)}</td><td>{l.message}</td><td className="mono small">{JSON.stringify(l.data)}</td><td>{l.delivery || "только журнал"}</td></tr>))}
          {!log.length && <tr><td colSpan={4} className="empty">Срабатываний не было</td></tr>}</tbody></table></div>
      <h3>Telegram</h3>
      <p className="muted">Отдельная настройка. Пока вы не сохраните токен бота и chat id и не включите отправку, сообщения не отправляются. Токен хранится только в локальной базе FlowLens.</p>
      <div className="alert-form">
        <label>Токен бота<input id="tg-token" type="password" placeholder={tg?.token_set ? `сохранён (${tg.token_hint})` : "123456:ABC…"} value={tgForm.token} onChange={(e) => setTgForm({ ...tgForm, token: e.target.value })} autoComplete="off" /></label>
        <label>Chat id<input id="tg-chat" value={tgForm.chat_id} onChange={(e) => setTgForm({ ...tgForm, chat_id: e.target.value })} /></label>
        <button onClick={() => api.putTelegram({ ...(tgForm.token ? { token: tgForm.token } : {}), chat_id: tgForm.chat_id }).then((d) => { setTg(d); setTgForm({ ...tgForm, token: "" }); setMsg("Настройки Telegram сохранены"); })}>Сохранить</button>
        <label className="check"><input type="checkbox" checked={!!tg?.enabled} onChange={(e) => api.putTelegram({ enabled: e.target.checked }).then(setTg)} />Отправка включена</label>
        <button onClick={() => api.testTelegram().then((d) => setMsg("Тест: " + d.result))} disabled={!tg?.token_set}>Отправить тест</button>
      </div>
    </div>
  );
}

// ---- settings drawer -------------------------------------------------------------------
export function SettingsDrawer({ settings, update, close, health }: { settings: Settings; update: (p: Partial<Settings>) => void; close: () => void; health: Record<string, any> | null }) {
  const prm = settings.params;
  const setP = (k: string, v: unknown) => update({ params: { ...prm, [k]: v } });
  return (
    <div className="drawer" role="dialog" aria-label="Настройки">
      <div className="drawer-head"><h3>Настройки</h3><button onClick={close} aria-label="Закрыть">×</button></div>
      {SNAPSHOT && <p className="muted small">В статичном снимке параметры расчёта зафиксированы. Отображение (карта глубины, палитра) меняется.</p>}
      <section>
        <h4>Единицы</h4>
        <label>Суммы стакана и сделок<select value={prm.units} onChange={(e) => setP("units", e.target.value)}>
          <option value="quote">номинал в котируемой валюте (p×q)</option><option value="base">количество базового актива</option></select></label>
      </section>
      <section>
        <h4>Z-score и полосы дисбаланса</h4>
        <label>База статистики<select value={prm.z_base} onChange={(e) => setP("z_base", e.target.value)}>{BASES.map((b) => <option key={b}>{b}</option>)}</select></label>
        <label>Порог |z|<input type="number" step={0.5} min={1} max={10} value={prm.z_threshold} onChange={(e) => setP("z_threshold", Number(e.target.value))} /></label>
        <label>Минимум валидных отсчётов<input type="number" min={5} value={prm.z_min_n} onChange={(e) => setP("z_min_n", Number(e.target.value))} /></label>
        <label>Минимальное покрытие окна<input type="number" step={0.05} min={0.5} max={1} value={prm.z_min_cov} onChange={(e) => setP("z_min_cov", Number(e.target.value))} /></label>
        <label>Палитра<select value={settings.bandPalette} onChange={(e) => update({ bandPalette: e.target.value as Settings["bandPalette"] })}>
          <option value="reference">красный/зелёный + голубой/оранжевый для «ниже базы»</option><option value="mono">без отдельного цвета «ниже базы»</option></select></label>
        <p className="muted small">Пояса 0–0.25 %, 0.25–0.5 %, 0.5–1 % вокруг mid считаются независимо. Цвет — по z-score объёма стороны пояса относительно базы (прошлые свечи, без текущей).</p>
        <label>Мин. доля свечи с валидным стаканом<input type="number" step={0.1} min={0} max={1} value={prm.book_min_time_cov} onChange={(e) => setP("book_min_time_cov", Number(e.target.value))} /></label>
      </section>
      <section>
        <h4>Карта глубины</h4>
        <label>Укрупнение по времени<select value={settings.heatAgg} onChange={(e) => update({ heatAgg: e.target.value as Settings["heatAgg"] })}>
          <option value="mean">средневзвешенное по времени</option><option value="max">максимум</option></select></label>
        <label>Единицы<select value={settings.heatUnits} onChange={(e) => update({ heatUnits: e.target.value as Settings["heatUnits"] })}>
          <option value="quote">номинал ≈ объём × середина бакета</option><option value="base">количество</option></select></label>
        <label>Шкала<select value={settings.heatScale} onChange={(e) => update({ heatScale: e.target.value as Settings["heatScale"] })}>
          <option value="auto">авто (98-й перцентиль)</option><option value="manual">вручную</option></select></label>
        {settings.heatScale === "manual" && (<>
          <label>Минимум<input type="number" value={settings.heatMin} onChange={(e) => update({ heatMin: Number(e.target.value) })} /></label>
          <label>Максимум<input type="number" value={settings.heatMax} onChange={(e) => update({ heatMax: Number(e.target.value) })} /></label></>)}
      </section>
      <section>
        <h4>Состояние сервера</h4>
        {health ? (
          <dl className="kv">
            <dt>Аптайм</dt><dd>{Math.round(health.uptime_s / 60)} мин</dd>
            <dt>Память (макс.)</dt><dd>{health.rss_max_mb ?? "—"} МБ</dd>
            <dt>Задержка такта, p50</dt><dd>{health.tick_lag_ms_p50} мс</dd>
            <dt>Запись в БД, p50</dt><dd>{health.flush_ms_p50} мс</dd>
            <dt>Размер БД</dt><dd>{health.db_size_mb} МБ</dd>
            <dt>Клиенты WS</dt><dd>{health.ws_clients}</dd>
          </dl>
        ) : <p className="muted">нет данных</p>}
      </section>
    </div>
  );
}
