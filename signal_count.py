"""
How often does the strategy fire on recent gold data? (no Telegram, nothing is sent)
Run in your repo folder:   python3 signal_count.py
"""
import collections
import time

import cloud_check as cc
import msnr_fib_backtest as bt

df = cc.get_candles()
sigs = bt.gen_signals(df)
first = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(df["ts"].iloc[0])))
last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(df["ts"].iloc[-1])))
print(f"Data: {len(df)} candles, {first} to {last} UTC")
print(f"Signals found: {len(sigs)}\n")

per_day = collections.Counter()
for i in sorted(sigs):
    side, sl = sigs[i]
    ts = int(df["ts"].iloc[i])
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    per_day[day] += 1
    print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))} UTC  {side:4}  close={df['close'].iloc[i]:.2f}  SL={sl:.2f}")

if sigs:
    days = len({time.strftime('%Y-%m-%d', time.gmtime(int(t))) for t in df["ts"]})
    print(f"\nAbout {len(sigs) / max(days, 1):.1f} signals per trading day, {len(sigs) / max(days, 1) * 5:.1f} per week")
else:
    print("No signals in this data. The rules may be too strict for 15m gold; tell me and we can adjust.")
