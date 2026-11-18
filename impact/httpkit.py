"""Tiny HTTP framework shared by the three services (stdlib only)."""
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class App:
    def __init__(self, name):
        self.name = name
        self.routes = []

    def route(self, method, pattern):
        rx = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn):
            self.routes.append((method, rx, fn))
            return fn

        return deco

    def _handle(self, h, method):
        parsed = urlparse(h.path)
        body = None
        if method in ("POST", "PUT", "PATCH"):
            raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    return _send(h, 400, {"error": "request body is not valid JSON"})
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        for m, rx, fn in self.routes:
            if m != method:
                continue
            match = rx.match(parsed.path)
            if not match:
                continue
            try:
                result = fn(h, body, query, **match.groupdict())
            except Exception as e:  # noqa: BLE001 - report any handler failure as 500
                traceback.print_exc()
                return _send(h, 500, {"error": "%s: %s" % (type(e).__name__, e)})
            if result is None:
                return
            status, payload = result[0], result[1]
            headers = result[2] if len(result) > 2 else {}
            return _send(h, status, payload, headers)
        _send(h, 404, {"error": "not found: %s %s" % (method, parsed.path)})

    def handler_class(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                app._handle(self, "GET")

            def do_POST(self):
                app._handle(self, "POST")

            def log_message(self, fmt, *args):
                pass

        return Handler


def _send(h, status, payload, headers=None):
    headers = dict(headers or {})
    if isinstance(payload, (dict, list)):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    else:
        data = payload
    h.send_response(status)
    for k, v in headers.items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), app.handler_class())
    print("[%s] listening on http://%s:%d" % (app.name, host, port), flush=True)
    httpd.serve_forever()
