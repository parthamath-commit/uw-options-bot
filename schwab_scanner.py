#!/usr/bin/env python3
"""
Schwab on-demand stock scanner -- shares the UW Options Bot's Schwab token.

Menu / CLI:
  1. Bollinger Band breakouts (closed above upper / below lower), avg vol > 500K
  2. Daily candlestick patterns, 30-day avg vol > 500K
  3. Weekly candlestick patterns, 30-day avg vol > 500K

  Interactive:      python3 schwab_scanner.py
  One-shot (SSH):   python3 schwab_scanner.py 2

Token handling:
  Reuses the UW bot's schwab-py setup. No separate token to manage.
  The scanner builds a schwab-py client from the SAME schwab_token.json the
  bot uses (path from SCHWAB_TOKEN_PATH in .env). schwab-py auto-refreshes the
  30-min access token on every call, and running this scanner counts as a call
  that rolls the 7-day refresh token forward -- same as the bot.

  Drop this file in the bot dir (/home/ubuntu/uw-options-bot) so it finds
  .env and schwab_token.json, and run it with the bot's venv:
      cd ~/uw-options-bot && source venv/bin/activate
      python3 schwab_scanner.py 2

Setup: no new dependencies -- uses schwab-py + numpy already in the bot venv.
"""

import os
import sys
import time
from pathlib import Path

import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

MIN_AVG_VOL = 1_000_000
VOL_WINDOW = 30
BB_WINDOW = 20
BB_STD = 2

SCHWAB_APP_KEY    = os.getenv("SCHWAB_APP_KEY", "")
SCHWAB_APP_SECRET = os.getenv("SCHWAB_APP_SECRET", "")
SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")

# email (SMTP) config -- read from .env, never hard-coded
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "partha_pal_1999@yahoo.com")

# Telegram config -- reuses the UW bot's existing bot token + chat id
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------- universe
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
            "AMD", "NFLX", "JPM", "BAC", "XOM", "CVX", "WMT", "KO"]


def load_universe():
    """Every run uses the full NASDAQ-listed + S&P 500 universe. The list is
    rebuilt weekly by a Saturday cron; scan runs reuse the existing symbols.txt.

    Order of preference:
      1. build_universe.build() -- rebuilds if the cache isn't from today
      2. an existing symbols.txt (if the builder can't fetch, e.g. offline)
      3. SYMBOLS_FILE override, if explicitly set
      4. the small built-in list (last-resort smoke test)
    """
    # explicit override wins, for ad-hoc testing
    path = os.environ.get("SYMBOLS_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            return [ln.strip().upper() for ln in f if ln.strip()]

    try:
        import build_universe
        syms = build_universe.build(force=False)  # reuse; Saturday cron rebuilds
        if syms:
            return syms
    except Exception as e:
        print(f"Universe build failed ({e}); falling back.")

    # fallback: reuse a prior symbols.txt if one exists
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "symbols.txt")
    if os.path.exists(fallback):
        with open(fallback) as f:
            return [ln.strip().upper() for ln in f if ln.strip()]

    print("No universe available; using built-in 15-symbol smoke-test list.")
    return UNIVERSE


# ---------------------------------------------------------------- client
def build_client():
    """
    Build a schwab-py client from the bot's token file. Standalone scanner has
    no local schwab.py to conflict with, so a plain import works.
    """
    token_path = Path(SCHWAB_TOKEN_PATH)
    if not token_path.exists():
        sys.exit(f"Token file not found: {token_path}\n"
                 f"Run the bot's auth first: python broker/schwab.py --auth")
    if not SCHWAB_APP_KEY or not SCHWAB_APP_SECRET:
        sys.exit("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set in .env")

    try:
        from schwab import auth as schwab_auth
    except ImportError:
        sys.exit("schwab-py not installed in this environment. "
                 "Activate the bot venv first.")

    return schwab_auth.client_from_token_file(
        token_path=str(token_path),
        api_key=SCHWAB_APP_KEY,
        app_secret=SCHWAB_APP_SECRET,
    )


