import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib.httputil import send_json  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # This deployment is paper-only by design. Live trading stays on the PC app.
        send_json(self, {"error": "This Vercel deployment is paper-only. "
                                 "Live trading runs on the PC version."}, 400)

    def log_message(self, *a):
        pass
