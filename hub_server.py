"""
hub_server.py - Raziel Hub: one page for everything her skills produce (reports, metrics, deliverables).

    http://127.0.0.1:8770/  (HUB_PORT)   - open it with the "Raziel Hub" Desktop shortcut (open_hub.pyw)

The page (hub/index.html, app.css, app.js) is her universe: the Nexus in the middle, the six beings around
it (universes.py), and under it what each one made - deliverables.py manifests, her Obsidian conversation
notes, the turn timings - plus vitals, Facts.md and an ask box.

Only this computer, and only the page: the server listens on 127.0.0.1 only; every /api/ call needs the
token from %APPDATA%\\Raziel\\hub.token (the shortcut puts it in the link's #fragment, which never reaches
the network), a Host header naming this server (no DNS rebinding) and, when a browser sends one, an Origin
of this server - so another website open in the same browser can neither read nor drive her. No CORS
headers are ever sent. The static files carry a strict Content-Security-Policy.

Settings: HUB_ENABLED, HUB_PORT.
"""

import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import config
import deliverables

logger = logging.getLogger("voice_assistant")

HOST = "127.0.0.1"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
MAX_BODY = 8192
ASK_TIMEOUT = 150.0

_server: Optional[ThreadingHTTPServer] = None
_ask: Optional[Callable[[str], str]] = None
_token: Optional[str] = None


def _cfg(name: str, default):
    return getattr(config, name, default)


def port() -> int:
    return int(_cfg("HUB_PORT", 8770))


