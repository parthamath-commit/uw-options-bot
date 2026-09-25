#!/usr/bin/env python3
"""
alpaca_news.py -- real-time market news (Benzinga via Alpaca) -> Telegram + files for the bots.

Always-on service (systemd: alpaca-news.service). One websocket, tiny memory.

What gets SENT to Telegram (batched once a minute, short sentences):
  🏦 analyst calls   -- a major firm initiates (Buy/Sell-type), upgrades or downgrades
  🚀 big catalysts   -- FDA approval, to be acquired / merger agreement, major contract,
                        guidance raise (any ticker)
  ⚠️ red flags      -- offering, reverse split, bankruptcy, delisting, going concern
                        (only for tickers your bots care about: watch list + Barchart bot's
                        recent tickers)
Everything else is only logged.

Files written (used by the other bots):
  news_log.csv       every headline with its classification
  recent_news.json   last 48h of headlines per ticker (Barchart news runner reads this)

Needs ALPACA_API_KEY / ALPACA_SECRET_KEY in uw-options-bot/.env.
Usage:  python alpaca_news.py [--dry-run] | --selftest
"""
import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(HERE, ".env"), override=True)

ET_TZ = ZoneInfo("America/New_York")
WS_URL = os.getenv("ALPACA_NEWS_WS", "wss://stream.data.alpaca.markets/v1beta1/news")
KEY = os.getenv("ALPACA_API_KEY", "")
SECRET = os.getenv("ALPACA_SECRET_KEY", "")
LOG_CSV = os.path.join(HERE, "news_log.csv")
RECENT_JSON = os.path.join(HERE, "recent_news.json")
FLUSH_SECONDS = 60

sys.path.insert(0, os.path.join(ROOT, "congress_watcher"))
try:
    from analyst_watcher import FIRMS, rating_class          # same firm list as the Yahoo watcher
except Exception:
    FIRMS = ["goldman", "morgan stanley", "jpmorgan", "jp morgan", "bank of america", "bofa", "citi",
             "barclays", "ubs", "wells fargo", "jefferies", "bernstein", "evercore", "piper sandler"]

    def rating_class(g):
        g = str(g).lower()
        if any(w in g for w in ("sell", "underperform", "underweight")):
            return "sell"
        if any(w in g for w in ("buy", "outperform", "overweight")):
            return "buy"
        return "neutral"

WATCH = {t.strip().upper() for t in os.getenv("NEWS_WATCH_TICKERS", os.getenv("CONGRESS_WATCH_TICKERS", "")).split(",") if t.strip()}

CATALYST_PATTERNS = [
    (r"\bFDA (?:approv|grants? approval|clears?)|\bapproved by (?:the )?FDA|receives? FDA approval", "FDA approval"),
    (r"\bto (?:be )?acquire[ds]?\b|\bto be acquired\b|merger agreement|definitive agreement to (?:be )?acquire|agrees? to buy\b|takeover (?:bid|offer)", "acquisition / merger"),
    (r"\b(?:awarded|wins?|secures?|lands?) (?:a )?\$?[\d.,]+\s*(?:million|billion|[mb])\b.*\bcontract|\bcontract (?:worth|valued)", "major contract"),
    (r"\braises? (?:fy|full[- ]year|annual|q\d)?\s*(?:guidance|outlook|forecast)", "guidance raised"),
]
RED_FLAG_PATTERNS = [
    (r"\b(?:public|registered direct|underwritten|at-the-market|atm) offering\b|\bprices? (?:a )?\$?[\d.,]+\s*(?:million|m)? ?(?:share )?offering|\bproposed offering\b", "share offering"),
    (r"\breverse (?:stock )?split\b", "reverse split"),
    (r"\bchapter 11\b|\bbankruptcy\b", "bankruptcy"),
    (r"\bdelist", "delisting"),
    (r"\bgoing concern\b", "going-concern warning"),
    (r"\bcuts? (?:fy|full[- ]year|annual|q\d)?\s*(?:guidance|outlook|forecast)|\blowers? (?:guidance|outlook)", "guidance cut"),
]
ANALYST_RE = re.compile(
    r"^(?P<firm>.+?)\s+(?P<act>upgrades?|downgrades?|initiates? coverage on|initiates?|assumes? coverage on|resumes? coverage on)\s+"
    r"(?P<rest>.+)$", re.I)


