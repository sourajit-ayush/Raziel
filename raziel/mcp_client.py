"""
A small MCP (Model Context Protocol) client: talks to one MCP server over stdio.

WHY NOT THE OFFICIAL `mcp` PACKAGE
---------------------------------
It is asyncio-based and brings pydantic, anyio, httpx and starlette with it; Raziel's venv is pinned
carefully (numpy, onnxruntime) and her tools run in plain threads. The stdio transport is small:
newline-delimited JSON-RPC 2.0 on the server's stdin/stdout. Only what a voice assistant needs is
here: the handshake, tools/list (with paging) and tools/call. Resources, prompts and sampling are not.

THREADS
-------
One reader thread per server matches responses to requests by id, so several tool calls can wait at
once. Writes to stdin are serialised with a lock. A server that exits fails every waiting request at
once (no one waits for a timeout on a dead process).

Requests the server sends US are answered: ping -> {}, roots/list -> no roots, anything else ->
"method not found". Notifications from the server are logged, and tools/list_changed sets a flag the
caller can look at (mcp_tools.py lists the tools again).
"""

import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("voice_assistant")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "raziel", "version": "1.0"}
INIT_TIMEOUT = 120.0          # `npx -y <package>` downloads the package the first time
DEFAULT_CALL_TIMEOUT = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class McpError(Exception):
    """The server answered with an error, timed out, or is not running."""


