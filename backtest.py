#!/usr/bin/env python3
"""
Honest backtest: runs both TradeBot strategies on recent 15-minute data
using the same rules as the live engine (5% position size, 3% stop-loss,
max 3 positions). Prints whatever the numbers actually are - good or bad.

Honest limits of this test:
- Fills are assumed at the bar close. Real fills have slippage and fees.
- No dividends, splits handling, or trading halts are modeled.
- 60 days of 15-min data is a short window; results can be luck.
- A good backtest does NOT mean future profits.
"""
import sys
sys.path.insert(0, ".")
from app import sma, rsi, signal_ma_cross, signal_rsi, bars_yfinance

SYMBOLS = ["AAPL", "TSLA", "SPY", "BTC-USD", "ETH-USD"]
STARTING_CASH = 10000.0


def run(symbol, closes, strat_fn, strat_name):
    cash = STARTING_CASH
    pos = None  # (qty, avg_price)
    trades = []  # realized pnls
    equity_curve = []
    peak = STARTING_CASH
    max_dd = 0.0
    n_positions = 0

    for i in range(52, len(closes)):
        price = closes[i]
        # stop-loss check
        if pos:
            qty, avg = pos
            if price <= avg * 0.97:
                pnl = (price - avg) * qty
                cash += qty * price
                trades.append(pnl)
                pos = None
                n_positions = 0
        in_pos = pos is not None
        sig = strat_fn(closes[:i + 1], in_pos)
        equity = cash + (pos[0] * price if pos else 0)
        if sig == "buy" and not in_pos and n_positions < 3:
            qty = (equity * 0.05) / price
            if qty * price <= cash and qty * price >= 1:
                cash -= qty * price
                pos = (qty, price)
                n_positions += 1
        elif sig == "sell" and in_pos:
            qty, avg = pos
            pnl = (price - avg) * qty
            cash += qty * price
            trades.append(pnl)
            pos = None
            n_positions = 0
        equity = cash + (pos[0] * price if pos else 0)
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    # close any open position at the end
    if pos:
        qty, avg = pos
        pnl = (closes[-1] - avg) * qty
        cash += qty * closes[-1]
        trades.append(pnl)
    wins = sum(1 for t in trades if t > 0)
    losses = sum(1 for t in trades if t <= 0)
    return {
        "symbol": symbol, "strategy": strat_name,
        "trades": len(trades), "wins": wins, "losses": losses,
        "win_rate": (wins / len(trades) * 100) if trades else 0,
        "total_pnl": sum(trades),
        "final": cash,
        "return_pct": (cash - STARTING_CASH) / STARTING_CASH * 100,
        "max_drawdown_pct": max_dd,
    }


def main():
    print("Downloading recent 15-minute data (last ~60 days)...")
    data = {}
    for s in SYMBOLS:
        try:
            bars = bars_yfinance(s.replace("-USD", "/USD") if "USD" in s else s, 1500)
            data[s] = [b["c"] for b in bars]
            print(f"  {s}: {len(data[s])} bars")
        except Exception as e:
            print(f"  {s}: FAILED ({e})")
    print()
    results = []
    for s, closes in data.items():
        for fn, name in [(signal_ma_cross, "ma_cross"), (signal_rsi, "rsi")]:
            r = run(s, closes, fn, name)
            results.append(r)
            print(f"{s:8} {name:8} trades={r['trades']:3} W/L={r['wins']}/{r['losses']} "
                  f"win%={r['win_rate']:5.1f} P&L=${r['total_pnl']:8.2f} "
                  f"ret={r['return_pct']:6.2f}% maxDD={r['max_drawdown_pct']:5.2f}%")
    print()
    for name in ("ma_cross", "rsi"):
        sub = [r for r in results if r["strategy"] == name]
        tpnl = sum(r["total_pnl"] for r in sub)
        tw = sum(r["wins"] for r in sub)
        tl = sum(r["losses"] for r in sub)
        mdd = max((r["max_drawdown_pct"] for r in sub), default=0)
        print(f"COMBINED {name}: {len(sub)} symbols, {tw+tl} trades, "
              f"win%={(tw/(tw+tl)*100) if tw+tl else 0:.1f}, "
              f"total P&L=${tpnl:,.2f} on ${STARTING_CASH:,.0f} per symbol, worst drawdown {mdd:.2f}%")
    print()
    print("Remember: simulated fills, no fees/slippage, short 60-day window.")
    print("A good backtest does NOT predict future profits.")


if __name__ == "__main__":
    main()
