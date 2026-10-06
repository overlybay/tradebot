#!/usr/bin/env python3
"""
TradeBot engine — Vercel serverless adaptation (PAPER MODE ONLY).

Same honest rules-based strategies as the PC version (MA 20/50 crossover,
RSI(14) mean reversion) with the same mandatory risk limits. No live-trading
code paths exist in this deployment. State lives in Vercel Blob as JSON
because serverless functions have no persistent disk.
"""
import copy
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- config
DEFAULTS = {
    "strategy": "ma_cross",
    "symbols": ["AAPL", "TSLA", "NVDA", "SPY", "BTC/USD", "ETH/USD"],
    "risk": {
        "max_position_pct": 5.0,
        "stop_loss_pct": 3.0,
        "daily_max_loss_pct": 2.0,
        "max_positions": 3,
    },
    "starting_cash": 1000000.0,
}

STATE_VERSION = 2

STRATEGY_LABELS = {
    "ma_cross": "Trend follow (MA 20/50 cross)",
    "rsi": "Mean reversion (RSI 14)",
}

ET = ZoneInfo("America/New_York")


def et_now():
    return datetime.now(ET)


def et_today():
    return et_now().strftime("%Y-%m-%d")


def fresh_state():
    return {
        "paper_cash": DEFAULTS["starting_cash"],
        "positions": {},
        "trades": [],
        "logs": [],
        "daily": {},
        "running": False,
        "kill_switch": False,
        "kill_reason": "",
        "last_cycle": None,
        "last_error": None,
        "broker_name": "paper-sim",
        "strategy": DEFAULTS["strategy"],
        "symbols": list(DEFAULTS["symbols"]),
        "risk": copy.deepcopy(DEFAULTS["risk"]),
        "watchlist": [],
        "version": STATE_VERSION,
    }


# ---------------------------------------------------------------- blob state
# State persists as a single JSON blob because serverless functions have no
# disk. Writes go through Vercel's Blob API; reads use the public blob URL.
BLOB_API = "https://vercel.com/api/blob?pathname=tradebot-state.json"
BLOB_PATHNAME = "tradebot-state.json"


def _blob_token():
    return os.environ.get("BLOB_READ_WRITE_TOKEN", "")


def _store_id(token):
    # token format: vercel_blob_rw_<storeId>_<secret>
    parts = (token or "").split("_")
    return parts[3] if len(parts) >= 5 else ""


def _public_url(token):
    sid = _store_id(token)
    if not sid:
        return ""
    return f"https://{sid.lower()}.public.blob.vercel-storage.com/{BLOB_PATHNAME}"


def load_state():
    token = _blob_token()
    url = os.environ.get("TRADEBOT_STATE_URL", "") or _public_url(token)
    st = fresh_state()
    if not url:
        return st
    stored_version = STATE_VERSION  # default: nothing stored yet -> fresh $1M
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                stored_version = data.get("version", 1)
                st.update(data)
    except Exception:
        pass
    if stored_version < STATE_VERSION:
        # One-time migration: $1,000,000 paper account. Keeps the user's
        # strategy, watchlist and risk settings; clears money/positions.
        keep = {k: st.get(k) for k in
                ("strategy", "symbols", "risk", "running", "logs", "watchlist")}
        migrated = fresh_state()
        for k, v in keep.items():
            if v is not None:
                migrated[k] = v
        st = migrated
        add_log(st, "INFO", "Migrated paper account to $1,000,000 starting cash.")
        try:
            save_state(st)
        except Exception:
            pass
    return st


def save_state(st):
    token = _blob_token()
    sid = _store_id(token)
    if not token or not sid:
        return False
    try:
        r = requests.put(
            BLOB_API,
            data=json_dumps(st),
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-version": "12",
                "x-vercel-blob-store-id": sid,
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
                "Content-Type": "application/json",
            },
            timeout=25,
        )
        return r.status_code == 200
    except Exception:
        return False


def json_dumps(obj):
    import json
    return json.dumps(obj)


def add_log(st, level, msg):
    st["logs"].append({"ts": et_now().isoformat(timespec="seconds"),
                       "level": level, "msg": msg[:300]})
    st["logs"] = st["logs"][-200:]


