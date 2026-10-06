# TradeBot — Vercel (paper-only) edition

Live demo: **https://tradebot-paper.vercel.app** (paper trading only — fake money).

This is the hosted, paper-only adaptation of TradeBot. It runs on Vercel's free
(Hobby) plan:

- **What runs on schedule:** a cron job hits `/api/tick` twice a day
  (about 10:30am and 2:30pm ET). If the bot is switched on in the dashboard,
  each tick runs one full paper-trading cycle: refresh prices, check the
  2%-daily-loss kill switch, sweep stop-losses, then apply the chosen strategy
  (MA 20/50 crossover or RSI-14 mean reversion) with the same risk limits as
  the PC version (max 5% per position, 3% stop-loss, max 3 positions).
- **Where state lives:** Vercel Blob as a single `state.json` file
  (serverless functions have no persistent disk). Paper cash, positions,
  trades, and logs persist between ticks.
- **Data:** free yfinance 15-minute bars by default; if Alpaca paper API keys
  are set as Vercel env vars (`ALPACA_KEY` / `ALPACA_SECRET`), the bot uses
  Alpaca's paper account instead. No live-trading code paths exist here —
  `/api/mode` always refuses, and the dashboard has no live controls.
- **Limits of the free plan:** Hobby cron jobs run at most once per day per
  job, so this uses two daily jobs. Timing is approximate (±1 hour).

## Honest note

Paper results are practice, not profit promises. The PC backtest lost $35 on
one strategy and made $42 on the other over 60 days — tiny numbers either way.
This bot follows simple public rules; it cannot predict the market. Never
trade money you can't afford to lose. Real-money trading stays on the PC
version (`main` branch), which keeps keys on your own machine.
