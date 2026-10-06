#!/usr/bin/env python3
"""
TradeBot - an HONEST rules-based trading bot.

What it is: a simple bot that follows well-known public trading rules
(moving-average crossover, RSI mean reversion) with strict risk limits.
What it is NOT: a profit predictor. No software can predict markets.
Past paper results do not guarantee future results. Live trading can lose
real money. Always start in PAPER mode.
"""
import json
import os
import socket
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_file

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ENV_PATH = os.path.join(BASE, ".env")
DB_PATH = os.path.join(BASE, "tradebot.db")
DASHBOARD_PATH = os.path.join(BASE, "dashboard.html")

DEFAULT_CONFIG = {
    "mode": "paper",          # "paper" or "live"
    "strategy": "ma_cross",   # "ma_cross" or "rsi"
    "symbols": ["AAPL", "TSLA", "NVDA", "SPY", "BTC/USD", "ETH/USD"],
    "risk": {
        "max_position_pct": 5.0,   # max % of equity in one position
        "stop_loss_pct": 3.0,      # sell if a position drops this far
        "daily_max_loss_pct": 2.0, # stop the bot for the day at this loss
        "max_positions": 3,
    },
    "starting_cash": 10000.0,
    "cycle_seconds": 120,
}

# In-memory runtime state (safe defaults: bot OFF at startup)
state = {
    "running": False,
    "kill_switch": False,
    "kill_reason": "",
    "last_cycle": None,
    "last_error": None,
    "broker_name": "paper-sim",
}

db_lock = threading.Lock()


# ---------------- config / secrets ----------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            merged["risk"] = dict(DEFAULT_CONFIG["risk"], **cfg.get("risk", {}))
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_env():
    out = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    return out


def save_env(data):
    # NEVER log these values anywhere.
    with open(ENV_PATH, "w") as f:
        for k, v in data.items():
            f.write(f"{k}={v}\n")
    os.chmod(ENV_PATH, 0o600)


config = load_config()


