"""Rebuild the passcode-protected brand dashboards in d/ with fresh Meta numbers from Windsor.ai.

Runs on GitHub Actions every 3 hours (.github/workflows/dashboards.yml). Needs two repository secrets:
  WINDSOR_API_KEY  - from Windsor.ai (Account / API key)
  DASH_BRANDS      - JSON: {"<slug>": {"name": "Cayani", "account": "714940761035479",
                                       "file": "d/cayani-003cf736.html", "code": "G8SH-56YB"}, ...}
Data is encrypted with each brand's passcode (AES-GCM, key from PBKDF2-SHA256), so the public repo
only ever holds ciphertext. Nothing about the data is printed to the logs.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RANGES = {"7d": "Last 7 days", "30d": "Last 30 days", "mtd": "This month", "lm": "Last month"}
API = "https://connectors.windsor.ai/facebook"


def windsor(fields: list[str], date_from: dt.date, date_to: dt.date, key: str) -> list[dict]:
    q = urllib.parse.urlencode({"api_key": key, "fields": ",".join(fields),
                                "date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
    with urllib.request.urlopen(f"{API}?{q}", timeout=300) as r:
        body = json.loads(r.read().decode())
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    raise RuntimeError("Unexpected response from Windsor (check WINDSOR_API_KEY)")


def paise(v) -> int:
    try:
        return int(round(float(v or 0) * 100))
    except (TypeError, ValueError):
        return 0


def bounds(key: str, today: dt.date) -> tuple[dt.date, dt.date]:
    if key == "30d":
        return today - dt.timedelta(days=29), today
    if key == "mtd":
        return today.replace(day=1), today
    if key == "lm":
        end = today.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1), end
    return today - dt.timedelta(days=6), today


def totals(rows: list[dict]) -> dict:
    spend = sum(r["spend"] for r in rows)
    sales = sum(r["sales"] for r in rows)
    orders = sum(r["orders"] for r in rows)
    return {"spend": spend, "sales": sales, "orders": orders,
            "roas": round(sales / spend, 2) if spend else None, "cpo": spend // orders if orders else None}


def change(now, before):
    if now is None or not before:
        return None
    return round((now - before) / before * 100, 1)


def summary(name: str, daily: dict, ads: list[dict], key: str, today: dt.date, synced: dt.datetime) -> dict:
    start, end = bounds(key, today)
    span = (end - start).days + 1
    pstart, pend = start - dt.timedelta(days=span), start - dt.timedelta(days=1)
    pick = lambda a, b: [v for d, v in daily.items() if a <= d <= b]  # noqa: E731
    t, p = totals(pick(start, end)), totals(pick(pstart, pend))
    series, d = [], start
    while d <= end:
        r = daily.get(d, {"sales": 0, "spend": 0, "orders": 0})
        series.append({"day": d.isoformat(), "sales": r["sales"], "spend": r["spend"], "orders": r["orders"]})
        d += dt.timedelta(days=1)
    agg = defaultdict(lambda: {"spend": 0, "sales": 0, "orders": 0, "name": "", "campaign": "", "thumb": None})
    for a in ads:
        if start <= a["day"] <= end:
            g = agg[a["ad_id"]]
            g["name"], g["campaign"] = a["name"], a["campaign"]
            g["spend"] += a["spend"]
            g["sales"] += a["sales"]
            g["orders"] += a["orders"]
    best = sorted(({"ad_id": k, **v, "roas": round(v["sales"] / v["spend"], 2) if v["spend"] else None}
                   for k, v in agg.items() if v["spend"] or v["sales"]), key=lambda x: (-x["sales"], x["spend"]))[:6]
    return {"client": {"name": name, "status": "active"},
            "range": {"key": key, "label": RANGES[key], "start": start.isoformat(), "end": end.isoformat()},
            "kpis": {**t, "change": {k: change(t[k], p[k]) for k in ("sales", "spend", "orders", "roas", "cpo")}},
            "budget": {"monthly": None, "spent_mtd": 0, "days_in_month": 30, "day_of_month": today.day},
            "series": series, "best_ads": best, "changes": [],
            "sync": {"last": synced.isoformat(), "error": None}}


def encrypt(data: str, code: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=250000).derive(code.encode())
    ct = AESGCM(key).encrypt(iv, data.encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"s": b(salt), "i": b(iv), "c": b(ct)}


def main() -> int:
    key = os.environ.get("WINDSOR_API_KEY", "").strip()
    brands = json.loads(os.environ.get("DASH_BRANDS", "{}") or "{}")
    if not key or not brands:
        print("Missing WINDSOR_API_KEY or DASH_BRANDS secret")
        return 1
    now = dt.datetime.now(IST)
    today = now.date()
    lm_start = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    since = min(today - dt.timedelta(days=60), lm_start - dt.timedelta(days=31))
    acct_rows = windsor(["date", "account_id", "spend", "action_values_omni_purchase", "actions_omni_purchase"],
                        since, today, key)
    ad_rows = windsor(["date", "account_id", "ad_id", "ad_name", "campaign", "spend",
                       "action_values_omni_purchase", "actions_omni_purchase"], lm_start, today, key)
    tpl = open(os.path.join(ROOT, "scripts", "dash_template.html"), encoding="utf-8").read()
    built = 0
    for slug, b in brands.items():
        acct = str(b["account"])
        daily: dict = {}
        for r in acct_rows:
            if str(r.get("account_id")) == acct:
                d = dt.date.fromisoformat(r["date"])
                x = daily.setdefault(d, {"spend": 0, "sales": 0, "orders": 0})
                x["spend"] += paise(r.get("spend"))
                x["sales"] += paise(r.get("action_values_omni_purchase"))
                x["orders"] += int(float(r.get("actions_omni_purchase") or 0))
        ads = [{"day": dt.date.fromisoformat(r["date"]), "ad_id": str(r.get("ad_id")), "name": (r.get("ad_name") or "")[:300],
                "campaign": (r.get("campaign") or "")[:300], "spend": paise(r.get("spend")),
                "sales": paise(r.get("action_values_omni_purchase")),
                "orders": int(float(r.get("actions_omni_purchase") or 0))}
               for r in ad_rows if str(r.get("account_id")) == acct and (r.get("spend") or r.get("action_values_omni_purchase"))]
        if not daily:
            print(f"{slug}: no data returned, page left unchanged")
            continue
        data = {k: summary(b["name"], daily, ads, k, today, now) for k in RANGES}
        enc = encrypt(json.dumps(data, ensure_ascii=False, default=str), b["code"])
        html = tpl.replace("{{NAME}}", b["name"]).replace("{{ENC}}", json.dumps(enc))
        with open(os.path.join(ROOT, b["file"]), "w", encoding="utf-8") as f:
            f.write(html)
        built += 1
        print(f"{slug}: updated")
    print(f"Done: {built}/{len(brands)} dashboards updated at {now:%d %b %H:%M} IST")
    return 0 if built else 1


if __name__ == "__main__":
    sys.exit(main())
