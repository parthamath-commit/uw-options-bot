#!/usr/bin/env python3
"""
sec_insider.py -- insider open-market BUYING from SEC Form 4 filings (official, free).

Every 5 minutes (weekdays 07:00-20:00 ET) reads the SEC "latest filings" feed for Form 4,
opens each new filing's XML, and alerts on open-market purchases (code "P") worth at least
SEC_MIN_BUY dollars (default $100,000) by directors, officers or 10% owners.
Optionally large open-market sales (SEC_ALERT_SELLS_OVER, default off).

Insiders must file within 2 business days of the trade, so this is the freshest free
"smart money" signal (Congress: up to 45 days).

SEC rules: max 10 requests/second and a User-Agent with a contact email.
Set SEC_CONTACT_EMAIL in uw-options-bot/.env.

Usage: python sec_insider.py [--dry-run] | --selftest
"""
import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(HERE, ".env"), override=True)

ET_TZ = ZoneInfo("America/New_York")
STATE = os.path.join(HERE, "sec_state.json")
CONTACT = os.getenv("SEC_CONTACT_EMAIL", "").strip()
UA = {"User-Agent": f"PalInitiatives research bot {CONTACT}".strip(), "Accept-Encoding": "gzip, deflate"}
MIN_BUY = float(os.getenv("SEC_MIN_BUY", "100000"))
SELLS_OVER = float(os.getenv("SEC_ALERT_SELLS_OVER", "0"))   # 0 = don't alert on sales
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
        "&owner=include&start={start}&count=100&output=atom")
ATOM = "{http://www.w3.org/2005/Atom}"


def log(*a):
    print(datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def get(url, **kw):
    time.sleep(0.15)                                  # well under 10 req/s
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


def latest_form4_filings(pages=3):
    """(accession, index_url) for recent Form 4 filings, newest first."""
    out, seen = [], set()
    for p in range(pages):
        root = ET.fromstring(get(FEED.format(start=p * 100)).content)
        for e in root.iter(f"{ATOM}entry"):
            link = e.find(f"{ATOM}link").get("href", "")
            m = re.search(r"/data/(\d+)/(\d{18})/", link) or re.search(r"(\d{10}-\d{2}-\d{6})", link)
            acc = re.search(r"(\d{10}-\d{2}-\d{6})", link)
            if not acc or acc.group(1) in seen:
                continue
            seen.add(acc.group(1))
            out.append((acc.group(1), link))
    return out


def form4_xml(index_url):
    folder = index_url.rsplit("/", 1)[0]
    items = get(folder + "/index.json").json()["directory"]["item"]
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml")]
    if not xmls:
        return None
    return get(f"{folder}/{xmls[0]}").content


def v(node, path):
    x = node.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_form4(xml_bytes):
    """Return dict(issuer, ticker, owner, role, trades=[...])."""
    root = ET.fromstring(xml_bytes)
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    roles = []
    if rel is not None:
        if v(rel, "isOfficer") in ("1", "true"):
            roles.append(v(rel, "officerTitle") or "Officer")
        if v(rel, "isDirector") in ("1", "true"):
            roles.append("Director")
        if v(rel, "isTenPercentOwner") in ("1", "true"):
            roles.append("10% owner")
    trades = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = v(t, "transactionCoding/transactionCode")
        try:
            sh = float(v(t, "transactionAmounts/transactionShares/value") or 0)
            px = float(v(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        after = v(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        trades.append({"code": code, "shares": sh, "price": px, "value": sh * px,
                       "date": v(t, "transactionDate/value"),
                       "after": float(after) if after.replace(".", "", 1).isdigit() else None})
    return {"issuer": v(root, "issuer/issuerName"), "ticker": v(root, "issuer/issuerTradingSymbol").upper(),
            "owner": v(root, "reportingOwner/reportingOwnerId/rptOwnerName"),
            "role": ", ".join(roles) or "insider", "trades": trades}


def money(x):
    return f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def title_name(n):
    parts = n.split()
    if len(parts) >= 2 and parts[0].isupper():          # SEC style "HUANG JEN HSUN"
        parts = parts[1:] + parts[:1]
    return " ".join(p.capitalize() for p in parts)


def summarize(doc):
    """One sentence for the filing's qualifying buys (and big sells if enabled)."""
    buys = [t for t in doc["trades"] if t["code"] == "P"]
    sells = [t for t in doc["trades"] if t["code"] == "S"]
    out = []
    bv = sum(t["value"] for t in buys)
    if buys and bv >= MIN_BUY:
        sh = sum(t["shares"] for t in buys)
        avg = bv / sh if sh else 0
        after = buys[-1]["after"]
        stake = f", now owning {after:,.0f} shares" if after else ""
        d = buys[-1]["date"]
        try:
            d = datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
        except Exception:
            pass
        out.append(("buy", f"{doc['ticker']}: {doc['role']} {title_name(doc['owner'])} bought {sh:,.0f} shares "
                           f"at about ${avg:,.2f} ({money(bv)}) on {d}{stake}."))
    sv = sum(t["value"] for t in sells)
    if SELLS_OVER > 0 and sells and sv >= SELLS_OVER:
        out.append(("sell", f"{doc['ticker']}: {doc['role']} {title_name(doc['owner'])} sold {money(sv)} of stock."))
    return out


def send(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("SEC_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg[:3900]}, timeout=20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not CONTACT:
        log("WARNING: set SEC_CONTACT_EMAIL in .env (SEC requires a contact in the User-Agent)")
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {"seen": [], "initialized": False}
    seen = set(st["seen"])
    filings = latest_form4_filings()
    new = [f for f in filings if f[0] not in seen]
    if not st.get("initialized"):
        log(f"first run: marking {len(new)} existing filings as seen (no alerts)")
        new_to_check = new[:40] if a.dry_run else []
    else:
        new_to_check = new
    lines = {"buy": [], "sell": []}
    for acc, url in new_to_check:
        try:
            x = form4_xml(url)
            if x:
                for kind, s in summarize(parse_form4(x)):
                    lines[kind].append(s)
        except Exception as e:
            log(f"{acc}: {e}")
    log(f"feed: {len(filings)} filings, new: {len(new)}, checked: {len(new_to_check)}, "
        f"alerts: {len(lines['buy']) + len(lines['sell'])}")
    parts = []
    if lines["buy"]:
        parts += ["🟢 INSIDER BUYING (SEC Form 4)"] + [f"• {s}" for s in lines["buy"]]
    if lines["sell"]:
        parts += ([""] if parts else []) + ["🔴 LARGE INSIDER SALES"] + [f"• {s}" for s in lines["sell"]]
    if parts:
        msg = "\n".join(parts)
        print(msg)
        if not a.dry_run:
            send(msg)
    if not a.dry_run:
        st["seen"] = (list(seen) + [f[0] for f in new])[-20000:]
        st["initialized"] = True
        json.dump(st, open(STATE, "w"))


def selftest():
    xml = b"""<?xml version="1.0"?><ownershipDocument>
<issuer><issuerCik>0001045810</issuerCik><issuerName>NVIDIA CORP</issuerName><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>HUANG JEN HSUN</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>President and CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>2026-09-24</value></transactionDate><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>5000</value></transactionShares><transactionPricePerShare><value>120.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>1005000</value></sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>2026-09-24</value></transactionDate><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>5000</value></transactionShares><transactionPricePerShare><value>121.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>1010000</value></sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""
    doc = parse_form4(xml)
    s = summarize(doc)
    assert s and s[0][0] == "buy", s
    print(s[0][1])
    print("selftest OK")


if __name__ == "__main__":
    main()