# ---------------- database ----------------
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT,
        qty REAL, price REAL, value REAL, reason TEXT, strategy TEXT, pnl REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS positions(
        symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL, opened_at TEXT, strategy TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS daily(
        date TEXT PRIMARY KEY, start_equity REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS kv(
        key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    con.close()


def log(level, msg):
    # msg must NEVER contain API keys.
    with db_lock:
        con = db()
        con.execute("INSERT INTO logs(ts, level, msg) VALUES(?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), level, msg))
        con.commit()
        con.close()
    print(f"[{level}] {msg}", flush=True)


def kv_get(key, default=None):
    with db_lock:
        con = db()
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        con.close()
    return row["value"] if row else default


def kv_set(key, value):
    with db_lock:
        con = db()
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, str(value)))
        con.commit()
        con.close()


# ---------------- indicators (plain math, no magic) ----------------
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


def is_crypto(symbol):
    return "/" in symbol


def market_open_now():
    """US stocks trade Mon-Fri 9:30a-4:00p Eastern. Crypto trades 24/7."""
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# ---------------- market data ----------------
def bars_yfinance(symbol, limit=120):
    """Free 15-minute bars, no API key needed. Used by the paper simulator."""
    import yfinance as yf
    yf_sym = symbol.replace("/", "-")  # BTC/USD -> BTC-USD
    df = yf.download(yf_sym, period="60d", interval="15m", progress=False,
                     auto_adjust=False)
    if df is None or len(df) == 0:
        raise RuntimeError(f"No data for {symbol}")
    if hasattr(df.columns, "get_level_values"):
        try:
            df.columns = df.columns.get_level_values(0)
        except Exception:
            pass
    out = []
    for ts, row in df.tail(limit).iterrows():
        out.append({"t": str(ts), "o": float(row["Open"]), "h": float(row["High"]),
                    "l": float(row["Low"]), "c": float(row["Close"]),
                    "v": float(row.get("Volume", 0) or 0)})
    return out


# ---------------- brokers ----------------
class PaperSimBroker:
    """Simulated broker: fake fills at the latest market price.

    Honest limits: fills are assumed instant at the last price. Real markets
    have slippage, partial fills, and fees. Paper profits are practice, not
    a promise.
    """
    name = "paper-sim"

    def __init__(self):
        self._prices = {}

    # -- market data --
    def get_bars(self, symbol, limit=120):
        bars = bars_yfinance(symbol, limit)
        self._prices[symbol] = bars[-1]["c"]
        return bars

    def last_price(self, symbol):
        return self._prices.get(symbol)

    # -- account --
    def get_cash(self):
        v = kv_get("paper_cash")
        if v is None:
            v = config["starting_cash"]
            kv_set("paper_cash", v)
        return float(v)

    def set_cash(self, v):
        kv_set("paper_cash", v)

    def get_positions(self):
        with db_lock:
            con = db()
            rows = con.execute("SELECT * FROM positions").fetchall()
            con.close()
        return [dict(r) for r in rows]

    def get_equity(self):
        cash = self.get_cash()
        total = cash
        for p in self.get_positions():
            px = self.last_price(p["symbol"])
            if px:
                total += p["qty"] * px
        return total

    # -- orders (simulated fills) --
    def market_buy(self, symbol, qty, reason, strategy):
        price = self.last_price(symbol)
        if not price or qty <= 0:
            raise RuntimeError("no price for " + symbol)
        cost = qty * price
        cash = self.get_cash()
        if cost > cash:
            raise RuntimeError(f"not enough cash: need ${cost:,.2f}, have ${cash:,.2f}")
        with db_lock:
            con = db()
            row = con.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            if row:
                new_qty = row["qty"] + qty
                new_avg = (row["qty"] * row["avg_price"] + cost) / new_qty
                con.execute("UPDATE positions SET qty=?, avg_price=? WHERE symbol=?",
                            (new_qty, new_avg, symbol))
            else:
                con.execute("INSERT INTO positions(symbol, qty, avg_price, opened_at, strategy)"
                            " VALUES(?,?,?,?,?)",
                            (symbol, qty, price, datetime.now().isoformat(timespec="seconds"), strategy))
            con.execute("INSERT INTO trades(ts, symbol, side, qty, price, value, reason, strategy, pnl)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), symbol, "BUY",
                         qty, price, cost, reason, strategy, None))
            con.commit()
            con.close()
        self.set_cash(cash - cost)
        return price

    def market_sell(self, symbol, qty, reason, strategy):
        price = self.last_price(symbol)
        if not price or qty <= 0:
            raise RuntimeError("no price for " + symbol)
        with db_lock:
            con = db()
            row = con.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            if not row or row["qty"] < qty - 1e-9:
                con.close()
                raise RuntimeError("not enough shares to sell")
            proceeds = qty * price
            pnl = (price - row["avg_price"]) * qty
            new_qty = row["qty"] - qty
            if new_qty <= 1e-9:
                con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            else:
                con.execute("UPDATE positions SET qty=? WHERE symbol=?", (new_qty, symbol))
            con.execute("INSERT INTO trades(ts, symbol, side, qty, price, value, reason, strategy, pnl)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), symbol, "SELL",
                         qty, price, proceeds, reason, strategy, pnl))
            con.commit()
            con.close()
        self.set_cash(self.get_cash() + proceeds)
        return price, pnl


class AlpacaBroker:
    """Real broker via Alpaca REST. Paper URL for practice, live URL for real money."""
    def __init__(self, key, secret, paper=True):
        self.key = key
        self.secret = secret
        self.base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.data = "https://data.alpaca.markets"
        self.name = "alpaca-paper" if paper else "alpaca-LIVE"
        self._prices = {}

    def _h(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def get_bars(self, symbol, limit=120):
        if is_crypto(symbol):
            sym = symbol.replace("/", "")
            url = f"{self.data}/v2/crypto/{sym}/bars"
        else:
            url = f"{self.data}/v2/stocks/{symbol}/bars"
        r = requests.get(url, headers=self._h(),
                         params={"timeframe": "15Min", "limit": limit, "sort": "asc"},
                         timeout=20)
        r.raise_for_status()
        bars = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                 "c": b["c"], "v": b.get("v", 0)} for b in r.json().get("bars", [])]
        if not bars:
            raise RuntimeError(f"No Alpaca bars for {symbol}")
        self._prices[symbol] = bars[-1]["c"]
        return bars

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
            out.append({"symbol": p["symbol"].replace("USD", "/USD") if p["asset_class"] == "crypto"
                        else p["symbol"],
                        "qty": float(p["qty"]), "avg_price": float(p["avg_entry_price"]),
                        "unrealized_pl": float(p.get("unrealized_pl", 0))})
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
        with db_lock:
            con = db()
            con.execute("INSERT INTO trades(ts, symbol, side, qty, price, value, reason, strategy, pnl)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), symbol, "BUY",
                         qty, price, qty * price, reason + " [alpaca]", strategy, None))
            con.commit()
            con.close()
        return price

    def market_sell(self, symbol, qty, reason, strategy):
        self._order(symbol, qty, "sell")
        price = self.last_price(symbol) or 0
        with db_lock:
            con = db()
            con.execute("INSERT INTO trades(ts, symbol, side, qty, price, value, reason, strategy, pnl)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), symbol, "SELL",
                         qty, price, qty * price, reason + " [alpaca]", strategy, None))
            con.commit()
            con.close()
        return price, None