class McpServer:
    def __init__(self, name: str, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                 popen: Callable = subprocess.Popen):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self._popen = popen
        self.proc = None
        self.server_info: dict = {}
        self.tools_changed = False
        self._ids = itertools.count(1)
        self._pending: Dict[int, dict] = {}
        self._lock = threading.Lock()          # _pending
        self._write_lock = threading.Lock()    # stdin
        self._dead = threading.Event()
        self._stderr_tail: List[str] = []

    # ------------------------------------------------------------------ process

    def _argv(self) -> List[str]:
        # On Windows `npx` / `uvx` are npx.cmd / uvx.exe: CreateProcess needs the real file name.
        exe = shutil.which(self.command) or self.command
        return [exe] + self.args

    def start(self, timeout: float = INIT_TIMEOUT) -> dict:
        """Starts the server and does the MCP handshake. Returns the server's initialize result."""
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env.items()})
        self._dead.clear()
        try:
            self.proc = self._popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, cwd=self.cwd,
                                    creationflags=_NO_WINDOW)
        except OSError as e:
            raise McpError(f"couldn't start {self.command!r}: {e}") from e
        threading.Thread(target=self._read_loop, name=f"mcp-{self.name}-out", daemon=True).start()
        if getattr(self.proc, "stderr", None) is not None:
            threading.Thread(target=self._drain_stderr, name=f"mcp-{self.name}-err", daemon=True).start()
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")
        logger.info("MCP %s: connected to %s %s (protocol %s)", self.name,
                    self.server_info.get("name", "?"), self.server_info.get("version", ""),
                    result.get("protocolVersion", "?"))
        return result

    @property
    def alive(self) -> bool:
        return self.proc is not None and not self._dead.is_set() and self.proc.poll() is None

    def close(self):
        proc, self.proc = self.proc, None
        self._dead.set()
        self._fail_all("the connection was closed")
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()                  # the polite way: most servers exit on EOF
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2)
            return
        except Exception:  # noqa: BLE001
            pass
        if sys.platform == "win32" and getattr(proc, "pid", None):
            # npx starts node under cmd.exe: kill the whole tree, not just the wrapper
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_NO_WINDOW)
        else:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ JSON-RPC

    def _send(self, msg: dict):
        if self.proc is None or self._dead.is_set():
            raise McpError(f"{self.name} is not running")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise McpError(f"{self.name} stopped: {e}") from e

    def notify(self, method: str, params: Optional[dict] = None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
        rid = next(self._ids)
        slot = {"event": threading.Event(), "msg": None}
        with self._lock:
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._send(msg)
            if not slot["event"].wait(timeout):
                try:   # tell the server to stop working on it (the result would be thrown away)
                    self.notify("notifications/cancelled", {"requestId": rid, "reason": "timed out"})
                except McpError:
                    pass
                raise McpError(f"{self.name} didn't answer {method} within {timeout:.0f} s")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        reply = slot["msg"] or {}
        if "error" in reply:
            err = reply["error"] or {}
            raise McpError(err.get("message") or str(err))
        if "result" not in reply:
            raise McpError(reply.get("_closed") or f"{self.name}: no result")
        return reply["result"] or {}

    def _read_loop(self):
        proc = self.proc
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.debug("MCP %s: not JSON on stdout: %.200s", self.name, line)
                    continue
                if isinstance(msg, list):          # a batch (older servers)
                    for m in msg:
                        self._dispatch(m)
                elif isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:  # noqa: BLE001 - the pipe broke: same as the process ending
            logger.debug("MCP %s: reader stopped", self.name, exc_info=True)
        finally:
            self._dead.set()
            _close_quietly(proc.stdout)
            _close_quietly(getattr(proc, "stdin", None))
            tail = " / ".join(self._stderr_tail[-3:])
            self._fail_all(f"{self.name} stopped" + (f": {tail}" if tail else ""))

    def _dispatch(self, msg: dict):
        if "method" not in msg:                        # a response to one of our requests
            with self._lock:
                slot = self._pending.get(msg.get("id"))
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
            return
        method = msg["method"]
        if "id" in msg:                                # the server asks us something
            if method == "ping":
                self._reply(msg["id"], {})
            elif method == "roots/list":
                self._reply(msg["id"], {"roots": []})
            else:
                self._reply_error(msg["id"], -32601, f"Raziel doesn't support {method}")
            return
        if method == "notifications/tools/list_changed":
            self.tools_changed = True
        elif method == "notifications/message":
            p = msg.get("params") or {}
            logger.debug("MCP %s log (%s): %.300s", self.name, p.get("level"), p.get("data"))

    def _reply(self, rid, result):
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        except McpError:
            pass

    def _reply_error(self, rid, code, message):
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})
        except McpError:
            pass

    def _fail_all(self, why: str):
        with self._lock:
            slots = list(self._pending.values())
        for slot in slots:
            if slot["msg"] is None:
                slot["msg"] = {"_closed": why}
            slot["event"].set()

    def _drain_stderr(self):
        stream = self.proc.stderr
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip() if isinstance(raw, bytes) else raw.rstrip()
                if line:
                    self._stderr_tail = (self._stderr_tail + [line])[-20:]
                    logger.debug("MCP %s stderr: %.300s", self.name, line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _close_quietly(stream)

    # ------------------------------------------------------------------ MCP

    def list_tools(self, timeout: float = DEFAULT_CALL_TIMEOUT) -> List[dict]:
        tools, cursor = [], None
        for _ in range(50):                            # (a server that pages forever)
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    def call_tool(self, name: str, arguments: Optional[dict] = None,
                  timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
        """Returns the raw CallToolResult ({"content": [...], "isError": bool, ...})."""
        started = time.time()
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        logger.info("MCP %s.%s took %.1fs", self.name, name, time.time() - started)
        return result


def _close_quietly(stream):
    try:
        if stream is not None:
            stream.close()
    except Exception:  # noqa: BLE001
        pass


def result_text(result: dict) -> str:
    """The readable part of a CallToolResult: its text blocks (and structuredContent if there are none)."""
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(str(res.get("text") or f"[a file: {res.get('uri', '?')}]"))
        elif kind == "resource_link":
            parts.append(f"[{block.get('name') or block.get('uri', 'a link')}]")
        elif kind in ("image", "audio"):
            parts.append(f"[{'an image' if kind == 'image' else 'audio'} - not shown]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(p for p in parts if p).strip()
