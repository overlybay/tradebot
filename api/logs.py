import os
import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json  # noqa: E402
from lib.bot import load_state  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        try:
            lim = min(int(q.get("limit", ["100"])[0]), 500)
        except Exception:
            lim = 100
        st = load_state()
        logs = st.get("logs", [])[-lim:]
        send_json(self, list(reversed(logs)))

    def log_message(self, *a):
        pass