def add_trade(st, ts, symbol, side, qty, price, value, reason, strategy, pnl):
    st["trades"].append({"ts": ts, "symbol": symbol, "side": side, "qty": qty,
                         "price": price, "value": value, "reason": reason,
                         "strategy": strategy, "pnl": pnl})
    st["trades"] = st["trades"][-300:]


# ---------------------------------------------------------------- indicators
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def signal_ma_cross(closes, in_position):
    if len(closes) < 52:
        return None
    s20_now, s50_now = sma(closes, 20), sma(closes, 50)
    s20_prev, s50_prev = sma(closes[:-1], 20), sma(closes[:-1], 50)
    if None in (s20_now, s50_now, s20_prev, s50_prev):
        return None
    if not in_position and s20_prev <= s50_prev and s20_now > s50_now:
        return "buy"
    if in_position and s20_prev >= s50_prev and s20_now < s50_now:
        return "sell"
    return None


def signal_rsi(closes, in_position):
    r = rsi(closes, 14)
    if r is None:
        return None
    if not in_position and r < 30:
        return "buy"
    if in_position and r > 70:
        return "sell"
    return None


SIGNALS = {"ma_cross": signal_ma_cross, "rsi": signal_rsi}


def is_crypto(symbol):
    return "/" in symbol


def market_open_now():
    et = et_now()
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# ---------------------------------------------------------------- market data
def bars_yfinance_batch(symbols, limit=120):
    """One batched yfinance call for all symbols. Returns {symbol: [bars]}."""
    import yfinance as yf
    yf_syms = [s.replace("/", "-") for s in symbols]
    df = yf.download(yf_syms, period="60d", interval="15m", progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    out = {}
    for sym, yfs in zip(symbols, yf_syms):
        try:
            import pandas as pd
            if isinstance(df.columns, pd.MultiIndex):
                sub = df[yfs]
            else:
                sub = df
            bars = []
            for ts, row in sub.iterrows():
                try:
                    c = float(row["Close"])
                    if c != c:  # NaN (e.g. stock symbols overnight)
                        continue
                    bars.append({"t": str(ts), "o": float(row["Open"]),
                                 "h": float(row["High"]), "l": float(row["Low"]),
                                 "c": c,
                                 "v": float(row.get("Volume", 0) or 0)})
                except Exception:
                    continue
            bars = bars[-limit:]
            if bars:
                out[sym] = bars
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- brokers
class PaperSimBroker:
    """Simulated fills at last price, settled against Blob state."""
    name = "paper-sim"

    def __init__(self, st):
        self.st = st
        self._prices = {}

    def set_bars(self, bars_map):
        for sym, bars in bars_map.items():
            if bars:
                self._prices[sym] = bars[-1]["c"]

    def last_price(self, symbol):
        return self._prices.get(symbol)

    def get_cash(self):
        return float(self.st.get("paper_cash", DEFAULTS["starting_cash"]))

    def get_positions(self):
        return [{"symbol": s, **p} for s, p in self.st.get("positions", {}).items()]

    def get_equity(self):
        total = self.get_cash()
        for p in self.get_positions():
            px = self.last_price(p["symbol"])
            if px:
                total += p["qty"] * px
        return total

    def market_buy(self, symbol, qty, reason, strategy):
        price = self.last_price(symbol)
        if not price or qty <= 0:
            raise RuntimeError("no price for " + symbol)
        cost = qty * price
        cash = self.get_cash()
        if cost > cash:
            raise RuntimeError(f"not enough cash: need ${cost:,.2f}, have ${cash:,.2f}")
        pos = self.st["positions"].get(symbol)
        ts = et_now().isoformat(timespec="seconds")
        if pos:
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + cost) / new_qty
            pos["qty"] = new_qty
        else:
            self.st["positions"][symbol] = {"qty": qty, "avg_price": price,
                                            "opened_at": ts, "strategy": strategy}
        add_trade(self.st, ts, symbol, "BUY", qty, price, cost, reason, strategy, None)
        self.st["paper_cash"] = cash - cost
        return price

    def market_sell(self, symbol, qty, reason, strategy):
        price = self.last_price(symbol)
        if not price or qty <= 0:
            raise RuntimeError("no price for " + symbol)
        pos = self.st["positions"].get(symbol)
        if not pos or pos["qty"] < qty - 1e-9:
            raise RuntimeError("not enough shares to sell")
        proceeds = qty * price
        pnl = (price - pos["avg_price"]) * qty
        new_qty = pos["qty"] - qty
        if new_qty <= 1e-9:
            del self.st["positions"][symbol]
        else:
            pos["qty"] = new_qty
        ts = et_now().isoformat(timespec="seconds")
        add_trade(self.st, ts, symbol, "SELL", qty, price, proceeds, reason, strategy, pnl)
        self.st["paper_cash"] = self.get_cash() + proceeds
        return price, pnl


