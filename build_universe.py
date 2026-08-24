#!/usr/bin/env python3
"""
build_universe.py -- build a stocks-only symbols.txt for schwab_scanner.py.

Sources (public, no auth):
  NASDAQ Trader symbol directory:
    https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
    https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

Keeps COMMON STOCK ONLY:
  - drops ETFs           (ETF flag == 'Y')
  - drops test issues    (Test Issue flag == 'Y')
  - drops non-common     (warrants/units/rights/preferreds via symbol suffix
                          and security-name keywords)

Output: symbols.txt (one ticker per line), NASDAQ + NYSE/other merged & deduped.

Run where the host is reachable (your VM or laptop):
    python3 build_universe.py
"""

import csv
import io
import sys
import urllib.request

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL  = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# security-name keywords that indicate NON-common-stock issues
BAD_NAME_BITS = (
    " ETF", " ETN", "PREFERRED", " WARRANT", " WARRANTS", " RIGHT", " RIGHTS",
    " UNIT", " UNITS", " NOTES", " DEPOSITARY", "% SR", "% NOTE", " FUND",
    " TRUST", "ACQUISITION", "  SPAC",
)
# symbols with these suffix letters are typically warrants/units/pfd/when-issued
def looks_noncommon_symbol(sym):
    s = sym.strip().upper()
    # Nasdaq uses 5th-letter conventions: W=warrant, U=unit, R=rights,
    # P/O/etc for preferreds. Also dotted/dashed suffixes on otherlisted.
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
    # pipe-delimited; last line is a File Creation Time footer
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


def parse_other(text):
    # otherlisted.txt uses 'ACT Symbol' as the trading symbol column
    out = []
    rdr = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in rdr:
        sym = (row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip()
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


def main():
    print("Fetching NASDAQ-listed...")
    nas = parse_nasdaq(fetch(NASDAQ_URL))
    print(f"  kept {len(nas)} common stocks")
    print("Fetching NYSE/other-listed...")
    oth = parse_other(fetch(OTHER_URL))
    print(f"  kept {len(oth)} common stocks")

    universe = sorted(set(nas) | set(oth))
    with open("symbols.txt", "w") as f:
        f.write("\n".join(universe) + "\n")
    print(f"\nWrote symbols.txt with {len(universe)} unique common-stock tickers.")
    print("Point the scanner at it:  export SYMBOLS_FILE=symbols.txt")


if __name__ == "__main__":
    main()