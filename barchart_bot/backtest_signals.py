import os
import requests
import pandas as pd
import yfinance as yf

from datetime import datetime, timedelta
from dotenv import load_dotenv
from openpyxl import load_workbook

load_dotenv()

CONFIG = {
    "excel_file": r"C:\BarchartBot\signals.xlsx",
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),

    "option_lookahead_days": 7,
    "option_success_pct": 2.0,
}


def send_telegram_message(message):
    token = CONFIG["telegram_bot_token"]
    chat_id = CONFIG["telegram_chat_id"]

    if not token or not chat_id:
        print("Telegram missing.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)


def get_price_history(ticker, start_date, days=10):
    end_date = start_date + timedelta(days=days + 5)

    df = yf.download(
        ticker,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=True,
    )

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


def update_option_signals(wb):
    if "OptionSignals" not in wb.sheetnames:
        return 0, 0, 0

    ws = wb["OptionSignals"]

    updated = 0
    wins = 0
    losses = 0

    headers = {cell.value: idx + 1 for idx, cell in enumerate(ws[1])}

    for row in range(2, ws.max_row + 1):
        status = ws.cell(row, headers["Status"]).value

        if status != "OPEN":
            continue

        ticker = ws.cell(row, headers["Ticker"]).value
        option_type = str(ws.cell(row, headers["Option Type"]).value).lower()
        signal_time = ws.cell(row, headers["DateTime"]).value

        if not ticker or not signal_time:
            continue

        try:
            signal_date = datetime.strptime(str(signal_time)[:10], "%Y-%m-%d")
        except:
            continue

        df = get_price_history(ticker, signal_date, CONFIG["option_lookahead_days"])

        if df.empty:
            continue

        entry_stock_price = float(df.iloc[0]["Close"])

        future_df = df.iloc[1:CONFIG["option_lookahead_days"] + 1]

        if future_df.empty:
            continue

        high_price = float(future_df["High"].max())
        low_price = float(future_df["Low"].min())

        call_target = entry_stock_price * (1 + CONFIG["option_success_pct"] / 100)
        put_target = entry_stock_price * (1 - CONFIG["option_success_pct"] / 100)

        result = None
        exit_price = None

        if "call" in option_type:
            if high_price >= call_target:
                result = "WIN"
                exit_price = round(high_price, 2)
            else:
                result = "LOSS"
                exit_price = round(float(future_df.iloc[-1]["Close"]), 2)

        elif "put" in option_type:
            if low_price <= put_target:
                result = "WIN"
                exit_price = round(low_price, 2)
            else:
                result = "LOSS"
                exit_price = round(float(future_df.iloc[-1]["Close"]), 2)

        if result:
            ws.cell(row, headers["Status"]).value = "CLOSED"
            ws.cell(row, headers["Success Date"]).value = datetime.now().strftime("%Y-%m-%d")
            ws.cell(row, headers["Exit Price"]).value = exit_price
            ws.cell(row, headers["Result"]).value = result

            updated += 1
            if result == "WIN":
                wins += 1
            else:
                losses += 1

    return updated, wins, losses


def update_stock_signals(wb):
    if "StockSignals" not in wb.sheetnames:
        return 0, 0, 0

    ws = wb["StockSignals"]

    updated = 0
    wins = 0
    losses = 0

    headers = {cell.value: idx + 1 for idx, cell in enumerate(ws[1])}

    for row in range(2, ws.max_row + 1):
        status = ws.cell(row, headers["Status"]).value

        if status != "OPEN":
            continue

        ticker = ws.cell(row, headers["Ticker"]).value
        signal_type = str(ws.cell(row, headers["Signal"]).value).upper()
        signal_time = ws.cell(row, headers["DateTime"]).value
        stop_loss = ws.cell(row, headers["Stop Loss"]).value
        target_1 = ws.cell(row, headers["Target 1"]).value

        if not ticker or not signal_time or not stop_loss or not target_1:
            continue

        try:
            signal_date = datetime.strptime(str(signal_time)[:10], "%Y-%m-%d")
            stop_loss = float(stop_loss)
            target_1 = float(target_1)
        except:
            continue

        df = get_price_history(ticker, signal_date, 20)

        if df.empty:
            continue

        future_df = df.iloc[1:]

        result = None
        exit_price = None

        for _, candle in future_df.iterrows():
            high_price = float(candle["High"])
            low_price = float(candle["Low"])
            close_price = float(candle["Close"])

            if signal_type == "BUY":
                if low_price <= stop_loss:
                    result = "LOSS"
                    exit_price = round(stop_loss, 2)
                    break
                if high_price >= target_1:
                    result = "WIN"
                    exit_price = round(target_1, 2)
                    break

            elif signal_type == "SHORT":
                if high_price >= stop_loss:
                    result = "LOSS"
                    exit_price = round(stop_loss, 2)
                    break
                if low_price <= target_1:
                    result = "WIN"
                    exit_price = round(target_1, 2)
                    break

        if result:
            ws.cell(row, headers["Status"]).value = "CLOSED"
            ws.cell(row, headers["Success Date"]).value = datetime.now().strftime("%Y-%m-%d")
            ws.cell(row, headers["Exit Price"]).value = exit_price
            ws.cell(row, headers["Result"]).value = result

            updated += 1
            if result == "WIN":
                wins += 1
            else:
                losses += 1

    return updated, wins, losses


def main():
    if not os.path.exists(CONFIG["excel_file"]):
        print("signals.xlsx not found.")
        return

    wb = load_workbook(CONFIG["excel_file"])

    option_updated, option_wins, option_losses = update_option_signals(wb)
    stock_updated, stock_wins, stock_losses = update_stock_signals(wb)

    wb.save(CONFIG["excel_file"])

    total_updated = option_updated + stock_updated
    total_wins = option_wins + stock_wins
    total_losses = option_losses + stock_losses

    win_rate = round((total_wins / total_updated) * 100, 2) if total_updated > 0 else 0

    message = f"""
📊 BACKTEST UPDATE COMPLETE

Updated Trades: {total_updated}

Options:
Wins: {option_wins}
Losses: {option_losses}

Stocks:
Wins: {stock_wins}
Losses: {stock_losses}

Overall:
Wins: {total_wins}
Losses: {total_losses}
Win Rate: {win_rate}%

Excel Updated:
{CONFIG["excel_file"]}
"""

    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL ERROR:", e)
        input("Press Enter to close...")