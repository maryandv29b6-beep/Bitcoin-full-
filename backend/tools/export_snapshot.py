"""Export a static, read-only snapshot of a running FlowLens server for the
snapshot UI build (frontend built with VITE_SNAPSHOT=1).

  python tools/export_snapshot.py --server http://127.0.0.1:8000 --out SITE_DIR

Writes SITE_DIR/data/*.json in the file layout the snapshot UI expects
(see snapshotFile() in frontend/src/lib.ts).
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

TFS = ["1m", "5m", "15m", "1h", "4h", "1d"]
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s)


def get(server: str, path: str, **params) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    with urllib.request.urlopen(f"{server}{path}{'?' + q if q else ''}", timeout=120) as r:
        return json.loads(r.read())


def put(out: Path, rel: str, data: dict) -> int:
    p = out / "data" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    p.write_text(raw, "utf-8")
    return len(raw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--params", default='{"z_base": "4h"}', help="indicator parameters used for the snapshot")
    a = ap.parse_args()
    out = Path(a.out)
    params = json.loads(a.params)
    now = int(time.time() * 1000)
    sizes = {}
    meta = get(a.server, "/api/meta")
    meta["snapshot"] = {"created": now, "params": params,
                        "note": "static snapshot of a FlowLens server; read-only"}
    sizes["meta"] = put(out, "meta.json", meta)
    inst = get(a.server, "/api/instruments")
    sizes["instruments"] = put(out, "instruments.json", inst)
    sizes["screener"] = put(out, "screener.json", get(a.server, "/api/screener"))
    sizes["health"] = put(out, "health.json", get(a.server, "/api/health"))
    put(out, "alerts.json", {"rules": [], "metrics": meta.get("alert_metrics", {}), "ops": meta.get("alert_ops", [])})
    put(out, "alerts_log.json", {"log": []})
    put(out, "telegram.json", {"enabled": False, "chat_id": "", "token_set": False, "token_hint": ""})
    pj = json.dumps(params)
    for it in inst["instruments"]:
        if not it["ready"]:
            continue
        key = it["key"]
        probe = get(a.server, "/api/chart", inst=key, tf="1h", params=pj)
        start = probe["availability"].get("candles_from") or (now - 86400_000)
        for tf in TFS:
            frm = start - start % TF_MS[tf]
            sizes[f"chart {key} {tf}"] = put(out, f"chart/{slug(key)}_{tf}.json",
                                             get(a.server, "/api/chart", inst=key, tf=tf, **{"from": frm}, params=pj))
            step = None
            if tf == "1m":
                step = float(it["steps"]["heatmap"] or 0) * 2 or None   # keep the 1m map light
            for agg in ("mean", "max"):
                sizes[f"heat {key} {tf} {agg}"] = put(out, f"heat/{slug(key)}_{tf}_{agg}.json",
                                                      get(a.server, "/api/heatmap", inst=key, tf=tf, agg=agg,
                                                          step=step, **{"from": frm}))
            sizes[f"fp {key} {tf}"] = put(out, f"fp/{slug(key)}_{tf}.json",
                                          get(a.server, "/api/footprint", inst=key, tf=tf, **{"from": frm, "to": now + TF_MS[tf]}))
        sizes[f"profile {key}"] = put(out, f"profile/{slug(key)}.json",
                                      get(a.server, "/api/profile", inst=key, **{"from": start, "to": now + 60_000}))
    total = sum(sizes.values())
    big = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
    print(json.dumps({"files": len(sizes) + 3, "total_mb": round(total / 1e6, 2),
                      "largest": [(k, round(v / 1e6, 2)) for k, v in big]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
