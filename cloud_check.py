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
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

import msnr_fib_backtest as bt

CAMBODIA_TZ = timezone(timedelta(hours=7))


def format_cambodia_time(ts):
    """Convert unix epoch timestamp to Cambodia local time (UTC+7)."""
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(CAMBODIA_TZ)
    return dt.strftime("%Y-%m-%d %I:%M %p (KH)")


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


def send(text, parse_mode="HTML"):
    if os.environ.get("DRY_RUN") == "1":
        print("[DRY RUN] would send:\n" + text)
        return {"dry_run": True, "text": text}
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ValueError(f"Missing Telegram credentials: TELEGRAM_TOKEN={'set' if token else 'missing'}, TELEGRAM_CHAT_ID={'set' if chat_id else 'missing'}")
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def format_signal_message(side, symbol, tf, when, entry, sl, tp, rr, risk, is_test=False):
    is_buy = side.upper() == "BUY"
    action_badge = "🟢 <b>BUY SIGNAL</b>" if is_buy else "🔴 <b>SELL SIGNAL</b>"
    trend_emoji = "📈" if is_buy else "📉"
    test_tag = "🧪 <b>[TEST REPLAY]</b>\n" if is_test else ""

    return (
        f"{test_tag}"
        f"{action_badge} | <b>{symbol}</b> ({tf}) {trend_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <b>Time (Cambodia):</b> <code>{when}</code>\n"
        f"🎯 <b>Entry:</b> <code>{entry:.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl:.2f}</code>\n"
        f"💰 <b>Take Profit:</b> <code>{tp:.2f}</code> (1:{rr:g} R:R)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📏 <b>Risk:</b> <code>{risk:.2f}</code> pts\n"
        f"⚠️ <i>Confirm live spread and price before entry.</i>"
    )


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
        when = format_cambodia_time(ts)
        msg = format_signal_message(side, SYMBOL, TF, when, entry, sl, tp, bt.RR, risk, is_test=False)
        send(msg)
        last = ts
        print(f"Signal sent for candle {when}")
    state["last_signal_ts"] = last
    # Daily heartbeat so GitHub does not disable the schedule after 60 quiet days
    state["heartbeat"] = time.strftime("%Y-%m-%d", time.gmtime())
    return state


def send_last_signal():
    """Find the most recent signal from data and send it immediately (useful for testing Telegram)."""
    df = get_candles()
    sigs = bt.gen_signals(df)
    if not sigs:
        return {"status": "no_signals_found"}
    last_i = max(sigs.keys())
    side, sl = sigs[last_i]
    entry = float(df["close"].iloc[last_i])
    ts = int(df["ts"].iloc[last_i])
    risk = (entry - sl) if side == "BUY" else (sl - entry)
    tp = entry + bt.RR * risk if side == "BUY" else entry - bt.RR * risk
    when = format_cambodia_time(ts)
    msg = format_signal_message(side, SYMBOL, TF, when, entry, sl, tp, bt.RR, risk, is_test=True)
    resp = send(msg)
    return {
        "status": "sent",
        "telegram_response": resp,
        "candle_closed_cambodia": when,
        "side": side,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "risk_distance": round(risk, 2)
    }


def debug_telegram():
    """Inspect bot info and detect recent chat IDs from Telegram."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry_run = os.environ.get("DRY_RUN", "0")
    if not token:
        return {"error": "TELEGRAM_TOKEN is missing or empty in environment variables"}

    r_me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    me_data = r_me.json()

    r_up = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    up_data = r_up.json()

    recent = []
    if up_data.get("ok"):
        for u in up_data.get("result", [])[-10:]:
            msg = u.get("message") or u.get("channel_post") or {}
            c = msg.get("chat")
            if c:
                recent.append({
                    "chat_id": c.get("id"),
                    "chat_type": c.get("type"),
                    "title_or_name": c.get("title") or c.get("first_name"),
                    "username": c.get("username"),
                    "text": msg.get("text")
                })

    return {
        "dry_run": dry_run,
        "configured_chat_id": chat_id,
        "bot_info": me_data,
        "recent_chats_detected": recent
    }


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
