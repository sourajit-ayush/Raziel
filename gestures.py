"""
gestures.py - hand gestures in front of the webcam.

  Only a hand you SHOW her counts: raised above desk level, upright (fingers pointing up for a palm)
  and held still for a moment. A bar under her fills while you hold; drop your hand to cancel.

  Music and media
    open palm, held still ~1 s     play / pause (Spotify, YouTube, Netflix... - the media key)
    swipe right / left             next / previous song (raise an open hand, hold it a moment, then swipe)
    swipe up / down                volume up / down
    fist, turned like a knob       fine volume control (clockwise = louder)
    two fingers up, turned         the same for screen brightness
    thumbs up while a song plays   add it to your Spotify Liked Songs
  Presentations and reading
    swipe right / left             next / previous slide or page, when PowerPoint, a PDF or Google
                                   Slides is in front
    swipe down while she talks     skip to the next part of what she is reading
  Moving her around
    pinch (thumb + first finger, other fingers open, like an OK sign) and move
                                   drag her window
    pinch with both hands, pull apart / push together
                                   bigger / smaller (remembered, like Ctrl + mouse wheel)

  Voice: "stop watching" / "watch my hands" / "are you watching?" / "what gestures do you know?"

How it works
    A background thread reads the webcam (OpenCV) about 15 times a second, mirrored like a
    selfie, and Google's MediaPipe gesture recogniser (a small local model, models\\
    gesture_recognizer.task) finds up to two hands: 21 points per hand plus a guess at the pose
    (open palm, fist, victory, thumbs up...). GestureEngine turns those points over time into
    gestures - a pose must be HELD, a swipe must be FAST - and GestureActions does what they mean.
    Nothing is recorded or saved; frames never leave the process.

    The camera is let go during a call (quiet_mode.py: Zoom / Discord / Teams using the mic) and
    while "stop watching" is in effect (remembered across restarts in gestures_state.json).

Setup: install_gestures.bat (mediapipe + OpenCV, and the 8 MB model). To see what she sees:
    venv\\Scripts\\python.exe gestures.py --check      (close Raziel or say "stop watching" first)

Settings (config.py): GESTURES_ENABLED, GESTURES_CAMERA, GESTURES_PAUSE_IN_CALLS, GESTURES_MUSIC,
GESTURES_KNOB, GESTURES_LIKE, GESTURES_SLIDES, GESTURES_SKIP, GESTURES_WINDOW, GESTURES_HOLD_SECONDS,
GESTURES_SWIPE_DISTANCE, GESTURES_KNOB_DEGREES, GESTURES_VOLUME_STEP, GESTURES_BRIGHTNESS_STEP,
GESTURES_RAISED, GESTURES_UPRIGHT_DEGREES, GESTURES_DEBUG_LOG.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

import config
import lang

logger = logging.getLogger("voice_assistant")

# MediaPipe hand landmarks
WRIST = 0
THUMB = (1, 2, 3, 4)
FINGERS = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12), "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}
PALM = (0, 5, 9, 13, 17)

VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3
VK_PRIOR, VK_NEXT, VK_LEFT, VK_RIGHT = 0x21, 0x22, 0x25, 0x27        # Page Up / Page Down / arrows

MODEL_URL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"


def _cfg(name: str, default):
    return getattr(config, name, default)


# ======================================================================== one hand, one frame

@dataclass
class Hand:
    pts: Sequence[Tuple[float, float, float]]     # 21 (x, y, z), x/y in 0..1 of the (mirrored) frame
    gesture: str = "None"                        # MediaPipe's guess: Open_Palm, Closed_Fist, Victory, ...
    score: float = 0.0
    label: str = ""                              # "Left" / "Right"

    def p(self, i: int) -> Tuple[float, float]:
        return float(self.pts[i][0]), float(self.pts[i][1])


def _d(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hand_size(h: Hand) -> float:
    """Wrist to the middle finger's knuckle: the hand's scale in the frame."""
    return max(1e-6, _d(h.p(WRIST), h.p(9)))


def palm_center(h: Hand) -> Tuple[float, float]:
    xs = [h.p(i)[0] for i in PALM]
    ys = [h.p(i)[1] for i in PALM]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def finger_extended(h: Hand, name: str) -> bool:
    mcp, pip, _dip, tip = FINGERS[name]
    w = h.p(WRIST)
    return _d(w, h.p(tip)) > 1.15 * _d(w, h.p(pip)) and _d(h.p(mcp), h.p(tip)) > 0.55 * hand_size(h)


def thumb_extended(h: Hand) -> bool:
    return _d(h.p(4), h.p(17)) > 1.1 * _d(h.p(3), h.p(17)) and _d(h.p(4), h.p(5)) > 0.45 * hand_size(h)


def extended(h: Hand) -> Dict[str, bool]:
    return {n: finger_extended(h, n) for n in FINGERS}


def pinch_distance(h: Hand) -> float:
    """Thumb tip to first-finger tip, in hand sizes."""
    return _d(h.p(4), h.p(8)) / hand_size(h)


def pinch_point(h: Hand) -> Tuple[float, float]:
    a, b = h.p(4), h.p(8)
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def knob_angle(h: Hand) -> float:
    """Direction of the knuckle line (first finger -> little finger), degrees; grows clockwise as you see
    your mirrored hand. A fist facing the camera turns this line in the picture like a knob."""
    a, b = h.p(5), h.p(17)
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


PINCH_ON, PINCH_OFF = 0.32, 0.5          # in hand sizes (hysteresis)
MODEL_SURE = 0.55


def upright_degrees(h: Hand) -> float:
    """How far the hand leans away from pointing straight up (wrist -> middle knuckle), in degrees:
    0 = upright, 90 = sideways, 180 = hanging down."""
    w, m = h.p(WRIST), h.p(9)
    return abs(math.degrees(math.atan2(m[0] - w[0], -(m[1] - w[1]))))


def fingers_up(h: Hand) -> bool:
    """The fingers point up: at least three fingertips clearly above their own knuckles (a little
    finger that bends or hides behind the others still counts)."""
    s = hand_size(h)
    return sum(h.p(tip)[1] < h.p(mcp)[1] - 0.35 * s for mcp, _pip, _dip, tip in FINGERS.values()) >= 3


def pose_of(h: Hand, pinching: bool = False) -> str:
    """palm | fist | victory | thumb_up | pinch | other. When MediaPipe is sure of a pose, the finger
    positions must agree too (its "Open_Palm" alone fired on a hand resting against a face)."""
    ext = extended(h)
    n = sum(ext.values())
    others_open = ext["middle"] or ext["ring"] or ext["pinky"]
    limit = PINCH_OFF if pinching else PINCH_ON
    if pinch_distance(h) < limit and others_open and h.gesture != "Closed_Fist":
        return "pinch"
    g = h.gesture if h.score >= MODEL_SURE else ""
    if g == "Thumb_Up":
        return "thumb_up" if n <= 1 else "other"
    if g == "Victory":
        return "victory" if ext["index"] and ext["middle"] else "other"
    if g == "Closed_Fist":
        return "fist" if n <= 1 else "other"
    if g == "Open_Palm":
        return "palm" if n >= 3 and ext["index"] and ext["middle"] else "other"
    if g in ("Pointing_Up", "ILoveYou", "Thumb_Down"):
        return "other"
    # MediaPipe isn't sure: judge by the fingers
    if n == 4 and thumb_extended(h):
        return "palm"
    if ext["index"] and ext["middle"] and not ext["ring"] and not ext["pinky"]:
        return "victory"
    if n == 0:
        tip, knuckle = h.p(4), h.p(5)
        if thumb_extended(h) and tip[1] < knuckle[1] - 0.3 * hand_size(h):
            return "thumb_up"
        return "fist"
    return "other"


# ======================================================================== frames over time -> gestures

@dataclass
class _Track:
    t: float
    x: float
    y: float
    fingers: int
    size: float
    ready: bool = False            # at this moment: a raised, upright, open hand, held still, armed
    palm: bool = False             # at this moment: a raised, upright, open hand (still or not)


@dataclass
class EngineSettings:
    hold: float = 1.0              # palm held this long -> play / pause
    hold_show: float = 0.4         # ...the hold bar appears under her after this much of it
    thumb_hold: float = 0.8
    still: float = 0.05            # "held still": the hand moves less than this (frame widths; less for a far hand)
    raised: float = 0.8            # the palm's centre must be above this line (share of the picture's height)
    upright: float = 35.0          # palm / fist: at most this many degrees from pointing straight up
    ready: float = 0.15            # a swipe starts from an open, upright hand held still this long
    ready_upright: float = 45.0    # ...leaning at most this much (a swipe starts from a relaxed hand)
    swipe: float = 0.18            # a swipe covers at least this much of the frame...
    swipe_time: float = 0.6        # ...within this long
    straight: float = 2.0          # ...and is this many times longer than it is off-line
    exit_swipe: float = 0.5        # a hand that swipes OUT of the picture needs this share of `swipe`...
    exit_gone: float = 0.12        # ...and stays gone this long (a one-frame dropout mid-picture is no swipe)
    swipe_cooldown: float = 1.2
    edge: float = 0.06             # a swipe can't start this close to the picture's edge (a hand arriving)
    vertical_confirm: float = 0.2  # a down/up swipe counts only if the hand is still there after it
    knob_enter: float = 0.6        # fist / two fingers held up this long -> knob
    knob_degrees: float = 12.0     # one volume / brightness step per this much turning
    knob_first: float = 24.0       # ...but the first step needs a clear turn (a resting fist wobbles)
    knob_idle: float = 3.0         # a knob nobody turns ends by itself
    knob_min_size: float = 0.07    # a fist smaller than this (far away, someone behind you) is no knob
    pinch_enter: float = 0.4       # a pinch held still this long -> drag
    release: float = 0.3           # a pose / hand may vanish this long before a mode ends / a hold re-arms
    debounce: int = 3              # frames a new pose must last before it counts (one misread frame is ignored)
    min_size: float = 0.025        # smaller hands (far away, or noise) are ignored


class GestureEngine:
    """
    update(t, hands) once per camera frame -> emit(event, *args):
      ("play_pause",)  ("swipe", "left"|"right"|"up"|"down")  ("like",)
      ("hold", "palm"|"thumb_up", total_seconds, held_seconds)  ("hold_cancel",)   <- the hold bar
      ("knob_start", "volume"|"brightness")  ("knob", kind, +1|-1)  ("knob_end",)
      ("drag_start",) ("drag", dx, dy) ("drag_end",)  ("resize_start",) ("resize", k) ("resize_end",)
    dx / dy: frame widths / heights moved since the pinch began; k: the hands' distance / at the start.

    Only a hand that is deliberately SHOWN counts: raised above the desk (`raised`), upright (`upright`),
    fingers pointing up for a palm, and held still. Held poses fire once per hold: to fire again, the
    hand must leave or show another pose for `release` seconds. A swipe must start from such a hand
    held still for `ready` seconds, then be fast and straight - waving while you talk, reaching for the
    mouse or dropping your hand don't count.
    """

    def __init__(self, emit: Callable[..., None], settings: Optional[EngineSettings] = None):
        self.emit = emit
        self.s = settings or EngineSettings()
        self.mode = ""                     # "" | knob | drag | resize
        self.kind = ""                     # knob: volume | brightness
        self.track: Deque[_Track] = deque()
        self.swipe_until = 0.0             # no swipes before this (after a swipe / a mode / a hold)
        self.drag_until = 0.0              # no one-hand drag before this (right after resizing)
        self.pending_vertical: Optional[Tuple[str, float]] = None
        self.seen_since: Optional[float] = None     # the hand has been in view since
        self.raw_pose = ""
        self.raw_count = 0
        self.pose = ""                     # debounced
        self.pose_since = 0.0
        self.fired: Optional[str] = None   # the pose that already fired in this hold
        self.fired_off_since: Optional[float] = None
        self.anchor: Tuple[float, float] = (0.0, 0.0)
        self.lost_since: Optional[float] = None
        self.off_since: Optional[float] = None
        self.knob_last = 0.0
        self.knob_acc = 0.0
        self.knob_steps = 0
        self.knob_moved_at = 0.0
        self.p0: Tuple[float, float] = (0.0, 0.0)
        self.smooth: Optional[Tuple[float, float]] = None
        self.k_smooth = 1.0
        self.d0 = 1.0
        self.pinch_since: Dict[int, float] = {}
        self.pinch_anchor: Optional[Tuple[float, float]] = None
        self.pinch_pair = None
        self.exit_pending: Optional[str] = None     # a swipe out of the picture, decided when the hand stays gone
        self.knob_shown = False            # knob_start is sent with the first step (a fist that never turns shows nothing)
        self.holding: Optional[str] = None # the hold bar is showing for this pose
        self.hold_quiet_until = 0.0

    # -- helpers
    def _main(self, hands: List[Hand]) -> Optional[Hand]:
        return max(hands, key=hand_size) if hands else None

    def _still_limit(self, h: Hand) -> float:
        return min(self.s.still, max(0.012, 0.4 * hand_size(h)))

    def _raised(self, h: Hand) -> bool:
        return palm_center(h)[1] <= self.s.raised

    def shown(self, h: Hand, pose: str, upright: Optional[float] = None) -> bool:
        """Is this pose deliberately shown to the camera (not a hand resting on the desk, a chin, a face)?"""
        if not self._raised(h):
            return False
        if pose == "palm":
            return upright_degrees(h) <= (self.s.upright if upright is None else upright) and fingers_up(h)
        if pose in ("fist", "victory"):
            return upright_degrees(h) <= self.s.upright + 10
        return True                         # thumbs up (a sideways hand), pinch

    def _hold_show(self, pose: str, total: float, held: float, t: float):
        if self.holding != pose and t >= self.hold_quiet_until:
            self.holding = pose
            self.emit("hold", pose, total, held)

    def _hold_cancel(self, t: Optional[float] = None):
        if self.holding is not None:
            self.holding = None
            if t is not None:
                self.hold_quiet_until = t + 0.8       # a hand drifting in and out of "still" mustn't flicker it
            self.emit("hold_cancel")

    def _end_mode(self, t: float, pose: str = ""):
        ended = self.mode
        if ended == "drag":
            self.emit("drag_end")
        elif ended == "resize":
            self.emit("resize_end")
            self.drag_until = t + 1.0
        elif ended == "knob":
            if self.knob_shown:
                self.emit("knob_end")
            self.knob_shown = False
        self.mode, self.kind = "", ""
        self.off_since = None
        self.smooth = None
        self.track.clear()
        self.pinch_since.clear()
        self.pinch_anchor = None
        self.pinch_pair = None
        self.pending_vertical = None
        self.exit_pending = None
        self.swipe_until = max(self.swipe_until, t + 0.8)
        # whatever the hand shows now has to be made afresh before it does anything
        self.pose = self.raw_pose = pose
        self.fired, self.fired_off_since = (pose or None), None
        self.pose_since = t

    def reset(self):
        self._hold_cancel()
        if self.mode:
            self._end_mode(0.0)
        self.track.clear()
        self.pose = self.raw_pose = ""
        self.fired = None
        self.pending_vertical = None
        self.pinch_since.clear()
        self.pinch_anchor = None
        self.pinch_pair = None
        self.exit_pending = None
        self.lost_since = self.seen_since = None

    def _debounce(self, pose: str, t: float) -> str:
        if pose == self.raw_pose:
            self.raw_count += 1
        else:
            self.raw_pose, self.raw_count = pose, 1
        if pose != self.pose and self.raw_count >= self.s.debounce:
            self.pose, self.pose_since = pose, t
        # a fired pose re-arms only after the hand has shown something else for a while
        if self.fired is not None:
            if self.pose == self.fired:
                self.fired_off_since = None
            else:
                self.fired_off_since = self.fired_off_since or t
                if t - self.fired_off_since >= self.s.release:
                    self.fired, self.fired_off_since = None, None
        return self.pose

    def _fire(self, pose: str, t: float, *event):
        self.holding = None                 # (the action replaces the hold bar with what it did)
        self.fired, self.fired_off_since = pose, None
        self.pending_vertical = None
        self.track.clear()
        self.swipe_until = max(self.swipe_until, t + self.s.swipe_cooldown)   # lowering the hand isn't a swipe
        self.emit(*event)

    # -- one camera frame
    def update(self, t: float, hands: List[Hand]):
        hands = [h for h in hands if hand_size(h) >= self.s.min_size]
        pinchers = [h for h in hands if pose_of(h, pinching=self.mode in ("drag", "resize")) == "pinch"
                    and (self.mode in ("drag", "resize") or self._raised(h))]

        # ---- both hands pinching: bigger / smaller
        if len(pinchers) >= 2:
            self._hold_cancel()
            a, b = pinch_point(pinchers[0]), pinch_point(pinchers[1])
            d = max(1e-6, _d(a, b))
            if self.mode != "resize":
                # both pinches held still first (the log: two hands passing each other started a resize)
                lim = min(self._still_limit(pinchers[0]), self._still_limit(pinchers[1]))
                if (self.pinch_pair is None or _d(a, self.pinch_pair[0]) >= lim or _d(b, self.pinch_pair[1]) >= lim):
                    self.pinch_since[2], self.pinch_pair = t, (a, b)
                if t - self.pinch_since.setdefault(2, t) >= self.s.pinch_enter:
                    if self.mode:
                        self._end_mode(t, "pinch")
                    self.mode, self.d0, self.k_smooth = "resize", d, 1.0
                    self.emit("resize_start")
                return
            self.off_since = None
            self.k_smooth = 0.5 * self.k_smooth + 0.5 * (d / self.d0)
            self.emit("resize", self.k_smooth)
            return
        self.pinch_since.pop(2, None)
        self.pinch_pair = None
        main = self._main(hands)
        if self.mode == "resize":
            self.off_since = self.off_since or t
            if t - self.off_since >= self.s.release / 2:
                self._end_mode(t, pose_of(main) if main is not None else "")
            return

        if main is None:
            if self.lost_since is None:
                self.exit_pending = self._exit_swipe(t)
            elif self.exit_pending and t - self.lost_since >= self.s.exit_gone:
                direction, self.exit_pending = self.exit_pending, None
                self._hold_cancel()
                self.swipe_until = t + self.s.swipe_cooldown
                self.track.clear()
                self.emit("swipe", direction)
            self.lost_since = self.lost_since or t
            self._hold_cancel(t)
            self._check_vertical(t, gone=True)
            if t - self.lost_since >= self.s.release:
                if self.mode:
                    self._end_mode(t)
                self.track.clear()
                self.pose = self.raw_pose = ""
                self.fired = None
                self.seen_since = None
            return
        self.lost_since = None
        self.exit_pending = None               # back in view: whatever it was, the tracked movement decides
        if self.seen_since is None:
            self.seen_since = t
        raw = pose_of(main, pinching=self.mode == "drag")

        # ---- one hand pinching: drag her window
        if self.mode == "drag":
            if raw == "pinch":
                self.off_since = None
                p = pinch_point(main)
                sm = self.smooth or p
                self.smooth = (0.5 * sm[0] + 0.5 * p[0], 0.5 * sm[1] + 0.5 * p[1])
                self.emit("drag", self.smooth[0] - self.p0[0], self.smooth[1] - self.p0[1])
            else:
                self.off_since = self.off_since or t
                if t - self.off_since >= self.s.release / 2:
                    self._end_mode(t, raw)
            return
        if raw == "pinch" and len(pinchers) == 1 and t >= self.drag_until:
            self._hold_cancel()
            p = pinch_point(main)
            if self.pinch_anchor is None or _d(p, self.pinch_anchor) >= self._still_limit(main):
                self.pinch_since[1], self.pinch_anchor = t, p      # a moving pinch: not yet
            elif t - self.pinch_since.get(1, t) >= self.s.pinch_enter:
                if self.mode:
                    self._end_mode(t, "pinch")
                self.mode = "drag"
                self.p0 = p
                self.smooth = self.p0
                self.pinch_anchor = None
                self.emit("drag_start")
            return
        self.pinch_since.pop(1, None)
        self.pinch_anchor = None
        pose = self._debounce(raw, t)

        # ---- a fist / two fingers, turned like a knob
        if self.mode == "knob":
            want = "fist" if self.kind == "volume" else "victory"
            if raw == want or pose == want:
                self.off_since = None
                a = knob_angle(main)
                self.knob_acc += (a - self.knob_last + 180.0) % 360.0 - 180.0
                self.knob_last = a
                need = self.s.knob_first if self.knob_steps == 0 else self.s.knob_degrees
                while abs(self.knob_acc) >= need - 1e-6:
                    step = 1 if self.knob_acc > 0 else -1
                    self.knob_acc -= step * need
                    self.knob_steps += 1
                    self.knob_moved_at = t
                    need = self.s.knob_degrees
                    if not self.knob_shown:
                        self.knob_shown = True
                        self.emit("knob_start", self.kind)
                    self.emit("knob", self.kind, step)
                if t - self.knob_moved_at >= self.s.knob_idle:
                    self._end_mode(t, want)              # nobody is turning it (a fist resting on a chin)
            else:
                self.off_since = self.off_since or t
                if t - self.off_since >= self.s.release:
                    self._end_mode(t, pose)
            return

        # ---- poses held still: palm = play / pause, thumbs up = like; fist / two fingers start a knob
        c = palm_center(main)
        if _d(c, self.anchor) >= self._still_limit(main):
            self.pose_since, self.anchor = t, c              # moving: the hold starts again
        held = t - self.pose_since
        armed = self.fired is None
        shown = self.shown(main, pose)
        if (armed and shown and pose in ("fist", "victory") and held >= self.s.knob_enter
                and self._model_says(main, pose) and hand_size(main) >= self.s.knob_min_size):
            # (MediaPipe must see the fist too: every false volume knob in gestures_debug.jsonl was a small,
            # far hand that only the finger check called a fist, with the model unsure)
            self._hold_cancel()
            self.mode, self.kind = "knob", ("volume" if pose == "fist" else "brightness")
            self.knob_last, self.knob_acc, self.knob_steps, self.knob_moved_at = knob_angle(main), 0.0, 0, t
            self.off_since = None
            self.fired = pose
            self.track.clear()
            self.pending_vertical = None
            self.knob_shown = False
            return
        for want, total, event in (("palm", self.s.hold, "play_pause"), ("thumb_up", self.s.thumb_hold, "like")):
            if armed and shown and pose == want:
                if held >= total:
                    self._fire(want, t, event)
                    return
                if held >= min(self.s.hold_show, total * 0.5):
                    self._hold_show(want, total, held, t)
                break
        if self.holding is not None and not (armed and shown and pose == self.holding and held > 0):
            self._hold_cancel(t)

        # ---- swipes
        swipe_shown = pose == "palm" and self.shown(main, pose, upright=self.s.ready_upright)
        ready = armed and swipe_shown and held >= self.s.ready
        self._swipe(t, main, c, ready, pose == "palm" and shown)

    def _swipe(self, t: float, h: Hand, c: Tuple[float, float], ready: bool, palm: bool = False):
        fingers = sum(extended(h).values())
        self.track.append(_Track(t, c[0], c[1], fingers, hand_size(h), ready, palm))
        while self.track and t - self.track[0].t > self.s.swipe_time:
            self.track.popleft()
        self._check_vertical(t)
        if t < self.swipe_until or len(self.track) < 3:
            return
        now = self.track[-1]
        thr = self._swipe_threshold(now.size)
        e, k = self.s.edge, self.s.straight
        for i, first in enumerate(self.track):
            if not first.ready:                        # it must start from a still, raised, open hand
                continue
            if not (e <= first.x <= 1 - e and e <= first.y <= 1 - e):
                continue                               # ...not one coming in from the edge
            if not self._stayed_open(i):
                continue                               # ...and stay an open hand (a blurred frame may miss a finger)
            dx, dy = now.x - first.x, now.y - first.y
            if abs(dx) >= thr and abs(dx) >= k * abs(dy):
                self._hold_cancel()
                self.swipe_until = t + self.s.swipe_cooldown
                self.track.clear()
                self.emit("swipe", "right" if dx > 0 else "left")
                return
            if abs(dy) >= thr and abs(dy) >= k * abs(dx) and self.pending_vertical is None:
                self._hold_cancel()
                self.swipe_until = t + self.s.swipe_cooldown
                self.track.clear()
                # dropping your hand looks like a swipe down: it only counts if the hand is still in view
                self.pending_vertical = ("down" if dy > 0 else "up", t)
                return

    def _swipe_threshold(self, size: float) -> float:
        # a hand far from the camera (small) makes smaller movements in the picture; a near one (your
        # hands were 0.2 - 0.3 of the picture in gestures_debug.jsonl) leaves the picture half way
        return max(0.10, min(self.s.swipe, 3.0 * size))

    def _stayed_open(self, start: int) -> bool:
        frames = list(self.track)[start:]
        return bool(frames) and sum(f.fingers >= 2 for f in frames) * 2 >= len(frames) and frames[-1].fingers >= 1

    def _exit_swipe(self, t: float) -> Optional[str]:
        """The hand vanished. A near hand swiped sideways goes out of the picture (or blurs too much for the
        hand model) before it has covered the full distance - that is still a swipe, if it started from a
        ready hand, moved fast and straight sideways, was last seen moving that way and was about to reach
        the picture's edge. Returns the direction (decided once the hand stays gone), else None."""
        if t < self.swipe_until or len(self.track) < 3 or self.mode:
            return None
        frames = list(self.track)
        last, prev = frames[-1], frames[-2]
        if t - last.t > 0.3:
            return None
        thr = self._swipe_threshold(last.size) * self.s.exit_swipe
        e = self.s.edge
        for i, first in enumerate(frames[:-1]):
            if not first.ready or not (e <= first.x <= 1 - e and e <= first.y <= 1 - e):
                continue
            if not self._stayed_open(i):
                continue
            dx, dy = last.x - first.x, last.y - first.y
            step = last.x - prev.x
            sign = 1.0 if dx > 0 else -1.0
            reach = last.x + sign * (0.6 * last.size + 2.0 * abs(step))     # the hand's edge, a moment later
            if (abs(dx) >= thr and abs(dx) >= self.s.straight * abs(dy) and step * dx > 0
                    and abs(step) >= 0.012 and last.t - first.t <= self.s.swipe_time
                    and (reach >= 0.85 if dx > 0 else reach <= 0.15)):
                return "right" if dx > 0 else "left"
        return None

    @staticmethod
    def _model_says(h: Hand, pose: str) -> bool:
        want = {"fist": "Closed_Fist", "victory": "Victory"}.get(pose)
        return want is None or (h.gesture == want and h.score >= MODEL_SURE)

    def _check_vertical(self, t: float, gone: bool = False):
        if self.pending_vertical is None:
            return
        direction, since = self.pending_vertical
        if gone:
            self.pending_vertical = None                # the hand left the picture: dropped, not swiped
            return
        if t - since >= self.s.vertical_confirm:
            self.pending_vertical = None
            last = self.track[-1] if self.track else None
            # only if the hand is still SHOWN afterwards (raised, open, upright): dropping it to cancel
            # the hold bar, or letting it fall into a relaxed fist, is not a swipe
            if last is not None and last.palm:
                self.emit("swipe", direction)


