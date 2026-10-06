import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def send_json(h, obj, code=200):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def read_json(h):
    try:
        n = int(h.headers.get("Content-Length", 0) or 0)
    except Exception:
        n = 0
    if n <= 0:
        return {}
    try:
        return json.loads(h.rfile.read(n).decode() or "{}")
    except Exception:
        return {}
