import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json, read_json  # noqa: E402
from lib.bot import load_state, save_state, add_log, et_now  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        data = read_json(self)
        action = data.get("action")
        st = load_state()
        if action == "start":
            if st.get("kill_switch"):
                send_json(self, {"error": "Kill switch is on for today. It resets tomorrow."}, 400)
                return
            st["running"] = True
            st["last_error"] = None
            add_log(st, "INFO", "Bot STARTED from dashboard (paper mode).")
            save_state(st)
            send_json(self, {"running": True})
        elif action == "stop":
            st["running"] = False
            add_log(st, "INFO", "Bot STOPPED from dashboard. Open positions kept, not sold.")
            save_state(st)
            send_json(self, {"running": False})
        else:
            send_json(self, {"error": "action must be start or stop"}, 400)

    def log_message(self, *a):
        pass
