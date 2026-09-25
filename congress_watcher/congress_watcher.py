#!/usr/bin/env python3
"""
congress_watcher.py -- free, near-real-time alerts for US Congress stock & OPTION trades.

Sources (official, free, earliest possible -- same day a report is filed):
  House : https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip  (index)
          https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf   (report)
  Senate: https://efdsearch.senate.gov  (Periodic Transaction Reports, report type 11)

Every run: find new Periodic Transaction Reports (PTRs), parse the trades, and send ONE
Telegram message -- options trades first (🎯), then stocks. Paper/scanned reports that
can't be read are sent as a link. Already-seen reports are remembered in state.json.

Legal reality: members have up to 45 days to disclose, so every trade is 2-6 weeks old.

Usage:
  python congress_watcher.py                 # normal run (Telegram)
  python congress_watcher.py --dry-run       # print, don't send, don't save state
  python congress_watcher.py --days 7        # look-back window for the first run (default 3)
  python congress_watcher.py --selftest      # parser tests, no network
"""
import argparse
import html
import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(HERE, ".env"), override=True)

ET_TZ = ZoneInfo("America/New_York")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"}
OPTIONS_ONLY = os.getenv("CONGRESS_OPTIONS_ONLY", "false").lower() == "true"
WATCH = {t.strip().upper() for t in os.getenv("CONGRESS_WATCH_TICKERS", "").split(",") if t.strip()}
BOT_CANDIDATES = os.path.join(ROOT, "barchart_bot", "candidate_log.csv")

TX_WORDS = {"P": "bought", "PURCHASE": "bought", "S": "sold", "SALE": "sold",
            "S (PARTIAL)": "partly sold", "SALE (PARTIAL)": "partly sold", "SALE (FULL)": "sold",
            "E": "exchanged", "EXCHANGE": "exchanged"}


# ─────────────────────────── helpers ───────────────────────────
def log(*a):
    print(datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"seen": [], "initialized": False}


def save_state(st):
    st["seen"] = st["seen"][-5000:]
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


def short_amount(txt):
    """'$1,001 - $15,000' -> '$1K–$15K'; 'Over $50,000,000' -> 'over $50M'."""
    def k(v):
        v = float(v.replace(",", ""))
        if v >= 1e6:
            return f"${v/1e6:g}M"
        if v >= 1e3:
            return f"${round(v/1e3):g}K"
        return f"${v:g}"
    nums = re.findall(r"\$\s?([\d,]+)", str(txt))
    if len(nums) >= 2:
        return f"{k(nums[0])}–{k(nums[1])}"
    if len(nums) == 1:
        return ("over " if "over" in str(txt).lower() else "") + k(nums[0])
    return str(txt).strip() or "undisclosed amount"


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            pass
    return None


def option_details(text):
    """Pull call/put, strike, expiry, contracts from a free-text asset/description."""
    t = " ".join(str(text).split())
    low = t.lower()
    kind = "call" if "call" in low else ("put" if "put" in low else "")
    strike = re.search(r"strike(?: price)?(?: of)?:?\s*\$?\s?([\d,]+(?:\.\d+)?)", t, re.I)
    exp = re.search(r"(?:expiration(?: date)?(?: of)?|expires?:?)\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", t, re.I)
    qty = re.search(r"(\d[\d,]*)\s+(?:call|put)", t, re.I)
    parts = []
    if strike:
        parts.append(f"${strike.group(1)} strike")
    if exp:
        d = parse_date(exp.group(1))
        parts.append(f"expiring {d.strftime('%b %d, %Y') if d else exp.group(1)}")
    if qty:
        parts.append(f"{qty.group(1)} contracts")
    return kind, ", ".join(parts)


def is_option(asset_type, text):
    at = str(asset_type).upper()
    return at in ("OP", "STOCK OPTION", "OPTIONS") or "OPTION" in at or bool(
        re.search(r"\b(call|put)s?\b.*\boption|\boption", str(text), re.I))


# ─────────────────────────── House ───────────────────────────
def house_new_filings(year, since):
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name = [n for n in z.namelist() if n.lower().endswith(".xml")][0]
    root = ET.fromstring(z.read(name))
    out = []
    for m in root.iter("Member"):
        g = lambda tag: (m.findtext(tag) or "").strip()
        if g("FilingType") != "P":
            continue
        fd = parse_date(g("FilingDate"))
        if not fd or fd < since:
            continue
        doc = g("DocID")
        who = " ".join(x for x in [g("Prefix") or "Rep.", g("First"), g("Last"), g("Suffix")] if x)
        out.append({
            "id": f"H{doc}", "chamber": "House", "who": who, "where": g("StateDst"),
            "filed": fd, "url": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf",
            "paper": doc.startswith("8") or doc.startswith("9"),
        })
    return out


TX_RE = re.compile(
    r"(?P<type>S \(partial\)|P|S|E)\s+(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+|\$[\d,]+)", re.I)


