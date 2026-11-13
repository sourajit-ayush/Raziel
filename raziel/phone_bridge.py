"""
phone_bridge.py - Phase 6: the phone -> PC half of the bridge. A small local HTTP server that
lets your phone send Raziel a text command, forward a phone notification for her to read aloud,
or fetch a file/document off this PC - the same three things "control my phone" naturally needs
in reverse. See PHONE_BRIDGE_SETUP.md next to this file for the full setup: Tailscale + Tasker
on the phone side, config.PHONE_BRIDGE_TOKEN here.

Routes (all POST/GET bodies and query strings are plain JSON / query params):
  POST /command       {"text": "..."}                       -> {"reply": "..."}
  POST /notification  {"app": "...", "title": "...", "text": "..."} -> {"ok": true}
  GET  /file?name=...                                        -> the file's bytes, or 404 JSON
  GET  /ping                                                 -> {"ok": true} (still needs the token)

SECURITY: every request must carry the shared secret from config.PHONE_BRIDGE_TOKEN in an
"X-Raziel-Token" header, checked with a constant-time comparison. There is no unauthenticated
route, not even the ping - a wrong or missing token always gets a 401 and nothing else. This
server is not meant to be reachable from the open internet: it binds every interface so Tailscale
can reach it, but it should never be forwarded on your router. Treat the token like a password -
anyone who has it can do anything Raziel can do locally (open apps, send WhatsApp/SMS after
answering her own yes/no, even shut the PC down), exactly as if they were speaking to her in the
room.
"""

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config

logger = logging.getLogger("voice_assistant")

_MAX_BODY_BYTES = 64 * 1024   # a command/notification is at most a few KB; refuse anything absurd

_httpd = None
_thread = None

# Set by start(). Plain module-level slots (not handler-instance state) since a fresh _Handler
# instance is created by http.server for every single request.
_command_handler = None        # (text: str) -> str
_notification_handler = None   # (app: str, title: str, body: str) -> None
_file_resolver = None          # (name: str) -> (path_or_None, error_message_or_None)


def _token_ok(given: str) -> bool:
    expected = getattr(config, "PHONE_BRIDGE_TOKEN", "") or ""
    if not expected or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8", "ignore"), expected.encode("utf-8", "ignore"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "Raziel/1.0"

    def log_message(self, *args):
        pass  # the handlers below log the events that actually matter; skip the raw access log

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self) -> bool:
        given = self.headers.get("X-Raziel-Token", "")
        if _token_ok(given):
            return True
        logger.warning("Phone bridge: rejected a request from %s (bad or missing token)", self.client_address[0])
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _read_json_body(self):
        """Returns a dict, or None if it already sent an error response (bad/too-large/absent body
        is NOT an error by itself - an empty body just becomes {})."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            self._send_json(413, {"error": "request too large"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return None

    # --- POST -------------------------------------------------------------------------------

    def do_POST(self):
        if not self._authed():
            return
        path = urlparse(self.path).path
        body = self._read_json_body()
        if body is None:
            return  # _read_json_body already sent the error response

        if path == "/command":
            self._handle_command(body)
        elif path == "/notification":
            self._handle_notification(body)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_command(self, body: dict):
        text = str(body.get("text") or "").strip()
        if not text:
            self._send_json(400, {"error": "missing 'text'"})
            return
        if _command_handler is None:
            self._send_json(503, {"error": "not ready yet"})
            return
        try:
            reply = _command_handler(text)
        except Exception:
            logger.exception("Phone bridge: command handler raised on %r", text)
            reply = "Something went wrong handling that on my end."
        self._send_json(200, {"reply": reply})

    def _handle_notification(self, body: dict):
        app = str(body.get("app") or "").strip()
        title = str(body.get("title") or "").strip()
        text = str(body.get("text") or "").strip()
        if not (title or text):
            self._send_json(400, {"error": "missing 'title'/'text'"})
            return
        if _notification_handler is not None:
            try:
                _notification_handler(app, title, text)
            except Exception:
                logger.exception("Phone bridge: notification handler raised on app=%r title=%r", app, title)
        self._send_json(200, {"ok": True})

    # --- GET --------------------------------------------------------------------------------

    def do_GET(self):
        if not self._authed():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/file":
            self._handle_file(parsed.query)
        elif path == "/ping":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_file(self, query: str):
        name = (parse_qs(query).get("name") or [""])[0].strip()
        if not name:
            self._send_json(400, {"error": "missing 'name'"})
            return
        if _file_resolver is None:
            self._send_json(503, {"error": "not ready yet"})
            return
        try:
            found_path, error = _file_resolver(name)
        except Exception:
            logger.exception("Phone bridge: file resolver raised on %r", name)
            found_path, error = None, "something went wrong looking for that file"
        if not found_path:
            self._send_json(404, {"error": error or f"couldn't find a file called {name}"})
            return
        self._send_file(found_path)

    def _send_file(self, path: str):
        try:
            size = os.path.getsize(path)
        except OSError:
            self._send_json(404, {"error": "that file no longer exists"})
            return
        filename = os.path.basename(path).replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            logger.info("Phone bridge: sent file '%s' (%d bytes)", filename, size)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("Phone bridge: file transfer for '%s' was interrupted", filename)


def start(command_handler, notification_handler=None, file_resolver=None) -> bool:
    """Starts the phone-bridge server on a background thread. Returns True if it started, False if
    it's disabled or unconfigured (never raises - a phone-bridge problem must never stop Raziel
    from starting normally). Call stop() to shut it down (main.py registers this with
    shutdown.on_shutdown)."""
    global _httpd, _thread, _command_handler, _notification_handler, _file_resolver

    if not getattr(config, "PHONE_BRIDGE_ENABLED", True):
        logger.info("Phone bridge: disabled (config.PHONE_BRIDGE_ENABLED = False)")
        return False
    if not getattr(config, "PHONE_BRIDGE_TOKEN", ""):
        logger.info("Phone bridge: not starting - config.PHONE_BRIDGE_TOKEN is empty. Generate one "
                    "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`) and paste "
                    "it into config.py to turn this on.")
        return False

    _command_handler = command_handler
    _notification_handler = notification_handler
    _file_resolver = file_resolver

    port = int(getattr(config, "PHONE_BRIDGE_PORT", 8765))
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as e:
        logger.error("Phone bridge: couldn't bind port %d (%s) - is it already in use?", port, e)
        return False

    httpd.daemon_threads = True
    _httpd = httpd
    _thread = threading.Thread(target=httpd.serve_forever, name="phone-bridge", daemon=True)
    _thread.start()
    logger.info("Phone bridge: listening on port %d (reachable over Tailscale once that's set up)", port)
    return True


def is_running() -> bool:
    return _httpd is not None


def stop():
    global _httpd, _thread
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            logger.exception("Phone bridge: error while stopping")
        _httpd = None
    _thread = None
