"""
One-shot gold signal check for GitHub Actions (free). Runs, checks, sends Telegram if needed, exits.
Uses the SAME rules as msnr_fib_backtest.py (keep both files in the repo).

Environment variables (set as GitHub Secrets / workflow env):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   required
  DATA_SOURCE   yahoo (default) | twelvedata | csv (testing)
  TWELVE_API_KEY  only if DATA_SOURCE=twelvedata
  TF            5m | 15m (default) | 30m | 1h
  DRY_RUN=1     print instead of sending Telegram
Educational only. Not financial advice. Signals only, no trading.
"""
import json
import os
import sys
import time

import pandas as pd
import requests

import msnr_fib_backtest as bt

SYMBOL = os.environ.get("SYMBOL_LABEL", "XAUUSD")
TF = os.environ.get("TF", "15m")
SOURCE = os.environ.get("DATA_SOURCE", "yahoo")
STATE_FILE = "state.json"
LOOKBACK_BARS = 3          # also check the last 3 closed candles, in case GitHub starts a run late


def to_epoch(series):
    t = pd.to_datetime(series, utc=True)
    return ((t - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).astype("int64")


def get_candles():
    """Return CLOSED candles only."""
    if SOURCE == "csv":
        df = pd.read_csv(os.environ["CSV_PATH"])[["ts", "open", "high", "low", "close"]]
        return df.reset_index(drop=True)

    if SOURCE == "twelvedata":
        iv = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[TF]
        r = requests.get("https://api.twelvedata.com/time_series",
                         params=dict(symbol="XAU/USD", interval=iv, outputsize=1000,
                                     order="ASC", timezone="UTC", apikey=os.environ["TWELVE_API_KEY"]),
                         timeout=30)
        j = r.json()
        if "values" not in j:
            raise RuntimeError(f"Twelve Data error: {j}")
        d = pd.DataFrame(j["values"])
        d["ts"] = to_epoch(d["datetime"])
        for c in ("open", "high", "low", "close"):
            d[c] = d[c].astype(float)
        df = d[["ts", "open", "high", "low", "close"]]
    else:
        import yfinance as yf
        iv = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}[TF]
        d = yf.download("GC=F", period="30d" if iv != "1h" else "90d", interval=iv,
                        auto_adjust=False, progress=False)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna().reset_index()
        d.columns = [str(c).lower() for c in d.columns]
        tcol = "datetime" if "datetime" in d.columns else d.columns[0]
        d["ts"] = to_epoch(d[tcol])
        df = d[["ts", "open", "high", "low", "close"]]

    if len(df) < 200:
        raise RuntimeError(f"Not enough candles ({len(df)}). Market closed or data source problem.")
    return df.iloc[:-1].reset_index(drop=True)      # drop the still-forming candle


def send(text):
    if os.environ.get("DRY_RUN") == "1":
        print("[DRY RUN] would send:\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
                      json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=20)
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def run(df, state):
    """Check recent closed candles; send new signals; return updated state."""
    sigs = bt.gen_signals(df)
    n = len(df)
    last = int(state.get("last_signal_ts", 0))
    for i in sorted(sigs):
        if i < n - LOOKBACK_BARS:
            continue
        ts = int(df["ts"].iloc[i])
        if ts <= last:
            continue
        side, sl = sigs[i]
        entry = float(df["close"].iloc[i])
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue
        tp = entry + bt.RR * risk if side == "BUY" else entry - bt.RR * risk
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
        send(f"{side} {SYMBOL} {TF}\n"
             f"Candle closed: {when}\n"
             f"Entry (approx): {entry:.2f}\n"
             f"SL: {sl:.2f}\n"
             f"TP: {tp:.2f}  (1:{bt.RR:g})\n"
             f"Risk distance: {risk:.2f}\n"
             f"Check live price before entering.")
        last = ts
        print(f"Signal sent for candle {when}")
    state["last_signal_ts"] = last
    # Daily heartbeat so GitHub does not disable the schedule after 60 quiet days
    state["heartbeat"] = time.strftime("%Y-%m-%d", time.gmtime())
    return state


def main():
    df = get_candles()
    state = run(df, load_state())
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print(f"Checked {len(df)} candles, last closed: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(df['ts'].iloc[-1])))}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
