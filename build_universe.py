#!/usr/bin/env python3
"""
build_universe.py -- build a stocks-only symbols.txt for schwab_scanner.py.

Search space:
  ALL NASDAQ-listed common stocks  +  S&P 500 constituents (the S&P 500 adds
  its ~250 NYSE-listed names on top of NASDAQ; NASDAQ-listed S&P names are
  already included and dedup'd).

Sources (public, no auth):
  NASDAQ Trader symbol directory:
    https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  S&P 500 constituents:
    https://en.wikipedia.org/wiki/List_of_S%26P_500_companies   (parsed)
    fallback: https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv

Keeps COMMON STOCK ONLY (drops ETFs, test issues, warrants/units/rights/pfd).

Rebuild policy: a normal scan reuses the existing symbols.txt and never
re-fetches. The list is rebuilt once a week by a Saturday cron that runs this
with --force. Force a rebuild manually anytime with:  python3 build_universe.py --force

Output: symbols.txt (one ticker per line), deduped & sorted.
"""

import csv
import io
import os
import sys
import datetime
import urllib.request

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_CSV_FALLBACK = ("https://raw.githubusercontent.com/datasets/"
                      "s-and-p-500-companies/main/data/constituents.csv")

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols.txt")
SP500_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500.txt")

BAD_NAME_BITS = (
    " ETF", " ETN", "PREFERRED", " WARRANT", " WARRANTS", " RIGHT", " RIGHTS",
    " UNIT", " UNITS", " NOTES", " DEPOSITARY", "% SR", "% NOTE", " FUND",
    " TRUST", "ACQUISITION", "  SPAC",
)


def looks_noncommon_symbol(sym):
    s = sym.strip().upper()
    if any(ch in s for ch in (".", "$", "+", "=")):
        return True
    if len(s) == 5 and s[-1] in ("W", "U", "R", "P", "Q"):
        return True
    return False


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_nasdaq(text):
    out = []
    rdr = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in rdr:
        sym = (row.get("Symbol") or "").strip()
        if not sym or sym.startswith("File Creation"):
            continue
        if (row.get("ETF") or "").strip().upper() == "Y":
            continue
        if (row.get("Test Issue") or "").strip().upper() == "Y":
            continue
        name = (row.get("Security Name") or "").upper()
        if any(b in name for b in BAD_NAME_BITS):
            continue
        if looks_noncommon_symbol(sym):
            continue
        out.append(sym.upper())
    return out


def parse_sp500_wiki(html):
    """Extract tickers from the Wikipedia constituents table without external
    libs: the first column of each data row is the symbol, in a <td> after an
    href to the NYSE/NASDAQ quote. We grab the bold-ish symbol cells."""
    import re
    syms = set()
    # Wikipedia lists symbols like >AAPL</a> inside the constituents table.
    # Match tickers that are 1-5 uppercase letters or with a dot class (BRK.B).
    for m in re.finditer(r'>([A-Z]{1,5}(?:\.[A-Z])?)</a>', html):
        t = m.group(1)
        syms.add(t)
    return syms


def get_sp500():
    try:
        syms = parse_sp500_wiki(fetch(SP500_WIKI))
        if len(syms) >= 400:  # sanity: a good parse yields ~500
            return syms
        print(f"  wiki parse looked thin ({len(syms)}), trying fallback...")
    except Exception as e:
        print(f"  wiki fetch failed ({e}), trying fallback...")
    # fallback CSV
    try:
        text = fetch(SP500_CSV_FALLBACK)
        out = set()
        rdr = csv.DictReader(io.StringIO(text))
        for row in rdr:
            s = (row.get("Symbol") or "").strip().upper()
            if s:
                out.add(s)
        return out
    except Exception as e:
        print(f"  S&P 500 fallback failed ({e}); proceeding NASDAQ-only.")
        return set()


def normalize_sp_symbol(s):
    # Schwab/most feeds use a dot for share classes (BRK.B). Wikipedia already
    # uses that form; some sources use a dash (BRK-B) -> normalize to dot.
    return s.replace("-", ".").upper()


def build(force=False):
    """Build symbols.txt and return the tickers.

    Rebuild policy: rebuilds ONLY when forced (the weekly Saturday cron passes
    --force). On a normal scan run (force=False) it reuses the existing
    symbols.txt and never re-fetches -- so mid-week scans are fast and the
    universe is stable between Saturday rebuilds. If no file exists yet, it
    builds once regardless so the first run isn't empty."""
    if not force and os.path.exists(OUT_PATH) and os.path.exists(SP500_PATH):
        with open(OUT_PATH) as f:
            cached = [ln.strip() for ln in f if ln.strip()]
        if cached:
            age_days = (datetime.date.today()
                        - datetime.date.fromtimestamp(os.path.getmtime(OUT_PATH))).days
            print(f"Universe: reusing symbols.txt ({len(cached)} tickers, "
                  f"{age_days}d old; rebuilt weekly by Saturday cron).")
            return cached

    print("Fetching NASDAQ-listed common stocks...")
    nas = parse_nasdaq(fetch(NASDAQ_URL))
    print(f"  kept {len(nas)}")

    print("Fetching S&P 500 constituents...")
    sp = {normalize_sp_symbol(s) for s in get_sp500()}
    # keep only plausible common-stock tickers from the S&P set
    sp = {s for s in sp if not looks_noncommon_symbol(s.replace(".", ""))}
    print(f"  kept {len(sp)}")

    # write the S&P 500 list on its own (used by the S&P-only scan runs)
    if sp:
        with open(SP500_PATH, "w") as f:
            f.write("\n".join(sorted(sp)) + "\n")
        print(f"Wrote {SP500_PATH} with {len(sp)} S&P 500 tickers.")

    universe = sorted(set(nas) | sp)
    with open(OUT_PATH, "w") as f:
        f.write("\n".join(universe) + "\n")
    print(f"\nWrote {OUT_PATH} with {len(universe)} unique tickers "
          f"(NASDAQ-listed + S&P 500).")
    return universe


def load_sp500():
    """Return the S&P 500 tickers from sp500.txt (built alongside symbols.txt).
    If the file is missing, trigger a build to create it."""
    if not os.path.exists(SP500_PATH):
        build(force=True)
    if os.path.exists(SP500_PATH):
        with open(SP500_PATH) as f:
            return [ln.strip() for ln in f if ln.strip()]
    return []


def main():
    force = "--force" in sys.argv[1:]
    build(force=force)


if __name__ == "__main__":
    main()
