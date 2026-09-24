"""
avatar_server.py — WebSocket bridge between the Python voice loop and avatar.html.

Drop-in replacement. Keeps the same shape as before (asyncio loop on a background
thread, synchronous API for the rest of the app) and adds:

  * the behaviour protocol avatar.html now expects (state / emotion / gesture)
  * last-state caching, so the avatar restores correctly after a reconnect
  * a real stop() that unblocks the asyncio loop, wired into shutdown.py

Public API — all safe to call from any thread, all non-blocking:

    avatar_server.start()
    avatar_server.set_state("listening")        # idle | listening | thinking | speaking
    avatar_server.set_emotion("focused", 0.9)
    avatar_server.speech_start()
    avatar_server.send_viseme("aa", 0.09)
    avatar_server.send_level(0.7)               # optional RMS 0..1 for fallback mouth
    avatar_server.speech_end()
    avatar_server.gesture("nod")
    avatar_server.set_framing("full")           # full | upper | face
    avatar_server.stop()
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, Iterable, Optional, Set

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

HOST = "127.0.0.1"
PORT = 8765

VALID_STATES = ("idle", "listening", "thinking", "speaking")
VALID_EMOTIONS = ("neutral", "happy", "amused", "sad", "concerned",
                  "focused", "surprised", "angry", "confident")

# emotion.py's keyword classifier may emit words outside the set above.
# Map them rather than silently collapsing everything to neutral.
EMOTION_ALIASES = {
    "joy": "happy", "excited": "happy", "pleased": "happy", "cheerful": "happy",
    "laugh": "amused", "amusement": "amused", "playful": "amused",
    "funny": "amused", "smug": "amused",
    "sorrow": "sad", "unhappy": "sad", "disappointed": "sad",
    "sympathy": "concerned", "empathy": "concerned", "worried": "concerned",
    "worry": "concerned", "apologetic": "concerned", "serious": "concerned",
    "thinking": "focused", "curious": "focused", "analytical": "focused",
    "attentive": "focused", "listening": "focused", "processing": "focused",
    "question": "focused", "confused": "focused",
    "shock": "surprised", "shocked": "surprised", "astonished": "surprised",
    "alert": "surprised", "warning": "surprised",
    "annoyed": "angry", "frustrated": "angry", "stern": "angry",
    "assertive": "confident", "certain": "confident", "affirmative": "confident",
    "acknowledge": "confident", "answer": "confident", "calm": "neutral",
    "idle": "neutral", "none": "neutral", "": "neutral",
}


def normalize_emotion(name) -> str:
    if not name:
        return "neutral"
    key = str(name).strip().lower()
    if key in VALID_EMOTIONS:
        return key
    return EMOTION_ALIASES.get(key, "neutral")

VALID_GESTURES = (
    # head
    "nod", "doubleNod", "shake", "tilt", "glanceAway", "scanRoom",
    # torso
    "shrug", "lean", "torsoTwist", "weightRock", "stretch", "settle",
    # hands, speaking
    "beat", "openPalm", "bothPalms", "pointForward", "handToChest",
    "dismiss", "countOff", "smallWave",
    # hands, thinking / idle
    "chinTouch", "hairTouch", "armFidget",
)

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_clients: Set[Any] = set()
_server = None
_started = threading.Event()
_stopping = threading.Event()

# Replayed to any client that connects, so a mid-conversation reload doesn't
# leave the avatar sitting in the wrong state.
_last: Dict[str, Dict[str, Any]] = {
    "state": {"type": "state", "state": "idle"},
    "emotion": {"type": "emotion", "emotion": "neutral", "intensity": 1.0},
    "framing": {"type": "framing", "mode": "full"},
}


# ---------------------------------------------------------------- asyncio side

async def _handler(websocket, *_args):
    _clients.add(websocket)
    try:
        for msg in _last.values():
            await websocket.send(json.dumps(msg))
        async for _raw in websocket:
            pass  # avatar.html only sends a 'hello'; nothing to act on
    except Exception:
        pass
    finally:
        _clients.discard(websocket)


async def _serve():
    global _server
    if websockets is None:
        print("[avatar] websockets not installed — avatar disabled")
        return
    _server = await websockets.serve(_handler, HOST, PORT, ping_interval=20, ping_timeout=20)
    _started.set()
    print(f"[avatar] bridge listening on ws://{HOST}:{PORT}")
    try:
        await asyncio.Future()  # run until cancelled
    except asyncio.CancelledError:
        pass
    finally:
        _server.close()
        await _server.wait_closed()


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_serve())
    except Exception as exc:
        print(f"[avatar] loop ended: {exc}")
    finally:
        try:
            pending = asyncio.all_tasks(_loop)
            for task in pending:
                task.cancel()
            if pending:
                _loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        _loop.close()
        _started.set()  # never leave start() blocked


async def _broadcast(payload: str):
    if not _clients:
        return
    await asyncio.gather(
        *(c.send(payload) for c in list(_clients)),
        return_exceptions=True,
    )


def _send(msg: Dict[str, Any]):
    """Thread-safe fire-and-forget send."""
    if _stopping.is_set() or _loop is None or _loop.is_closed():
        return
    payload = json.dumps(msg)
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(payload), _loop)
    except RuntimeError:
        pass


# ---------------------------------------------------------------- public API

def start(timeout: float = 3.0) -> bool:
    """Start the bridge. Returns True once the server is listening."""
    global _thread
    if _thread and _thread.is_alive():
        return True
    _stopping.clear()
    _started.clear()
    _thread = threading.Thread(target=_run_loop, name="avatar-ws", daemon=True)
    _thread.start()
    ok = _started.wait(timeout)

    # Auto-register with the shutdown coordinator if it's in use.
    try:
        import shutdown
        shutdown.on_shutdown(stop)
    except Exception:
        pass
    return ok


def stop():
    """Stop the bridge. Safe to call twice, safe from any thread."""
    if _stopping.is_set():
        return
    _stopping.set()
    if _loop is not None and not _loop.is_closed():
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except RuntimeError:
            pass
    if _thread and _thread.is_alive():
        _thread.join(timeout=1.5)


def is_connected() -> bool:
    return bool(_clients)


def set_state(state: str):
    """idle | listening | thinking | speaking — drives posture, gaze, brow."""
    if state not in VALID_STATES:
        return
    _last["state"] = {"type": "state", "state": state}
    _send(_last["state"])


def set_emotion(emotion: str, intensity: float = 1.0):
    emotion = normalize_emotion(emotion)
    intensity = max(0.0, min(1.4, float(intensity)))
    _last["emotion"] = {"type": "emotion", "emotion": emotion, "intensity": intensity}
    _send(_last["emotion"])


def speech_start():
    set_state("speaking")


def speech_end(next_state: str = "idle"):
    _send({"type": "speech_end", "next": next_state})
    _last["state"] = {"type": "state", "state": next_state}


def send_viseme(viseme: str, duration: float = 0.09, weight: float = 1.0):
    _send({"type": "viseme", "viseme": viseme,
           "duration": float(duration), "weight": float(weight)})


def send_viseme_sequence(sequence: Iterable[Dict[str, Any]]):
    """
    Schedule a whole utterance at once — lower jitter than per-frame sends.
    Each item: {"v": "aa", "t": 0.12, "d": 0.08}  (t = seconds from now)
    """
    seq = [{"v": i.get("v") or i.get("viseme"),
            "t": float(i.get("t", 0.0)),
            "d": float(i.get("d", i.get("duration", 0.09))),
            "w": float(i.get("w", 1.0))} for i in sequence]
    _send({"type": "visemes", "sequence": seq})


def send_level(level: float):
    """Optional audio RMS 0..1. Only used if visemes stop arriving."""
    _send({"type": "level", "value": max(0.0, min(1.0, float(level)))})


def gesture(name: str, amplitude: float = 1.0):
    if name in VALID_GESTURES:
        _send({"type": "gesture", "name": name, "amplitude": float(amplitude)})


def blink(double: bool = False):
    _send({"type": "blink", "double": bool(double)})


def set_framing(mode: str = "full"):
    if mode not in ("full", "upper", "face"):
        return
    _last["framing"] = {"type": "framing", "mode": mode}
    _send(_last["framing"])


def _normalize_timeline(timeline) -> list:
    """
    Turn whatever viseme_map.py produces into [{v, t, d, w}, ...] with times in
    seconds from the start of the utterance.

    Accepts, because I'd rather handle every plausible shape than guess:
      [("aa", 0.00, 0.08), ...]                  (viseme, start, duration)
      [("aa", 0.08), ...]                        (viseme, duration) — sequential
      [{"viseme": "aa", "start": 0.0, "end": 0.08}, ...]
      [{"v": "aa", "t": 0.0, "d": 0.08}, ...]
      [{"phoneme": "aa", "time": 0.0, "duration": 0.08}, ...]
      ["aa", "ih", ...]                          bare names, evenly spaced
    Milliseconds are detected and converted.
    """
    if not timeline:
        return []

    out = []
    cursor = 0.0

    for item in timeline:
        v = t = d = None
        w = 1.0

        if isinstance(item, (list, tuple)):
            if len(item) >= 3:
                v, t, d = item[0], item[1], item[2]
            elif len(item) == 2:
                v, d = item[0], item[1]      # sequential, no explicit start
            elif len(item) == 1:
                v = item[0]
        elif isinstance(item, dict):
            v = item.get("v") or item.get("viseme") or item.get("phoneme") or item.get("name")
            t = item.get("t", item.get("time", item.get("start", item.get("offset"))))
            d = item.get("d", item.get("duration", item.get("dur", item.get("length"))))
            if d is None and t is not None and item.get("end") is not None:
                d = float(item["end"]) - float(t)
            w = float(item.get("w", item.get("weight", 1.0)))
        elif isinstance(item, str):
            v = item

        if not v:
            continue
        v = str(v).strip().lower()
        if v not in ("aa", "ih", "ou", "ee", "oh"):
            continue  # silence / consonant markers close the mouth on their own

        d = 0.09 if d is None else float(d)
        t = cursor if t is None else float(t)
        out.append({"v": v, "t": t, "d": d, "w": w})
        cursor = t + d

    if not out:
        return []

    # Heuristic: a spoken viseme lasts roughly 40-200ms. If the typical
    # duration is above ~5, the source units are milliseconds, not seconds.
    durations = sorted(e["d"] for e in out)
    median = durations[len(durations) // 2]
    if median > 5:
        for e in out:
            e["t"] /= 1000.0
            e["d"] /= 1000.0

    # Normalize to start at zero so playback lines up with audio start.
    t0 = out[0]["t"]
    if t0 > 0:
        for e in out:
            e["t"] -= t0

    return out


def send_speech(timeline, emotion=None, intensity: float = 1.0):
    """
    Start an utterance: switch to the speaking state and schedule the whole
    viseme timeline in one message.

    Called from speaker.py right before audio playback begins.
    """
    if emotion is not None:
        set_emotion(emotion, intensity)
    set_state("speaking")
    seq = _normalize_timeline(timeline)
    if seq:
        send_viseme_sequence(seq)
    return len(seq)


def end_speech(next_state: str = "idle"):
    speech_end(next_state)


def send_speech_stop(next_state: str = "idle"):
    """Utterance finished — settle the hands and return to the given state."""
    speech_end(next_state)


def send_speech_start(emotion=None, intensity: float = 1.0):
    if emotion is not None:
        set_emotion(emotion, intensity)
    set_state("speaking")


# ---------------------------------------------------------------- safety net
# The rest of the project was written against an older version of this module,
# and an unknown attribute here kills the voice thread outright. Rather than
# discover the remaining names one crash at a time, unknown attributes resolve
# to a no-op that warns once. The avatar degrades; the assistant keeps talking.
#
# Anything that shows up in these warnings is a real gap worth wiring properly.

_warned = set()


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(f"module 'avatar_server' has no attribute '{name}'")

    def _missing(*args, **kwargs):
        if name not in _warned:
            _warned.add(name)
            print(f"[avatar] note: avatar_server.{name}() is not implemented — "
                  f"ignoring. Args: {args if args else '()'}")
        return None

    _missing.__name__ = name
    return _missing


# ---------------------------------------------------------------- back-compat
# Names the previous avatar_server.py exposed. Aliased so speaker.py, main.py
# and llm_brain.py don't need editing.

def send_emotion(emotion, intensity: float = 1.0):
    set_emotion(emotion, intensity)


def send_state(state):
    set_state(state)


def send_visemes(sequence):
    send_viseme_sequence(sequence)


def send_gesture(name, amplitude: float = 1.0):
    gesture(name, amplitude)


def start_speaking():
    speech_start()


def stop_speaking(next_state: str = "idle"):
    speech_end(next_state)


def set_speaking(flag: bool):
    speech_start() if flag else speech_end()


def set_listening(flag: bool = True):
    set_state("listening" if flag else "idle")


def set_thinking(flag: bool = True):
    set_state("thinking" if flag else "idle")


def set_idle():
    set_state("idle")


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def has_clients() -> bool:
    return is_connected()


def shutdown():
    stop()


# ---------------------------------------------------------------- demo
if __name__ == "__main__":
    import time
    start()
    print("Open avatar.html, then watch her cycle states. Ctrl+C to quit.")
    try:
        while True:
            for st, emo in (("listening", "focused"), ("thinking", "focused"),
                            ("speaking", "confident"), ("idle", "neutral")):
                set_state(st)
                set_emotion(emo)
                if st == "speaking":
                    for _ in range(45):
                        send_viseme(["aa", "ih", "ou", "ee", "oh"][int(time.time() * 11) % 5], 0.1)
                        time.sleep(0.09)
                else:
                    time.sleep(5)
    except KeyboardInterrupt:
        stop()