# ======================================================================== what gestures do

_T = {
    "play_pause": {"en": "Play / pause", "hi": "प्ले / पॉज़"},
    "paused": {"en": "Paused", "hi": "रोका"},
    "playing": {"en": "Playing", "hi": "चला दिया"},
    "hold_play": {"en": "Hold still to play / pause", "hi": "प्ले / पॉज़ के लिए हाथ रोके रखिए"},
    "hold_like": {"en": "Hold to like this song", "hi": "गाना पसंद करने के लिए रोके रखिए"},
    "knob_volume": {"en": "Turn to change the volume", "hi": "आवाज़ के लिए घुमाइए"},
    "knob_brightness": {"en": "Turn to change the brightness", "hi": "ब्राइटनेस के लिए घुमाइए"},
    "next": {"en": "Next", "hi": "अगला"},
    "prev": {"en": "Previous", "hi": "पिछला"},
    "next_slide": {"en": "Next slide", "hi": "अगली स्लाइड"},
    "prev_slide": {"en": "Previous slide", "hi": "पिछली स्लाइड"},
    "vol": {"en": "Volume {p}%", "hi": "आवाज़ {p}%"},
    "bright": {"en": "Brightness {p}%", "hi": "ब्राइटनेस {p}%"},
    "skip": {"en": "Skipping ahead", "hi": "आगे बढ़ रही हूँ"},
    "liked": {"en": "Added to Liked Songs: {song}", "hi": "पसंदीदा गानों में जोड़ा: {song}"},
    "already": {"en": "Already in your Liked Songs", "hi": "ये पहले से पसंदीदा में है"},
    "like_perm": {"en": "To like songs I need one more Spotify permission: next time you ask me to play something, approve it in the browser.",
                  "hi": "गाने पसंद करने के लिए Spotify की एक और अनुमति चाहिए: अगली बार गाना चलाने को कहें तो ब्राउज़र में मंज़ूरी दे दीजिए।"},
    "moving": {"en": "Moving", "hi": "खिसका रही हूँ"},
    "sizing": {"en": "Resizing", "hi": "साइज़ बदल रही हूँ"},
    # voice
    "on": {"en": "Okay, I'm watching for hand gestures.", "hi": "ठीक है, मैं हाथ के इशारे देख रही हूँ।"},
    "off": {"en": "Okay, I've stopped watching. The camera is off.", "hi": "ठीक है, मैंने देखना बंद कर दिया। कैमरा बंद है।"},
    "status_on": {"en": "Yes, I'm watching for hand gestures.", "hi": "हाँ, मैं हाथ के इशारे देख रही हूँ।"},
    "status_call": {"en": "Not right now: a call or another app needs the camera, so I've let go of it.",
                    "hi": "अभी नहीं: किसी कॉल या दूसरे ऐप को कैमरा चाहिए, इसलिए मैंने उसे छोड़ दिया है।"},
    "status_error": {"en": "Something went wrong with the camera, so I've started it again.",
                     "hi": "कैमरे में कुछ गड़बड़ हुई थी, इसलिए मैंने उसे फिर से शुरू किया है।"},
    "status_off": {"en": "No, I'm not watching. Say 'watch my hands' to turn it on.",
                   "hi": "नहीं, मैं नहीं देख रही। चालू करने के लिए कहिए 'मेरे हाथ देखो'।"},
    "not_set_up": {"en": "Hand gestures need a one-time setup: run install_gestures.bat in my folder, then restart me.",
                   "hi": "हाथ के इशारों के लिए एक बार सेटअप चाहिए: मेरे फ़ोल्डर में install_gestures.bat चलाइए, फिर मुझे रीस्टार्ट कीजिए।"},
    "no_camera": {"en": "I can't open the camera. Is another app using it, or is camera access for desktop apps off in Windows settings?",
                  "hi": "मैं कैमरा नहीं खोल पा रही। क्या कोई और ऐप उसे चला रहा है, या Windows सेटिंग्स में डेस्कटॉप ऐप्स के लिए कैमरा बंद है?"},
    "disabled": {"en": "Hand gestures are switched off in the settings.", "hi": "हाथ के इशारे सेटिंग्स में बंद हैं।"},
    "help": {"en": "Raise your hand so I can see it, and hold it still for a moment. Open palm to play or pause. Swipe right "
                   "or left for the next or previous song, or slide. Swipe up or down "
                   "for the volume, or down to skip ahead while I'm reading. Turn a fist like a knob for the volume, or two "
                   "fingers for the brightness. Thumbs up likes the song. Pinch to move me, and pinch with both hands to "
                   "make me bigger or smaller.",
             "hi": "हथेली दिखाइए तो प्ले या पॉज़। दाएँ या बाएँ स्वाइप से अगला या पिछला गाना या स्लाइड। ऊपर या नीचे स्वाइप से आवाज़, "
                   "और मेरे पढ़ते समय नीचे स्वाइप से आगे। मुट्ठी को नॉब की तरह घुमाइए तो आवाज़, दो उँगलियों से ब्राइटनेस। अंगूठा "
                   "ऊपर तो गाना पसंद। चुटकी से मुझे खिसकाइए, दोनों हाथों की चुटकी से बड़ा या छोटा कीजिए।"},
}