def log(*a):
    print(datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def interest_tickers():
    s = set(WATCH)
    try:
        cut = (datetime.now(ET_TZ) - timedelta(days=5)).strftime("%Y-%m-%d")
        with open(os.path.join(ROOT, "barchart_bot", "candidate_log.csv"), newline="") as f:
            s |= {r["ticker"].upper() for r in csv.DictReader(f) if r.get("logged_at", "") >= cut}
    except Exception:
        pass
    return s


def classify(item):
    """Return (kind, label, detail). kind in analyst|catalyst|redflag|other."""
    head = " ".join(str(item.get("headline", "")).split())
    text = f"{head} {item.get('summary', '')}"
    m = ANALYST_RE.match(head)
    if m and any(f in m.group("firm").lower() for f in FIRMS):
        act = m.group("act").lower()
        rest = m.group("rest")
        to = re.search(r"\bto\s+([A-Za-z\- ]+?)(?:,|\s+(?:raises|lowers|maintains|announces|price|pt|from)\b|$)", rest, re.I)
        rating = to.group(1).strip() if to else ""
        if act.startswith("upgrade"):
            return "analyst", "upgrade", rating
        if act.startswith("downgrade"):
            return "analyst", "downgrade", rating
        # initiation / assumption: only Buy- or Sell-type ratings count
        with_rating = re.search(r"\bwith (?:an? )?([A-Za-z\- ]+?) rating", rest, re.I)
        rating = (with_rating.group(1) if with_rating else rating).strip()
        if rating_class(rating) in ("buy", "sell"):
            return "analyst", "initiation", rating
    for pat, label in RED_FLAG_PATTERNS:
        if re.search(pat, text, re.I):
            return "redflag", label, ""
    for pat, label in CATALYST_PATTERNS:
        if re.search(pat, head, re.I):
            return "catalyst", label, ""
    return "other", "", ""


def fmt_time(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET_TZ).strftime("%H:%M ET")
    except Exception:
        return ""


def to_sentence(item, kind, label):
    head = " ".join(str(item.get("headline", "")).split()).rstrip(".")
    syms = ", ".join(item.get("symbols", [])[:3])
    when = fmt_time(item.get("created_at", ""))
    prefix = f"{syms}: " if syms else ""
    if kind == "redflag":
        return f"{prefix}{label.capitalize()} -- {head} ({when})."
    return f"{prefix}{head} ({when})."


class Sink:
    """Collects items, writes files, and flushes Telegram messages once a minute."""

    def __init__(self, dry_run=False):
        self.dry = dry_run
        self.lock = threading.Lock()
        self.buf = {"analyst": [], "catalyst": [], "redflag": []}
        self.recent = self._load_recent()
        self.seen = set()
        self.interest = interest_tickers()
        self.interest_ts = time.time()

    def _load_recent(self):
        try:
            return json.load(open(RECENT_JSON))
        except Exception:
            return {}

    def handle(self, item):
        nid = item.get("id")
        if nid in self.seen:
            return
        self.seen.add(nid)
        if len(self.seen) > 20000:
            self.seen = set(list(self.seen)[-10000:])
        kind, label, detail = classify(item)
        syms = [s.upper() for s in item.get("symbols", [])]
        self._log(item, kind, label, detail)
        now = datetime.now(timezone.utc).isoformat()
        for s in syms:
            self.recent.setdefault(s, []).append({
                "title": item.get("headline", ""), "summary": item.get("summary", "")[:400],
                "source": item.get("source", "benzinga"), "url": item.get("url", ""),
                "published": item.get("created_at", now), "kind": kind, "label": label})
        if time.time() - self.interest_ts > 900:
            self.interest, self.interest_ts = interest_tickers(), time.time()
        if kind == "other":
            return
        if kind == "redflag" and not (set(syms) & self.interest):
            return
        star = "⭐ " if set(syms) & self.interest else ""
        with self.lock:
            self.buf[kind].append(star + to_sentence(item, kind, label))

    def _log(self, item, kind, label, detail):
        new = not os.path.exists(LOG_CSV)
        with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["received_at", "created_at", "symbols", "kind", "label", "detail", "headline", "url"])
            w.writerow([datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M:%S"), item.get("created_at", ""),
                        " ".join(item.get("symbols", [])), kind, label, detail,
                        item.get("headline", ""), item.get("url", "")])

    def flush(self):
        with self.lock:
            buf, self.buf = self.buf, {"analyst": [], "catalyst": [], "redflag": []}
        # keep only 48h in recent_news.json
        cut = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        self.recent = {k: [x for x in v if str(x.get("published", "")) >= cut][-20:]
                       for k, v in self.recent.items()}
        self.recent = {k: v for k, v in self.recent.items() if v}
        tmp = RECENT_JSON + ".tmp"
        json.dump(self.recent, open(tmp, "w"))
        os.replace(tmp, RECENT_JSON)
        parts = []
        for kind, title in (("analyst", "🏦 ANALYST CALLS"), ("catalyst", "🚀 CATALYSTS"), ("redflag", "⚠️ RED FLAGS")):
            if buf[kind]:
                parts += ([""] if parts else []) + [title] + [f"• {s}" for s in buf[kind]]
        if parts:
            msg = "\n".join(parts)
            print(msg, flush=True)
            if not self.dry:
                send(msg)


