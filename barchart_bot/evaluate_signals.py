#!/usr/bin/env python3
"""
evaluate_signals.py -- measures real accuracy of Barchart bot candidates (v18).

Reads candidate_log.csv (every candidate the bot evaluated: SENT / TRADE / WATCHLIST /
IGNORE / REJECT) and labels each one with what the stock actually did afterwards:

  win3  = stock moved 3% in the signal's direction BEFORE moving 3% against it,
          within 5 trading days (35 hourly bars), entry = next hourly open after the alert.
          1 = win, 0 = loss, blank = neither level hit (excluded from accuracy).
  ret1d / ret3d / ret5d = direction-signed stock return (entry -> +1/+3/+5 days)

Writes evaluation.csv + evaluation_report.txt and (with --telegram) a short summary to
the investment channel. Runs daily at 17:30 ET via barchart-eval.timer.

Usage:  venv/bin/python evaluate_signals.py [--telegram] [--days 30]
"""
import os
import sys
import argparse
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

HOME = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HOME, ".env"))
LOG = os.path.join(HOME, "candidate_log.csv")
OUT_CSV = os.path.join(HOME, "evaluation.csv")
OUT_TXT = os.path.join(HOME, "evaluation_report.txt")
TH = 0.03          # 3% move
HORIZON = 35       # hourly bars (~5 trading days)
TARGET_ACC = 80.0


def load_prices(tickers):
    """Hourly bars for the last 60 days (yfinance limit for 60m)."""
    out = {}
    for t in sorted(set(tickers)):
        try:
            df = yf.Ticker(t).history(period="60d", interval="60m", auto_adjust=False)
            if df is None or df.empty:
                continue
            df = df.tz_convert("America/New_York")[["Open", "High", "Low", "Close"]]
            out[t] = df
        except Exception as e:
            print(f"price load failed {t}: {e}")
    return out


def label(row, px):
    d = px.get(row["ticker"])
    if d is None:
        return (np.nan,) * 5
    fut = d[d.index >= row["ts"]]
    if len(fut) < 2:
        return (np.nan,) * 5
    entry = fut["Open"].iloc[0]
    sgn = row["dir"]
    w = fut.iloc[:HORIZON]
    if sgn > 0:
        fav, adv = w["High"] / entry - 1, 1 - w["Low"] / entry
    else:
        fav, adv = 1 - w["Low"] / entry, w["High"] / entry - 1
    hf = int(np.argmax(fav.values >= TH)) if (fav.values >= TH).any() else 10**6
    ha = int(np.argmax(adv.values >= TH)) if (adv.values >= TH).any() else 10**6
    if hf == ha == 10**6:
        win = np.nan if len(w) >= HORIZON else np.nan   # unresolved / still open
    else:
        win = 1.0 if hf < ha else 0.0
    def r(n):
        return sgn * (fut["Close"].iloc[n] / entry - 1) if len(fut) > n else np.nan
    return win, r(7), r(21), r(34), entry


