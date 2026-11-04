"""The HTTP side of the bridge. Standard library only, so the drone image gains no dependency."""

import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY_BYTES = 256 * 1024
BIND_RETRY_S = 2.0


class _Handler(BaseHTTPRequestHandler):
    server_version = "uav-ui-bridge"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, *_args):
        pass

    def _send(self, status, body=b"", content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, separators=(",", ":")).encode())

    def _refused_browser(self):
        # The GUI backend is the only client. A page open in any browser on the ground station can
        # reach this address too, and a plain form POST would need no preflight.
        if self.headers.get("Origin"):
            self._json({"status": "rejected", "error": "browser requests are not accepted"}, 403)
            return True
        return False

    def do_GET(self):
        if self._refused_browser():
            return
        api = self.server.api
        url = urlsplit(self.path)
        query = parse_qs(url.query)

        def since(name):
            try:
                return int(query.get(name, ["0"])[0])
            except ValueError:
                return 0

        if url.path == "/info":
            self._json(api.info())
        elif url.path == "/poll":
            self._json(
                api.poll(
                    since("state"),
                    since("events"),
                    since("map"),
                    since("actions"),
                )
            )
        elif url.path.startswith("/image/"):
            name = url.path[len("/image/") :]
            result = api.image(name)
            if result is None:
                self._json({"error": f"no image '{name}'"}, 404)
            elif result[0] is None:
                self._send(204)
            else:
                jpeg, etag = result
                if self.headers.get("If-None-Match") == etag:
                    self._send(304, headers={"ETag": etag})
                else:
                    self._send(200, jpeg, "image/jpeg", {"ETag": etag})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self._refused_browser():
            return
        api = self.server.api
        url = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._json({"status": "rejected", "error": "bad request size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._json({"status": "rejected", "error": "body must be a JSON object"}, 400)
            return

        if url.path.startswith("/commands/"):
            name = url.path[len("/commands/") :]
            self._json(api.run_command(name, body.get("id"), body.get("args")))
        elif url.path.startswith("/actions/") and url.path.endswith("/cancel"):
            action_id = url.path[len("/actions/") : -len("/cancel")]
            self._json(api.cancel_action(action_id))
        else:
            self._json({"error": "not found"}, 404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_forever(api, address, port, logger, should_stop):
    """Binds and serves. The tunnel may come up after the node, so a failed bind is retried."""
    reported = False
    while not should_stop():
        try:
            server = _Server((address, port), _Handler)
            break
        except OSError as exc:
            if not reported:
                logger.warn(f"Cannot bind {address}:{port} yet ({exc}), retrying.")
                reported = True
            time.sleep(BIND_RETRY_S)
    else:
        return None
    server.api = api
    server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    logger.info(f"Listening on {address}:{port}")
    return server
