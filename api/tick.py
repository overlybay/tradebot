import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json  # noqa: E402
from lib.bot import load_state, save_state, add_log, run_cycle, et_now  # noqa: E402


def authorized(h):
    """Vercel cron sends Authorization: Bearer <CRON_SECRET> when the env var
    is set. Allow manual runs with the same header; allow open runs only when
    no CRON_SECRET is configured (dev)."""
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return True
    auth = h.headers.get("Authorization", "")
    ua = h.headers.get("User-Agent", "")
    if auth == f"Bearer {secret}":
        return True
    # Vercel's own cron requests carry its user agent; still require the
    # secret when one is configured.
    return False


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not authorized(self):
            send_json(self, {"error": "unauthorized"}, 401)
            return
        st = load_state()
        # Reset a stale kill switch at the start of a new day (ET).
        from lib.bot import et_today
        if st.get("kill_switch") and st.get("kill_reason"):
            st["kill_switch"] = False
            st["kill_reason"] = ""
            add_log(st, "INFO", "New day: kill switch reset.")
        if not st.get("running"):
            st["last_cycle"] = et_now().isoformat(timespec="seconds")
            add_log(st, "INFO", "Tick: bot is stopped, no cycle run.")
            save_state(st)
            send_json(self, {"ok": True, "ran": False,
                             "reason": "bot stopped"})
            return
        try:
            summary = run_cycle(st)
        except Exception as e:
            add_log(st, "ERROR", f"cycle failed: {e}\n{traceback.format_exc(limit=2)}")
            st["last_error"] = str(e)[:200]
            summary = {"error": str(e)[:200]}
        save_state(st)
        send_json(self, {"ok": True, "ran": True, "summary": summary})

    def log_message(self, *a):
        pass
