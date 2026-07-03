# UW Options Bot v1.0
**Pal Initiatives LLC** — Options flow scanner powered exclusively by Unusual Whales API + IBKR

---

## Folder Structure

```
uw_options_bot/
├── main.py                  ← CLI entry point (run / scan / dealer / check)
├── config.py                ← All settings — reads .env
├── models.py                ← DealerExposure, FlowSignal, ScoredSignal
├── scanner.py               ← UWOptionsBot orchestrator
│
├── data/
│   └── uw_client.py         ← Sole data source: Unusual Whales REST API
│
├── broker/
│   └── ibkr.py              ← IBKR ib_async — live quotes + account value
│
├── scoring/
│   ├── additive.py          ← Additive scorer (structure/IV/GEX/darkpool)
│   ├── institutional.py     ← Institutional scorer (VEX/CHEX/DEX/flow_dir)
│   └── utils.py             ← composite_score(), calculate_position_size()
│
├── alerts/
│   └── telegram.py          ← Signal alerts + startup/shutdown/error
│
├── persistence/
│   └── excel.py             ← Colour-coded signals.xlsx logger
│
├── output/                  ← signals.xlsx written here
├── logs/                    ← uw_bot.log written here
├── requirements.txt
└── .env.template
```

---

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.template .env
# Add: UW_API_KEY, IBKR_ACCOUNT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 3. Start TWS or IB Gateway (API enabled, port 7497)

# 4. Verify API access  ← do this first
python main.py --mode check

# 5. Test dealer data
python main.py --mode dealer

# 6. Test single scan
python main.py --mode scan

# 7. Run full loop
python main.py
```

---

## Data Sources

| Layer | Source | Endpoint |
|-------|--------|----------|
| UOA flow signals | Unusual Whales | `/api/option-trades/flow-alerts` |
| GEX / DEX / VEX / CHEX | Unusual Whales | `/api/stock/{ticker}/greek-exposure` |
| Spot GEX / gamma flip | Unusual Whales | `/api/stock/{ticker}/spot-exposures` |
| Flow direction | Unusual Whales | `/api/stock/{ticker}/greek-flow` |
| IV percentile | Unusual Whales | `/api/stock/{ticker}/greek-flow` |
| Dark pool sentiment | Unusual Whales | `/api/darkpool/{ticker}` |
| Live option quote | IBKR (primary) | `ib_async reqMktData` |
| Option quote fallback | Unusual Whales | `/api/stock/{ticker}/option-contracts` |
| Account value | IBKR | `accountValues` |

---

## Scoring

**Composite = Additive × 0.55 + Institutional × 0.45**

| Engine | Key inputs | Weight |
|--------|-----------|--------|
| Additive | Structure, IV pct, GEX regime, dark pool, premium | 55% |
| Institutional | VEX, CHEX, DEX, flow_direction, ask_side | 45% |

Dark pool sentiment is a UW-exclusive bonus layer — not available in FlashAlpha or Barchart.
Ask-side aggressor confirmation feeds both engines.

---

## Signal columns in signals.xlsx

Standard: Timestamp, Symbol, Strike, Right, Expiry, Structure, Intent, Scores, IV Pct, Regime, Sizing

**New vs barchart_pro_bot:**
- `Ask Side` — aggressor paid ask or above (true institutional urgency signal)
- `Premium ($)` — raw dollar premium from UW flow alert
- `Dark Pool` — bullish / bearish / neutral from `/api/darkpool/{ticker}`
- `DEX ($M)` — dealer delta exposure
- `Gamma Flip` — price where GEX sign changes
- `Call Wall / Put Wall` — peak positive / negative GEX strike
- `Flow Dir` — amplifying / dampening / regime_flip / neutral / no_flow
- `Institutional Score` — shown separately (not blended-only)

---

## CLI Modes

```bash
python main.py                  # continuous loop
python main.py --mode check     # API health check — verify tier coverage
python main.py --mode dealer    # GEX snapshot for watchlist
python main.py --mode scan      # single cycle, top 15 signals
```
