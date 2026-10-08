"""
MSNR + Fibonacci BACKTEST (100% free, no account, no API key)

Install:  pip install ccxt pandas matplotlib
Run:      python msnr_fib_backtest.py                       (downloads Binance data)
          python msnr_fib_backtest.py --symbol ETH/USDT --tf 1h --days 730
          python msnr_fib_backtest.py --csv mydata.csv      (columns: ts,open,high,low,close[,vol])

Educational only. Not financial advice.
"""
import argparse
import os
import pandas as pd

# ---------- Strategy settings ----------
SWING = 5
ATR_LEN = 14
RR = 2.0
FIB_LO, FIB_HI = 0.5, 0.786
# ---------- Cost / risk settings ----------
FEE = 0.0005          # 0.05% per side (taker fee); raise it to be more conservative
SLIPPAGE = 0.0002     # 0.02% per side
RISK_PCT = 1.0        # % of equity risked per trade
START_EQUITY = 10000


# ================= Data =================
def fetch(symbol, tf, days):
    import ccxt
    cache = f"{symbol.replace('/', '_')}_{tf}_{days}d.csv"
    if os.path.exists(cache):
        return pd.read_csv(cache)
    ex = ccxt.binance()
    step = ex.parse_timeframe(tf) * 1000
    since = ex.milliseconds() - days * 86400000
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + step
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df = df.drop_duplicates("ts")
    df.to_csv(cache, index=False)
    return df


def atr(df, n):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ================= Signals =================
def gen_signals(df):
    """Return {bar_index: (side, sl)} for every bar where the rules fire (decided on bar close)."""
    a = atr(df, ATR_LEN).values
    h, l, c, o = df["high"].values, df["low"].values, df["close"].values, df["open"].values
    A, V = [], []
    sw_hi = sw_lo = None
    hi_i = lo_i = -1
    signals = {}

    for i in range(SWING * 2 + 2, len(df)):
        p = i - SWING
        if h[p] == max(h[p - SWING:i + 1]):
            sw_hi, hi_i = h[p], p
        if l[p] == min(l[p - SWING:i + 1]):
            sw_lo, lo_i = l[p], p

        if c[i - 1] > c[i - 2] and c[i - 1] > c[i]:
            A.append((c[i - 1], i - 1))
        if c[i - 1] < c[i - 2] and c[i - 1] < c[i]:
            V.append((c[i - 1], i - 1))

        rng = (sw_hi - sw_lo) if (sw_hi is not None and sw_lo is not None) else 0
        up_leg = rng > 0 and lo_i < hi_i
        dn_leg = rng > 0 and hi_i < lo_i
        fired = None

        for lvl, ix in V[:]:
            if i >= ix + 2 and l[i] <= lvl:
                if up_leg and fired is None:
                    top, bot = sw_hi - FIB_LO * rng, sw_hi - FIB_HI * rng
                    if bot <= lvl <= top and c[i] > lvl and c[i] > o[i]:
                        fired = ("BUY", min(l[i], lvl) - 0.5 * a[i])
                V.remove((lvl, ix))
        for lvl, ix in A[:]:
            if i >= ix + 2 and h[i] >= lvl:
                if dn_leg and fired is None:
                    bot, top = sw_lo + FIB_LO * rng, sw_lo + FIB_HI * rng
                    if bot <= lvl <= top and c[i] < lvl and c[i] < o[i]:
                        fired = ("SELL", max(h[i], lvl) + 0.5 * a[i])
                A.remove((lvl, ix))
        if fired:
            signals[i] = fired
    return signals


# ================= Backtest =================
def run_backtest(df, signals):
    h, l, o, c = df["high"].values, df["low"].values, df["open"].values, df["close"].values
    n = len(df)
    trades = []
    busy_until = -1

    for i in sorted(signals):
        if i <= busy_until or i + 1 >= n:
            continue
        side, sl = signals[i]
        entry = o[i + 1]                                   # enter at NEXT bar's open
        entry *= (1 + SLIPPAGE) if side == "BUY" else (1 - SLIPPAGE)
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue
        tp = entry + RR * risk if side == "BUY" else entry - RR * risk

        exit_px, exit_i = c[n - 1], n - 1                  # default: close at end of data
        for j in range(i + 1, n):
            if side == "BUY":
                if l[j] <= sl:                             # SL checked first (conservative)
                    exit_px, exit_i = sl, j
                    break
                if h[j] >= tp:
                    exit_px, exit_i = tp, j
                    break
            else:
                if h[j] >= sl:
                    exit_px, exit_i = sl, j
                    break
                if l[j] <= tp:
                    exit_px, exit_i = tp, j
                    break

        exit_px *= (1 - SLIPPAGE) if side == "BUY" else (1 + SLIPPAGE)
        pnl = (exit_px - entry) if side == "BUY" else (entry - exit_px)
        fee_cost = FEE * (entry + exit_px)                 # per unit, both sides
        r_mult = (pnl - fee_cost) / risk
        trades.append(dict(bar=i + 1, exit_bar=exit_i, side=side, entry=entry,
                           sl=sl, tp=tp, exit=exit_px, r=r_mult))
        busy_until = exit_i
    return trades


def stats(trades, label):
    if not trades:
        print(f"\n[{label}] No trades.")
        return None
    r = pd.Series([t["r"] for t in trades])
    equity, peak, max_dd = START_EQUITY, START_EQUITY, 0.0
    curve = [equity]
    for x in r:
        equity *= 1 + (RISK_PCT / 100) * x
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        curve.append(equity)
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    print(f"\n===== {label} =====")
    print(f"Trades:            {len(r)}")
    print(f"Win rate:          {len(wins) / len(r) * 100:.1f}%")
    print(f"Avg R per trade:   {r.mean():.3f}   (expectancy; must be > 0)")
    print(f"Profit factor:     {pf:.2f}")
    print(f"Max drawdown:      {max_dd * 100:.1f}%")
    print(f"Final equity:      {equity:,.0f}  (start {START_EQUITY:,}, risk {RISK_PCT}%/trade)")
    print(f"Longest losing streak: {max_streak(r)}")
    return curve


def max_streak(r):
    best = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def main():
    global FEE, SLIPPAGE
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--fee", type=float, default=FEE, help="fee per side, e.g. 0.0005 = 0.05%%")
    ap.add_argument("--slip", type=float, default=SLIPPAGE, help="slippage/spread per side")
    args = ap.parse_args()
    FEE, SLIPPAGE = args.fee, args.slip

    df = pd.read_csv(args.csv) if args.csv else fetch(args.symbol, args.tf, args.days)
    df = df.reset_index(drop=True)
    print(f"Loaded {len(df)} candles")

    signals = gen_signals(df)
    trades = run_backtest(df, signals)

    curve = stats(trades, "ALL DATA")

    # In-sample vs out-of-sample (the honest check): first 70% vs last 30%
    cut = int(len(df) * 0.7)
    stats([t for t in trades if t["bar"] < cut], "IN-SAMPLE (first 70%)")
    stats([t for t in trades if t["bar"] >= cut], "OUT-OF-SAMPLE (last 30%)")

    pd.DataFrame(trades).to_csv("trades.csv", index=False)
    print("\nTrade list saved to trades.csv")

    if args.plot and curve:
        import matplotlib.pyplot as plt
        plt.plot(curve)
        plt.title("Equity curve")
        plt.xlabel("Trade #")
        plt.ylabel("Equity")
        plt.grid(True)
        plt.show()


if __name__ == "__main__":
    main()
