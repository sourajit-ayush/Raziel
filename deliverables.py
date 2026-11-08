"""
deliverables.py - what Raziel's skills produce, in one place for the hub (hub_server.py).

A deliverable is one small JSON manifest in DELIVERABLES_DIR (deliverables/ by default), with an optional
Markdown body beside it:

    deliverables/20261108-143412-briefing-1a2b.json
    {"id": "20261108-143412-briefing-1a2b", "skill": "briefing", "being": "time", "kind": "report",
     "title": "Afternoon briefing", "summary": "29° in Dhupgari ...", "created": "2026-11-08T14:34:12",
     "files": ["20261108-143412-briefing-1a2b.md"], "metrics": {}}

A skill calls publish(); the hub lists them, newest first, under the being that made it (universes.py):
music, messages, knowledge, forge, time, shield, or nexus (Raziel herself). Nothing here may ever stop a
skill: every function swallows its own errors.

Turn timings go to turns.jsonl in the same folder, one line per turn (record_turn()), for the hub's metrics.
"""

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from typing import Dict, List, Optional

import config

logger = logging.getLogger("voice_assistant")

BEINGS = ("nexus", "music", "messages", "knowledge", "forge", "time", "shield")
KINDS = ("report", "metric", "memory", "event", "answer")
MAX_KEEP = 2000              # oldest manifests (and their files) beyond this are removed
MAX_TURNS_BYTES = 2_000_000  # turns.jsonl starts over (keeping its newest half) beyond this
_lock = threading.Lock()


def folder() -> str:
    path = getattr(config, "DELIVERABLES_DIR", None) or os.path.join(
        getattr(config, "_SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__))), "deliverables")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:24] or "item"


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def publish(skill: str, title: str, summary: str = "", *, being: str = "nexus", kind: str = "report",
            body: Optional[str] = None, metrics: Optional[Dict] = None, links: Optional[List[str]] = None,
            when: Optional[datetime] = None) -> Optional[str]:
    """Saves one deliverable; returns its id (None if saving failed, which never raises)."""
    try:
        when = when or datetime.now()
        did = f"{when.strftime('%Y%m%d-%H%M%S')}-{_slug(skill)}-{secrets.token_hex(2)}"
        root = folder()
        files = []
        if body:
            name = did + ".md"
            with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            files.append(name)
        manifest = {
            "id": did, "skill": skill, "being": being if being in BEINGS else "nexus",
            "kind": kind if kind in KINDS else "report", "title": (title or "").strip()[:140],
            "summary": (summary or "").strip()[:600], "created": when.isoformat(timespec="seconds"),
            "files": files, "links": list(links or []), "metrics": dict(metrics or {}),
        }
        with _lock:
            _write_json(os.path.join(root, did + ".json"), manifest)
            _prune(root)
        return did
    except Exception:  # noqa: BLE001
        logger.exception("deliverables: couldn't save %r", title)
        return None


def _prune(root: str) -> None:
    names = sorted(n for n in os.listdir(root) if n.endswith(".json"))
    for name in names[:max(0, len(names) - MAX_KEEP)]:
        stem = name[:-5]
        for ext in (".json", ".md"):
            try:
                os.remove(os.path.join(root, stem + ext))
            except OSError:
                pass


def load(limit: int = 200, being: Optional[str] = None) -> List[dict]:
    """The newest manifests first (unreadable ones skipped)."""
    out = []
    try:
        root = folder()
        names = sorted((n for n in os.listdir(root) if n.endswith(".json")), reverse=True)
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or (being and m.get("being") != being):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def body(deliverable_id: str) -> Optional[str]:
    """The Markdown body of a deliverable, or None. Only ids this module made are accepted."""
    if not re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]{1,24}-[0-9a-f]{4}", deliverable_id or ""):
        return None
    try:
        with open(os.path.join(folder(), deliverable_id + ".md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ------------------------------------------------------------------ turn metrics

def _turns_path() -> str:
    return os.path.join(folder(), "turns.jsonl")


def record_turn(said: str, transcribe: float, route: float, first_sound: Optional[float],
                when: Optional[datetime] = None) -> None:
    """One turn's timing (seconds; first_sound None = she said nothing)."""
    try:
        total = None if first_sound is None else round(transcribe + route + first_sound, 2)
        line = json.dumps({"at": (when or datetime.now()).isoformat(timespec="seconds"), "said": (said or "")[:200],
                           "transcribe": round(transcribe, 2), "route": round(route, 2),
                           "first_sound": None if first_sound is None else round(first_sound, 2),
                           "total": total}, ensure_ascii=False)
        path = _turns_path()
        with _lock:
            if os.path.exists(path) and os.path.getsize(path) > MAX_TURNS_BYTES:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("deliverables: couldn't record the turn timing")


def turns(limit: int = 500) -> List[dict]:
    """The newest turns last."""
    try:
        with open(_turns_path(), encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