def _here() -> str:
    return getattr(config, "_SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))


def token_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Raziel", "hub.token")


def token() -> str:
    """The hub's token: made once and kept, so a bookmark of the shortcut's link keeps working."""
    global _token
    if _token:
        return _token
    path = token_path()
    try:
        with open(path, encoding="ascii") as f:
            existing = f.read().strip()
        if len(existing) >= 32:
            _token = existing
            return _token
    except OSError:
        pass
    _token = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(_token)
    return _token


def url(with_token: bool = True) -> str:
    return f"http://{HOST}:{port()}/" + (f"#t={token()}" if with_token else "")


# ================================================================== data for the page

BEING_ORDER = ("music", "messages", "knowledge", "forge", "time", "shield")


def _beings() -> List[dict]:
    try:
        import universes
        nexus = dict(universes.NEXUS)
        out = [nexus] + [dict(b) for b in universes.BEINGS]
    except Exception:  # noqa: BLE001
        return []
    return [{"key": b["key"], "name": b["name"], "title": b["title"], "role": b["role"], "color": b["color"],
             "glyph": b["glyph"], "helpers": list(b.get("helpers") or [])[:5]} for b in out]


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _log_tail(max_bytes: int = 400_000) -> str:
    path = _cfg("LOG_FILE", "assistant.log")
    if not os.path.isabs(path):
        path = os.path.join(_here(), path)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _last_match(pattern: str, text: str):
    found = re.findall(pattern, text)
    return found[-1] if found else None


def _ollama_gpu() -> Optional[dict]:
    """{"model", "gpu": 0..1} for the loaded chat model, from Ollama's /api/ps (None = not loaded / down)."""
    host = str(_cfg("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    try:
        with urllib.request.urlopen(host + "/api/ps", timeout=1.0) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for m in data.get("models") or []:
        if m.get("name", "").startswith(str(_cfg("OLLAMA_MODEL", ""))):
            size = float(m.get("size") or 0)
            return {"model": m.get("name"), "gpu": (float(m.get("size_vram") or 0) / size) if size else 0.0}
    return None


def _count_sql(db: str, sql: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            return int(conn.execute(sql).fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _facts() -> List[str]:
    try:
        import obsidian_memory
        facts = obsidian_memory.facts()
        if facts is not None:
            return facts
    except Exception:  # noqa: BLE001
        pass
    try:
        import memory
        return memory.get_recent_facts(limit=100)
    except Exception:  # noqa: BLE001
        return []


def _notes() -> List[dict]:
    """Her Obsidian conversation notes, as hub items."""
    try:
        import obsidian_memory
        paths = obsidian_memory._notes()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for path in paths[:200]:
        name = os.path.basename(path)[:-3]
        m = re.match(r"(\d{4}-\d\d-\d\d) (\d\d)(\d\d) (.*)", name)
        if not m:
            continue
        text = obsidian_memory._note_text(path)
        summary = text.split("\n", 1)[1] if "\n" in text else ""
        summary = summary.split("## Worth remembering")[0].split("Day:")[0].strip()
        out.append({"id": "note:" + name, "being": "nexus", "kind": "memory", "skill": "conversation-notes",
                    "title": m.group(4), "summary": summary[:300],
                    "created": f"{m.group(1)}T{m.group(2)}:{m.group(3)}:00", "has_body": True})
    return out


def items(being: Optional[str] = None, kind: Optional[str] = None, limit: int = 120) -> List[dict]:
    """Deliverables and conversation notes, newest first."""
    rows = [{"id": d.get("id"), "being": d.get("being", "nexus"), "kind": d.get("kind", "report"),
             "skill": d.get("skill", ""), "title": d.get("title", ""), "summary": d.get("summary", ""),
             "created": d.get("created", ""), "has_body": bool(d.get("files")), "metrics": d.get("metrics") or {}}
            for d in deliverables.load(limit=limit * 2)]
    rows += _notes()
    if being and being != "nexus":
        rows = [r for r in rows if r["being"] == being]
    if kind:
        rows = [r for r in rows if r["kind"] in kind.split(",")]
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    return rows[:limit]


def item_body(item_id: str) -> Optional[str]:
    if item_id.startswith("note:"):
        name = item_id[5:]
        if not re.fullmatch(r"\d{4}-\d\d-\d\d \d{4} [^\\/:*?\"<>|]{1,80}", name):
            return None
        try:
            import obsidian_memory
            root = obsidian_memory.vault()
            if not root:
                return None
            path = os.path.join(root, obsidian_memory.CONVERSATIONS, name + ".md")
            return obsidian_memory._read(path) or None
        except Exception:  # noqa: BLE001
            return None
    return deliverables.body(item_id)


def daily_transcribe(log_text: Optional[str] = None) -> List[dict]:
    """Median seconds from "Transcribing..." to "Transcript:" per day, from the log (and its older parts)."""
    texts = [log_text] if log_text is not None else []
    if log_text is None:
        base = _cfg("LOG_FILE", "assistant.log")
        base = base if os.path.isabs(base) else os.path.join(_here(), base)
        for path in [base + f".{i}" for i in range(int(_cfg("LOG_BACKUPS", 3)), 0, -1)] + [base]:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
            except OSError:
                continue
    by: Dict[str, List[float]] = {}
    start = None
    for line in "\n".join(texts).splitlines():
        if "] Transcribing..." in line:
            start = line[:23]
        elif "] Transcript:" in line and start:
            try:
                a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S,%f")
                b = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                by.setdefault(line[:10], []).append((b - a).total_seconds())
            except ValueError:
                pass
            start = None
    out = []
    for day in sorted(by)[-14:]:
        v = sorted(by[day])
        out.append({"day": day, "median": round(v[len(v) // 2], 1), "turns": len(v)})
    return out


def overview() -> dict:
    log = _log_tail()
    turns = deliverables.turns(50)
    last = next((t for t in reversed(turns) if t.get("total") is not None), None)
    vm = None
    try:
        import psutil
        v = psutil.virtual_memory()
        vm = {"free_gb": round(v.available / 2**30, 1), "total_gb": round(v.total / 2**30, 1)}
    except Exception:  # noqa: BLE001
        pass
    gpu = _ollama_gpu()
    echo = _last_match(r"Mic level while speaking: peak (\d+) raw, (\d+) after echo cancelling \(barge-in threshold (\d+)",
                       log)
    voice = _last_match(r"Voice ID: ([0-9.]+) \(", log)
    state, mode = "unknown", ""
    try:
        import avatar_server
        state = avatar_server.current_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        import quiet_mode
        mode = quiet_mode.mode()
    except Exception:  # noqa: BLE001
        pass
    facts = _facts()
    today = datetime.now().strftime("%Y-%m-%d")
    notes = _notes()
    all_items = deliverables.load(limit=500)
    count = lambda being, kind=None: sum(1 for d in all_items if d.get("being") == being  # noqa: E731
                                         and (kind is None or d.get("kind") == kind))
    playlists = (_read_json(os.path.join(_here(), "spotify_playlists.json")) or {}).get("names") or []
    folders = (_read_json(os.path.join(_here(), "folder_names.json")) or {}).get("folders") or []
    files_indexed = _count_sql(os.path.join(_here(), "file_index.db"), "SELECT COUNT(*) FROM files")
    reminders_due = _count_sql(_cfg("MEMORY_DB_PATH", "memory.db"),
                               "SELECT COUNT(*) FROM reminders WHERE done = 0")
    shots = _cfg("SCREENSHOTS_DIR", "screenshots")
    shots = shots if os.path.isabs(shots) else os.path.join(_here(), shots)
    try:
        n_shots = len([n for n in os.listdir(shots) if n.lower().endswith(".png")])
    except OSError:
        n_shots = 0
    on = lambda x: "on" if x else "off"  # noqa: E731
    fmt = lambda n: "—" if n is None else f"{n:,}"  # noqa: E731
    stats = {
        "nexus": [[f"{last['total']:.1f} s" if last else "—", "last turn"], [str(len(facts)), "facts known"],
                  [str(sum(1 for n in notes if n["created"].startswith(today))), "notes today"]],
        "music": [[str(len(playlists)), "playlists saved"], [on(_cfg("MEDIA_DUCK_ENABLED", True)), "music ducking"],
                  [str(count("music")), "deliverables"]],
        "messages": [[on(_cfg("PHONE_BRIDGE_TOKEN", "")), "phone bridge"],
                     [on(os.path.exists(os.path.join(_here(), "google_token.json"))), "Google linked"],
                     [str(count("messages")), "deliverables"]],
        "knowledge": [[fmt(files_indexed), "files indexed"], [str(count("knowledge", "answer")), "AI answers kept"],
                      [str(len(notes)), "conversation notes"]],
        "forge": [[fmt(len(folders)), "folder names"], [fmt(n_shots), "screenshots"], [str(count("forge")), "deliverables"]],
        "time": [[fmt(reminders_due), "reminders pending"], [str(count("time", "report")), "briefings kept"],
                 [str(count("time")), "deliverables"]],
        "shield": [[voice or "—", "last voice match"], [mode or "off", "quiet mode now"],
                   [on(_cfg("VOICE_ID_ENABLED", True)), "voice ID"]],
    }
    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "state": state, "mode": mode,
        "beings": _beings(), "stats": stats, "facts": facts,
        "vitals": {
            "last_turn": last, "ram": vm, "llm": gpu,
            "echo": {"raw": int(echo[0]), "clean": int(echo[1]), "threshold": int(echo[2])} if echo else None,
        },
    }


def metrics() -> dict:
    return {"days": daily_transcribe(), "turns": list(reversed(deliverables.turns(30)))}


def forget(fact: str) -> bool:
    fact = (fact or "").strip()
    if not fact:
        return False
    removed = False
    try:
        import obsidian_memory
        root = obsidian_memory.vault()
        if root:
            path = os.path.join(root, obsidian_memory.FACTS)
            with obsidian_memory._lock:
                lines = obsidian_memory._read(path).splitlines()
                kept = [ln for ln in lines if obsidian_memory._fact_lines(ln) != [fact]]
                if len(kept) != len(lines):
                    obsidian_memory._write(path, "\n".join(kept) + "\n")
                    removed = True
    except Exception:  # noqa: BLE001
        logger.exception("Hub: forgetting in Facts.md failed")
    try:
        import memory
        conn = memory.connect()
        try:
            cur = conn.execute("DELETE FROM facts WHERE content = ?", (fact,))
            conn.commit()
            removed = removed or cur.rowcount > 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("Hub: forgetting in memory.db failed")
    if removed:
        logger.info("Hub: forgot a fact (%d chars)", len(fact))
    return removed


def ask(text: str) -> dict:
    """Runs `text` like something said to her (the same path as the notch's ask), and keeps the answer."""
    if _ask is None:
        return {"ok": False, "reply": "Raziel can't take questions right now."}
    result: Dict[str, object] = {}

    def run():
        try:
            result["reply"] = _ask(text)
        except Exception:  # noqa: BLE001
            logger.exception("Hub: asking failed")
            result["reply"] = None

    t = threading.Thread(target=run, name="hub-ask", daemon=True)
    t.start()
    t.join(ASK_TIMEOUT)
    reply = result.get("reply")
    if t.is_alive():
        return {"ok": False, "reply": "That's taking a long time. It may still finish."}
    if not isinstance(reply, str) or not reply.strip():
        return {"ok": False, "reply": "Something went wrong."}
    deliverables.publish("hub-ask", text[:120], reply[:300], being="nexus", kind="answer",
                         body=f"# {text}\n\n{reply}\n")
    return {"ok": True, "reply": reply}


# ================================================================== HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RazielHub"
    sys_version = ""

    def log_message(self, fmt, *args):  # the access log would hold the token-less paths only; keep it quiet
        return

    # -- checks
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.lower() in (f"http://127.0.0.1:{self.server.server_port}",
                                  f"http://localhost:{self.server.server_port}")

    def _token_ok(self) -> bool:
        given = self.headers.get("X-Raziel-Token") or ""
        return hmac.compare_digest(given.encode(), token().encode())

    # -- replies
    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _deny(self, code: int = 403):
        self._json(code, {"error": "not allowed"})

    # -- routes
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if not self._host_ok():
            return self._deny(421)
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                with open(os.path.join(_here(), "hub", name), "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": "missing"})
            return self._send(200, body, ctype, {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not (self._origin_ok() and self._token_ok()):
            return self._deny()
        q = {k: v[0] for k, v in parse_qs(query).items()}
        try:
            if path == "/api/overview":
                return self._json(200, overview())
            if path == "/api/items":
                return self._json(200, {"items": items(q.get("being"), q.get("kind"))})
            if path == "/api/item":
                text = item_body(q.get("id", ""))
                return self._json(200 if text is not None else 404, {"body": text})
            if path == "/api/metrics":
                return self._json(200, metrics())
        except Exception:  # noqa: BLE001
            logger.exception("Hub: %s failed", path)
            return self._json(500, {"error": "failed"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._host_ok():
            return self._deny(421)
        if not (self._origin_ok() and self._token_ok()):
            return self._deny()
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "json only"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "too big"})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not isinstance(data, dict):
            return self._json(400, {"error": "bad json"})
        if path == "/api/forget":
            return self._json(200, {"ok": forget(str(data.get("fact", "")))})
        if path == "/api/ask":
            text = str(data.get("text", "")).strip()
            if not text or len(text) > 500:
                return self._json(400, {"ok": False, "reply": "That question is empty or too long."})
            return self._json(200, ask(text))
        return self._json(404, {"error": "not found"})


def start(ask_handler: Optional[Callable[[str], str]] = None) -> bool:
    """Starts the hub in the background (once). False when it is off or the port is taken."""
    global _server, _ask
    _ask = ask_handler
    if not _cfg("HUB_ENABLED", True) or _server is not None:
        return _server is not None
    token()
    try:
        _server = ThreadingHTTPServer((HOST, port()), Handler)
    except OSError as e:
        logger.warning("Hub: couldn't listen on %s:%d (%s) - the hub is off.", HOST, port(), e)
        return False
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, name="hub", daemon=True).start()
    try:
        import shutdown
        shutdown.on_shutdown(stop)
    except Exception:  # noqa: BLE001
        pass
    logger.info("Hub: on at %s (open it with the Raziel Hub shortcut).", url(with_token=False))
    return True


def stop():
    global _server
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:  # noqa: BLE001
            pass
        _server = None
