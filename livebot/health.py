"""Minimal Koyeb/Render-compatible TCP + HTTP health endpoint."""
from __future__ import annotations
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK - Public Live Recorder"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, _format, *_args):
        return

def start_health_server() -> ThreadingHTTPServer:
    """Bind Koyeb's required PORT in a daemon thread before Telegram starts."""
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    log.info("Health server listening on 0.0.0.0:%s", port)
    return server
