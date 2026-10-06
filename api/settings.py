import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json, read_json  # noqa: E402
from lib.bot import (load_state, save_state, add_log, DEFAULTS,  # noqa: E402
                     STRATEGY_LABELS)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        st = load_state()
        send_json(self, {
            "keys_set": bool(os.environ.get("ALPACA_KEY") and os.environ.get("ALPACA_SECRET")),
            "strategy": st.get("strategy", DEFAULTS["strategy"]),
            "symbols": st.get("symbols", DEFAULTS["symbols"]),
            "risk": st.get("risk", DEFAULTS["risk"]),
            "strategies": STRATEGY_LABELS,
            "paper_only": True,
        })

    def do_POST(self):
        data = read_json(self)
        st = load_state()
        # No API key storage on the Vercel deployment. Paper Alpaca keys (if
        # Dee ever wants them) go in Vercel project env vars, set by his agent.
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
        send_json(self, {"ok": True})

    def log_message(self, *a):
        pass