def get_broker():
    keys = load_env()
    if keys.get("ALPACA_KEY") and keys.get("ALPACA_SECRET"):
        return AlpacaBroker(keys["ALPACA_KEY"], keys["ALPACA_SECRET"],
                            paper=(config["mode"] == "paper"))
    if config["mode"] == "live":
        raise RuntimeError("LIVE mode needs Alpaca API keys first (Settings tab).")
    return PaperSimBroker()


# ---------------- strategies (public, well-known rules) ----------------
def signal_ma_cross(closes, in_position):
    """Buy when the 20-bar average crosses above the 50-bar average (uptrend
    starting). Sell when it crosses back below. Needs 51+ bars."""
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
    """Buy when RSI(14) drops under 30 (oversold bounce idea). Sell when it
    pushes over 70 (overbought)."""
    r = rsi(closes, 14)
    if r is None:
        return None
    if not in_position and r < 30:
        return "buy"
    if in_position and r > 70:
        return "sell"
    return None


STRATEGIES = {
    "ma_cross": ("Trend follow (MA 20/50 cross)", signal_ma_cross),
    "rsi": ("Mean reversion (RSI 14)", signal_rsi),
}


# ---------------- risk manager (mandatory, not optional) ----------------
def risk_allows_buy(broker, symbol, price, equity, positions):
    r = config["risk"]
    if len(positions) >= r["max_positions"]:
        return False, f"blocked: already holding {len(positions)} positions (max {r['max_positions']})"
    if any(p["symbol"] == symbol for p in positions):
        return False, f"blocked: already holding {symbol}"
    notional = equity * r["max_position_pct"] / 100.0
    if notional > broker.get_cash():
        return False, f"blocked: ${notional:,.2f} exceeds cash ${broker.get_cash():,.2f}"
    if notional < 1:
        return False, "blocked: position size under $1"
    return True, "ok"


def check_daily_kill_switch(equity):
    """2% daily max-loss kill switch. Stops the bot for the rest of the day."""
    r = config["risk"]
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock:
        con = db()
        row = con.execute("SELECT start_equity FROM daily WHERE date=?", (today,)).fetchone()
        if row is None:
            con.execute("INSERT INTO daily(date, start_equity) VALUES(?,?)", (today, equity))
            con.commit()
            start = equity
        else:
            start = row["start_equity"]
        con.close()
    limit = r["daily_max_loss_pct"] / 100.0
    if start > 0 and equity <= start * (1 - limit):
        return True, (f"DAILY KILL SWITCH: equity ${equity:,.2f} is down "
                      f"{(1 - equity/start)*100:.2f}% from today's start ${start:,.2f} "
                      f"(limit {r['daily_max_loss_pct']}%). Bot stopped for today.")
    return False, ""


