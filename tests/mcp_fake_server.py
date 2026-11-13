"""
A tiny MCP server over stdio, for test_mcp.py (no packages, no network).

Tools: echo (read-only), save_note (changes things: needs a spoken yes), fail (returns isError),
slow (sleeps). The tool list comes in two pages, and after the handshake the server pings the client
once, to check that Raziel answers requests from the server.
"""

import json
import sys
import time

NOTES = []
TOOLS = [
    {"name": "echo", "description": "Repeats the text.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "save_note", "title": "Save note", "description": "Saves a note.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Takes a while.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:
        return                                      # a response (to our ping)
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.1"}}})
    elif method == "notifications/initialized":
        print("fake server: ready", file=sys.stderr, flush=True)
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
    elif method == "tools/list":
        if params.get("cursor") == "page2":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[2:]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:2], "nextCursor": "page2"}})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": args.get("text", "")}]}
        elif name == "save_note":
            NOTES.append(args.get("text", ""))
            result = {"content": [{"type": "text", "text": f"Saved note {len(NOTES)}."}]}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "disk full"}], "isError": True}
        elif name == "slow":
            time.sleep(float(args.get("seconds", 5)))
            result = {"content": [{"type": "text", "text": "done"}]}
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {name}"}})
            return
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})


for line in sys.stdin:
    if line.strip():
        handle(json.loads(line))
