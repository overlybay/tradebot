#!/usr/bin/env python3
"""
TradeBot — Vercel (paper-only) edition.

Single Flask app (WSGI) serving the dashboard API. The trading engine runs
one discrete cycle per invocation of /api/tick, driven by Vercel Cron.
State persists in Vercel Blob as JSON. No live-trading code paths exist here.
"""
import copy
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, send_file  # noqa: E402

from lib.bot import (  # noqa: E402
    load_state, save_state, add_log, run_cycle, status_payload,
    positions_payload, market_payload, chart_data, broker_price_for,
    risk_allows_manual_buy, get_broker, fresh_state, PaperSimBroker,
    DEFAULTS, STATE_VERSION, STRATEGY_LABELS, et_now, et_today,
)

BASE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(os.path.dirname(BASE), "index.html")

app = Flask(__name__)


@app.route("/")
def index():
    return send_file(DASHBOARD)


@app.route("/api/status")
def api_status():
    return jsonify(status_payload(load_state()))


@app.route("/api/positions")
def api_positions():
    payload = positions_payload(load_state())
    if isinstance(payload, dict) and "error" in payload:
        return jsonify(payload), 500
    return jsonify(payload)


@app.route("/api/trades")
def api_trades():
    try:
        lim = min(int(request.args.get("limit", 100)), 500)
    except Exception:
        lim = 100
    st = load_state()
    return jsonify(list(reversed(st.get("trades", [])[-lim:])))


@app.route("/api/logs")
def api_logs():
    try:
        lim = min(int(request.args.get("limit", 100)), 500)
    except Exception:
        lim = 100
    st = load_state()
    return jsonify(list(reversed(st.get("logs", [])[-lim:])))


@app.route("/api/bot", methods=["POST"])
def api_bot():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    st = load_state()
    if action == "start":
        if st.get("kill_switch"):
            return jsonify({"error": "Kill switch is on for today. It resets tomorrow."}), 400
        st["running"] = True
        st["last_error"] = None
        add_log(st, "INFO", "Bot STARTED from dashboard (paper mode).")
        save_state(st)
        return jsonify({"running": True})
    if action == "stop":
        st["running"] = False
        add_log(st, "INFO", "Bot STOPPED from dashboard. Open positions kept, not sold.")
        save_state(st)
        return jsonify({"running": False})
    return jsonify({"error": "action must be start or stop"}), 400


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    st = load_state()
    if request.method == "GET":
        return jsonify({
            "keys_set": bool(os.environ.get("ALPACA_KEY") and os.environ.get("ALPACA_SECRET")),
            "strategy": st.get("strategy", DEFAULTS["strategy"]),
            "symbols": st.get("symbols", DEFAULTS["symbols"]),
            "risk": st.get("risk", DEFAULTS["risk"]),
            "strategies": STRATEGY_LABELS,
            "paper_only": True,
        })
    data = request.get_json(force=True, silent=True) or {}
    # No API key storage on this deployment. Alpaca paper keys (optional) go
    # in Vercel project env vars, set by Dee's agent.
    if "strategy" in data and data["strategy"] in STRATEGY_LABELS:
        st["strategy"] = data["strategy"]
    if "symbols" in data and isinstance(data["symbols"], list):
        st["symbols"] = [s.strip().upper().replace(" ", "")
                         for s in data["symbols"] if str(s).strip()][:10]
    if "risk" in data and isinstance(data["risk"], dict):
        for k in ("max_position_pct", "stop_loss_pct", "daily_max_loss_pct", "max_positions"):
            if k in data["risk"]:
                try:
                    st["risk"][k] = float(data["risk"][k])
                except Exception:
                    pass
    add_log(st, "INFO", "Settings updated from dashboard.")
    save_state(st)
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    return jsonify({"error": "This Vercel deployment is paper-only. "
                             "Live trading runs on the PC version."}), 400


def _cron_authorized():
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {secret}"


@app.route("/api/tick", methods=["GET"])
def api_tick():
    if not _cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    st = load_state()
    if st.get("kill_switch"):
        st["kill_switch"] = False
        st["kill_reason"] = ""
        add_log(st, "INFO", "New day: kill switch reset.")
    if not st.get("running"):
        st["last_cycle"] = et_now().isoformat(timespec="seconds")
        add_log(st, "INFO", "Tick: bot is stopped, no cycle run.")
        save_state(st)
        return jsonify({"ok": True, "ran": False, "reason": "bot stopped"})
    try:
        summary = run_cycle(st)
    except Exception as e:
        add_log(st, "ERROR", f"cycle failed: {e}\n{traceback.format_exc(limit=2)}")
        st["last_error"] = str(e)[:200]
        summary = {"error": str(e)[:200]}
    save_state(st)
    return jsonify({"ok": True, "ran": True, "summary": summary})


# ---------------------------------------------------------------- stocks tab
_market_cache = {}
_chart_cache = {}