def run_cycle():
    broker = get_broker()
    state["broker_name"] = broker.name
    strat_name, strat_fn = STRATEGIES[config["strategy"]]

    # refresh prices for everything we hold + watch
    positions = broker.get_positions()
    watch = list(dict.fromkeys(config["symbols"] + [p["symbol"] for p in positions]))
    bars_map = {}
    for sym in watch:
        if not is_crypto(sym) and not market_open_now():
            continue  # stocks only trade during market hours
        try:
            bars_map[sym] = broker.get_bars(sym, 120)
        except Exception as e:
            log("WARN", f"data failed for {sym}: {e}")

    equity = broker.get_equity()
    killed, why = check_daily_kill_switch(equity)
    if killed:
        state["running"] = False
        state["kill_switch"] = True
        state["kill_reason"] = why
        log("RISK", why)
        return

    # 1) stop-loss sweep on open positions
    r = config["risk"]
    for p in positions:
        px = broker.last_price(p["symbol"])
        if not px:
            continue
        if px <= p["avg_price"] * (1 - r["stop_loss_pct"] / 100.0):
            try:
                _, pnl = broker.market_sell(p["symbol"], p["qty"], "stop-loss 3%", config["strategy"])
                pnl_s = f" P&L ${pnl:,.2f}" if pnl is not None else ""
                log("RISK", f"STOP-LOSS sold {p['symbol']} @ ${px:,.2f}.{pnl_s}")
            except Exception as e:
                log("ERROR", f"stop-loss failed for {p['symbol']}: {e}")

    if not state["running"]:
        return

    # 2) strategy signals
    positions = broker.get_positions()
    pos_syms = {p["symbol"] for p in positions}
    for sym, bars in bars_map.items():
        closes = [b["c"] for b in bars]
        sig = strat_fn(closes, sym in pos_syms)
        price = closes[-1]
        if sig == "buy":
            ok, why = risk_allows_buy(broker, sym, price, equity, positions)
            if not ok:
                log("RISK", f"{sym}: buy signal ignored - {why}")
                continue
            qty = (equity * r["max_position_pct"] / 100.0) / price
            try:
                fill = broker.market_buy(sym, qty, f"{strat_name} buy signal", config["strategy"])
                log("TRADE", f"BOUGHT {qty:.4f} {sym} @ ${fill:,.2f}")
                positions = broker.get_positions()
                pos_syms = {p["symbol"] for p in positions}
            except Exception as e:
                log("ERROR", f"buy failed {sym}: {e}")
        elif sig == "sell" and sym in pos_syms:
            p = next(x for x in positions if x["symbol"] == sym)
            try:
                _, pnl = broker.market_sell(sym, p["qty"], f"{strat_name} sell signal", config["strategy"])
                pnl_s = f" P&L ${pnl:,.2f}" if pnl is not None else ""
                log("TRADE", f"SOLD {p['qty']:.4f} {sym} @ ${price:,.2f}.{pnl_s}")
                positions = broker.get_positions()
                pos_syms = {p["symbol"] for p in positions}
            except Exception as e:
                log("ERROR", f"sell failed {sym}: {e}")

    state["last_cycle"] = datetime.now().isoformat(timespec="seconds")
    state["last_error"] = None


def engine_loop():
    log("INFO", "TradeBot engine started. Bot is OFF until you press Start.")
    while True:
        try:
            if state["running"]:
                run_cycle()
        except Exception as e:
            state["last_error"] = str(e)[:200]
            log("ERROR", f"cycle failed: {e}\n{traceback.format_exc(limit=3)}")
        time.sleep(config["cycle_seconds"])


# ---------------- web dashboard ----------------
app = Flask(__name__)


@app.route("/")
def index():
    return send_file(DASHBOARD_PATH)


def _positions_with_pnl(broker):
    out = []
    for p in broker.get_positions():
        px = broker.last_price(p["symbol"])
        if px is None:
            try:
                broker.get_bars(p["symbol"], 5)
                px = broker.last_price(p["symbol"])
            except Exception:
                px = None
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


@app.route("/api/status")
def api_status():
    keys = load_env()
    try:
        broker = get_broker()
        equity = broker.get_equity()
        cash = broker.get_cash()
    except Exception as e:
        equity, cash = None, None
        state["last_error"] = str(e)[:200]
    with db_lock:
        con = db()
        day = con.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE date(ts)=date('now','localtime')").fetchone()[0]
        n_trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins = con.execute("SELECT COUNT(*) FROM trades WHERE side='SELL' AND pnl>0").fetchone()[0]
        losses = con.execute("SELECT COUNT(*) FROM trades WHERE side='SELL' AND pnl<=0").fetchone()[0]
        con.close()
    return jsonify({
        "mode": config["mode"],
        "running": state["running"],
        "kill_switch": state["kill_switch"],
        "kill_reason": state["kill_reason"],
        "strategy": config["strategy"],
        "strategy_label": STRATEGIES[config["strategy"]][0],
        "broker": state["broker_name"],
        "equity": equity, "cash": cash,
        "daily_pnl": day, "total_trades": n_trades,
        "wins": wins, "losses": losses,
        "win_rate": (wins / (wins + losses) * 100) if (wins + losses) else None,
        "market_open": market_open_now(),
        "last_cycle": state["last_cycle"],
        "last_error": state["last_error"],
        "keys_set": bool(keys.get("ALPACA_KEY") and keys.get("ALPACA_SECRET")),
    })