def _tr(key: str, **fmt) -> str:
    return lang.tr(_T, key, **fmt)


_PDF_EXES = {"acrord32.exe", "acrobat.exe", "sumatrapdf.exe", "foxitpdfreader.exe", "foxitreader.exe",
             "pdfxedit.exe", "pdfxcview.exe", "nitropdf.exe"}
_BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}


def page_keys(win: dict) -> Optional[Tuple[int, int]]:
    """(next, previous) keys when a presentation / PDF is in front, else None (swipes are for music)."""
    exe = str(win.get("exe") or "").lower()
    title = str(win.get("title") or "")
    tl = title.lower()
    if exe == "powerpnt.exe" or (exe == "soffice.bin" and "impress" in tl):
        return VK_NEXT, VK_PRIOR
    if exe in _PDF_EXES:
        return VK_NEXT, VK_PRIOR
    if exe in _BROWSERS:
        if re.search(r"google slides|powerpoint|\bslides\b|canva", tl):
            return VK_RIGHT, VK_LEFT
        if re.search(r"\.pdf\b", tl):
            return VK_NEXT, VK_PRIOR
    if exe == "applicationframehost.exe" and re.search(r"\.pdf\b", tl):
        return VK_NEXT, VK_PRIOR
    return None


LIKE_SCOPE = ("user-modify-playback-state user-read-playback-state playlist-read-private "
              "playlist-read-collaborative user-library-read user-library-modify")


