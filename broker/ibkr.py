"""
broker/ibkr.py
==============
IBKR ib_async client — live option quotes + account value.

TWS or IB Gateway must be running with API enabled.
  TWS port  : 7497  (paper or live)
  Gateway   : 4001
Set IBKR_PORT in .env.

Always opens readonly=True — no order submission from this bot.
Order execution is a separate concern added later once
the signal quality validation phase is complete.
"""

import logging
from config import cfg

log = logging.getLogger("UWBot.IBKR")

try:
    from ib_async import IB, Option
    IBKR_AVAILABLE = True
except ImportError:
    IBKR_AVAILABLE = False
    log.warning("ib_async not installed — run: pip install ib_async")


class IBKRClient:
    """
    Wraps ib_async for live option snapshot quotes and LLC account value.
    Auto-reconnects on dropped connection.
    readonly=True enforced — zero risk of accidental order submission.
    """

    def __init__(self):
        self.ib = IB() if IBKR_AVAILABLE else None
        self._connected = False

    # ── Connection ────────────────────────────────────────────────────────────
    def connect(self):
        if not IBKR_AVAILABLE:
            log.error("ib_async not installed.")
            return
        try:
            self.ib.connect(
                cfg.IBKR_HOST,
                cfg.IBKR_PORT,
                clientId=cfg.IBKR_CLIENT_ID,
                readonly=True,
            )
            self._connected = True
            log.info(
                f"IBKR connected  {cfg.IBKR_HOST}:{cfg.IBKR_PORT}  "
                f"account={cfg.IBKR_ACCOUNT or 'default'}"
            )
        except Exception as e:
            log.error(f"IBKR connect failed: {e}")
            self._connected = False

    def disconnect(self):
        if self._connected and self.ib:
            self.ib.disconnect()
            self._connected = False
            log.info("IBKR disconnected.")

    def _ensure_connected(self):
        """
        Only reconnect if we were previously connected and then lost it.
        Never retry if initial connection failed -- TWS is not running.
        """
        if self._connected and self.ib and not self.ib.isConnected():
            log.info("IBKR reconnecting...")
            self.connect()
        # _connected=False means initial connect failed -- TWS not open, don't retry

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self.ib is not None
            and self.ib.isConnected()
        )

    # ── Option Quote ──────────────────────────────────────────────────────────
    def get_option_quote(
        self,
        symbol: str,
        expiry_str: str,   # YYYYMMDD  (no hyphens)
        strike: float,
        right: str,        # C or P
    ) -> dict:
        # Fast bail-out: if never connected, don't attempt (TWS not running)
        if not self._connected:
            return {}
        """
        Fetch live bid/ask + model greeks for a single option contract.

        Returns dict with keys:
          bid, ask, last, delta, gamma, vega, theta, iv, underlying_px

        Returns {} on any failure — caller waterfalls to UW chain quote.
        """
        self._ensure_connected()
        if not self.is_connected:
            return {}
        try:
            contract = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=expiry_str,
                strike=strike,
                right=right,
                exchange="SMART",
                currency="USD",
            )
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                log.debug(f"Could not qualify {symbol} {strike}{right} {expiry_str}")
                return {}

            ticker = self.ib.reqMktData(qualified[0], "", False, False)
            self.ib.sleep(2)

            greeks = ticker.modelGreeks
            result = {
                "bid":           ticker.bid  or 0.0,
                "ask":           ticker.ask  or 0.0,
                "last":          ticker.last or 0.0,
                "delta":         greeks.delta      if greeks else 0.0,
                "gamma":         greeks.gamma      if greeks else 0.0,
                "vega":          greeks.vega       if greeks else 0.0,
                "theta":         greeks.theta      if greeks else 0.0,
                "iv":            greeks.impliedVol if greeks else 0.0,
                "underlying_px": greeks.undPrice   if greeks else 0.0,
            }
            self.ib.cancelMktData(qualified[0])
            return result

        except Exception as e:
            log.error(f"Quote error {symbol} {strike}{right}: {e}")
            return {}

    # ── Account Value ─────────────────────────────────────────────────────────
    def get_account_value(self) -> float:
        """
        Return NetLiquidation (USD) for the Pal Initiatives LLC account.
        Falls back to cfg.PORTFOLIO_SIZE if IBKR unreachable.
        """
        self._ensure_connected()
        if not self.is_connected:
            log.warning("IBKR unreachable — using cfg.PORTFOLIO_SIZE")
            return cfg.PORTFOLIO_SIZE
        try:
            av = self.ib.accountValues(account=cfg.IBKR_ACCOUNT or None)
            for v in av:
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    val = float(v.value)
                    log.debug(f"IBKR NetLiquidation: ${val:,.0f}")
                    return val
        except Exception as e:
            log.error(f"Account value error: {e}")
        return cfg.PORTFOLIO_SIZE
