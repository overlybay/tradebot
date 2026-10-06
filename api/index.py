#!/usr/bin/env python3
"""
TradeBot — Vercel (paper-only) edition.

Single Flask app (WSGI) serving the dashboard API. The trading engine runs
one discrete cycle per invocation of /api/tick, driven by Vercel Cron.
State persists in Vercel Blob as JSON. No live-trading code paths exist here.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, send_file  # noqa: E402

from lib.bot import (  # noqa: E402
    load_state, save_state, add_log, run_cycle, status_payload,
    positions_payload, DEFAULTS, STRATEGY_LABELS, et_now, et_today,
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