def get_candles(client, symbol, timeframe="daily"):
    """Pull candles via schwab-py. Returns list of candle dicts."""
    PH = client.PriceHistory
    try:
        if timeframe == "daily":
            resp = client.get_price_history(
                symbol,
                period_type=PH.PeriodType.MONTH,
                period=PH.Period.THREE_MONTHS,
                frequency_type=PH.FrequencyType.DAILY,
                frequency=PH.Frequency.DAILY,
                need_extended_hours_data=False,
            )
        elif timeframe == "daily_long":
            # ~2 years of daily candles: enough for a 200-day SMA plus the
            # multi-month SMA-trend lookback the Minervini template needs.
            resp = client.get_price_history(
                symbol,
                period_type=PH.PeriodType.YEAR,
                period=PH.Period.TWO_YEARS,
                frequency_type=PH.FrequencyType.DAILY,
                frequency=PH.Frequency.DAILY,
                need_extended_hours_data=False,
            )
        else:  # weekly
            resp = client.get_price_history(
                symbol,
                period_type=PH.PeriodType.YEAR,
                period=PH.Period.ONE_YEAR,
                frequency_type=PH.FrequencyType.WEEKLY,
                frequency=PH.Frequency.WEEKLY,
                need_extended_hours_data=False,
            )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as e:
        print(f"  ! {symbol}: {e}")
        return []


def avg_volume(candles, window=VOL_WINDOW):
    vols = [c["volume"] for c in candles[-window:]]
    return np.mean(vols) if vols else 0


def volume_shrinking_3d(candles):
    """True if the last 3 daily volumes are non-increasing:
    vol[-3] >= vol[-2] >= vol[-1]."""
    if len(candles) < 3:
        return False
    v3, v2, v1 = candles[-3]["volume"], candles[-2]["volume"], candles[-1]["volume"]
    return v3 >= v2 >= v1


# ---------------------------------------------------------------- Bollinger
def bb_signal(closes, window=BB_WINDOW, num_std=BB_STD):
    if len(closes) < window:
        return None
    arr = np.array(closes[-window:])
    sma, sd = arr.mean(), arr.std(ddof=0)
    upper, lower = sma + num_std * sd, sma - num_std * sd
    last = closes[-1]
    if last > upper:
        return "above_upper"
    if last < lower:
        return "below_lower"
    return "inside"


# ---------------------------------------------------------------- patterns
def _parts(c):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return o, h, l, cl, body, upper, lower


def is_hammer(c):
    o, h, l, cl, body, upper, lower = _parts(c)
    return body > 0 and lower >= 2 * body and upper <= body


def is_inverted_hammer(c):
    o, h, l, cl, body, upper, lower = _parts(c)
    return body > 0 and upper >= 2 * body and lower <= body


def is_shooting_star(c):
    return is_inverted_hammer(c)  # same shape; trend context distinguishes


def is_bullish_engulfing(prev, cur):
    return (prev["close"] < prev["open"] and cur["close"] > cur["open"]
            and cur["close"] >= prev["open"] and cur["open"] <= prev["close"])


def is_bearish_engulfing(prev, cur):
    return (prev["close"] > prev["open"] and cur["close"] < cur["open"]
            and cur["open"] >= prev["close"] and cur["close"] <= prev["open"])


def _body(c):
    return abs(c["close"] - c["open"])


def _mid(c):
    return (c["open"] + c["close"]) / 2


def is_morning_star(a, b, c):
    return (a["close"] < a["open"] and _body(b) < _body(a) * 0.5
            and c["close"] > c["open"] and c["close"] > _mid(a))


def is_evening_star(a, b, c):
    return (a["close"] > a["open"] and _body(b) < _body(a) * 0.5
            and c["close"] < c["open"] and c["close"] < _mid(a))


PATTERNS = ["hammer", "inverted_hammer", "shooting_star",
            "bullish_engulfing", "bearish_engulfing",
            "morning_star", "evening_star"]


def detect_patterns(candles):
    if len(candles) < 3:
        return []
    a, b, c = candles[-3], candles[-2], candles[-1]
    found = []
    if is_hammer(c): found.append("hammer")
    if is_inverted_hammer(c): found.append("inverted_hammer")
    if is_shooting_star(c): found.append("shooting_star")
    if is_bullish_engulfing(b, c): found.append("bullish_engulfing")
    if is_bearish_engulfing(b, c): found.append("bearish_engulfing")
    if is_morning_star(a, b, c): found.append("morning_star")
    if is_evening_star(a, b, c): found.append("evening_star")
    return found


# ------------------------------------------------ Minervini Trend Template
# SEPA stage-2 filter. All criteria come from daily closes except the RS
# Rating, which is a market-wide percentile rank (see rs_rating note below).
def sma(vals, window):
    if len(vals) < window:
        return None
    return float(np.mean(vals[-window:]))


