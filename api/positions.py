import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json  # noqa: E402
from lib.bot import load_state, positions_payload  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        st = load_state()
        payload = positions_payload(st)
        if isinstance(payload, dict) and "error" in payload:
            send_json(self, payload, 500)
        else:
            send_json(self, payload)

    def log_message(self, *a):
        pass
