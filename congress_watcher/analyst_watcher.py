#!/usr/bin/env python3
"""
analyst_watcher.py -- free alerts when a reputable Wall Street firm initiates coverage
with a Buy/Sell-type rating, or upgrades / downgrades a stock.

Data: Yahoo Finance analyst actions (via yfinance), checked for the S&P 500 plus your
watch tickers and the Barchart bot's recent tickers. Runs 4x per weekday (most rating
changes are published before the open).

Only firms in ANALYST_FIRMS count (default list below). Neutral initiations (Hold /
Equal-Weight / Neutral) are skipped; upgrades and downgrades are always reported.

Usage:
  python analyst_watcher.py              # normal run (Telegram)
  python analyst_watcher.py --dry-run    # print only
  python analyst_watcher.py --tickers NVDA,MU --days 5 --dry-run
  python analyst_watcher.py --selftest
"""
import argparse
import csv
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(HERE, ".env"), override=True)

ET_TZ = ZoneInfo("America/New_York")
STATE = os.path.join(HERE, "analyst_state.json")

DEFAULT_FIRMS = (
    "Goldman Sachs,Morgan Stanley,JP Morgan,JPMorgan,Bank of America,BofA,Citigroup,Citi,"
    "Wells Fargo,Barclays,UBS,Deutsche Bank,Jefferies,Bernstein,Evercore,Piper Sandler,"
    "Raymond James,RBC Capital,TD Cowen,Cowen,Mizuho,KeyBanc,Oppenheimer,Truist,BMO Capital,"
    "Wedbush,Needham,Stifel,HSBC,Macquarie,Bernstein,Baird,William Blair,Guggenheim,Cantor Fitzgerald,"
    "Wolfe Research,Redburn,Loop Capital,Rosenblatt,Susquehanna,Morningstar"
)
FIRMS = [f.strip().lower() for f in os.getenv("ANALYST_FIRMS", DEFAULT_FIRMS).split(",") if f.strip()]
WATCH = {t.strip().upper() for t in os.getenv("ANALYST_WATCH_TICKERS", os.getenv("CONGRESS_WATCH_TICKERS", "")).split(",") if t.strip()}

BUY_WORDS = ("buy", "outperform", "overweight", "accumulate", "positive", "add", "top pick", "market outperform", "sector outperform")
SELL_WORDS = ("sell", "underperform", "underweight", "reduce", "negative", "market underperform", "sector underperform")


