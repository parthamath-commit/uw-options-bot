"""
persistence/excel.py
====================
Excel signal logger — writes every scored signal to signals.xlsx.

New columns vs barchart_pro_bot version:
  Dark Pool Sentiment  — bullish/bearish/neutral from UW dark pool
  Ask Side             — whether aggressor paid ask or above
  Premium ($)          — raw premium from UW flow alert
  Gamma Flip           — dealer gamma flip price level
  Call Wall / Put Wall — key dealer support/resistance levels

Colour coding:
  Green  = bullish (not blocked)
  Red    = bearish (not blocked)
  Amber  = GEX regime blocked
  Navy header
"""

import logging
from pathlib import Path
from config import cfg
from models import ScoredSignal

log = logging.getLogger("UWBot.Excel")

try:
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False
    log.warning("openpyxl not installed — run: pip install openpyxl")


class ExcelLogger:
    COLUMNS = [
        "Timestamp", "Symbol", "Strike", "Right", "Expiry", "Signal Type", "Rule Type", "ML Validation",
        "Structure", "Ask Side", "Intent", "Premium ($)",
        "UW Score", "Additive", "Institutional", "Composite",
        "IV Pct", "Dark Pool", "GEX ($M)", "DEX ($M)",
        "Regime", "Gamma Flip", "Call Wall", "Put Wall",
        "Flow Dir", "GEX Blocked",
        "Bid", "Ask", "Delta",
        "Contracts", "Max Risk $", "Target Premium", "Stop Premium",
    ]
    WIDTHS = [
        22, 8, 9, 6, 12, 14, 12, 14,
        10, 9, 10, 13,
        9, 9, 13, 10,
        8, 12, 10, 10,
        18, 11, 11, 11,
        12, 11,
        7, 7, 7,
        10, 12, 15, 13,
    ]

    def __init__(self):
        self.path = Path(cfg.EXCEL_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if OPENPYXL_OK:
            self._hdr  = PatternFill("solid", fgColor="1F3864")
            self._bull = PatternFill("solid", fgColor="C6EFCE")
            self._bear = PatternFill("solid", fgColor="FFC7CE")
            self._blok = PatternFill("solid", fgColor="FFEB9C")
            self._ensure_workbook()

    def _ensure_workbook(self):
        if self.path.exists():
            return
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Signals"
        ws.append(self.COLUMNS)
        for i, (cell, w) in enumerate(zip(ws[1], self.WIDTHS), 1):
            cell.fill      = self._hdr
            cell.font      = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        wb.save(self.path)
        log.info(f"Created: {self.path}")

    def _rebuild_workbook(self):
        """Recreate a fresh, valid workbook (used when the file is corrupt)."""
        import datetime as _dt
        if self.path.exists():
            bad_name = self.path.with_name(
                self.path.stem + "_corrupt_{}.xlsx".format(
                    _dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
            try:
                self.path.rename(bad_name)
                log.warning("Corrupt workbook moved to {}".format(bad_name))
            except Exception as e:
                log.error("Could not move corrupt workbook: {}".format(e))
                try:
                    self.path.unlink()
                except Exception:
                    pass
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Signals"
        ws.append(self.COLUMNS)
        for i, (cell, w) in enumerate(zip(ws[1], self.WIDTHS), 1):
            cell.fill      = self._hdr
            cell.font      = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        wb.save(self.path)
        log.info("Rebuilt fresh workbook: {}".format(self.path))

    def log_signal(self, sig: ScoredSignal, _retry: bool = True):
        if not OPENPYXL_OK:
            return
        try:
            wb = openpyxl.load_workbook(self.path)
            ws = wb["Signals"]
            ws.append([
                sig.timestamp,
                sig.symbol,
                sig.strike,
                sig.right,
                sig.expiry,
                sig.structure,
                getattr(sig, "signal_type",   "unidirectional"),
                getattr(sig, "rule_type",     "unidirectional"),
                getattr(sig, "ml_validation", "rule_only"),
                "YES" if sig.ask_side else "NO",
                sig.intent,
                sig.premium,
                sig.uw_score,
                sig.additive_score,
                sig.institutional_score,
                sig.composite_score,
                sig.iv_percentile,
                sig.darkpool_sentiment,
                round(sig.dealer_gex / 1_000_000, 2),
                round(sig.dealer_dex / 1_000_000, 2),
                sig.dealer_regime,
                sig.gamma_flip,
                sig.call_wall,
                sig.put_wall,
                sig.flow_direction,
                "YES" if sig.gex_regime_blocked else "NO",
                sig.live_bid,
                sig.live_ask,
                sig.live_delta,
                sig.suggested_contracts,
                sig.max_loss_dollar,
                sig.target_exit_premium,
                sig.stop_premium,
            ])
            fill = (
                self._blok if sig.gex_regime_blocked
                else self._bull if sig.intent == "bullish"
                else self._bear
            )
            for cell in ws[ws.max_row]:
                cell.fill = fill
            wb.save(self.path)
        except Exception as e:
            log.error(f"Excel write error: {e}")
            if _retry and (
                "not a zip file"  in str(e)
                or "BadZipFile"   in str(e)
                or "File is not a zip" in str(e)
                or "Bad CRC-32"   in str(e)
                or "crc32"        in str(e).lower()
                or "Content_Types" in str(e)          # corrupt archive missing content types
                or "no item named" in str(e).lower()  # generic missing-entry corruption
            ):
                try:
                    self._rebuild_workbook()
                    self.log_signal(sig, _retry=False)
                except Exception as e2:
                    log.error(f"Excel rebuild/retry failed: {e2}")