@app.route("/api/positions")
def api_positions():
    try:
        return jsonify(_positions_with_pnl(get_broker()))
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/trades")
def api_trades():
    lim = min(int(request.args.get("limit", 100)), 500)
    with db_lock:
        con = db()
        rows = con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
        con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/logs")
def api_logs():
    lim = min(int(request.args.get("limit", 100)), 500)
    with db_lock:
        con = db()
        rows = con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
        con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/bot/start", methods=["POST"])
def api_start():
    if state["kill_switch"]:
        return jsonify({"error": "Kill switch is on for today. It resets tomorrow."}), 400
    state["running"] = True
    state["last_error"] = None
    log("INFO", f"Bot STARTED by user ({config['mode']} mode, {config['strategy']}).")
    return jsonify({"running": True})


@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    log("INFO", "Bot STOPPED by user. Open positions are kept, not sold.")
    return jsonify({"running": False})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    data = request.get_json(force=True)
    mode = data.get("mode")
    if mode not in ("paper", "live"):
        return jsonify({"error": "mode must be paper or live"}), 400
    if mode == "live":
        keys = load_env()
        if not (keys.get("ALPACA_KEY") and keys.get("ALPACA_SECRET")):
            return jsonify({"error": "Enter Alpaca API keys in Settings first."}), 400
        if not data.get("confirm"):
            return jsonify({"error": "Live mode needs explicit confirmation."}), 400
        log("WARN", "LIVE mode enabled by user. Real money is now at risk.")
    config["mode"] = mode
    save_config(config)
    return jsonify({"mode": mode})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        keys = load_env()
        return jsonify({
            "keys_set": bool(keys.get("ALPACA_KEY") and keys.get("ALPACA_SECRET")),
            "strategy": config["strategy"],
            "symbols": config["symbols"],
            "risk": config["risk"],
            "cycle_seconds": config["cycle_seconds"],
            "strategies": {k: v[0] for k, v in STRATEGIES.items()},
        })
    data = request.get_json(force=True)
    # API keys: stored to .env (chmod 600), never returned, never logged.
    if "alpaca_key" in data or "alpaca_secret" in data:
        env = load_env()
        if data.get("alpaca_key"):
            env["ALPACA_KEY"] = data["alpaca_key"].strip()
        if data.get("alpaca_secret"):
            env["ALPACA_SECRET"] = data["alpaca_secret"].strip()
        save_env(env)
        log("INFO", "Alpaca API keys saved (values hidden).")
    if "strategy" in data and data["strategy"] in STRATEGIES:
        config["strategy"] = data["strategy"]
    if "symbols" in data and isinstance(data["symbols"], list):
        config["symbols"] = [s.strip().upper().replace(" ", "") for s in data["symbols"] if s.strip()][:10]
    if "risk" in data and isinstance(data["risk"], dict):
        for k in ("max_position_pct", "stop_loss_pct", "daily_max_loss_pct", "max_positions"):
            if k in data["risk"]:
                config["risk"][k] = float(data["risk"][k])
    if "cycle_seconds" in data:
        config["cycle_seconds"] = max(30, int(data["cycle_seconds"]))
    save_config(config)
    log("INFO", "Settings updated.")
    return jsonify({"ok": True})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    init_db()
    log("INFO", "TradeBot starting. Default: PAPER mode, bot OFF.")
    t = threading.Thread(target=engine_loop, daemon=True)
    t.start()
    ip = lan_ip()
    print("=" * 60, flush=True)
    print(f"  TradeBot is running!", flush=True)
    print(f"  On this PC:  http://localhost:5000", flush=True)
    print(f"  On your phone (same Wi-Fi):  http://{ip}:5000", flush=True)
    print("=" * 60, flush=True)
    app.run(host="0.0.0.0", port=5000, threaded=True)