def log(*a):
    print(datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def rating_class(grade):
    g = str(grade or "").strip().lower()
    if not g:
        return "unknown"
    if any(w in g for w in SELL_WORDS):
        return "sell"
    if any(w in g for w in BUY_WORDS):
        return "buy"
    return "neutral"


def reputable(firm):
    f = str(firm or "").lower()
    return any(x in f for x in FIRMS)


def universe(extra):
    syms = set(extra) | WATCH
    for name in ("sp500.txt",):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            syms |= {l.strip().upper() for l in open(p) if l.strip() and not l.startswith("#")}
    try:  # tickers the Barchart bot flagged in the last 5 days
        cut = (datetime.now(ET_TZ) - timedelta(days=5)).strftime("%Y-%m-%d")
        with open(os.path.join(ROOT, "barchart_bot", "candidate_log.csv"), newline="") as f:
            syms |= {r["ticker"].upper() for r in csv.DictReader(f) if r.get("logged_at", "") >= cut}
    except Exception:
        pass
    return sorted(s.replace(".", "-") for s in syms if s)


def bot_tickers():
    try:
        cut = (datetime.now(ET_TZ) - timedelta(days=5)).strftime("%Y-%m-%d")
        with open(os.path.join(ROOT, "barchart_bot", "candidate_log.csv"), newline="") as f:
            return {r["ticker"].upper() for r in csv.DictReader(f)
                    if r.get("logged_at", "") >= cut and r.get("stage") in ("SENT", "TRADE", "WATCHLIST")}
    except Exception:
        return set()


def fetch_actions(ticker, since):
    """Rows of analyst actions for one ticker on/after `since` (date)."""
    import yfinance as yf
    df = yf.Ticker(ticker).upgrades_downgrades
    if df is None or len(df) == 0:
        return []
    df = df.reset_index()
    date_col = "GradeDate" if "GradeDate" in df.columns else df.columns[0]
    out = []
    for _, r in df.iterrows():
        try:
            d = r[date_col]
            d = d.date() if hasattr(d, "date") else datetime.fromisoformat(str(d)[:10]).date()
        except Exception:
            continue
        if d < since:
            continue
        out.append({
            "ticker": ticker.replace("-", "."), "date": d, "firm": str(r.get("Firm", "")),
            "to": str(r.get("ToGrade", "") or ""), "from": str(r.get("FromGrade", "") or ""),
            "action": str(r.get("Action", "") or "").lower(),
            "pt": r.get("currentPriceTarget"), "pt_prior": r.get("priorPriceTarget"),
        })
    return out


def keep(a):
    """Reputable firm + (bullish/bearish initiation, or any upgrade/downgrade)."""
    if not reputable(a["firm"]):
        return False
    if a["action"] == "init":
        return rating_class(a["to"]) in ("buy", "sell")
    return a["action"] in ("up", "down")


def fmt_pt(a):
    try:
        pt = float(a.get("pt") or 0)
        prior = float(a.get("pt_prior") or 0)
    except Exception:
        return ""
    if pt <= 0:
        return ""
    if prior > 0 and abs(prior - pt) > 0.01:
        return f", with a price target of ${pt:,.0f} (was ${prior:,.0f})"
    return f", with a price target of ${pt:,.0f}"


def sentence(a, stars):
    star = "⭐ " if a["ticker"] in stars else ""
    day = a["date"].strftime("%b %d")
    if a["action"] == "init":
        return f"{star}{a['firm']} started covering {a['ticker']} with a {a['to']} rating{fmt_pt(a)} ({day})."
    verb = "upgraded" if a["action"] == "up" else "downgraded"
    frm = f" from {a['from']}" if a["from"] and a["from"].lower() != "nan" else ""
    return f"{star}{a['firm']} {verb} {a['ticker']}{frm} to {a['to']}{fmt_pt(a)} ({day})."


def build_message(actions):
    if not actions:
        return ""
    stars = WATCH | bot_tickers()
    groups = {"🟢 BUY SIGNALS": [], "🔴 SELL SIGNALS": []}
    for a in sorted(actions, key=lambda x: (x["ticker"] not in stars, x["ticker"])):
        bullish = a["action"] == "up" or (a["action"] == "init" and rating_class(a["to"]) == "buy")
        groups["🟢 BUY SIGNALS" if bullish else "🔴 SELL SIGNALS"].append("• " + sentence(a, stars))
    parts = [f"🏦 Analyst calls from major firms ({datetime.now(ET_TZ):%b %d})"]
    for title, rows in groups.items():
        if rows:
            parts += ["", title] + rows
    return "\n".join(parts)


def send(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("ANALYST_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log("Telegram token/chat missing")
        return
    for i in range(0, len(msg), 3900):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg[i:i + 3900]}, timeout=20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--tickers", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    try:
        st = json.load(open(STATE))
    except Exception:
        st = {"seen": []}
    seen = set(st["seen"])
    since = datetime.now(ET_TZ).date() - timedelta(days=a.days)
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] or universe([])
    log(f"checking {len(tickers)} tickers since {since}")

    found, errors = [], 0
    for i, t in enumerate(tickers):
        try:
            for act in fetch_actions(t, since):
                key = f"{act['ticker']}|{act['firm']}|{act['date']}|{act['action']}|{act['to']}"
                if key not in seen and keep(act):
                    found.append(act)
                    seen.add(key)
        except Exception as e:
            errors += 1
            if errors <= 5:
                log(f"{t}: {e}")
        time.sleep(0.4)
    log(f"new qualifying analyst actions: {len(found)} (errors: {errors})")

    msg = build_message(found)
    if msg:
        print(msg)
        if not a.dry_run:
            send(msg)
    if not a.dry_run:
        st["seen"] = list(seen)[-20000:]
        json.dump(st, open(STATE, "w"))


def selftest():
    from datetime import date
    rows = [
        {"ticker": "NVDA", "date": date(2026, 9, 25), "firm": "Goldman Sachs", "to": "Buy", "from": "", "action": "init", "pt": 250, "pt_prior": None},
        {"ticker": "MU", "date": date(2026, 9, 25), "firm": "Morgan Stanley", "to": "Overweight", "from": "Equal-Weight", "action": "up", "pt": 1300, "pt_prior": 1100},
        {"ticker": "INTC", "date": date(2026, 9, 25), "firm": "Barclays", "to": "Underweight", "from": "Equal-Weight", "action": "down", "pt": 90, "pt_prior": 110},
        {"ticker": "AAPL", "date": date(2026, 9, 25), "firm": "JP Morgan", "to": "Neutral", "from": "", "action": "init", "pt": 250, "pt_prior": None},
        {"ticker": "TSLA", "date": date(2026, 9, 25), "firm": "Tiny Research LLC", "to": "Buy", "from": "Hold", "action": "up", "pt": 500, "pt_prior": 400},
        {"ticker": "AMD", "date": date(2026, 9, 25), "firm": "UBS", "to": "Buy", "from": "Buy", "action": "main", "pt": 700, "pt_prior": 650},
    ]
    kept = [r for r in rows if keep(r)]
    assert [r["ticker"] for r in kept] == ["NVDA", "MU", "INTC"], kept
    print(build_message(kept))
    print("\nselftest OK")


if __name__ == "__main__":
    main()