def parse_house_text(text):
    """Parse the text of an electronic House PTR into trade dicts."""
    t = " ".join(text.replace("\u0000", " ").split())
    trades = []
    ms = list(TX_RE.finditer(t))
    for i, m in enumerate(ms):
        prev_end = ms[i - 1].end() if i else 0
        next_start = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        before, after = t[prev_end:m.start()], t[m.end():next_start]
        # the asset "(TICKER) [TY]" is usually just before the type code, but can wrap after it
        tick = re.findall(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", before) or re.findall(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", after[:120])
        atype = re.findall(r"\[([A-Z]{2})\]", before) or re.findall(r"\[([A-Z]{2})\]", after[:120])
        desc = re.search(r"D(?:ESCRIPTION)?\s*:\s*(.+?)(?:\s+(?:ID Owner|Filing ID|\* For the|S O :|SUBHOLDING|C :|FILING STATUS)|$)",
                         after, re.I)
        asset = re.sub(r"^(SP|JT|DC)\s+", "", before.strip())[-120:]
        trades.append({
            "ticker": tick[-1] if tick else "",
            "asset": asset,
            "asset_type": atype[-1] if atype else "",
            "type": m.group("type").upper(),
            "date": parse_date(m.group("tdate")),
            "amount": m.group("amt"),
            "detail": desc.group(1)[:250] if desc else "",
        })
    return trades


def house_trades(filing):
    if filing["paper"]:
        return None
    try:
        import pdfplumber
        r = requests.get(filing["url"], headers=UA, timeout=60)
        r.raise_for_status()
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if len(text.strip()) < 50:
            return None                      # scanned image
        return parse_house_text(text)
    except Exception as e:
        log(f"House parse failed {filing['url']}: {e}")
        return None


# ─────────────────────────── Senate ───────────────────────────
class Senate:
    BASE = "https://efdsearch.senate.gov"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _csrf(self, page):
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        return m.group(1) if m else ""

    def login(self):
        home = self.s.get(f"{self.BASE}/search/home/", timeout=30)
        tok = self._csrf(home.text)
        self.s.post(f"{self.BASE}/search/home/", timeout=30,
                    data={"prohibition_agreement": "1", "csrfmiddlewaretoken": tok},
                    headers={"Referer": f"{self.BASE}/search/home/"})

    def new_filings(self, since):
        self.login()
        tok = self.s.cookies.get("csrftoken", "")
        r = self.s.post(f"{self.BASE}/search/report/data/", timeout=30,
                        headers={"Referer": f"{self.BASE}/search/", "X-CSRFToken": tok},
                        data={"start": "0", "length": "100", "report_types": "[11]", "filer_types": "[]",
                              "submitted_start_date": since.strftime("%m/%d/%Y") + " 00:00:00",
                              "submitted_end_date": "", "candidate_state": "", "senator_state": "",
                              "office_id": "", "first_name": "", "last_name": "",
                              "csrfmiddlewaretoken": tok})
        r.raise_for_status()
        out = []
        for row in r.json().get("data", []):
            first, last, office, link, date = (row + ["", "", "", "", ""])[:5]
            m = re.search(r'href="([^"]+)"', link)
            if not m:
                continue
            path = m.group(1)
            out.append({
                "id": "S" + path.strip("/").split("/")[-1], "chamber": "Senate",
                "who": f"Sen. {first.strip()} {last.strip()}".replace("  ", " "),
                "where": "", "filed": parse_date(date) or since,
                "url": self.BASE + path, "paper": "/paper/" in path,
            })
        return out

    def trades(self, filing):
        if filing["paper"]:
            return None
        try:
            page = self.s.get(filing["url"], timeout=30,
                              headers={"Referer": f"{self.BASE}/search/"}).text
            body = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        except Exception as e:
            log(f"Senate report failed {filing['url']}: {e}")
            return None
        trades = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            cells = [" ".join(c.split()) for c in cells]
            if len(cells) < 8:
                continue
            # #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, Comment
            _, tdate, owner, ticker, asset, atype, ttype, amount = cells[:8]
            comment = cells[8] if len(cells) > 8 else ""
            trades.append({
                "ticker": "" if ticker in ("--", "") else ticker, "asset": asset,
                "asset_type": atype, "type": ttype.upper(), "date": parse_date(tdate),
                "amount": amount, "detail": f"{asset} {comment}".strip(),
            })
        return trades


# ─────────────────────────── message ───────────────────────────
def bot_tickers():
    """Tickers the Barchart bot flagged in the last 5 days (for ⭐ highlighting)."""
    try:
        import csv
        cut = (datetime.now(ET_TZ) - timedelta(days=5)).strftime("%Y-%m-%d")
        with open(BOT_CANDIDATES, newline="") as f:
            return {r["ticker"].upper() for r in csv.DictReader(f)
                    if r.get("logged_at", "") >= cut and r.get("stage") in ("SENT", "TRADE", "WATCHLIST")}
    except Exception:
        return set()


