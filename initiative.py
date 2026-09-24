"""
initiative.py — lets Raziel speak first.

The problem with proactive assistants is not making them talk. It's making them
shut up. An assistant that pipes up every ninety seconds is unbearable within a
day, and the failure is invisible during testing because you're paying attention
then. So the design here is mostly restraint:

  * Every trigger has its own cooldown, on top of a global one.
  * An hourly budget caps total unprompted speech.
  * If she speaks twice and you don't answer, she stops until you talk to her.
  * Quiet hours are hard-blocked.
  * Most "initiative" is non-verbal — a glance, a head tilt. Presence without
    interruption, and it costs nothing to ignore.

Self-triggering matters here. Per the project notes, BARGE_IN_RMS_THRESHOLD is
tuned for headphones; on speakers the mic hears her own voice. Proactive speech
makes that worse because it fires when you aren't expecting it, so this module
holds a mic-suppression window open across and just after her own speech.

Wiring (see bottom of file for the full integration block):

    import initiative
    engine = initiative.Initiative(
        speak=speaker.speak,
        memory=memory,
        avatar=avatar_server,
        llm=llm_brain,
    )
    engine.start()
    # ... in the voice loop, around each user turn:
    engine.user_turn_started()
    engine.user_turn_ended(transcript)
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------- tunables
# Copy these into config.py if you'd rather keep all tunables in one place;
# the constructor accepts overrides for every one of them.

INITIATIVE_ENABLED = True

GLOBAL_COOLDOWN_S = 240        # minimum gap between any two unprompted lines
MAX_PER_HOUR = 6               # hard ceiling on unprompted speech
IGNORED_LIMIT = 2              # unanswered lines before she goes quiet
IGNORED_WINDOW_S = 90          # how long she waits before counting one ignored
QUIET_HOURS = (1, 8)           # 01:00–07:59, no unprompted speech at all

NONVERBAL_MIN_S = 25           # glances and shifts are much more frequent
NONVERBAL_MAX_S = 70

MIC_SUPPRESS_TAIL_S = 0.6      # keep the mic gated this long after she stops

TICK_S = 2.0                   # how often the engine re-evaluates


# ---------------------------------------------------------------- triggers

@dataclass
class Trigger:
    name: str
    cooldown: int                      # seconds between firings of THIS trigger
    weight: int                        # relative likelihood when several match
    lines: List[str]
    condition: Callable[["Context"], bool]
    prompt: Optional[str] = None       # optional LLM prompt for a fresher line
    last_fired: float = field(default=0.0)


@dataclass
class Context:
    """Everything a trigger needs to decide whether it applies."""
    now: _dt.datetime
    idle_s: float                      # since the user last said anything
    session_turns: int                 # turns in the current conversation
    absence_s: float                   # gap before the current session began
    last_user_text: str
    last_reply_text: str
    facts: Dict[str, str]              # keyed facts from memory.db
    first_contact_today: bool


def _hour(ctx: Context) -> int:
    return ctx.now.hour


# The line banks are deliberately short and specific. Generic filler ("Is there
# anything else?") is what makes assistants feel like software.
TRIGGERS: List[Trigger] = [

    Trigger(
        name="return_after_absence",
        cooldown=3 * 3600,
        weight=5,
        condition=lambda c: c.absence_s > 4 * 3600 and c.session_turns == 0,
        lines=[
            "You've been gone a while. Systems have been idle.",
            "Back. I logged four hours of nothing.",
            "You were away longer than usual.",
        ],
        prompt="The user has just returned after being away for hours. "
               "Greet them in one short line.",
    ),

    Trigger(
        name="late_night",
        cooldown=6 * 3600,
        weight=4,
        condition=lambda c: _hour(c) >= 0 and _hour(c) < 3 and c.session_turns > 2,
        lines=[
            "It is past midnight. You have been at this a while.",
            "Late. Your response times are slower than they were an hour ago.",
            "Still running. So are you, apparently.",
        ],
    ),

    Trigger(
        name="idle_checkin",
        cooldown=15 * 60,
        weight=3,
        condition=lambda c: 300 < c.idle_s < 2400 and c.session_turns > 0,
        lines=[
            "Still here if you need me.",
            "Standing by.",
            "You went quiet. I'm listening whenever.",
        ],
    ),

    Trigger(
        name="long_silence",
        cooldown=45 * 60,
        weight=2,
        condition=lambda c: c.idle_s > 2400,
        lines=[
            "It has been quiet for a while. I'll stay out of the way.",
            "Forty minutes of nothing. I'm here.",
        ],
    ),

    Trigger(
        name="memory_followup",
        cooldown=2 * 3600,
        weight=6,
        condition=lambda c: bool(c.facts) and c.idle_s > 240 and c.session_turns > 1,
        lines=[],  # generated from the fact itself, see _memory_line
        prompt="Reference something the user told you earlier, in one short line.",
    ),

    Trigger(
        name="first_today",
        cooldown=12 * 3600,
        weight=5,
        condition=lambda c: c.first_contact_today and c.session_turns == 0,
        lines=[
            "First contact today. Ready.",
            "Systems nominal. Standing by.",
            "You're up. I'm ready when you are.",
        ],
    ),
]


# ---------------------------------------------------------------- engine

# Set this to the live Initiative instance right after constructing it in
# main.py: `initiative.engine = engine`. That lets wake_word.py and
# audio_recorder.py check `initiative.engine.mic_should_ignore()` by just
# importing this module — no need to import main.py (which would risk a
# circular import if main.py ever imports either of those back) and no need
# to thread the engine through every function signature by hand.
engine: "Optional[Initiative]" = None


class Initiative:
    def __init__(
        self,
        speak: Callable[[str], None],
        memory=None,
        avatar=None,
        llm=None,
        enabled: bool = INITIATIVE_ENABLED,
        global_cooldown: int = GLOBAL_COOLDOWN_S,
        max_per_hour: int = MAX_PER_HOUR,
        quiet_hours=QUIET_HOURS,
        use_llm: bool = False,
    ):
        self.speak_fn = speak
        self.memory = memory
        self.avatar = avatar
        self.llm = llm
        self.enabled = enabled
        self.global_cooldown = global_cooldown
        self.max_per_hour = max_per_hour
        self.quiet_hours = quiet_hours
        self.use_llm = use_llm

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        now = time.time()
        self.last_user_time = now
        self.last_speak_time = 0.0
        self.last_nonverbal = now
        self.session_start = now
        self.session_turns = 0
        self.absence_s = 0.0
        self.last_user_text = ""
        self.last_reply_text = ""
        self.spoken_times: List[float] = []
        self.ignored_count = 0
        self.pending_since: Optional[float] = None
        self.quiet_mode = False
        self._busy = False
        self._suppress_until = 0.0
        self._last_contact_date: Optional[_dt.date] = None
        self._next_nonverbal = random.uniform(NONVERBAL_MIN_S, NONVERBAL_MAX_S)

    # ---------------------------------------------------------- lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="initiative", daemon=True)
        self._thread.start()
        try:
            import shutdown
            shutdown.on_shutdown(self.stop)
        except Exception:
            pass

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------- signals in
    # main.py calls these so the engine knows what's happening.

    def user_turn_started(self):
        """User is speaking or being transcribed. Never interrupt this."""
        with self._lock:
            self._busy = True
            # She spoke and got an answer, so nothing was ignored.
            self.pending_since = None
            self.ignored_count = 0
            self.quiet_mode = False

    def user_turn_ended(self, transcript: str = ""):
        with self._lock:
            self._busy = False
            now = time.time()
            if now - self.last_user_time > 4 * 3600:
                self.absence_s = now - self.last_user_time
                self.session_start = now
                self.session_turns = 0
            self.last_user_time = now
            self.last_user_text = transcript or ""
            self.session_turns += 1

    def assistant_replied(self, text: str = ""):
        with self._lock:
            self.last_reply_text = text or ""
            self._busy = False

    def set_busy(self, flag: bool):
        """Block initiative during tool execution, playback, anything blocking."""
        with self._lock:
            self._busy = bool(flag)

    def suppress(self, seconds: float):
        """Hold initiative off for a while (e.g. during a long tool run)."""
        with self._lock:
            self._suppress_until = max(self._suppress_until, time.time() + seconds)

    def is_busy(self) -> bool:
        """True while a user turn is in progress or she is speaking / just spoke. Timers and reminders
        (reminders.Scheduler) wait for a quiet moment instead of talking over a conversation."""
        with self._lock:
            return bool(self._busy) or time.time() < self._suppress_until

    def mic_should_ignore(self) -> bool:
        """
        True while she is speaking and for a short tail afterwards.

        The wake word and voice-trigger loops should check this and skip, or on
        speakers her own proactive speech will wake her up. This is the single
        most important integration point in this file.
        """
        return time.time() < self._suppress_until

    # ---------------------------------------------------------- main loop

    def _loop(self):
        while not self._stop.wait(TICK_S):
            try:
                self._tick()
            except Exception as exc:
                print(f"[initiative] tick failed: {exc}")

    def _tick(self):
        if not self.enabled:
            return
        with self._lock:
            now = time.time()

            # Did she speak and get no answer?
            if self.pending_since and now - self.pending_since > IGNORED_WINDOW_S:
                self.ignored_count += 1
                self.pending_since = None
                if self.ignored_count >= IGNORED_LIMIT:
                    self.quiet_mode = True

            if self._busy or now < self._suppress_until:
                return

            # Non-verbal initiative runs even in quiet mode. Looking up at
            # someone is not an interruption.
            if now - self.last_nonverbal > self._next_nonverbal:
                self.last_nonverbal = now
                self._next_nonverbal = random.uniform(NONVERBAL_MIN_S, NONVERBAL_MAX_S)
                self._nonverbal()

            if self.quiet_mode:
                return
            if not self._budget_ok(now):
                return

            ctx = self._context()
            if self._in_quiet_hours(ctx.now):
                return

            trigger = self._select(ctx, now)
            if trigger:
                self._fire(trigger, ctx, now)

    # ---------------------------------------------------------- selection

    def _budget_ok(self, now: float) -> bool:
        if now - self.last_speak_time < self.global_cooldown:
            return False
        hour_ago = now - 3600
        self.spoken_times = [t for t in self.spoken_times if t > hour_ago]
        return len(self.spoken_times) < self.max_per_hour

    def _in_quiet_hours(self, now: _dt.datetime) -> bool:
        start, end = self.quiet_hours
        h = now.hour
        return start <= h < end if start < end else (h >= start or h < end)

    def _context(self) -> Context:
        now_dt = _dt.datetime.now()
        facts = {}
        if self.memory is not None:
            for getter in ("all_facts", "get_all_facts", "list_facts"):
                fn = getattr(self.memory, getter, None)
                if callable(fn):
                    try:
                        raw = fn()
                        facts = dict(raw) if not isinstance(raw, dict) else raw
                        break
                    except Exception:
                        pass
        first_today = self._last_contact_date != now_dt.date()
        return Context(
            now=now_dt,
            idle_s=time.time() - self.last_user_time,
            session_turns=self.session_turns,
            absence_s=self.absence_s,
            last_user_text=self.last_user_text,
            last_reply_text=self.last_reply_text,
            facts=facts,
            first_contact_today=first_today,
        )

    def _select(self, ctx: Context, now: float) -> Optional[Trigger]:
        eligible = []
        for t in TRIGGERS:
            if now - t.last_fired < t.cooldown:
                continue
            try:
                if t.condition(ctx):
                    eligible.append(t)
            except Exception:
                continue
        if not eligible:
            return None
        total = sum(t.weight for t in eligible)
        r = random.uniform(0, total)
        for t in eligible:
            r -= t.weight
            if r <= 0:
                return t
        return eligible[0]

    # ---------------------------------------------------------- firing

    def _fire(self, trigger: Trigger, ctx: Context, now: float):
        line = self._line_for(trigger, ctx)
        if not line:
            return

        trigger.last_fired = now
        self.last_speak_time = now
        self.spoken_times.append(now)
        self.pending_since = now
        self._last_contact_date = ctx.now.date()

        # Gate the mic for the duration plus a tail. Rough estimate: ~13
        # characters per second of speech at PIPER_LENGTH_SCALE 1.15.
        est = max(1.2, len(line) / 13.0)
        self._suppress_until = now + est + MIC_SUPPRESS_TAIL_S

        if self.avatar is not None:
            try:
                self.avatar.set_emotion("confident", 0.8)
            except Exception:
                pass

        print(f"[initiative] {trigger.name}: {line}")
        threading.Thread(target=self._speak_safe, args=(line,), daemon=True).start()

    def _speak_safe(self, line: str):
        try:
            self.speak_fn(line)
        except Exception as exc:
            print(f"[initiative] speak failed: {exc}")
        finally:
            # Release the mic gate as soon as playback actually finishes, which
            # is more accurate than the character-count estimate.
            with self._lock:
                self._suppress_until = time.time() + MIC_SUPPRESS_TAIL_S

    def _line_for(self, trigger: Trigger, ctx: Context) -> str:
        if trigger.name == "memory_followup":
            return self._memory_line(ctx)
        if self.use_llm and trigger.prompt and self.llm is not None:
            generated = self._llm_line(trigger, ctx)
            if generated:
                return generated
        return random.choice(trigger.lines) if trigger.lines else ""

    def _memory_line(self, ctx: Context) -> str:
        favourites = {k: v for k, v in ctx.facts.items() if k.startswith("favorite_")}
        if not favourites:
            return ""
        key, content = random.choice(list(favourites.items()))
        category = key.replace("favorite_", "").replace("_", " ")
        # memory stores the whole sentence ("User's favorite musician is
        # Arijit Singh"); the lines below want just the value.
        value = re.sub(r"^user's favou?rite .+? is\s+", "", str(content),
                       flags=re.IGNORECASE).strip().rstrip(".")
        if not value:
            return ""
        return random.choice([
            f"You never did say why {value} is your favourite {category}.",
            f"Your favourite {category} is still logged as {value}.",
            f"Filed under {category}: {value}. Still accurate?",
        ])

    def _llm_line(self, trigger: Trigger, ctx: Context) -> str:
        """
        Optional. qwen3:8b is unreliable at structured output, so this is
        heavily guarded and falls back to the template bank on anything odd.
        """
        try:
            prompt = (
                f"{trigger.prompt}\n"
                f"Time: {ctx.now.strftime('%H:%M')}. "
                f"Last thing the user said: {ctx.last_user_text[:120] or '(nothing yet)'}\n"
                "Reply with ONE sentence, under 15 words, no quotes, no preamble."
            )
            raw = self.llm.quick_completion(prompt) if hasattr(self.llm, "quick_completion") else None
            if not raw:
                return ""
            line = str(raw).strip().strip('"').split("\n")[0]
            # Sanity gate: reject anything long, empty, or that looks like the
            # model narrating instead of speaking.
            if not line or len(line) > 120 or len(line.split()) > 20:
                return ""
            if any(bad in line.lower() for bad in ("as an ai", "<think", "tool_call", "here is")):
                return ""
            return line
        except Exception:
            return ""

    # ---------------------------------------------------------- non-verbal

    def _nonverbal(self):
        """
        A glance, a shift, a head tilt. This is the cheapest way to feel present
        and it never interrupts anything, so it runs far more often than speech
        and keeps running in quiet mode.
        """
        if self.avatar is None:
            return
        idle = time.time() - self.last_user_time
        if idle < 20:
            pool = ["tilt", "nod", "settle", "armFidget"]
        elif idle < 300:
            pool = ["scanRoom", "hairTouch", "weightRock", "settle", "armFidget"]
        else:
            pool = ["scanRoom", "stretch", "weightRock", "torsoTwist"]
        try:
            self.avatar.gesture(random.choice(pool), random.uniform(0.5, 1.0))
        except Exception:
            pass


# ---------------------------------------------------------------- integration
"""
main.py
-------
    import initiative

    engine = initiative.Initiative(
        speak=speaker.speak,
        memory=memory,
        avatar=avatar_server,
        llm=llm_brain,
        use_llm=False,          # start with templates; turn on once stable
    )
    engine.start()
    shutdown.on_shutdown(engine.stop)

In the voice loop, around each user turn:

    # after the wake word fires and before recording
    engine.user_turn_started()

    transcript = transcriber.transcribe(audio)
    engine.user_turn_ended(transcript)

    reply = llm_brain.respond(transcript)
    speaker.speak(reply)
    engine.assistant_replied(reply)

wake_word.py and audio_recorder.py
----------------------------------
This one is not optional on speakers. Before evaluating a wake-word frame or a
voice trigger:

    if engine.mic_should_ignore():
        continue        # she's talking; don't let her wake herself

Pass the engine in, or import a module-level singleton — whichever fits your
existing structure.

Long tool runs
--------------
    engine.suppress(30)     # don't pipe up mid-screenshot/search
"""