def send(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("NEWS_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log("Telegram token/chat missing")
        return
    for i in range(0, len(msg), 3900):
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": msg[i:i + 3900],
                                "disable_web_page_preview": "true"}, timeout=20)
        except Exception as e:
            log("telegram error:", e)


def run(dry_run=False):
    import websocket    # websocket-client
    if not (KEY and SECRET):
        log("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in .env")
        sys.exit(1)
    sink = Sink(dry_run)

    def flusher():
        while True:
            time.sleep(FLUSH_SECONDS)
            try:
                sink.flush()
            except Exception as e:
                log("flush error:", e)
    threading.Thread(target=flusher, daemon=True).start()

    backoff = 5
    while True:
        try:
            ws = websocket.create_connection(WS_URL, timeout=90)
            ws.send(json.dumps({"action": "auth", "key": KEY, "secret": SECRET}))
            ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
            log("connected to Alpaca news stream")
            backoff = 5
            while True:
                raw = ws.recv()
                if not raw:
                    continue
                for m in json.loads(raw):
                    t = m.get("T")
                    if t == "n":
                        sink.handle(m)
                    elif t == "error":
                        log("stream error:", m)
                        if m.get("code") in (402, 406, 409):   # auth failed / connection limit / not subscribed
                            time.sleep(300)
                    elif t in ("success", "subscription"):
                        log("stream:", m)
        except websocket.WebSocketTimeoutException:
            log("no data for 90s -- reconnecting")
        except Exception as e:
            log(f"stream dropped: {e} -- reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def selftest():
    samples = [
        {"id": 1, "headline": "Goldman Sachs Upgrades Micron Technology to Buy, Raises Price Target to $1,700", "symbols": ["MU"], "created_at": "2026-09-28T13:02:00Z"},
        {"id": 2, "headline": "Morgan Stanley Initiates Coverage On Arm Holdings with Overweight Rating, Announces Price Target of $210", "symbols": ["ARM"], "created_at": "2026-09-28T13:05:00Z"},
        {"id": 3, "headline": "JP Morgan Initiates Coverage On Apple with Neutral Rating", "symbols": ["AAPL"], "created_at": "2026-09-28T13:06:00Z"},
        {"id": 4, "headline": "Tiny Research Upgrades Tesla to Buy", "symbols": ["TSLA"], "created_at": "2026-09-28T13:07:00Z"},
        {"id": 5, "headline": "Acme Bio Receives FDA Approval For Lead Drug", "symbols": ["ACME"], "created_at": "2026-09-28T13:08:00Z"},
        {"id": 6, "headline": "XYZ Corp Announces $50 Million Public Offering Of Common Stock", "symbols": ["XYZ"], "created_at": "2026-09-28T13:09:00Z"},
        {"id": 7, "headline": "Barclays Downgrades Intel to Underweight, Lowers Price Target to $90", "symbols": ["INTC"], "created_at": "2026-09-28T13:10:00Z"},
        {"id": 8, "headline": "Globex To Acquire Initech In $2.1 Billion Deal", "symbols": ["GLBX", "INTK"], "created_at": "2026-09-28T13:11:00Z"},
    ]
    got = [(s["id"], classify(s)[:2]) for s in samples]
    exp = {1: ("analyst", "upgrade"), 2: ("analyst", "initiation"), 3: ("other", ""), 4: ("other", ""),
           5: ("catalyst", "FDA approval"), 6: ("redflag", "share offering"), 7: ("analyst", "downgrade"),
           8: ("catalyst", "acquisition / merger")}
    for i, c in got:
        assert c == exp[i], (i, c, exp[i])
    global LOG_CSV, RECENT_JSON
    LOG_CSV, RECENT_JSON = "/tmp/_nl.csv", "/tmp/_rn.json"
    sink = Sink(dry_run=True)
    sink.interest = {"XYZ"}
    for s in samples:
        sink.handle(s)
    sink.flush()
    print("\nselftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else run(a.dry_run)