def sma_series(vals, window):
    """Rolling SMA as a list aligned to vals (None until enough history)."""
    out = []
    for i in range(len(vals)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(float(np.mean(vals[i + 1 - window:i + 1])))
    return out


def minervini_check(closes, highs, lows, rs_rating=None):
    """
    Returns (passed: bool, reasons: dict of criterion -> bool).
    Needs ~1 year+ of daily data. RS rating is optional; if None, the RS
    criterion is marked None (not evaluated) rather than failed.
    """
    if len(closes) < 200:
        return False, {"insufficient_history": True}

    price = closes[-1]
    sma50 = sma(closes, 50)
    sma150 = sma(closes, 150)
    sma200 = sma(closes, 200)

    # 200-day SMA trending up: compare today's 200-SMA vs ~1 month ago (21
    # trading days). "Preferably 4-5 months" -> also track the 100-day-ago
    # slope as a stronger, informational signal.
    s200 = sma_series(closes, 200)
    sma200_1mo_ago = s200[-22] if len(s200) >= 22 and s200[-22] is not None else None
    sma200_5mo_ago = s200[-106] if len(s200) >= 106 and s200[-106] is not None else None

    hi_52w = max(highs[-252:]) if len(highs) >= 1 else None
    lo_52w = min(lows[-252:]) if len(lows) >= 1 else None

    c = {}
    c["price_above_sma150"] = price > sma150
    c["price_above_sma200"] = price > sma200
    c["sma150_above_sma200"] = sma150 > sma200
    c["sma200_trending_up_1mo"] = (
        sma200_1mo_ago is not None and sma200 > sma200_1mo_ago)
    c["sma50_above_sma150"] = sma50 > sma150
    c["sma50_above_sma200"] = sma50 > sma200
    c["price_above_sma50"] = price > sma50
    c["price_30pct_above_52w_low"] = (
        lo_52w is not None and price >= lo_52w * 1.30)
    c["price_within_25pct_52w_high"] = (
        hi_52w is not None and price >= hi_52w * 0.75)

    # RS rating: 70+ required. If not supplied, don't fail on it -- flag it.
    if rs_rating is None:
        c["rs_rating_70plus"] = None
    else:
        c["rs_rating_70plus"] = rs_rating >= 70

    # Pass = all hard criteria true. RS counts only if provided.
    hard = [v for k, v in c.items() if v is not None]
    passed = all(hard)

    # informational extras (don't gate the pass)
    c["_sma200_trending_up_5mo"] = (
        sma200_5mo_ago is not None and sma200 > sma200_5mo_ago)
    return passed, c


def compute_rs_ratings(client, symbols):
    """
    RS Rating is a 1-99 PERCENTILE RANK of a stock's price performance vs. all
    other stocks -- it cannot be computed from one symbol alone. We approximate
    IBD's rating: a weighted trailing return (recent quarters weighted more),
    then percentile-rank across the scanned universe.

    NOTE: this is a universe-relative approximation. A true IBD RS Rating ranks
    against the entire US market (~thousands of names). With a small universe
    the percentile is only meaningful relative to that set. For a real rating,
    feed a broad universe (e.g. S&P 500+) or plug in an external RS source.
    """
    perf = {}
    for s in symbols:
        candles = get_candles(client, s, "daily_long")
        closes = [c["close"] for c in candles]
        if len(closes) < 252:
            continue
        # IBD-style weighting: quarters at ~3,6,9,12 months back.
        p0 = closes[-1]
        def ret(n):
            return (p0 / closes[-n] - 1) if len(closes) >= n else 0.0
        q1, q2, q3, q4 = ret(63), ret(126), ret(189), ret(252)
        weighted = 0.4 * q1 + 0.2 * q2 + 0.2 * q3 + 0.2 * q4
        perf[s] = weighted
        time.sleep(0.3)

    if not perf:
        return {}
    # percentile rank -> 1..99
    ranked = sorted(perf.items(), key=lambda kv: kv[1])
    n = len(ranked)
    ratings = {}
    for i, (sym, _) in enumerate(ranked):
        pct = (i / (n - 1)) if n > 1 else 1.0
        ratings[sym] = int(round(1 + pct * 98))
    return ratings


# ---------------------------------------------------------------- scans
def scan_bb(client, symbols):
    """BB breakouts. Gate: 30d avg vol > 1M AND last-3-day volume shrinking."""
    above, below = [], []
    for s in symbols:
        candles = get_candles(client, s, "daily")
        if avg_volume(candles) < MIN_AVG_VOL:
            continue
        if not volume_shrinking_3d(candles):
            continue
        sig = bb_signal([c["close"] for c in candles])
        if sig == "above_upper":
            above.append(s)
        elif sig == "below_lower":
            below.append(s)
        time.sleep(0.3)
    return {"above": above, "below": below}


def format_bb(res):
    lines = ["== Bollinger Band breakouts "
             "(30d avg vol > 1M, last-3-day volume shrinking) =="]
    lines.append(f"Closed ABOVE upper band ({len(res['above'])}): "
                 f"{', '.join(res['above']) or '-'}")
    lines.append(f"Closed BELOW lower band ({len(res['below'])}): "
                 f"{', '.join(res['below']) or '-'}")
    return "\n".join(lines)


def scan_patterns(client, symbols, timeframe):
    """Candlestick patterns. Gate: 30d avg vol > 1M. Daily also requires the
    last-3-day volume to be shrinking; weekly does not."""
    lists = {p: [] for p in PATTERNS}
    for s in symbols:
        candles = get_candles(client, s, timeframe)
        if avg_volume(candles) < MIN_AVG_VOL:
            continue
        if timeframe == "daily" and not volume_shrinking_3d(candles):
            continue
        for p in detect_patterns(candles):
            lists[p].append(s)
        time.sleep(0.3)
    return lists


def format_patterns(lists, timeframe):
    extra = ", last-3-day volume shrinking" if timeframe == "daily" else ""
    lines = [f"== {timeframe.capitalize()} candlestick patterns "
             f"(30d avg vol > 1M{extra}) =="]
    for p in PATTERNS:
        lines.append(f"{p:20s} ({len(lists[p])}): {', '.join(lists[p]) or '-'}")
    return "\n".join(lines)


def scan_sepa(client, symbols):
    """Minervini Trend Template (SEPA). Only the 8 template criteria decide
    pass/fail -- no volume or other filter."""
    print("Computing RS ratings across universe (needs 1 full pass)...")
    rs = compute_rs_ratings(client, symbols)
    passed, near_miss = [], []
    for s in symbols:
        candles = get_candles(client, s, "daily_long")
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        ok, crit = minervini_check(closes, highs, lows, rs.get(s))
        if crit.get("insufficient_history"):
            continue
        if ok:
            passed.append((s, rs.get(s), crit))
        else:
            fails = [k for k, v in crit.items()
                     if v is False and not k.startswith("_")]
            near_miss.append((s, rs.get(s), fails))
        time.sleep(0.3)
    return {"passed": passed, "near_miss": near_miss}


def format_sepa(res, include_near_miss=True):
    lines = ["== Minervini Trend Template / SEPA (8 criteria only) =="]
    passed = res["passed"]
    if passed:
        lines.append(f"\nPASSED ALL CRITERIA ({len(passed)}):")
        for s, r, crit in passed:
            tag = " [200-SMA up 5mo+]" if crit.get("_sma200_trending_up_5mo") else ""
            lines.append(f"  {s:6s}  RS={r if r is not None else 'n/a'}{tag}")
    else:
        lines.append("\nPASSED ALL CRITERIA (0): -")
    if include_near_miss and res["near_miss"]:
        lines.append(f"\nNEAR MISSES (failed 1+ criteria) ({len(res['near_miss'])}):")
        for s, r, fails in res["near_miss"]:
            lines.append(f"  {s:6s}  RS={r if r is not None else 'n/a'}  "
                         f"failed: {', '.join(fails)}")
    lines.append("\nNote: RS Rating is ranked within THIS universe only.")
    return "\n".join(lines)


# thin wrappers that run + print, for the interactive menu
def run_bb_scan(client, symbols):
    print("\n" + format_bb(scan_bb(client, symbols)))


def run_pattern_scan(client, symbols, timeframe):
    print("\n" + format_patterns(scan_patterns(client, symbols, timeframe), timeframe))


def run_sepa_scan(client, symbols):
    print("\n" + format_sepa(scan_sepa(client, symbols)))


def run_all(client, symbols, which=("1", "2", "3", "4")):
    """Run the selected scans and return a combined text report."""
    blocks = []
    if "1" in which:
        blocks.append(format_bb(scan_bb(client, symbols)))
    if "2" in which:
        blocks.append(format_patterns(scan_patterns(client, symbols, "daily"), "daily"))
    if "3" in which:
        blocks.append(format_patterns(scan_patterns(client, symbols, "weekly"), "weekly"))
    if "4" in which:
        blocks.append(format_sepa(scan_sepa(client, symbols)))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- email
def send_email(subject, body):
    """Send a plain-text report via SMTP. Returns True on success."""
    import smtplib
    from email.mime.text import MIMEText

    if not SMTP_USER or not SMTP_APP_PASSWORD:
        print("SMTP_USER / SMTP_APP_PASSWORD not set in .env -- cannot email.")
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_APP_PASSWORD)
            srv.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        print(f"Emailed report to {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False


def send_telegram(text):
    """Send a report to the UW bot's Telegram chat. Mirrors the bot's own
    sender (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env). Splits into
    <=4096-char chunks since Telegram rejects longer messages."""
    import urllib.parse
    import urllib.request

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env -- "
              "cannot send Telegram.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    LIMIT = 4000  # under the 4096 cap, leaving headroom

    # Split on line boundaries so no scan block is cut mid-line.
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > LIMIT:
            if cur:
                chunks.append(cur)
            # a single over-long line: hard-split it
            while len(line) > LIMIT:
                chunks.append(line[:LIMIT])
                line = line[LIMIT:]
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)

    ok_all = True
    for i, chunk in enumerate(chunks, 1):
        tag = f"[{i}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        data = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": tag + chunk}).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as resp:
                if resp.status != 200:
                    print(f"Telegram HTTP {resp.status}")
                    ok_all = False
        except Exception as e:
            print(f"Telegram send failed: {e}")
            ok_all = False
        time.sleep(0.5)  # avoid Telegram flood limits between chunks
    if ok_all:
        print(f"Sent report to Telegram ({len(chunks)} message(s)).")
    return ok_all