def acc_table(df, by, min_n=1):
    g = df.groupby(by, dropna=False).agg(n=("win3", "count"), acc=("win3", "mean"),
                                         ret5d=("ret5d", "mean"))
    g["acc"] = (g["acc"] * 100).round(1)
    g["ret5d"] = (g["ret5d"] * 100).round(2)
    return g[g["n"] >= min_n].sort_values("n", ascending=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    if not os.path.exists(LOG):
        print("No candidate_log.csv yet -- bot has not logged candidates.")
        return
    c = pd.read_csv(LOG)
    c["ts"] = pd.to_datetime(c["logged_at"]).dt.tz_localize("America/New_York")
    c = c[c["ts"] >= pd.Timestamp.now(tz="America/New_York") - timedelta(days=a.days)]
    # one row per contract per day; keep SENT over other stages
    c["rank"] = (c["stage"] == "SENT").astype(int)
    c["day"] = c["ts"].dt.date
    c = c.sort_values("rank").drop_duplicates(
        ["day", "ticker", "option_type", "strike", "expiry", "source"], keep="last")
    c["dir"] = np.where(c["option_type"].astype(str).str.upper() == "CALL", 1, -1)
    # side-aware direction for rejected SELL-side prints (tests the buy/sell hypothesis)
    sold = c["aggressor"].astype(str).eq("SELL")
    c.loc[sold, "dir"] = -c.loc[sold, "dir"]

    px = load_prices(c["ticker"].unique())
    lab = c.apply(lambda r: label(r, px), axis=1, result_type="expand")
    c[["win3", "ret1d", "ret3d", "ret5d", "entry_px"]] = lab
    c.drop(columns=["rank"]).to_csv(OUT_CSV, index=False)

    res = c[c["win3"].notna()]
    sent = res[res["stage"] == "SENT"]
    lines = []
    P = lines.append
    P(f"Barchart bot accuracy report -- {datetime.now():%Y-%m-%d %H:%M} (last {a.days} days)")
    P(f"Win = stock moves 3% in signal direction before 3% against, within 5 trading days.")
    P(f"Candidates logged: {len(c)}   resolved: {len(res)}   target accuracy: {TARGET_ACC:.0f}%")
    P("")
    if len(sent):
        P(f"SENT alerts (TRADE): {len(sent)} resolved, accuracy {sent['win3'].mean()*100:.1f}%, "
          f"avg 5-day move {sent['ret5d'].mean()*100:+.2f}%")
    else:
        P("SENT alerts (TRADE): none resolved yet")
    P(f"All resolved candidates: accuracy {res['win3'].mean()*100:.1f}%" if len(res) else "")
    P("\nBy stage:\n" + acc_table(res, "stage").to_string())
    P("\nBy aggressor side (BUY vs SELL vs UNKNOWN; SELL rows scored with side-aware direction):\n"
      + acc_table(res, ["source", "aggressor"]).to_string())
    P("\nBy reject reason (what each gate removed):\n"
      + acc_table(res[res["stage"] == "REJECT"], "reject_reason").to_string())
    for col, bins in [("score", [-99, 20, 25, 30, 35, 40, 99]),
                      ("premium", [0, 1e5, 5e5, 1e6, 2e6, 1e12]),
                      ("dte", [0, 3, 7, 14, 30, 60, 999])]:
        x = res.copy()
        x[col] = pd.to_numeric(x[col], errors="coerce")
        x = x[x[col].notna()]
        if len(x):
            x["bucket"] = pd.cut(x[col], bins)
            P(f"\nBy {col}:\n" + acc_table(x, "bucket").to_string())
    for col in ["price_reaction", "daily_trend", "open_flag", "option_type", "leg"]:
        if col in res:
            P(f"\nBy {col}:\n" + acc_table(res, col).to_string())
    res2 = res.copy()
    res2["hour"] = res2["ts"].dt.hour
    P("\nBy hour (ET):\n" + acc_table(res2, "hour").to_string())

    # what-if: TRADE rule = buy-side single-leg call + price reaction + score >= X
    base = res[(res["stage"].isin(["SENT", "TRADE", "WATCHLIST", "IGNORE"]))].copy()
    base["score"] = pd.to_numeric(base["score"], errors="coerce")
    P("\nWhat-if score thresholds (non-rejected candidates):")
    for thr in [20, 25, 30, 35, 40, 45]:
        m = base["score"] >= thr
        pr = m & base["price_reaction"].astype(str).str.lower().eq("true")
        P(f"  score>={thr:2d}: n={m.sum():4d} acc={base[m]['win3'].mean()*100 if m.sum() else float('nan'):5.1f}%"
          f"   +price reaction: n={pr.sum():4d} acc={base[pr]['win3'].mean()*100 if pr.sum() else float('nan'):5.1f}%")
    report = "\n".join(lines)
    open(OUT_TXT, "w").write(report)
    print(report)

    if a.telegram:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat = os.getenv("TELEGRAM_INVESTMENT_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
        if token and chat:
            s_acc = f"{sent['win3'].mean()*100:.0f}% of {len(sent)}" if len(sent) else "none resolved yet"
            a_acc = f"{res['win3'].mean()*100:.0f}% of {len(res)}" if len(res) else "n/a"
            msg = (f"📊 Barchart bot accuracy (last {a.days}d)\n"
                   f"Sent alerts: {s_acc}\nAll candidates: {a_acc}\n"
                   f"Target: {TARGET_ACC:.0f}%  (win = 3% move in direction before 3% against, 5 days)")
            try:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": msg}, timeout=20)
            except Exception as e:
                print("telegram failed:", e)


if __name__ == "__main__":
    main()
