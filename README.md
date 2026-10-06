# TradeBot

A simple, honest trading bot. It follows two well-known public trading rules and
protects you with strict risk limits. **It cannot predict the market, and it does
not guarantee profits.** Anyone who tells you otherwise is lying.

## What it does
- **Paper mode (default):** trades with $10,000 of FAKE money so you can learn safely.
- **Live mode:** only if YOU enter Alpaca API keys and confirm twice. Real money, real risk.
- Two strategies you can pick from:
  1. **Trend follow** — buys when the 20-period average crosses above the 50-period average.
  2. **Mean reversion** — buys when RSI(14) drops under 30 (oversold), sells over 70.
- **Risk limits (always on):** max 5% of your account in one trade, 3% stop-loss per
  trade, max 3 open trades, and a 2% daily-loss kill switch that stops the bot for the day.

## Real backtest results (60 days, 15-minute data, $10,000 per symbol)
Ran October 2026 on AAPL, TSLA, SPY, BTC, ETH. Simulated fills, no fees counted.

| Strategy | Trades | Win rate | Total P&L | Worst drawdown |
|---|---|---|---|---|
| Trend follow (MA cross) | 80 | 37.5% | **-$34.94** (lost a little) | 0.80% |
| Mean reversion (RSI) | 43 | 65.1% | **+$41.52** (tiny profit) | 1.22% |

Read that again: one strategy lost money, the other made about $40 on $50,000 of
test capital. **These are practice results, not a promise.** Real trading has fees,
slippage, and worse fills. Never trade money you can't afford to lose.

## Setup (do this once)

1. **Install Python:** go to python.org, download Python 3.11 or newer, and during
   install CHECK the box that says "Add python.exe to PATH".
2. **Unzip** this folder somewhere, like `C:\tradebot`.
3. **Double-click `start.bat`.** It installs what it needs and starts the bot.
   A black window opens and shows you a phone address like `http://192.168.1.5:5000`.
4. **On your phone** (same Wi-Fi as the PC), open that address in your browser.
5. **Paper trade first.** The bot starts OFF and in PAPER mode. Press Start and watch
   it for days before you even think about real money.

## Going live (only when YOU decide)
1. Get FREE API keys at alpaca.markets (paper keys are fine to start).
2. In the app, open **Settings** and paste your key + secret. They're saved only on
   your PC and never shown again.
3. On the **Bot** tab, tap **Go LIVE** and read both warnings. It asks twice on purpose.

Stocks trade 9:30am–4:00pm Eastern on weekdays. Crypto trades 24/7.