def deliver(subject, report):
    """Send the report by whichever channels are configured. Telegram is
    preferred (reuses the bot's setup); email is used too if SMTP is set."""
    sent = False
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        sent = send_telegram(f"{subject}\n\n{report}") or sent
    if SMTP_USER and SMTP_APP_PASSWORD:
        sent = send_email(subject, report) or sent
    if not sent:
        print("No delivery channel configured (set Telegram or SMTP in .env).")
    return sent


# ---------------------------------------------------------------- menu
MENU = """
========= Schwab Scanner =========
  1. Bollinger Band breakouts (daily)
  2. Daily candlestick patterns
  3. Weekly candlestick patterns
  4. Minervini Trend Template (SEPA)
  5. Run ALL (1 + 2 + 3 + 4)
  0. Exit
==================================
Choose: """


def run_choice(choice, client, symbols):
    if choice == "1":
        run_bb_scan(client, symbols)
    elif choice == "2":
        run_pattern_scan(client, symbols, "daily")
    elif choice == "3":
        run_pattern_scan(client, symbols, "weekly")
    elif choice == "4":
        run_sepa_scan(client, symbols)
    elif choice == "5":
        print("\n" + run_all(client, symbols, ("1", "2", "3", "4")))
    else:
        print("Invalid choice.")


def main():
    client = build_client()
    symbols = load_universe()
    print(f"Universe: {len(symbols)} symbols loaded.")

    args = [a.strip() for a in sys.argv[1:]]

    # Email modes for the scheduler (non-interactive):
    #   --email-daily   -> scans 1,2,4 emailed
    #   --email-weekly  -> scans 1,2,3,4 emailed
    import datetime as _dt
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    if "--email-daily" in args or "--report-daily" in args:
        report = run_all(client, symbols, ("1", "2", "4"))
        deliver(f"[Scanner] Daily results 1,2,4 -- {stamp}", report)
        return
    if "--email-weekly" in args or "--report-weekly" in args:
        report = run_all(client, symbols, ("1", "2", "3", "4"))
        deliver(f"[Scanner] Friday results 1,2,3,4 -- {stamp}", report)
        return

    if args:
        run_choice(args[0], client, symbols)
        return

    while True:
        choice = input(MENU).strip()
        if choice == "0":
            break
        run_choice(choice, client, symbols)


if __name__ == "__main__":
    main()