def spotify_like() -> Tuple[str, str]:
    """-> ("liked", song) | ("already", song) | ("not_playing", "") | ("need_permission", "") | ("off", "")
    Uses the saved Spotify login only; never opens a login page (this runs in the background)."""
    import spotify_auth
    token, why = spotify_auth.saved_login_token(LIKE_SCOPE)
    if not token:
        return ("off" if why == "off" else "need_permission"), ""
    import spotipy
    sp = spotipy.Spotify(auth=token["access_token"], requests_timeout=5, retries=0)
    pb = sp.current_playback()
    item = (pb or {}).get("item") or {}
    if not pb or not pb.get("is_playing") or not item.get("id"):
        return "not_playing", ""
    song = str(item.get("name") or "")
    if sp.current_user_saved_tracks_contains([item["id"]])[0]:
        return "already", song
    sp.current_user_saved_tracks_add([item["id"]])
    return "liked", song


class GestureActions:
    """Turns engine events into actions. Every Windows / Spotify / window call goes through a small
    backend function, so all of it is testable."""

    def __init__(self, backend: Optional[dict] = None):
        b = self.b = dict(self._default_backend())
        b.update(backend or {})
        self.rect0 = None
        self.last_brightness_at = 0.0
        self.brightness_target: Optional[int] = None

    # -- defaults (Windows)
    @staticmethod
    def _default_backend() -> dict:
        def key(vk):
            import winutil
            winutil.press_volume_key(vk)          # a single extended-key tap (works for any key)

        def get_volume():
            import sysctl
            return sysctl._get_volume()

        def set_volume(p):
            import sysctl
            return sysctl._set_volume(p)

        def get_brightness():
            import sysctl
            return sysctl._get_brightness()

        def set_brightness(p):
            import sysctl
            return sysctl._set_brightness(p)

        def foreground():
            import winutil
            return winutil.foreground_window()

        def speaking():
            try:
                import speaker
                return speaker.is_playing()
            except Exception:  # noqa: BLE001
                return False

        def skip():
            import speaker
            return speaker.skip_current()

        def card(icon, text):
            try:
                import avatar_server
                avatar_server.show_card({"id": "gesture", "kind": "status", "icon": icon, "text": text, "ttl": 2.5})
            except Exception:  # noqa: BLE001
                pass

        def show_card(c):
            try:
                import avatar_server
                avatar_server.show_card(c)
            except Exception:  # noqa: BLE001
                pass

        def hide_card(card_id):
            try:
                import avatar_server
                avatar_server.hide_card(card_id)
            except Exception:  # noqa: BLE001
                pass

        def media_playing():
            try:
                import media_duck
                return media_duck.playing(0.8) if media_duck.can_tell() else None
            except Exception:  # noqa: BLE001
                return None

        def notify(text):
            try:
                import quiet_mode
                quiet_mode.notify(text, icon="♥")
            except Exception:  # noqa: BLE001
                logger.info("gestures: %s", text)

        def controller():
            import avatar_place
            return avatar_place.controller()

        return {"key": key, "get_volume": get_volume, "set_volume": set_volume, "get_brightness": get_brightness,
                "set_brightness": set_brightness, "foreground": foreground, "speaking": speaking, "skip": skip,
                "card": card, "notify": notify, "like": spotify_like, "controller": controller, "now": time.monotonic,
                "show_card": show_card, "hide_card": hide_card, "media_playing": media_playing, "wall": time.time}

    def _on(self, name: str) -> bool:
        return bool(_cfg(name, True))

    # -- the events
    def handle(self, event: str, *args):
        try:
            getattr(self, "_" + event)(*args)
        except Exception:  # noqa: BLE001
            logger.exception("gestures: %s failed", event)

    # -- the hold bar under the orb: fills while you hold a palm / thumbs up; drop your hand to cancel
    def _hold(self, pose: str, total: float, held: float):
        if pose == "palm" and not self._on("GESTURES_MUSIC") or pose == "thumb_up" and not self._on("GESTURES_LIKE"):
            return
        now_ms = int(self.b["wall"]() * 1000)
        self.b["show_card"]({"id": "gesture-hold", "kind": "hold", "icon": "✋" if pose == "palm" else "👍",
                             "text": _tr("hold_play" if pose == "palm" else "hold_like"),
                             "start": now_ms - int(held * 1000), "total": round(float(total), 2),
                             "ttl": round(float(total) + 1.5, 2)})

    def _hold_cancel(self):
        self.b["hide_card"]("gesture-hold")

    def _play_pause(self):
        self.b["hide_card"]("gesture-hold")
        if not self._on("GESTURES_MUSIC"):
            return
        was_playing = self.b["media_playing"]()
        self.b["key"](VK_MEDIA_PLAY_PAUSE)
        if was_playing is None:
            self.b["card"]("⏯️", _tr("play_pause"))
        else:
            self.b["card"]("⏸️" if was_playing else "▶️", _tr("paused" if was_playing else "playing"))
        logger.info("Gesture: open palm -> %s", "play / pause" if was_playing is None else
                    ("pause" if was_playing else "play"))

    def _swipe(self, direction: str):
        if direction in ("left", "right"):
            keys = page_keys(self.b["foreground"]() or {}) if self._on("GESTURES_SLIDES") else None
            nxt = direction == "right"
            if keys is not None:
                self.b["key"](keys[0] if nxt else keys[1])
                self.b["card"]("▶️" if nxt else "◀️", _tr("next_slide" if nxt else "prev_slide"))
                logger.info("Gesture: swipe %s -> %s slide", direction, "next" if nxt else "previous")
            elif self._on("GESTURES_MUSIC"):
                self.b["key"](VK_MEDIA_NEXT if nxt else VK_MEDIA_PREV)
                self.b["card"]("⏭️" if nxt else "⏮️", _tr("next" if nxt else "prev"))
                logger.info("Gesture: swipe %s -> %s song", direction, "next" if nxt else "previous")
            return
        if direction == "down" and self._on("GESTURES_SKIP") and self.b["speaking"]():
            if self.b["skip"]():
                self.b["card"]("⏩", _tr("skip"))
                logger.info("Gesture: swipe down -> skip ahead in what she's reading")
                return
        if self._on("GESTURES_MUSIC"):
            step = int(_cfg("VOLUME_STEP", 10))
            self._volume(step if direction == "up" else -step)

    def _volume(self, delta: int):
        cur = self.b["get_volume"]()
        if cur is None:
            for _ in range(max(1, abs(delta) // 2)):
                self.b["key"](0xAF if delta > 0 else 0xAE)       # the keyboard's volume keys (2 % each)
            return
        target = max(0, min(100, cur + delta))
        if target != cur:
            self.b["set_volume"](target)
        self.b["card"]("🔊" if target else "🔇", _tr("vol", p=target))

    def _knob_start(self, kind: str):
        if self._on("GESTURES_KNOB"):
            self.b["card"]("🎛️", _tr("knob_volume" if kind == "volume" else "knob_brightness"))

    def _knob(self, kind: str, step: int):
        if not self._on("GESTURES_KNOB"):
            return
        if kind == "volume":
            self._volume(step * int(_cfg("GESTURES_VOLUME_STEP", 2)))
            return
        cur = self.brightness_target
        if cur is None:
            cur = self.b["get_brightness"]()
            if cur is None:
                return
        self.brightness_target = max(0, min(100, cur + step * int(_cfg("GESTURES_BRIGHTNESS_STEP", 4))))
        self.b["card"]("🔆", _tr("bright", p=self.brightness_target))
        now = self.b["now"]()
        if now - self.last_brightness_at >= 0.25:              # the screen takes a moment per change
            self.last_brightness_at = now
            self.b["set_brightness"](self.brightness_target)

    def _knob_end(self):
        """End of a turn: the last brightness value is applied, and read fresh next time."""
        if self.brightness_target is not None:
            self.b["set_brightness"](self.brightness_target)
            self.brightness_target = None

    def _like(self):
        self.b["hide_card"]("gesture-hold")
        if not self._on("GESTURES_LIKE"):
            return
        # Spotify can take a few seconds to answer: not on the thread that presses the media keys
        threading.Thread(target=self._like_now, name="gesture-like", daemon=True).start()

    def _like_now(self):
        try:
            result, song = self.b["like"]()
        except Exception:  # noqa: BLE001
            logger.exception("gestures: liking the song failed")
            return
        if result == "liked":
            self.b["card"]("♥", _tr("liked", song=song[:40]))
            logger.info("Gesture: thumbs up -> liked %r on Spotify", song)
        elif result == "already":
            self.b["card"]("♥", _tr("already"))
        elif result == "need_permission":
            self.b["notify"](_tr("like_perm"))

    # -- her window
    def _ctrl(self):
        return self.b["controller"]() if self._on("GESTURES_WINDOW") else None

    def _drag_start(self):
        c = self._ctrl()
        self.rect0 = c.get_rect() if c is not None else None
        if self.rect0 is not None:
            self.b["card"]("🤏", _tr("moving"))

    def _drag(self, dx: float, dy: float):
        c = self._ctrl()
        if c is None or self.rect0 is None:
            return
        import avatar_place
        areas = c.areas()
        home = avatar_place.home_area(self.rect0, areas) or (0, 0, 1920, 1040)
        gain = float(_cfg("GESTURES_DRAG_GAIN", 1.6))
        x, y, w, h = self.rect0
        nx = x + int(round(dx * gain * (home[2] - home[0])))
        ny = y + int(round(dy * gain * (home[3] - home[1])))
        c.set_rect((nx, ny, w, h))

    def _drag_end(self):
        c = self._ctrl()
        if c is None or self.rect0 is None:
            return
        import avatar_place
        cur = c.get_rect()
        if cur is not None:
            fixed = avatar_place.fit_rect(cur, c.areas())
            if fixed != cur:
                c.set_rect(fixed)
            avatar_place.note(c.style, fixed)
        self.rect0 = None

    def _resize_start(self):
        c = self._ctrl()
        self.rect0 = c.get_rect() if c is not None else None
        if self.rect0 is not None:
            self.b["card"]("↔️", _tr("sizing"))

    def _resize(self, k: float):
        c = self._ctrl()
        if c is None or self.rect0 is None:
            return
        import avatar_place
        new, _limit = avatar_place.scale_rect(self.rect0, max(0.2, min(5.0, k)), c.areas(),
                                              min_side=int(avatar_place.MIN_SIZE * c.scale()))
        c.set_rect(new)

    def _resize_end(self):
        c = self._ctrl()
        if c is not None and self.rect0 is not None:
            import avatar_place
            cur = c.get_rect()
            if cur is not None:
                avatar_place.note(c.style, cur)
        self.rect0 = None


# ======================================================================== the camera thread

def model_path() -> str:
    return str(_cfg("GESTURES_MODEL", os.path.join(getattr(config, "_SCRIPT_DIR", "."), "models",
                                                   "gesture_recognizer.task")))


def _state_path() -> str:
    return os.path.join(getattr(config, "_SCRIPT_DIR", "."), "gestures_state.json")


def _load_watching() -> bool:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return bool(json.load(f).get("watching", True))
    except (OSError, ValueError, AttributeError):
        return True


def _save_watching(on: bool):
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump({"watching": bool(on)}, f)
    except OSError:
        logger.warning("gestures: couldn't save the on/off state")


class Recognizer:
    """MediaPipe's gesture recogniser, video mode. hands(rgb_frame, t_ms) -> [Hand]."""

    def __init__(self, path: str):
        os.environ.setdefault("MPLBACKEND", "Agg")          # mediapipe imports matplotlib; no windows
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        self._mp = mp
        opts = mp_vision.GestureRecognizerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=path),
            running_mode=mp_vision.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.6, min_tracking_confidence=0.5)
        self._rec = mp_vision.GestureRecognizer.create_from_options(opts)
        self._last_ms = -1

    def hands(self, rgb, t_ms: int) -> List[Hand]:
        t_ms = max(int(t_ms), self._last_ms + 1)            # video mode needs rising timestamps
        self._last_ms = t_ms
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self._rec.recognize_for_video(img, t_ms)
        out = []
        for i, lms in enumerate(res.hand_landmarks or []):
            g = res.gestures[i][0] if res.gestures and i < len(res.gestures) and res.gestures[i] else None
            hd = res.handedness[i][0] if res.handedness and i < len(res.handedness) and res.handedness[i] else None
            out.append(Hand([(p.x, p.y, p.z) for p in lms], g.category_name if g else "None",
                            float(g.score) if g else 0.0, hd.category_name if hd else ""))
        return out

    def close(self):
        # closed (and dropped) while MediaPipe's own worker still runs: left to the garbage collector at exit it
        # printed "Exception ignored in GestureRecognizer.__del__ ... cannot schedule new futures after shutdown"
        rec, self._rec = self._rec, None
        if rec is None:
            return
        try:
            rec.close()
        except Exception:  # noqa: BLE001
            pass


