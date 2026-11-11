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
RANGES = {"7d": "Last 7 days", "30d": "Last 30 days", "mtd": "This month"}
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


def inr(p: int) -> str:
    r = round(p / 100)
    s = str(abs(r))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if r < 0 else "") + "\u20b9" + s


def ad_totals(ads: list[dict], a: dt.date, b: dt.date) -> list[dict]:
    agg = defaultdict(lambda: {"spend": 0, "sales": 0, "orders": 0, "name": ""})
    for x in ads:
        if a <= x["day"] <= b:
            g = agg[x["ad_id"]]
            g["name"] = x["name"] or g["name"]
            g["spend"] += x["spend"]
            g["sales"] += x["sales"]
            g["orders"] += x["orders"]
    return sorted(agg.values(), key=lambda g: -g["sales"])


def updates(daily: dict, ads: list[dict], today: dt.date, now: dt.datetime) -> list[dict]:
    """True, data-based notes for the 'Latest updates' panel. No invented actions."""
    s7, e7 = today - dt.timedelta(days=6), today
    p7s, p7e = today - dt.timedelta(days=13), today - dt.timedelta(days=7)
    cur = totals([v for d, v in daily.items() if s7 <= d <= e7])
    prev = totals([v for d, v in daily.items() if p7s <= d <= p7e])
    out = []
    week = ad_totals(ads, s7, e7)
    if week and week[0]["sales"]:
        t = week[0]
        r = f" at {t['sales'] / t['spend']:.1f}x ROAS" if t["spend"] else ""
        out.append(("Top ad this week: \u201c" + t["name"] + "\u201d",
                    f"{inr(t['sales'])} in sales from {inr(t['spend'])} spent{r}."))
    if cur["roas"] is not None:
        if prev["roas"]:
            ch = (cur["roas"] - prev["roas"]) / prev["roas"] * 100
            word = "up" if ch >= 0 else "down"
            out.append((f"ROAS this week: {cur['roas']:.2f}x, {word} {abs(ch):.0f}% on last week",
                        f"{inr(cur['sales'])} in sales from {inr(cur['spend'])} spent in the last 7 days."))
        else:
            out.append((f"ROAS this week: {cur['roas']:.2f}x",
                        f"{inr(cur['sales'])} in sales from {inr(cur['spend'])} spent in the last 7 days."))
    days = [(d, v) for d, v in daily.items() if s7 <= d <= e7 and v["sales"]]
    if days:
        d, v = max(days, key=lambda x: x[1]["sales"])
        out.append((f"Best day this week: {d.strftime('%a %d %b')}",
                     f"{inr(v['sales'])} in sales from {inr(v['spend'])} spent, {v['orders']} order{'s' if v['orders'] != 1 else ''}."))
    weak = [g for g in week if g["spend"] >= 150000 and g["sales"] < g["spend"]]
    if weak:
        w = max(weak, key=lambda g: g["spend"])
        out.append(("Worth reviewing: \u201c" + w["name"] + "\u201d",
                    f"{inr(w['spend'])} spent this week for {inr(w['sales'])} in sales."))
    return [{"at": (now - dt.timedelta(minutes=i)).isoformat(), "title": t, "detail": d, "by": "Advolo"}
            for i, (t, d) in enumerate(out)]


def rachna(ads: list[dict], today: dt.date) -> list[dict]:
    """Two creative ideas based on which ads are selling."""
    month = ad_totals(ads, today - dt.timedelta(days=29), today)
    month = [g for g in month if g["spend"] or g["sales"]]
    if not month:
        return []
    is_cat = lambda n: any(k in n.lower() for k in ("catalog", "catalogue", "dpa", "advantage+ catalog"))  # noqa: E731
    creatives = [g for g in month if not is_cat(g["name"]) and g["sales"]]
    top = creatives[0] if creatives else month[0]
    roas = f" at {top['sales'] / top['spend']:.1f}x" if top["spend"] else ""
    ideas = [{"title": "Make 3 fresh versions of \u201c" + top["name"] + "\u201d",
              "why": f"It\u2019s your best-selling creative this month: {inr(top['sales'])} in sales{roas}. "
                     "Keep the product and the offer the same, and only change the first 3 seconds.",
              "shoot": "Shoot: same product, 3 new openings (a question, a close-up, a reaction)"}]
    names = " ".join(g["name"].lower() for g in month[:3])
    weak = [g for g in month if g["spend"] >= 100000 and g["sales"] < g["spend"] and g["name"] != top["name"]]
    if any(k in names for k in ("catalog", "catalogue", "dpa", "carousel", "collection")) and not any(
            k in names for k in ("reel", "video", "ugc", "review", "wear", "outfit", "try")):
        ideas.append({"title": "Add a simple try-on or how-to-use video",
                      "why": "Your top ads are catalog or carousel ads. A real person showing the product gives "
                             "new buyers a reason to trust it.",
                      "shoot": "Shoot: 15\u201320 sec phone video, natural light, show it in use"})
    elif weak:
        w = max(weak, key=lambda g: g["spend"])
        ideas.append({"title": "Replace \u201c" + w["name"] + "\u201d with a customer-review video",
                      "why": f"It spent {inr(w['spend'])} this month for {inr(w['sales'])} in sales. "
                             "Real reviews usually earn more trust than polished visuals.",
                      "shoot": "Shoot: a happy customer (or you) saying why they love it, 20 sec"})
    else:
        ideas.append({"title": "Try a short founder-story video",
                      "why": "Your best ads are product-led. A quick \u2018why we made this\u2019 video helps "
                             "new buyers trust the brand.",
                      "shoot": "Shoot: you on camera, 20\u201330 sec, one take is fine"})
    return ideas


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
    # Windsor's trial serves the last 30 days only; a paid plan can widen this via WINDSOR_DAYS.
    days = int(os.environ.get("WINDSOR_DAYS", "30") or 30)
    since = today - dt.timedelta(days=days - 1)
    lm_start = since
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
        notes, ideas = updates(daily, ads, today, now), rachna(ads, today)
        for v in data.values():
            v["changes"], v["rachna"] = notes, ideas
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