class AlpacaPaperBroker:
    """Alpaca PAPER account (never live in this deployment)."""

    name = "alpaca-paper"

    def __init__(self, st, key, secret):
        self.st = st
        self.key = key
        self.secret = secret
        self.base = "https://paper-api.alpaca.markets"
        self.data = "https://data.alpaca.markets"
        self._prices = {}

    def _h(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def get_bars(self, symbols, limit=120):
        out = {}
        stocks = [s for s in symbols if not is_crypto(s)]
        cryptos = [s for s in symbols if is_crypto(s)]
        if stocks:
            r = requests.get(
                f"{self.data}/v2/stocks/bars",
                headers=self._h(),
                params={"symbols": ",".join(stocks), "timeframe": "15Min",
                        "limit": limit, "sort": "asc"},
                timeout=25)
            r.raise_for_status()
            for sym, bars in (r.json().get("bars") or {}).items():
                conv = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                         "c": b["c"], "v": b.get("v", 0)} for b in bars]
                if conv:
                    out[sym] = conv
                    self._prices[sym] = conv[-1]["c"]
        for sym in cryptos:
            csym = sym.replace("/", "")
            r = requests.get(
                f"{self.data}/v2/crypto/{csym}/bars",
                headers=self._h(),
                params={"timeframe": "15Min", "limit": limit, "sort": "asc"},
                timeout=25)
            r.raise_for_status()
            conv = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "c": b["c"], "v": b.get("v", 0)}
                    for b in r.json().get("bars", [])]
            if conv:
                out[sym] = conv
                self._prices[sym] = conv[-1]["c"]
        return out

    def last_price(self, symbol):
        return self._prices.get(symbol)

    def get_account(self):
        r = requests.get(f"{self.base}/v2/account", headers=self._h(), timeout=20)
        r.raise_for_status()
        a = r.json()
        return {"equity": float(a["equity"]), "cash": float(a["cash"])}

    def get_cash(self):
        return self.get_account()["cash"]

    def get_equity(self):
        return self.get_account()["equity"]

    def get_positions(self):
        r = requests.get(f"{self.base}/v2/positions", headers=self._h(), timeout=20)
        r.raise_for_status()
        out = []
        for p in r.json():
            sym = p["symbol"]
            if p.get("asset_class") == "crypto" and sym.endswith("USD"):
                sym = sym[:-3] + "/USD"
            out.append({"symbol": sym, "qty": float(p["qty"]),
                        "avg_price": float(p["avg_entry_price"])})
        return out

    def _order(self, symbol, qty, side):
        sym = symbol.replace("/", "") if is_crypto(symbol) else symbol
        r = requests.post(f"{self.base}/v2/orders", headers=self._h(), json={
            "symbol": sym, "qty": round(qty, 6), "side": side,
            "type": "market", "time_in_force": "gtc"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def market_buy(self, symbol, qty, reason, strategy):
        self._order(symbol, qty, "buy")
        price = self.last_price(symbol) or 0
        ts = et_now().isoformat(timespec="seconds")
        add_trade(self.st, ts, symbol, "BUY", qty, price, qty * price,
                  reason + " [alpaca-paper]", strategy, None)
        return price

    def market_sell(self, symbol, qty, reason, strategy):
        self._order(symbol, qty, "sell")
        price = self.last_price(symbol) or 0
        ts = et_now().isoformat(timespec="seconds")
        add_trade(self.st, ts, symbol, "SELL", qty, price, qty * price,
                  reason + " [alpaca-paper]", strategy, None)
        return price, None


def get_broker(st):
    key = os.environ.get("ALPACA_KEY", "")
    secret = os.environ.get("ALPACA_SECRET", "")
    if key and secret:
        return AlpacaPaperBroker(st, key, secret)
    return PaperSimBroker(st)


# ---------------------------------------------------------------- risk
def risk_allows_buy(broker, symbol, equity, positions, risk):
    if len(positions) >= risk["max_positions"]:
        return False, f"already holding {len(positions)} (max {risk['max_positions']})"
    if any(p["symbol"] == symbol for p in positions):
        return False, f"already holding {symbol}"
    notional = equity * risk["max_position_pct"] / 100.0
    if notional > broker.get_cash():
        return False, f"${notional:,.2f} exceeds cash ${broker.get_cash():,.2f}"
    if notional < 1:
        return False, "position size under $1"
    return True, "ok"


def risk_allows_manual_buy(broker, symbol, notional, equity, positions, risk):
    """Risk gate for user-tapped Buy buttons (paper only)."""
    held = [p for p in positions if p["symbol"] == symbol]
    others = [p for p in positions if p["symbol"] != symbol]
    if not held and len(others) >= risk["max_positions"]:
        return False, f"already holding max {risk['max_positions']} positions"
    max_notional = equity * risk["max_position_pct"] / 100.0
    if notional > max_notional:
        return False, (f"${notional:,.2f} exceeds max position size "
                       f"${max_notional:,.2f} ({risk['max_position_pct']}% of equity)")
    try:
        cash = broker.get_cash()
    except Exception:
        cash = 0
    if notional > cash:
        return False, f"${notional:,.2f} exceeds cash ${cash:,.2f}"
    if notional < 1:
        return False, "minimum order is $1"
    return True, "ok"


def _flat_close(df):
    import pandas as pd
    c = df["Close"]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    return c


def quote_yf(symbol):
    """Return (price, prev_close) for one symbol via yfinance. None-safe."""
    import yfinance as yf
    yfs = symbol.replace("/", "-")
    try:
        df = yf.download(yfs, period="5d", interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return None, None
        closes = [float(c) for c in _flat_close(df).dropna()]
        if not closes:
            return None, None
        return closes[-1], (closes[-2] if len(closes) > 1 else closes[0])
    except Exception:
        return None, None


def quotes_payload(symbols):
    """Batched quotes: [{symbol, price, change, change_pct}]. One yfinance call."""
    import yfinance as yf
    import pandas as pd
    symbols = list(dict.fromkeys(symbols))
    yf_syms = [s.replace("/", "-") for s in symbols]
    out = []
    try:
        df = yf.download(yf_syms, period="5d", interval="1d", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
    except Exception as e:
        return [{"symbol": s, "price": None, "error": str(e)[:100]}
                for s in symbols]
    for sym, yfs in zip(symbols, yf_syms):
        try:
            if isinstance(df.columns, pd.MultiIndex):
                sub = df[yfs]["Close"]
                if isinstance(sub, pd.DataFrame):
                    sub = sub.iloc[:, 0]
            else:
                sub = df["Close"]
            closes = [float(c) for c in sub.dropna()]
            if not closes:
                out.append({"symbol": sym, "price": None})
                continue
            price = closes[-1]
            prev = closes[-2] if len(closes) > 1 else closes[0]
            chg = price - prev
            out.append({"symbol": sym, "price": round(price, 2),
                        "change": round(chg, 2),
                        "change_pct": round(chg / prev * 100, 2) if prev else 0})
        except Exception:
            out.append({"symbol": sym, "price": None})
    return out


def market_payload(symbols, watchlist=None):
    """One batched intraday call for the Stocks watchlist.

    Returns [{symbol, price, change, change_pct, spark:[...], starred}],
    starred symbols first.
    """
    import yfinance as yf
    import pandas as pd
    symbols = list(dict.fromkeys(symbols))
    watchlist = watchlist or []
    yf_syms = [s.replace("/", "-") for s in symbols]
    out = []
    try:
        df = yf.download(yf_syms, period="1d", interval="5m", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
    except Exception as e:
        return [{"symbol": s, "price": None, "starred": s in watchlist,
                 "error": str(e)[:100]} for s in symbols]
    for sym, yfs in zip(symbols, yf_syms):
        try:
            if isinstance(df.columns, pd.MultiIndex):
                sub = df[yfs]
            else:
                sub = df
            closes_s = sub["Close"]
            if isinstance(closes_s, pd.DataFrame):
                closes_s = closes_s.iloc[:, 0]
            opens_s = sub["Open"]
            if isinstance(opens_s, pd.DataFrame):
                opens_s = opens_s.iloc[:, 0]
            closes = [float(c) for c in closes_s.dropna()]
            if not closes:
                out.append({"symbol": sym, "price": None,
                            "starred": sym in watchlist})
                continue
            price = closes[-1]
            try:
                day_open = float(opens_s.dropna().iloc[0])
            except Exception:
                day_open = closes[0]
            if not day_open or day_open != day_open:
                day_open = closes[0]
            chg = price - day_open
            n = len(closes)
            step = max(1, n // 30)
            spark = [round(c, 2) for c in closes[::step]][-30:]
            out.append({"symbol": sym, "price": round(price, 2),
                        "change": round(chg, 2),
                        "change_pct": round(chg / day_open * 100, 2),
                        "spark": spark, "starred": sym in watchlist})
        except Exception:
            out.append({"symbol": sym, "price": None,
                        "starred": sym in watchlist})
    out.sort(key=lambda x: (0 if x.get("starred") else 1))
    return out


def chart_data(symbol, rng="1d"):
    """Intraday bars for the Stocks tab chart. {symbol, price, change, change_pct, bars}."""
    import yfinance as yf
    import pandas as pd
    yfs = symbol.replace("/", "-")
    period, interval = ("1d", "5m") if rng == "1d" else ("5d", "15m")
    try:
        df = yf.download(yfs, period=period, interval=interval,
                         progress=False, auto_adjust=False)
    except Exception as e:
        return {"error": f"chart data failed: {str(e)[:120]}"}
    if df is None or len(df) == 0:
        return {"error": f"no data for {symbol}"}
    closes = _flat_close(df)
    opens = df["Open"]
    if isinstance(opens, pd.DataFrame):
        opens = opens.iloc[:, 0]
    bars = []
    for ts, c in closes.items():
        try:
            cf = float(c)
            if cf != cf:
                continue
            bars.append({"t": str(ts)[:16].replace("T", " "), "c": round(cf, 2)})
        except Exception:
            continue
    if not bars:
        return {"error": f"no data for {symbol}"}
    price = bars[-1]["c"]
    ref = bars[0]["c"]
    try:
        o0 = float(opens.dropna().iloc[0])
        if o0 == o0 and o0 > 0:
            ref = o0
    except Exception:
        pass
    chg = price - ref
    return {"symbol": symbol, "price": price,
            "change": round(chg, 2),
            "change_pct": round(chg / ref * 100, 2) if ref else 0,
            "bars": bars}


def broker_price_for(broker, symbol):
    """Best-effort last price for a manual order; seeds the sim broker."""
    px = None
    try:
        px = broker.last_price(symbol)
    except Exception:
        pass
    if px:
        return px
    if isinstance(broker, PaperSimBroker):
        try:
            bars = bars_yfinance_batch([symbol], 5).get(symbol, [])
            if bars:
                broker.set_bars({symbol: bars})
                return bars[-1]["c"]
        except Exception:
            pass
    price, _prev = quote_yf(symbol)
    if price and isinstance(broker, PaperSimBroker):
        broker.set_bars({symbol: [{"c": price}]})
    return price


def check_daily_kill_switch(st, equity, risk):
    today = et_today()
    day = st["daily"].get(today)
    if day is None:
        st["daily"][today] = {"start_equity": equity}
        start = equity
    else:
        start = day["start_equity"]
    # keep only recent days
    for d in list(st["daily"]):
        if d < today and len(st["daily"]) > 8:
            del st["daily"][d]
    limit = risk["daily_max_loss_pct"] / 100.0
    if start > 0 and equity <= start * (1 - limit):
        return True, (f"DAILY KILL SWITCH: equity ${equity:,.2f} is down "
                      f"{(1 - equity/start)*100:.2f}% from today's start "
                      f"${start:,.2f}. Bot stopped for today.")
    return False, ""


# ---------------------------------------------------------------- one cycle
def run_cycle(st):
    """Run a single paper-trading cycle. Mutates st. Returns a summary dict."""
    summary = {"trades": 0, "errors": []}
    broker = get_broker(st)
    st["broker_name"] = broker.name
    strat_key = st.get("strategy", "ma_cross")
    strat_fn = SIGNALS.get(strat_key, signal_ma_cross)
    strat_label = STRATEGY_LABELS.get(strat_key, strat_key)
    risk = st.get("risk", copy.deepcopy(DEFAULTS["risk"]))

    symbols = st.get("symbols", list(DEFAULTS["symbols"]))
    positions = broker.get_positions()
    watch = list(dict.fromkeys(symbols + [p["symbol"] for p in positions]))
    tradeable = [s for s in watch if is_crypto(s) or market_open_now()]

    bars_map = {}
    if isinstance(broker, AlpacaPaperBroker):
        try:
            bars_map = broker.get_bars(tradeable, 120)
        except Exception as e:
            add_log(st, "ERROR", f"alpaca bars failed: {e}")
            summary["errors"].append(str(e)[:150])
            # fall back to simulator data for pricing
            sim = PaperSimBroker(st)
            try:
                fb = bars_yfinance_batch(tradeable, 120)
                sim.set_bars(fb)
                for s, b in fb.items():
                    bars_map.setdefault(s, b)
            except Exception as e2:
                summary["errors"].append(str(e2)[:150])
    else:
        try:
            bars_map = bars_yfinance_batch(tradeable, 120)
        except Exception as e:
            add_log(st, "ERROR", f"market data failed: {e}")
            summary["errors"].append(str(e)[:150])
            st["last_error"] = str(e)[:200]
            return summary
        broker.set_bars(bars_map)

    if not bars_map:
        add_log(st, "WARN", "no market data this cycle")
        return summary

    try:
        equity = broker.get_equity()
    except Exception as e:
        add_log(st, "ERROR", f"equity read failed: {e}")
        st["last_error"] = str(e)[:200]
        return summary

    killed, why = check_daily_kill_switch(st, equity, risk)
    if killed:
        st["running"] = False
        st["kill_switch"] = True
        st["kill_reason"] = why
        add_log(st, "RISK", why)
        summary["killed"] = True
        return summary

    # 1) stop-loss sweep
    for p in positions:
        px = broker.last_price(p["symbol"])
        if not px:
            continue
        if px <= p["avg_price"] * (1 - risk["stop_loss_pct"] / 100.0):
            try:
                _, pnl = broker.market_sell(p["symbol"], p["qty"], "stop-loss", strat_key)
                pnl_s = f" P&L ${pnl:,.2f}" if pnl is not None else ""
                add_log(st, "RISK", f"STOP-LOSS sold {p['symbol']} @ ${px:,.2f}.{pnl_s}")
                summary["trades"] += 1
            except Exception as e:
                add_log(st, "ERROR", f"stop-loss failed {p['symbol']}: {e}")

    if not st.get("running"):
        return summary

    # 2) strategy signals
    positions = broker.get_positions()
    pos_syms = {p["symbol"] for p in positions}
    for sym, bars in bars_map.items():
        closes = [b["c"] for b in bars]
        sig = strat_fn(closes, sym in pos_syms)
        price = closes[-1]
        if sig == "buy":
            ok, why = risk_allows_buy(broker, sym, equity, positions, risk)
            if not ok:
                add_log(st, "RISK", f"{sym}: buy ignored - {why}")
                continue
            qty = (equity * risk["max_position_pct"] / 100.0) / price
            try:
                fill = broker.market_buy(sym, qty, f"{strat_label} buy signal", strat_key)
                add_log(st, "TRADE", f"BOUGHT {qty:.4f} {sym} @ ${fill:,.2f}")
                summary["trades"] += 1
                positions = broker.get_positions()
                pos_syms = {p["symbol"] for p in positions}
            except Exception as e:
                add_log(st, "ERROR", f"buy failed {sym}: {e}")
        elif sig == "sell" and sym in pos_syms:
            p = next(x for x in positions if x["symbol"] == sym)
            try:
                _, pnl = broker.market_sell(sym, p["qty"], f"{strat_label} sell signal", strat_key)
                pnl_s = f" P&L ${pnl:,.2f}" if pnl is not None else ""
                add_log(st, "TRADE", f"SOLD {p['qty']:.4f} {sym} @ ${price:,.2f}.{pnl_s}")
                summary["trades"] += 1
                positions = broker.get_positions()
                pos_syms = {p["symbol"] for p in positions}
            except Exception as e:
                add_log(st, "ERROR", f"sell failed {sym}: {e}")

    st["last_cycle"] = et_now().isoformat(timespec="seconds")
    st["last_error"] = None
    summary["equity"] = round(broker.get_equity(), 2) if not isinstance(broker, AlpacaPaperBroker) else None
    return summary


# ---------------------------------------------------------------- dashboard data
def status_payload(st):
    try:
        broker = get_broker(st)
        equity = broker.get_equity()
        cash = broker.get_cash()
        err = None
    except Exception as e:
        equity, cash, err = None, None, str(e)[:200]
    trades = st.get("trades", [])
    today = et_today()
    day_pnl = sum(t["pnl"] for t in trades
                  if t.get("pnl") is not None and t.get("ts", "")[:10] == today)
    sells = [t for t in trades if t.get("side") == "SELL" and t.get("pnl") is not None]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    losses = sum(1 for t in sells if t["pnl"] <= 0)
    return {
        "mode": "paper",
        "running": st.get("running", False),
        "kill_switch": st.get("kill_switch", False),
        "kill_reason": st.get("kill_reason", ""),
        "strategy": st.get("strategy", "ma_cross"),
        "strategy_label": STRATEGY_LABELS.get(st.get("strategy", "ma_cross")),
        "broker": st.get("broker_name", "paper-sim"),
        "equity": equity, "cash": cash,
        "daily_pnl": day_pnl, "total_trades": len(trades),
        "wins": wins, "losses": losses,
        "win_rate": (wins / (wins + losses) * 100) if (wins + losses) else None,
        "market_open": market_open_now(),
        "last_cycle": st.get("last_cycle"),
        "last_error": err or st.get("last_error"),
        "keys_set": bool(os.environ.get("ALPACA_KEY") and os.environ.get("ALPACA_SECRET")),
        "vercel": True,
    }


def positions_payload(st):
    try:
        broker = get_broker(st)
    except Exception as e:
        return {"error": str(e)[:200]}
    out = []
    if isinstance(broker, PaperSimBroker):
        tradeable = list(dict.fromkeys(
            st.get("symbols", []) + [p["symbol"] for p in broker.get_positions()]))
        try:
            bars_map = bars_yfinance_batch([s for s in tradeable
                                            if is_crypto(s) or market_open_now()], 5)
            broker.set_bars(bars_map)
        except Exception:
            pass
    else:
        try:
            poss = broker.get_positions()
            syms = [p["symbol"] for p in poss if is_crypto(p["symbol"]) or market_open_now()]
            if syms:
                broker.get_bars(syms, 5)
        except Exception:
            pass
    for p in broker.get_positions():
        px = broker.last_price(p["symbol"])
        if px:
            val = p["qty"] * px
            cost = p["qty"] * p["avg_price"]
            pnl = val - cost
            out.append({"symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"],
                        "price": px, "value": val, "pnl": pnl,
                        "pnl_pct": (pnl / cost * 100) if cost else 0})
        else:
            out.append({"symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"],
                        "price": None, "value": None, "pnl": None, "pnl_pct": None})
    return out