class Camera:
    """The webcam, mirrored, RGB. OpenCV; DirectShow first on Windows (opens fast)."""

    def __init__(self, index: int = 0, width: int = 640, height: int = 480):
        import cv2
        self.cv2 = cv2
        self.cap = None
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_ANY]
        for be in backends:
            cap = cv2.VideoCapture(index, be)
            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                ok, _ = cap.read()
                if ok:
                    self.cap = cap
                    break
            if cap is not None:
                cap.release()
        if self.cap is None:
            raise OSError("the camera can't be opened")

    def grab(self) -> bool:
        """Takes the next frame without decoding it (keeps the picture fresh between processed frames)."""
        return bool(self.cap.grab())

    def read(self):
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        frame = self.cv2.flip(frame, 1)                       # like a mirror: your right is the frame's right
        return self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)

    def close(self):
        try:
            self.cap.release()
        except Exception:  # noqa: BLE001
            pass


_busy = threading.Event()


def set_busy(flag: bool):
    """main.py: Whisper is transcribing your sentence - recognition pauses meanwhile (both want the CPU,
    and the turn matters more)."""
    if flag:
        _busy.set()
    else:
        _busy.clear()


# The last couple of seconds of hand points before every gesture that DID something, one JSON line each
# (gestures_debug.jsonl: numbers only, no pictures), so a gesture that fired by mistake can be looked
# at afterwards and the thresholds tuned from real hands. GESTURES_DEBUG_LOG = False turns it off.
DEBUG_EVENTS = {"play_pause", "swipe", "like", "knob_start", "drag_start", "resize_start"}
DEBUG_MAX_BYTES = 2_000_000