@app.route("/api/market")
def api_market():
    """Watchlist data: price, day change, sparkline for every tracked symbol."""
    st = load_state()
    symbols = st.get("symbols", list(DEFAULTS["symbols"]))
    watchlist = st.get("watchlist", [])
    key = ",".join(symbols)
    now = time.time()
    hit = _market_cache.get(key)
    if hit and now - hit[0] < 45:
        data = [dict(r) for r in hit[1]]
    else:
        data = market_payload(symbols, watchlist)
        _market_cache[key] = (now, [dict(r) for r in data])
    for row in data:
        row["starred"] = row["symbol"] in watchlist
    data.sort(key=lambda r: (0 if r.get("starred") else 1))
    return jsonify(data)


@app.route("/api/chart")
def api_chart():
    symbol = request.args.get("symbol", "").strip().upper().replace(" ", "")
    rng = request.args.get("range", "1d")
    if rng not in ("1d", "5d"):
        rng = "1d"
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    key = (symbol, rng)
    now = time.time()
    hit = _chart_cache.get(key)
    if hit and now - hit[0] < 60:
        return jsonify(hit[1])
    payload = chart_data(symbol, rng)
    if "error" in payload:
        return jsonify(payload), 400
    _chart_cache[key] = (now, payload)
    return jsonify(payload)


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist():
    data = request.get_json(force=True, silent=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper().replace(" ", "")
    starred = bool(data.get("starred", True))
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    st = load_state()
    wl = list(st.get("watchlist", []))
    if starred and symbol not in wl:
        wl.append(symbol)
    elif not starred and symbol in wl:
        wl.remove(symbol)
    st["watchlist"] = wl
    save_state(st)
    return jsonify({"ok": True, "watchlist": wl})


@app.route("/api/order", methods=["POST"])
def api_order():
    """Manual Buy/Sell simulation (paper only). Routes through broker + risk."""
    data = request.get_json(force=True, silent=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper().replace(" ", "")
    side = str(data.get("side", "")).lower()
    try:
        notional = float(data.get("notional_usd", 0))
    except (TypeError, ValueError):
        notional = 0
    if side not in ("buy", "sell"):
        return jsonify({"error": "side must be buy or sell"}), 400
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    st = load_state()
    if st.get("kill_switch"):
        return jsonify({"error": "Kill switch is on for today. Manual orders blocked."}), 400
    broker = get_broker(st)
    price = broker_price_for(broker, symbol)
    if not price:
        return jsonify({"error": f"no price available for {symbol}"}), 400
    risk = st.get("risk", copy.deepcopy(DEFAULTS["risk"]))
    positions = broker.get_positions()
    try:
        equity = broker.get_equity()
    except Exception as e:
        return jsonify({"error": f"equity read failed: {str(e)[:120]}"}), 500
    ts = et_now().isoformat(timespec="seconds")
    if side == "buy":
        ok, why = risk_allows_manual_buy(broker, symbol, notional, equity,
                                         positions, risk)
        if not ok:
            add_log(st, "RISK", f"MANUAL BUY {symbol} rejected: {why}")
            save_state(st)
            return jsonify({"error": why}), 400
        qty = notional / price
        try:
            fill = broker.market_buy(symbol, qty, "manual", "manual")
        except Exception as e:
            return jsonify({"error": str(e)[:200]}), 400
        add_log(st, "TRADE", f"MANUAL BOUGHT {qty:.4f} {symbol} @ ${fill:,.2f}")
        save_state(st)
        return jsonify({"ok": True, "side": "buy", "symbol": symbol,
                        "qty": round(qty, 6), "price": round(fill, 2)})
    pos = next((p for p in positions if p["symbol"] == symbol), None)
    if not pos:
        return jsonify({"error": f"no position in {symbol} to sell"}), 400
    qty = min(notional / price, pos["qty"])
    if qty <= 0:
        return jsonify({"error": "order too small"}), 400
    try:
        fill, pnl = broker.market_sell(symbol, qty, "manual", "manual")
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400
    pnl_s = f" P&L ${pnl:+,.2f}" if pnl is not None else ""
    add_log(st, "TRADE", f"MANUAL SOLD {qty:.4f} {symbol} @ ${fill:,.2f}.{pnl_s}")
    save_state(st)
    return jsonify({"ok": True, "side": "sell", "symbol": symbol,
                    "qty": round(qty, 6), "price": round(fill, 2), "pnl": pnl})


@app.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    """Ops reset: $1M paper account, keeps strategy/symbols/risk/watchlist."""
    if not _cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    st = load_state()
    keep = {k: st.get(k) for k in
            ("strategy", "symbols", "risk", "watchlist")}
    new = fresh_state()
    for k, v in keep.items():
        if v is not None:
            new[k] = v
    new["version"] = STATE_VERSION
    add_log(new, "INFO", "ADMIN RESET: paper account reset to $1,000,000.")
    save_state(new)
    return jsonify({"ok": True, "paper_cash": new["paper_cash"]})
