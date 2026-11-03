"""User-defined alerts (extension). Rules are created explicitly in the UI and
evaluated on screener metrics once per second. Conditions on invalid data
never fire; candle-based metrics use closed candles by default. Every firing
is written to the journal with the values and parameters used."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger("flowlens.alerts")

METRICS = {
    "price": "Цена, quote",
    "spread_bps": "Спред, bps",
    "imbalance_0.0025": "Bid Dominance 0.25 %, %",
    "imbalance_0.005": "Bid Dominance 0.5 %, %",
    "imbalance_0.01": "Bid Dominance 1 %, %",
    "dynamics_0.01": "Imbalance Dynamics 1 % за 5 мин, п.п.",
    "delta_5m": "Дельта сделок 5 мин (закрытые минуты), quote",
    "z_bid_1": "Z-score бидов 1 % (закрытая минута)",
    "z_ask_1": "Z-score асков 1 % (закрытая минута)",
}
OPS = (">", "<", ">=", "<=", "cross_up", "cross_down")


def metric_value(row: Dict[str, Any], metric: str) -> Optional[float]:
    if metric in ("price", "spread_bps", "delta_5m", "z_bid_1", "z_ask_1"):
        if metric == "delta_5m" and row.get("delta_partial"):
            return None
        v = row.get(metric)
        return float(v) if isinstance(v, (int, float)) else None
    if metric.startswith("imbalance_"):
        b = (row.get("bands") or {}).get(metric.split("_", 1)[1])
        return b.get("imbalance") if b else None
    if metric.startswith("dynamics_"):
        d = row.get("dynamics_5m") or {}
        return d.get(metric.split("_", 1)[1])
    return None


def validate_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    if rule.get("metric") not in METRICS:
        raise ValueError("unknown metric")
    if rule.get("op") not in OPS:
        raise ValueError("unknown operator")
    out = {"metric": rule["metric"], "op": rule["op"], "value": float(rule["value"]),
           "confirm_s": max(0, int(rule.get("confirm_s", 10))), "cooldown_s": max(10, int(rule.get("cooldown_s", 300))),
           "note": str(rule.get("note", ""))[:200], "telegram": bool(rule.get("telegram", False))}
    return out


class AlertEngine:
    def __init__(self, storage, screener, telegram) -> None:
        self.storage = storage
        self.screener = screener
        self.telegram = telegram
        self.state: Dict[int, Dict[str, Any]] = {}   # rule id -> {since, prev}
        self.listeners: List = []

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.evaluate()
            except Exception as e:  # pragma: no cover
                log.exception("alerts: %s", e)

    async def evaluate(self, rows: Optional[List[Dict[str, Any]]] = None, now: Optional[int] = None) -> List[dict]:
        rules = [r for r in self.storage.alert_rules() if r["enabled"]]
        if not rules:
            return []
        rows = rows if rows is not None else await self.screener.rows()
        by_key = {r["key"]: r for r in rows}
        now = now or int(time.time() * 1000)
        fired = []
        for r in rules:
            row = by_key.get(r["instrument"])
            rule = r["rule"]
            st = self.state.setdefault(r["id"], {"since": None, "prev": None})
            v = metric_value(row, rule["metric"]) if row else None
            if row is not None and row.get("status") in ("STALE", "RESYNC", "CONNECTING", "ERROR"):
                v = None
            if v is None:
                st["since"] = None
                continue
            prev = st["prev"]
            st["prev"] = v
            op, thr = rule["op"], rule["value"]
            cond = {">": v > thr, "<": v < thr, ">=": v >= thr, "<=": v <= thr,
                    "cross_up": prev is not None and prev <= thr < v,
                    "cross_down": prev is not None and prev >= thr > v}[op]
            if not cond:
                if op not in ("cross_up", "cross_down"):
                    st["since"] = None
                continue
            if st["since"] is None:
                st["since"] = now
            held = (now - st["since"]) / 1000
            if op in ("cross_up", "cross_down"):
                held = rule["confirm_s"]
            if held < rule["confirm_s"]:
                continue
            if r["last_fired"] and now - r["last_fired"] < rule["cooldown_s"] * 1000:
                continue
            msg = f"{r['instrument']}: {METRICS[rule['metric']]} {op} {thr:g} (значение {v:.4g})"
            data = {"value": v, "rule": rule, "instrument": r["instrument"], "row_status": row.get("status"),
                    "price": row.get("price"), "held_s": held}
            delivery = ""
            if rule.get("telegram") and self.telegram.enabled():
                delivery = await self.telegram.send(f"FlowLens: {msg}")
            self.storage.log_alert(r["id"], now, msg, data, delivery)
            self.storage.update_alert_rule(r["id"], last_fired=now)
            st["since"] = None
            fired.append({"rule_id": r["id"], "message": msg, "t": now})
            for fn in self.listeners:
                fn("alert", {"rule_id": r["id"], "message": msg, "t": now})
        return fired


class Telegram:
    """Optional delivery. Disabled until the user stores a bot token + chat id
    and enables it; nothing is sent before that."""

    def __init__(self, storage, session_factory) -> None:
        self.storage = storage
        self.session_factory = session_factory

    def config(self) -> Dict[str, Any]:
        return self.storage.get_setting("telegram", {"enabled": False, "token": "", "chat_id": ""})

    def public_config(self) -> Dict[str, Any]:
        c = self.config()
        tok = c.get("token") or ""
        return {"enabled": bool(c.get("enabled")), "chat_id": c.get("chat_id", ""),
                "token_set": bool(tok), "token_hint": (tok[:4] + "…" + tok[-3:]) if len(tok) > 8 else ""}

    def enabled(self) -> bool:
        c = self.config()
        return bool(c.get("enabled") and c.get("token") and c.get("chat_id"))

    async def send(self, text: str) -> str:
        c = self.config()
        if not (c.get("token") and c.get("chat_id")):
            return "telegram: не настроен"
        url = f"https://api.telegram.org/bot{c['token']}/sendMessage"
        try:
            session = self.session_factory()
            async with session.post(url, json={"chat_id": c["chat_id"], "text": text},
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return "telegram: ok" if r.status == 200 else f"telegram: HTTP {r.status}"
        except Exception as e:
            return f"telegram: ошибка {type(e).__name__}"