def _debug_path() -> str:
    return os.path.join(getattr(config, "_SCRIPT_DIR", "."), "gestures_debug.jsonl")


def _hand_json(h: Hand) -> dict:
    return {"g": h.gesture, "s": round(float(h.score), 2), "l": h.label,
            "p": [[round(float(x), 4), round(float(y), 4), round(float(z), 4)] for x, y, z in h.pts]}


class Watcher:
    """Owns the camera thread and the actions thread."""

    def __init__(self, make_camera=None, make_recognizer=None, actions: Optional[GestureActions] = None,
                 in_call: Optional[Callable[[], bool]] = None, clock=time.monotonic):
        self.make_camera = make_camera or (lambda: Camera(int(_cfg("GESTURES_CAMERA", 0))))
        self.make_recognizer = make_recognizer or (lambda: Recognizer(model_path()))
        self.actions = actions or GestureActions()
        self.in_call = in_call or _in_call
        self.clock = clock
        self.watching = _load_watching()
        self.status = "starting"          # starting | watching | paused_call | off | no_model | no_camera | error
        self.events: "queue.Queue" = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.engine = GestureEngine(self._emit, self._settings())
        self._threads: List[threading.Thread] = []
        self.frames = 0
        self.hands_seen = 0
        self.debug = bool(_cfg("GESTURES_DEBUG_LOG", True))
        self._recent: Deque[tuple] = deque(maxlen=40)       # ~2.5 s of hand points (for the debug log)

    @staticmethod
    def _settings() -> EngineSettings:
        s = EngineSettings()
        s.hold = float(_cfg("GESTURES_HOLD_SECONDS", s.hold))
        s.swipe = float(_cfg("GESTURES_SWIPE_DISTANCE", s.swipe))
        s.knob_degrees = float(_cfg("GESTURES_KNOB_DEGREES", s.knob_degrees))
        s.raised = float(_cfg("GESTURES_RAISED", s.raised))
        s.upright = float(_cfg("GESTURES_UPRIGHT_DEGREES", s.upright))
        return s

    def _emit(self, event: str, *args):
        try:
            if event in ("drag", "resize"):                  # only the latest position matters
                self._drop_stale(event)
            self.events.put_nowait((event, args))
            if self.debug and event in DEBUG_EVENTS:
                self.events.put_nowait(("_debug", (event, args, list(self._recent))))
        except queue.Full:
            pass

    @staticmethod
    def _write_debug(event: str, args: tuple, frames: list):
        path = _debug_path()
        try:
            if os.path.exists(path) and os.path.getsize(path) > DEBUG_MAX_BYTES:
                os.replace(path, path[:-len(".jsonl")] + ".old.jsonl")
            line = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "args": list(args),
                               "frames": frames}, separators=(",", ":"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("gestures: couldn't write the debug log", exc_info=True)

    def _drop_stale(self, event: str):
        kept = []
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] != event:
                    kept.append(item)
        except queue.Empty:
            pass
        for item in kept:
            self.events.put_nowait(item)

    def set_watching(self, on: bool):
        self.watching = bool(on)
        _save_watching(self.watching)
        self.wake.set()
        if on:
            self.ensure_running()

    # -- threads
    def start(self):
        for target, name in ((self._camera_loop, "gesture-camera"), (self._action_loop, "gesture-actions")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def ensure_running(self) -> bool:
        """Restarts the camera thread if it ended after an error (not when the model is missing)."""
        cam_threads = [t for t in self._threads if t.name == "gesture-camera"]
        if self.stop_event.is_set() or not cam_threads or cam_threads[-1].is_alive() or self.status == "no_model":
            return False
        logger.info("Hand gestures: starting the camera thread again")
        self.status = "starting"
        t = threading.Thread(target=self._camera_loop, name="gesture-camera", daemon=True)
        t.start()
        self._threads.append(t)
        return True

    def stop(self, wait: float = 1.5):
        self.stop_event.set()
        self.wake.set()
        try:
            self.events.put_nowait(("_stop", ()))
        except queue.Full:
            pass
        # let the camera loop close the recognizer itself before Python starts shutting down
        me = threading.current_thread()
        for t in list(self._threads):
            if t is not me and t.is_alive() and wait > 0:
                t.join(timeout=wait)

    def _action_loop(self):
        while not self.stop_event.is_set():
            try:
                event, args = self.events.get(timeout=1.0)
            except queue.Empty:
                continue
            if event == "_stop":
                return
            if event == "_debug":
                self._write_debug(*args)
                continue
            self.actions.handle(event, *args)

    def _wait(self, seconds: float) -> bool:
        """False = stopping."""
        self.wake.wait(seconds)
        self.wake.clear()
        return not self.stop_event.is_set()

    def _camera_loop(self):
        if not os.path.isfile(model_path()):
            self.status = "no_model"
            logger.info("Hand gestures are off: the model isn't there yet (%s) - run install_gestures.bat", model_path())
            return
        try:
            rec = self.make_recognizer()
        except Exception as e:  # noqa: BLE001
            self.status = "no_model"
            logger.warning("Hand gestures are off: couldn't load MediaPipe (%s) - run install_gestures.bat", e)
            return
        cam = None
        told = False
        errors = 0
        fps = max(5.0, float(_cfg("GESTURES_FPS", 15)))
        idle_fps = 6.0
        last_hand = 0.0
        next_frame = 0.0
        paused = False
        try:
            while not self.stop_event.is_set():
                if _busy.is_set():                              # Whisper is working: skip frames meanwhile
                    if not paused:
                        paused = True
                        self.engine.reset()
                    self.stop_event.wait(0.1)
                    continue
                paused = False
                if not self.watching or (_cfg("GESTURES_PAUSE_IN_CALLS", True) and self.in_call()):
                    if cam is not None:
                        cam.close()
                        cam = None
                        self.engine.reset()
                        logger.info("Hand gestures: camera released (%s)",
                                    "a call / another app is using the camera" if self.watching else "switched off")
                    self.status = "off" if not self.watching else "paused_call"
                    if not self._wait(2.0):
                        return
                    continue
                if cam is None:
                    try:
                        cam = self.make_camera()
                        self.status = "watching"
                        told = False
                        logger.info("Hand gestures: watching (camera %s)", _cfg("GESTURES_CAMERA", 0))
                    except Exception as e:  # noqa: BLE001
                        self.status = "no_camera"
                        if not told:
                            logger.warning("Hand gestures: can't open the camera (%s) - trying again in a minute", e)
                            told = True
                        if not self._wait(60.0):
                            return
                        continue
                wait = next_frame - self.clock()
                if wait > 0:
                    # just wait: read() below returns a fresh picture anyway (and DirectShow's grab()
                    # returns at once, so grabbing in a loop would keep a CPU core busy)
                    self.stop_event.wait(min(wait, 0.2))
                    continue
                rgb = cam.read()
                if rgb is None:
                    cam.close()
                    cam = None
                    self.engine.reset()
                    logger.info("Hand gestures: the camera stopped sending pictures - reopening in a moment")
                    if not self._wait(5.0):
                        return
                    continue
                t = self.clock()
                try:
                    hands = rec.hands(rgb, int(t * 1000))
                    if self.debug:
                        self._recent.append((round(t, 3), [_hand_json(h) for h in hands]))
                    self.engine.update(t, hands)
                    errors = 0
                except Exception:  # noqa: BLE001 - one bad frame must not end gestures
                    errors += 1
                    if errors == 1:
                        logger.exception("Hand gestures: a frame failed")
                    if errors >= 20:
                        logger.warning("Hand gestures: 20 frames in a row failed - restarting the recogniser")
                        rec.close()
                        rec = self.make_recognizer()
                        errors = 0
                    next_frame = t + 0.5
                    continue
                self.frames += 1
                if hands:
                    last_hand = t
                    self.hands_seen += 1
                rate = fps if t - last_hand < 10.0 else idle_fps
                next_frame = t + 1.0 / rate
        except Exception:  # noqa: BLE001
            self.status = "error"
            logger.exception("Hand gestures stopped after an error")
        finally:
            if cam is not None:
                cam.close()
            rec.close()


_busy_cache = {"at": -1e9, "value": False}
# Apps that open the camera themselves (often for a preview before a call): she lets go while one is
# in front, so it can have the camera even on webcams that only allow one app at a time.
_CAMERA_APPS = {"zoom.exe", "ms-teams.exe", "teams.exe", "skype.exe", "webexmta.exe", "ciscocollabhost.exe",
                "atmgr.exe", "windowscamera.exe", "obs64.exe", "obs32.exe"}
_CAMERA_TITLES = re.compile(r"^meet\b|google meet|zoom meeting|^zoom\b|microsoft teams|jitsi|whereby|webex",
                            re.IGNORECASE)


def _camera_app_in_front() -> bool:
    try:
        import winutil
        w = winutil.foreground_window()
    except Exception:  # noqa: BLE001
        return False
    exe = str(w.get("exe") or "").lower()
    if exe in _CAMERA_APPS:
        return True
    return exe in _BROWSERS and bool(_CAMERA_TITLES.search(str(w.get("title") or "")))


def _session_locked() -> bool:
    """The PC is locked (Win+L): no one to watch."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32")
        desk = user32.OpenInputDesktop(0, False, 0x0100)         # DESKTOP_SWITCHDESKTOP
        if not desk:
            return True
        user32.CloseDesktop(desk)
        return False
    except Exception:  # noqa: BLE001
        return False


def _in_call() -> bool:
    """Let go of the camera: in a call (quiet_mode.py), another app using / about to use the webcam, or
    the PC is locked. Checked at most every 1.5 seconds (the camera loop asks every frame)."""
    now = time.monotonic()
    if now - _busy_cache["at"] < 1.5:
        return _busy_cache["value"]
    value = False
    try:
        import quiet_mode
        value = quiet_mode.mode() == "call" or bool(quiet_mode.camera_users())
    except Exception:  # noqa: BLE001
        value = False
    value = value or _camera_app_in_front() or _session_locked()
    _busy_cache.update(at=now, value=value)
    return value


_watcher: Optional[Watcher] = None


def start() -> Optional[Watcher]:
    """main.py: starts watching (if GESTURES_ENABLED)."""
    global _watcher
    if not _cfg("GESTURES_ENABLED", True):
        return None
    if _watcher is not None:
        return _watcher
    _watcher = Watcher()
    _watcher.start()
    try:
        import shutdown
        shutdown.on_shutdown(_watcher.stop)
    except Exception:  # noqa: BLE001
        pass
    return _watcher


# ======================================================================== voice commands

_POLITE = r"(?:(?:please|raziel|hey|ok|okay|can you|could you)\s+)*"
_ON_RE = re.compile(rf"^{_POLITE}(?:watch my hands|start watching(?: my hands| me)?|watch for (?:hand )?gestures|"
                    rf"(?:turn on|switch on|enable|start) (?:the )?(?:hand )?gestures?(?: control| mode)?|"
                    rf"(?:hand )?gestures?(?: control| mode)? on)(?:\s+(?:please|again|now))*$")
_OFF_RE = re.compile(rf"^{_POLITE}(?:stop watching(?: my hands| me)?|don't watch(?: me| my hands)?|"
                     rf"stop looking at me|(?:turn off|switch off|disable|stop) (?:the )?(?:hand )?gestures?(?: control| mode)?|"
                     rf"(?:hand )?gestures?(?: control| mode)? off)(?:\s+(?:please|now))*$")
_STATUS_RE = re.compile(rf"^{_POLITE}(?:are you watching(?: me| my hands)?|is (?:hand )?gesture(?: control| mode)? on|"
                        rf"can you see my hands?)$")
_HELP_RE = re.compile(rf"^{_POLITE}(?:what|which) (?:hand )?gestures (?:do you know|can you see|can i use|are there|work)|"
                      rf"^{_POLITE}how do (?:hand )?gestures work$")
_HI_ON_RE = re.compile(r"^(?:मेरे\s+)?हाथ\s+देखो$|^(?:mere\s+)?haath\s+dekho$|^इशारे\s+(?:चालू|ऑन)\s+करो$")
_HI_OFF_RE = re.compile(r"^देखना\s+बंद\s+करो$|^कैमरा\s+बंद\s+करो$|^इशारे\s+(?:बंद|ऑफ)\s+करो$|^dekhna\s+band\s+karo$")


def _norm(transcript: str) -> str:
    t = (transcript or "").lower().replace("’", "'")
    t = re.sub(r"[^\w\s'ऀ-ॿ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def try_handle(transcript: str) -> Optional[str]:
    t = _norm(transcript)
    if not t:
        return None
    on = bool(_ON_RE.match(t) or _HI_ON_RE.match(t))
    off = bool(_OFF_RE.match(t) or _HI_OFF_RE.match(t))
    status = bool(_STATUS_RE.match(t))
    helpq = bool(_HELP_RE.match(t))
    if not (on or off or status or helpq):
        return None
    if helpq:
        return _tr("help")
    if not _cfg("GESTURES_ENABLED", True):
        return _tr("disabled")
    w = _watcher
    if w is None:
        return _tr("not_set_up") if not os.path.isfile(model_path()) else _tr("disabled")
    if off:
        w.set_watching(False)
        return _tr("off")
    restarted = bool(getattr(w, "ensure_running", lambda: False)())
    if on:
        w.set_watching(True)
    if w.status == "no_model":
        return _tr("not_set_up")
    if restarted:
        return _tr("status_error")
    if not w.watching:
        return _tr("status_off")
    if w.status == "no_camera":
        return _tr("no_camera")
    if on:
        return _tr("on")
    if w.status == "paused_call":
        return _tr("status_call")
    return _tr("status_on")


# ======================================================================== "python gestures.py --check"

def _check():
    """A window showing what she sees: the hands, their pose, and the gestures she would act on
    (nothing is done). Esc closes it."""
    import cv2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not os.path.isfile(model_path()):
        print("The model isn't there yet:", model_path(), "- run install_gestures.bat first.")
        return 1
    rec = Recognizer(model_path())
    try:
        cam = Camera(int(_cfg("GESTURES_CAMERA", 0)))
    except OSError:
        print("Can't open the camera. Close Raziel (or say 'stop watching') and any app using it, then try again.")
        return 1
    log: Deque[str] = deque(maxlen=6)

    def show(e, *a):
        text = " ".join(str(round(x, 2)) if isinstance(x, float) else str(x) for x in a)
        log.append(f"{time.strftime('%H:%M:%S')}  {e} {text}")
        print("gesture:", e, text)

    eng = GestureEngine(show, Watcher._settings())
    print("Showing the camera. Esc (or Q) closes the window.")
    try:
        _check_loop(cv2, rec, cam, eng, log)
    finally:
        cam.close()
        rec.close()
        cv2.destroyAllWindows()
    return 0


def _check_loop(cv2, rec, cam, eng, log):
    while True:
        rgb = cam.read()
        if rgb is None:
            break
        t = time.monotonic()
        hands = rec.hands(rgb, int(t * 1000))
        eng.update(t, hands)
        img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        H, W = img.shape[:2]
        for h in hands:
            for x, y, _z in h.pts:
                cv2.circle(img, (int(x * W), int(y * H)), 3, (80, 220, 255), -1)
            x, y = h.p(WRIST)
            pose = pose_of(h)
            shown = eng.shown(h, pose)
            cv2.putText(img, f"{pose} ({h.gesture} {h.score:.2f}) size {hand_size(h):.2f}",
                        (int(x * W) - 60, int(y * H) + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.putText(img, f"{'COUNTS' if shown else 'ignored'}: lean {upright_degrees(h):.0f} deg, "
                             f"height {palm_center(h)[1]:.2f}",
                        (int(x * W) - 60, int(y * H) + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (80, 255, 80) if shown else (80, 80, 255), 1)
        # the line a palm must be above to count
        cv2.line(img, (0, int(eng.s.raised * H)), (W, int(eng.s.raised * H)), (90, 90, 90), 1)
        cv2.putText(img, f"mode: {eng.mode or '-'} {eng.kind}  hold: {eng.holding or '-'}", (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        for i, line in enumerate(log):
            cv2.putText(img, line, (10, H - 12 - 20 * (len(log) - 1 - i)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
        cv2.imshow("Raziel - hand gestures (Esc to close)", img)
        if cv2.waitKey(1) & 0xFF in (27, ord("q")):
            break


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_check())
    print(__doc__)