def sentence(f, t, star_set):
    verb = TX_WORDS.get(t["type"], t["type"].lower() or "traded")
    name = t["ticker"] or t["asset"][:40] or "an asset"
    lag = f", disclosed {(f['filed'] - t['date']).days} days later" if t["date"] and f["filed"] else ""
    when = f" on {t['date'].strftime('%b %d')}" if t["date"] else ""
    who = f"{f['who']}" + (f" ({f['where']})" if f["where"] else "")
    star = "⭐ " if t["ticker"] and t["ticker"].upper() in star_set else ""
    if is_option(t["asset_type"], t["detail"] or t["asset"]):
        kind, det = option_details(f"{t['asset']} {t['detail']}")
        what = f"{name} {kind + ' ' if kind else ''}options" + (f" ({det})" if det else "")
        return f"{star}{who} {verb} {what}, {short_amount(t['amount'])},{when}{lag}.", True
    return f"{star}{who} {verb} {short_amount(t['amount'])} of {name}{when}{lag}.", False


def build_message(results):
    star_set = WATCH | bot_tickers()
    opts, stocks, links = [], [], []
    for f, trades in results:
        if trades is None:
            links.append(f"{f['who']} filed a report that must be read manually: {f['url']}")
            continue
        for t in trades:
            s, is_opt = sentence(f, t, star_set)
            (opts if is_opt else stocks).append(s)
    if OPTIONS_ONLY:
        stocks = []
    if not (opts or stocks or links):
        return ""
    today = datetime.now(ET_TZ).strftime("%b %d")
    parts = [f"🏛️ Congress trades disclosed ({today})"]
    if opts:
        parts += ["", "🎯 OPTIONS"] + [f"• {s}" for s in opts]
    if stocks:
        parts += ["", "📈 STOCKS"] + [f"• {s}" for s in stocks[:40]]
        if len(stocks) > 40:
            parts.append(f"…and {len(stocks) - 40} more stock trades.")
    if links:
        parts += ["", "📄 NOT MACHINE-READABLE"] + [f"• {s}" for s in links]
    parts += ["", "Members can take up to 45 days to disclose, so these trades are weeks old."]
    return "\n".join(parts)


def send(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("CONGRESS_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log("Telegram token/chat missing")
        return
    for i in range(0, len(msg), 3900):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg[i:i + 3900],
                            "disable_web_page_preview": "true"}, timeout=20)


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    st = load_state()
    seen = set(st["seen"])
    today = datetime.now(ET_TZ).date()
    since = today - timedelta(days=a.days if not st.get("initialized") else 10)

    filings = []
    try:
        for y in sorted({since.year, today.year}):
            filings += house_new_filings(y, since)
    except Exception as e:
        log("House index failed:", e)
    sen = Senate()
    try:
        filings += sen.new_filings(since)
    except Exception as e:
        log("Senate search failed:", e)

    new = [f for f in filings if f["id"] not in seen]
    log(f"filings in window: {len(filings)}, new: {len(new)}")
    results = []
    for f in sorted(new, key=lambda x: x["filed"]):
        trades = house_trades(f) if f["chamber"] == "House" else sen.trades(f)
        results.append((f, trades))
        log(f"{f['chamber']} {f['who']} filed {f['filed']}: "
            f"{'unreadable' if trades is None else str(len(trades)) + ' trades'}  {f['url']}")
        time.sleep(1.5)

    msg = build_message(results)
    if msg:
        print(msg)
        if not a.dry_run:
            send(msg)
    if not a.dry_run:
        st["seen"] = list(seen | {f["id"] for f, _ in results})
        st["initialized"] = True
        save_state(st)


def selftest():
    sample = ("ID Owner Asset Transaction Type Date Notification Date Amount Cap. Gains > $200? "
              "SP NVIDIA Corporation - Common Stock (NVDA) [ST] P 08/17/2026 09/03/2026 $1,001 - $15,000 "
              "FILING STATUS: New "
              "Apple Inc. (AAPL) [OP] P 08/20/2026 09/03/2026 $250,001 - $500,000 "
              "FILING STATUS: New DESCRIPTION: Purchased 50 call options with a strike price of $200 and an "
              "expiration date of 1/15/2027. "
              "JT Microsoft Corporation - Common Stock (MSFT) [ST] S (partial) 08/21/2026 09/03/2026 $15,001 - $50,000")
    tr = parse_house_text(sample)
    assert [t["ticker"] for t in tr] == ["NVDA", "AAPL", "MSFT"], tr
    assert tr[1]["asset_type"] == "OP" and "strike" in tr[1]["detail"], tr[1]
    f = {"who": "Rep. Jane Doe", "where": "CA11", "filed": parse_date("09/03/2026"), "url": "u", "chamber": "House"}
    print(build_message([(f, tr), ({**f, "who": "Rep. Paper Filer", "url": "https://x/8001.pdf"}, None)]))
    print("\nselftest OK")


if __name__ == "__main__":
    main()
