"""
Phase 2 entry point.

Adds a conversation session on top of the Phase 1 voice pipeline:

  ASLEEP: wait for wake word
     -> on trigger, enters AWAKE mode
  AWAKE:  wait for real speech (silent, no wasted recording/transcription) ->
          record until you stop talking -> transcribe -> check for sleep phrase
     -> if sleep phrase: say goodbye, go back to ASLEEP
     -> otherwise: route the transcript (deterministic matchers first, the LLM
        brain last), speak the reply (interruptible - talking over it stops it
        and immediately starts capturing what you said next), stay AWAKE

Phase 3: risky actions (sending a WhatsApp message, overwriting a file) are not
run straight away. The tool parks them in confirmation.py and she asks out loud;
the NEXT transcript is checked there first (a spoken yes runs the action, no or
anything unclear cancels it) before normal routing sees it.

No wake word is needed between turns while AWAKE - it's a real back-and-forth
conversation that keeps waiting for your voice until you explicitly say
"goodbye" or "go to sleep".

Also wired in here: initiative.py (she can speak first), the transcript guard
(inside transcriber.py), and shutdown.py's cooperative Ctrl+C handling.

v3 additions: every utterance sets the language of the turn (lang.py: Devanagari = Hindi) so she
answers in the language she was spoken to in; dictation mode consumes transcripts first; timers /
reminders are announced by reminders.Scheduler even when she is idle; the first wake of the day
gives the morning briefing; the new feature modules (reminders, PC controls, calculator, weather,
news, notes, clipboard, calendar / mail, screen vision, Hindi commands) are deterministic
matchers ahead of the LLM, imported lazily so a broken one only disables itself.
"""
import shutdown  # MUST stay the first import: see shutdown.py (MKL Ctrl+C handler)
import importlib
import logging
import os
import re
import sys
import threading
import time

import pyaudio

import avatar_server
import avatar_window
import config
import confirmation
import initiative
import instance_guard
import lang
import memory
from routing import AskLLM
from audio_recorder import record_until_silence, wait_for_voice
from llm_brain import Brain
from speaker import Speaker
from tools import (
    TOOL_FUNCTIONS,
    find_file,
    match_quick_command,
    try_auto_datetime,
    try_auto_ai_followup,
    try_auto_ai_send,
    try_auto_open_app,
    try_auto_open_folder,
    try_auto_open_site_action,
    try_auto_type_into_ai_site,
    try_auto_youtube_nth,
    try_auto_play_music,
    try_auto_queue_music,
    try_auto_whatsapp,
    try_auto_recall_favorite,
    try_auto_remember_favorite,
    try_auto_screenshot,
    try_auto_smalltalk,
    warm_up_app_index,
)
from transcriber import Transcriber
from wake_word import WakeWordDetector

logger = logging.getLogger("voice_assistant")


def _utf8_console():
    """Hindi in assistant.log lines printed to a Windows console (cp1252) must not raise."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging():
    _utf8_console()
    handlers = [logging.FileHandler(config.LOG_FILE, encoding="utf-8")]
    # Started in the background by start_raziel.pyw there's no console to show log lines in: they
    # would only be copied into raziel_console.log, which is meant for what assistant.log can't
    # catch (a crash before logging starts, pywebview's own messages).
    if not os.environ.get("RAZIEL_BACKGROUND"):
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# "go to sleep" / "सो जाओ" put HER to sleep - unless the sentence names the computer ("computer go to sleep",
# "कंप्यूटर सो जाओ"), which is a PC-control request that sysctl handles. "goodbye" always ends the session.
_SLEEP_NEEDS_NO_PC = tuple(lang.fold_hindi(p) for p in ("go to sleep", "go back to sleep", "back to sleep",
                                                          "सो जाओ", "सो जाइए"))
# Whole sentences that end the conversation too ("go back to sleep" once reached the model, which put the
# PC to sleep). Whole-sentence only, so "play that's all folks" or "good night songs" are left alone.
_SLEEP_WHOLE_RE = re.compile(
    r"^(?:(?:ok|okay|now|so|and|raziel|please|alright|thanks|thank you)[\s,]+)*"
    r"(?:(?:you can |please )?go (?:back )?to sleep|back to sleep|sleep now|(?:you can |go )sleep|"
    r"(?:you can )?go to bed|that'?s (?:all|it)(?: for now| for today)?|bye(?: bye)?|good ?night|"
    r"stop listening|see you(?: later| soon)?|(?:you )?(?:can )?rest now|goodbye)"
    r"(?:[\s,]+(?:raziel|now|please|then|for now|for today|thanks|thank you))*[.!]*$")
_PC_WORDS = tuple(lang.fold_hindi(w) for w in ("computer", "laptop", "कंप्यूटर", "कम्प्यूटर", "पीसी", "लैपटॉप", "सिस्टम"))
_PC_WORD_RE = re.compile(r"(?:^|\s)(?:pc|" + "|".join(map(re.escape, _PC_WORDS)) + r")(?:\s|$)")


def is_sleep_phrase(transcript: str) -> bool:
    lowered = lang.fold_hindi(transcript)            # lower case, Hindi spelling variants folded
    if _SLEEP_WHOLE_RE.match(re.sub(r"[^\w\s']+", " ", lowered).strip()):
        return True
    names_pc = bool(_PC_WORD_RE.search(re.sub(r"[^\w\s\u0900-\u097F]+", " ", lowered)))
    for phrase in config.SLEEP_PHRASES:
        folded = lang.fold_hindi(phrase)
        if folded in lowered:
            if names_pc and folded in _SLEEP_NEEDS_NO_PC:
                continue
            return True
    try:                                             # a short, whole-utterance Hindi goodbye ("बाय बाय")
        import hindi_commands
        return bool(hindi_commands.is_sleep_hindi(transcript))
    except Exception:
        return False


# The few sentences main.py itself speaks (English + Hindi).
_M = {
    "listening": {"en": "Listening.", "hi": "सुन रही हूँ।"},
    "online": {"en": "Systems online.", "hi": "सिस्टम चालू हैं।"},
    "goodbye": {"en": "Goodbye.", "hi": "अलविदा।"},
    "goodbye_pending": {"en": "Goodbye. I did not go through with the pending request.",
                        "hi": "अलविदा। जो अनुरोध बाकी था, वह पूरा नहीं किया गया।"},
    "failed": {"en": "That request failed.", "hi": "वह अनुरोध पूरा नहीं हो पाया।"},
    "understood": {"en": "Understood.", "hi": "समझ गई।"},
    "yes": {"en": "Yes?", "hi": "हाँ?"},
    "didnt_catch": {"en": "Sorry, I didn't catch that. What should I do?", "hi": "माफ़ कीजिए, मैं समझ नहीं पाई। क्या करना है?"},
}


def _say(key: str) -> str:
    return lang.tr(_M, key)


# Her name on its own while she is already listening ("Raziel", or how Whisper hears it): she answers,
# instead of the model replying "Understood." to it.
_NAME_ONLY_RE = re.compile(r"^(?:(?:hey|hi|ok|okay|yo|hello)\s+)?(?:raziel|razil|rasiel|raziyel|razeel|raziyal|razial|"
                           r"rajiel|brazil|brasiel|braziel|rasil|raaziel|raziell|raziel raziel)(?:\s+(?:raziel|are you there|you there))?[.!?]*$")


def is_name_only(transcript: str) -> bool:
    return bool(_NAME_ONLY_RE.match(re.sub(r"[^\w\s']+", " ", (transcript or "").lower()).strip()))


# --- small helpers: initiative engine + avatar, both optional ----------------

def _engine(method: str, *args):
    """Call a method on the initiative engine if it is running. Never raises."""
    engine = initiative.engine
    if engine is None:
        return
    try:
        getattr(engine, method)(*args)
    except Exception as e:
        logger.debug("initiative.%s failed: %s", method, e)


def _avatar_state(state: str):
    """idle | listening | thinking | speaking. Nothing else drove listening or
    thinking before, so the avatar's posture for those two states never showed."""
    if not config.AVATAR_ENABLED:
        return
    try:
        avatar_server.set_state(state)
    except Exception as e:
        logger.debug("avatar set_state(%s) failed: %s", state, e)


# --- routing -------------------------------------------------------------------

def _quick_command_reply(transcript: str):
    tool_name = match_quick_command(transcript)
    if not tool_name:
        return None
    logger.info("Quick command matched: %s", tool_name)
    return TOOL_FUNCTIONS[tool_name]()


def _favorite_reply(transcript: str):
    return try_auto_remember_favorite(transcript) or try_auto_recall_favorite(transcript)


def _brain_note(brain, method: str, *args):
    """Calls an optional Brain history helper; a failure here is never fatal."""
    func = getattr(brain, method, None)
    if callable(func):
        try:
            func(*args)
        except Exception:
            logger.exception("Brain.%s failed (non-fatal)", method)


def confirmation_reply(transcript: str, brain, announce=None):
    """
    While a risky action is waiting for a spoken yes/no (confirmation.py), the
    next transcript is the ANSWER to it. Returns the text to speak, or None when
    nothing was waiting (or the user moved on to a new request, which then goes
    through normal routing).
    """
    outcome = confirmation.handle_transcript(transcript, announce)
    if not outcome.handled:
        return None
    if outcome.then_route:
        # She dropped the parked action because the user asked for something else.
        # Tell the model, so it can't believe the old request is still open.
        _brain_note(brain, "note_assistant", outcome.note)
        return None
    reply = _localize(outcome.reply or _say("understood"))
    logger.info("Confirmation answer handled (skipping LLM)")
    _brain_note(brain, "note_exchange", transcript, reply)      # so the model sees how it ended
    return reply


# One turn (confirmation check + deterministic matchers + the LLM brain) at a time, system-wide -
# whether it came from the mic or from the phone bridge (Phase 6). Both touch the same shared
# state (confirmation.py's pending-action queue, brain's conversation history), so two turns
# running at once could interleave and corrupt either one. Uncontended almost all the time (a
# phone command arriving mid-conversation is rare), so this never adds a noticeable delay.
_turn_lock = threading.Lock()


# --- lazily imported matchers (v3) ----------------------------------------------
# Each feature module is imported the first time it is asked, and an import error or an exception
# inside one matcher only switches THAT matcher off (logged once) - it can never take the assistant
# down or stop the matchers after it from running.

_BROKEN_MATCHERS = set()
_MATCHER_FAILURES = {}
_MATCHER_MAX_FAILURES = 3        # a matcher that raises this many times IN A ROW is switched off for the run


def _lazy(module_name: str, func_name: str = "try_handle"):
    label = f"{module_name}.{func_name}"

    def matcher(transcript: str):
        if label in _BROKEN_MATCHERS:
            return None
        try:
            result = getattr(importlib.import_module(module_name), func_name)(transcript)
        except ImportError:
            logger.exception("Matcher %s can't be imported - switching it off for this run", label)
            _BROKEN_MATCHERS.add(label)
            return None
        except Exception:
            n = _MATCHER_FAILURES.get(label, 0) + 1
            _MATCHER_FAILURES[label] = n
            if n >= _MATCHER_MAX_FAILURES:
                logger.exception("Matcher %s failed %d times in a row - switching it off for this run", label, n)
                _BROKEN_MATCHERS.add(label)
            else:
                logger.exception("Matcher %s failed (%d/%d) - this sentence goes to the next handler",
                                 label, n, _MATCHER_MAX_FAILURES)
            return None
        _MATCHER_FAILURES.pop(label, None)
        return result

    matcher.__name__ = label
    return matcher


def _localize(text):
    """English sentences from the older tools ("Opened notepad.") -> Hindi on a Hindi turn."""
    if not isinstance(text, str) or not lang.is_hindi():
        return text
    try:
        import hindi_commands
        return hindi_commands.localize(text)
    except Exception:
        return text


def _hindi_commands(transcript: str):
    if not getattr(config, "HINDI_COMMANDS_ENABLED", True) or not getattr(config, "HINDI_ENABLED", True):
        return None
    return _lazy("hindi_commands")(transcript)


# (label, matcher, record_in_session_memory), in PRIORITY order.
# The LLM is unreliable at picking tools (handoff section 5), so anything that
# can be matched in code is matched here first.
#
# Order matters and matching is LAZY: a matcher runs only if every matcher
# before it returned None. Several of these have side effects (opening an app,
# queueing a song, writing memory), so they must never run speculatively - the
# old code evaluated three of them eagerly before choosing, which would have
# double-fired as soon as two of them could match the same sentence.
#
# v3 ordering notes: reminders come first because they also own the follow-up answer to "When
# should I remind you?"; the specific feature matchers all sit BEFORE play-music / open-app (which
# are the broadest) and the open-ended web matchers come last.
DETERMINISTIC_MATCHERS = (
    ("reminders", _lazy("reminders"), True),                 # timers / alarms / reminders (+ "at what time?" follow-up)
    ("ai-followup", try_auto_ai_followup, True),             # the question after "Gemini is open. What should I ask it?"
    ("favorite-fact", _favorite_reply, True),
    ("open-site-action", try_auto_open_site_action, True),   # "open youtube and play X"
    ("open-ai-chat", try_auto_type_into_ai_site, True),       # "open chatgpt and type/ask X"
    ("ai-send", try_auto_ai_send, True),                     # "send it and read me the answer" (ChatGPT / Gemini in front)
    ("avatar-place", _lazy("avatar_place"), True),           # "make yourself bigger" / "move to the top left"
    ("universe", _lazy("universes"), True),                  # "show me the universe" / "who lives in your universe"
    ("avatar-forms", _lazy("avatar_forms"), True),           # orb avatar: "become a dragon" / "roar" / "dance"
    ("ai-answer", _lazy("ai_answers"), True),                # "read it all" (the last AI chat answer, in full)
    ("quiet-mode", _lazy("quiet_mode"), True),               # "do not disturb" / "quiet mode off"
    ("gestures", _lazy("gestures"), True),                   # "stop watching" / "watch my hands"
    ("file-search", _lazy("file_index"), True),              # "find the PDF about the bank loan" (local index)
    ("youtube-nth", try_auto_youtube_nth, True),             # "play the third video" (of the last YouTube search)
    ("queue-music", try_auto_queue_music, True),             # "add X to the queue"
    ("whatsapp", try_auto_whatsapp, True),                   # "message Fazal saying hi" (asks yes/no first)
    ("calendar-mail", _lazy("google_api"), True),            # Google Calendar / Gmail
    ("briefing", _lazy("briefing"), True),                   # "give me my briefing" / first "good morning" of the day
    ("hindi-commands", _hindi_commands, True),               # Hindi: open / play / WhatsApp / time / thanks ...
    ("system-control", _lazy("sysctl"), True),               # volume, brightness, lock, close app, shutdown (asks first)
    ("dictation", _lazy("dictation"), True),                 # "start typing" / "type hello"
    ("quick-command", _quick_command_reply, False),          # pause / next / skip ...
    ("date-time", try_auto_datetime, True),                  # "what's the date / time / day" - instant
    ("screenshot", try_auto_screenshot, True),               # "take a screenshot"
    ("screen-vision", _lazy("vision"), True),                # "what's on my screen"
    ("small-talk", try_auto_smalltalk, True),                # bare "thanks" / "hello"
    ("calculator", _lazy("calc"), True),                     # maths, units, currency
    ("weather", _lazy("webinfo", "try_handle_weather"), True),
    ("news", _lazy("webinfo", "try_handle_news"), True),     # ALWAYS live (Google News)
    ("notes", _lazy("notes"), True),                         # notes + to-do / shopping lists
    ("clipboard", _lazy("clipboard_tool"), True),            # read / summarise / save what was copied
    ("play-music", try_auto_play_music, True),               # "play X" (Spotify)
    ("open-folder", try_auto_open_folder, True),             # "open my COA folder"
    ("open-app", try_auto_open_app, True),                   # "open spotify" (skips ~4 s of LLM)
    ("web-search", _lazy("webinfo", "try_handle_search"), True),          # "google X" (Gemini + Google Search)
    ("live-question", _lazy("webinfo", "try_handle_live_question"), True),  # scores / prices / "who is the ..."
)


def _universe(fn: str, *args):
    """universes.py (Raziel's universe of beings); never lets it break a turn."""
    try:
        import universes
        if not universes.enabled():
            return None
        return getattr(universes, fn)(*args)
    except Exception:
        logger.exception("universes.%s failed", fn)
        return None


def _brain_turn(brain, transcript: str, model_text: str = None):
    # `model_text` is text a matcher fetched (the clipboard, ...): the model may read it but it must never
    # be able to act on it, so such a turn is marked untrusted (no tool runs).
    being = _universe("turn_being")                 # "Melody, ...": her personality and her own tools
    extra = {"being": being} if being else {}
    if getattr(config, "LLM_STREAMING", True) and hasattr(brain, "stream_turn"):
        if model_text:
            return brain.stream_turn(transcript, model_text, untrusted=True, **extra)
        return brain.stream_turn(transcript, **extra)
    if model_text:
        return brain.process_turn(transcript, model_text, untrusted=True, **extra)
    return brain.process_turn(transcript, **extra)


def _match_deterministic(transcript: str, brain, record_to_memory: bool = True):
    """Runs the deterministic matchers in order. (label, reply) for the first that handled it, else None.
    reply is a str, a routing.AskLLM, or an iterator of sentences (a streamed reply)."""
    for label, matcher, record in DETERMINISTIC_MATCHERS:
        reply = matcher(transcript)
        if not reply:
            continue
        _universe("note_matcher", label, transcript)    # that universe's galaxy lights up on the map
        if isinstance(reply, AskLLM) or not isinstance(reply, str):
            return label, reply
        reply = _localize(reply)
        logger.info("Deterministic %s handling matched (skipping LLM)", label)
        if record_to_memory:
            if confirmation.has_pending():
                # She just asked a yes/no question without the model's help; put it in
                # the model's history too, so it knows what "yes" refers to later.
                _brain_note(brain, "note_exchange", transcript, reply)
            elif record:
                memory.append_to_session(brain.session_id, "user", transcript)
                memory.append_to_session(brain.session_id, "assistant", reply)
        return label, reply
    return None


def _route_multi(transcript: str, brain):
    """ "open Spotify, play lo-fi and set the volume to 30": several commands in one sentence
    (multi_command.py). Returns the reply, or None to handle the sentence as one command."""
    if not getattr(config, "MULTI_COMMAND_ENABLED", True):
        return None
    try:
        import multi_command
        clauses = multi_command.split(transcript)
    except Exception:
        logger.exception("Splitting the sentence into commands failed - handling it as one")
        return None
    if not clauses:
        return None
    if len(clauses) == 1:
        return ("reworded", clauses[0])
    logger.info("Several commands in one sentence: %s", clauses)

    def record(user, assistant):
        if confirmation.has_pending():
            _brain_note(brain, "note_exchange", user, assistant)
        else:
            memory.append_to_session(brain.session_id, "user", user)
            memory.append_to_session(brain.session_id, "assistant", assistant)

    def turn(text, model_text=None):
        return _brain_turn(brain, text, model_text)

    def front():
        import winutil
        return winutil.foreground_window().get("hwnd") or None

    return multi_command.run(
        clauses, lambda clause: _match_deterministic(clause, brain, record_to_memory=False), turn,
        record=record,                    # (_match_deterministic already localised the replies)
        foreground=front if os.name == "nt" else None)


def _ai_question_pending() -> bool:
    try:
        import tools
        st = tools._ai_state
        return bool(st["pending_site"]) and time.time() <= st["pending_until"]
    except Exception:
        return False


def _clear_ai_question():
    try:
        import tools
        tools._ai_state["pending_site"] = ""
    except Exception:
        pass


def _match_one(label: str, fn, transcript: str):
    try:
        reply = fn(transcript)
    except Exception:
        logger.exception("The %s handler failed", label)
        return None
    if reply is not None:
        logger.info("Deterministic %s handling matched", label)
    return reply


def route_transcript(transcript: str, brain):
    """
    Handles a transcript. Returns either

      - a str: the finished reply of a deterministic handler (instant), or
      - an iterator of sentences (Brain.stream_turn) when the LLM has to answer
        and config.LLM_STREAMING is on, so speech can start with the first
        sentence instead of after the whole reply. speak_stream_interruptible()
        consumes it. (A few matchers stream too: reading an AI chat's answer.)

    With streaming off (or a brain without stream_turn) the LLM's reply is also
    returned as a plain str, exactly as before.

    A matcher may also return routing.AskLLM(prompt): the code did its part (e.g. read the
    clipboard) and the model writes the answer from `prompt`.

    A sentence with several commands in it ("open Spotify, play lo-fi and set the volume to 30")
    is split first (multi_command.py); each part goes through the same matchers.
    """
    if _ai_question_pending():
        # "Gemini is open. What should I ask it?" - the whole next sentence is the question (not split up)
        hit = _match_one("ai-followup", try_auto_ai_followup, transcript)
        if hit is not None:
            return hit
    multi = _route_multi(transcript, brain)
    if isinstance(multi, tuple):
        transcript = multi[1]                  # one command, reworded ("open google, search X")
    elif multi is not None:
        return multi

    hit = _match_deterministic(transcript, brain)
    if hit is not None:
        label, reply = hit
        if isinstance(reply, AskLLM):
            logger.info("Deterministic %s handling matched: handing the text to the model (%s)",
                        label, reply.note or "no note")
            return _brain_turn(brain, transcript, reply.prompt)
        if not isinstance(reply, str):
            logger.info("Deterministic %s handling matched (streamed reply)", label)
        return reply
    return _brain_turn(brain, transcript)


# --- unprompted speech: reminders, briefing -----------------------------------------

def _assistant_busy() -> bool:
    """For reminders.Scheduler: True while a conversation turn or her own speech is going on."""
    if confirmation.has_pending():
        return True              # a yes/no question is open: an announcement in between would get the "yes"
    engine = initiative.engine
    try:
        return bool(engine is not None and engine.is_busy())
    except Exception:
        return False


def _quiet_now() -> bool:
    try:
        import quiet_mode
        return quiet_mode.active()
    except Exception:
        return False


def _quiet_notify(text: str):
    try:
        import quiet_mode
        quiet_mode.notify(text)
    except Exception:
        logger.exception("Couldn't show the pop-up")


def _voice_check(wav_path: str):
    try:
        import voice_id
        voice_id.begin_turn(wav_path)
    except Exception:
        logger.exception("Voice ID check couldn't start (non-fatal)")


# --- other apps' sound (media_duck.py) -------------------------------------------
# While music or a video plays, an open mic takes the lyrics for commands and your real sentence arrives
# mixed with the song. So: while something plays she only listens for her name, and from "Raziel" until
# she has answered the other apps are turned down (media_duck.py). All optional - without pycaw these
# quietly do nothing and she behaves as before.
MEDIA_LISTEN_TIMEOUT = 8.0          # after "Raziel" over music: seconds to start talking before the music comes back
MEDIA_RESUME_AFTER_SILENCE = 3.0    # music stopped this long -> she listens to any speech again


def _media():
    try:
        import media_duck
        return media_duck
    except Exception:
        return None


def _media_call(name: str, *args, default=None):
    m = _media()
    if m is None:
        return default
    try:
        return getattr(m, name)(*args)
    except Exception:
        logger.debug("media_duck.%s failed", name, exc_info=True)
        return default


def _media_playing(within: float = 1.2) -> bool:
    return bool(_media_call("playing", within, default=False))


def _media_sound_now() -> bool:
    """Other apps made sound in the last half second (a song / video just started)."""
    return bool(_media_call("playing", 0.5, default=False)) or _media_silent_for() < 0.45


def _media_silent_for() -> float:
    v = _media_call("silent_for", default=60.0)
    return float(v) if isinstance(v, (int, float)) else 60.0


def _media_ducked() -> bool:
    return bool(_media_call("is_ducked", default=False))


def _media_duck(reason: str):
    """Turn the other apps down - only if something is actually playing."""
    if _media_playing(2.0):
        _media_call("duck", reason)


def _media_unduck(reason: str):
    _media_call("unduck", reason)


def _wait_for_name_over_music(wake_detector, speaker) -> bool:
    """Mid-conversation with music playing: wait for "Raziel" (lyrics aren't commands). True = she heard
    her name (the music is turned down and she said "Listening."); False = the music stopped, or shutdown."""
    _media_unduck("waiting for her name")
    _engine("set_busy", False)
    _avatar_state("idle")
    logger.info("Music is playing - waiting for 'Raziel' (the song's words aren't commands).")
    heard = wake_detector.wait_for_name(stop_when=lambda: _media_silent_for() >= MEDIA_RESUME_AFTER_SILENCE)
    if shutdown.is_shutting_down():
        return False
    if not heard:
        logger.info("The music stopped - listening to anything you say again.")
        return False
    _media_duck("her name over the music")
    _avatar_state("listening")
    speaker.speak(_say("listening"))
    return True


def _asks_back(reply) -> bool:
    """Her reply ends in a question ("Which playlist?"): the answer comes without her name."""
    return isinstance(reply, str) and reply.rstrip().rstrip('"\'').endswith("?")


def _not_your_voice(addressed: bool, media: bool = False) -> bool:
    """Was this recording NOT you (the song, a video, the TV, someone else in the room)? Voice ID decides;
    without a voice profile nothing is dropped. A clear "someone else" is always dropped. A "not sure":
    kept right after her name (`addressed`: a short command scores low); otherwise dropped with music
    playing, or when it is a long stretch of talking (a short command can't be judged, it is kept)."""
    try:
        import voice_id
        r = voice_id.current(wait=1.5)
    except Exception:
        return False
    if r is None or r.verdict == voice_id.OFF:
        return False
    if r.verdict == voice_id.OTHER:
        return True
    if r.verdict != voice_id.UNSURE or addressed:
        return False
    return bool(media) or float(getattr(r, "speech", 0.0) or 0.0) >= float(getattr(config, "VOICE_ID_UNSURE_DROP_SPEECH", 2.5))


def _gestures_busy(flag: bool):
    """Hand gestures pause while Whisper transcribes (both want the CPU; the turn is what matters)."""
    try:
        import sys as _sys
        g = _sys.modules.get("gestures")
        if g is not None and hasattr(g, "set_busy"):
            g.set_busy(flag)
    except Exception:
        pass


def _phone_trusted():
    """Phone commands (token-protected) count as the owner for Voice ID."""
    try:
        import voice_id
        return voice_id.trusted()
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _phone_no_wait():
    """Phone commands: "ask ChatGPT X" types it and returns; the answer stays on the PC's screen."""
    try:
        import ai_answers
        return ai_answers.no_wait()
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _announce_when_quiet(speaker, text: str, max_wait: float = 90.0):
    """Unprompted speech that waits for a quiet moment first (the AI-answer reader's LLM-tool path)."""
    deadline = time.time() + max_wait
    while _assistant_busy() and time.time() < deadline and not shutdown.is_shutting_down():
        shutdown.wait(0.5)
    announce_unprompted(speaker, text)


def _start_extras(speaker):
    """Other apps' sound (turned down while she listens), game / call mode, Voice ID, the AI-answer reader's
    announcer, the file index and hand gestures. Each is optional:
    a failure only switches that one feature off."""
    for label, start in (
        ("media watch", lambda: __import__("media_duck").start()),
        ("quiet mode", lambda: __import__("quiet_mode").start()),
        ("Voice ID", lambda: __import__("voice_id").ensure_ready_async()),
        ("AI answers", lambda: __import__("ai_answers").set_announcer(lambda t: _announce_when_quiet(speaker, t))),
        ("file index", lambda: __import__("file_index").start_background(is_busy=_assistant_busy)),
        ("hand gestures", lambda: __import__("gestures").start()),
        ("playlist names", lambda: __import__("tools").refresh_playlist_names_async()),
        ("folder names", lambda: __import__("tools").refresh_folder_names_async()),
        ("universe", lambda: __import__("universes").start()),
    ):
        try:
            start()
        except Exception:
            logger.exception("Couldn't start %s (that feature is off for this run)", label)


def announce_unprompted(speaker, text: str):
    """Says `text` out loud with the microphone gated (so her own voice can't wake her or count as the
    user talking). Used for due timers and reminders. Blocks until she has finished speaking."""
    if not text:
        return
    if _quiet_now():
        # A game, a call or "do not disturb": a pop-up instead of her voice (quiet_mode.py).
        _quiet_notify(text)
        return
    if confirmation.has_pending():
        # Only reachable after the "waited too long" fallback. Whatever the user says next must not be taken
        # as the answer to a question they have half forgotten: cancel it (fail-closed).
        confirmation.clear("announcement in between")
    _engine("suppress", max(2.0, len(text) / 9.0) + 1.5)
    _avatar_state("speaking")
    try:
        speaker.speak(text)
    finally:
        _engine("suppress", 0.8)
        _avatar_state("idle")


def start_reminder_scheduler(speaker):
    """Starts the background thread that announces due timers / reminders / alarms, awake or not."""
    if not getattr(config, "REMINDERS_ENABLED", True):
        return None
    try:
        import reminders
        def beep():
            if getattr(config, "REMINDER_BEEP", True) and not _quiet_now():
                reminders._default_beep()           # no beeps during a game / call either

        scheduler = reminders.Scheduler(lambda text: announce_unprompted(speaker, text),
                                        is_busy=_assistant_busy, beep=beep,
                                        guard=lambda seconds: _engine("suppress", seconds))
        scheduler.start()
        shutdown.on_shutdown(scheduler.stop)
        return scheduler
    except Exception:
        logger.exception("Couldn't start the reminder scheduler (timers won't ring)")
        return None


# --- phone bridge (Phase 6): phone <-> PC ---------------------------------------

def handle_phone_command(brain, transcript: str) -> str:
    """The phone -> PC command endpoint's core: runs `transcript` through the exact same
    pipeline as a normal spoken turn - a pending yes/no answer first, then the deterministic
    matchers, then the LLM brain - so a phone-issued command behaves identically to speaking to
    her in the room, including confirmation.py's two-turn "say yes to confirm" flow for risky
    actions (a phone "yes" arms/answers it exactly like a spoken one would). Nothing is played on
    THIS PC's speakers - the finished reply is just returned as text for the phone to show. Runs
    under _turn_lock, same as the mic loop, so the two can never run at once and interleave."""
    with _turn_lock, _phone_trusted(), _phone_no_wait():
        try:
            reply = confirmation_reply(transcript, brain)
            if reply is None:
                reply = route_transcript(transcript, brain)
            if not isinstance(reply, str):
                # Joined HERE, inside trusted(): a streamed reply runs its tools while it is read.
                reply = " ".join(p for p in reply if p)
        except Exception:
            logger.exception("Handling a phone command failed")
            return _say("failed")

        if not isinstance(reply, str):
            # Streaming LLM reply (an iterator of sentences) - there's no speaker to feed them to
            # here, so just join it into one string for the phone.
            reply = " ".join(reply)

        confirmation.settle(reply)
        confirmation.restart_timer()
        _remember_reply(reply)
        _engine("assistant_replied", reply)
        return reply


def handle_phone_notification(speaker, app: str, title: str, text: str) -> None:
    """Called when the phone forwards a notification (phone_bridge.py's /notification endpoint,
    fed by a Tasker profile on the phone) - read out loud the same way a due reminder is
    announced: mic gated so her own voice can't wake her or count as the user talking, and it
    never steps on an open yes/no."""
    if title and text and title != text:
        body = f"{title}: {text}"
    else:
        body = title or text or ""
    if not body:
        return
    if len(body) > 400:
        body = body[:400].rstrip() + "..."
    announce_unprompted(speaker, f"Notification from your phone, {app or 'an app'}: {body}")


def start_phone_bridge(brain, speaker):
    """Starts the phone <-> PC bridge (Phase 6): phone-issued text commands, phone notifications
    read aloud, and file fetches - all authenticated with config.PHONE_BRIDGE_TOKEN (the bridge
    stays off until that's set; see phone_bridge.py's module docstring for the full security
    note, and PHONE_BRIDGE_SETUP.md for how the phone side is set up). A problem here only
    disables the phone bridge - Raziel still starts and works normally from the mic either way."""
    try:
        import phone_bridge
        started = phone_bridge.start(
            command_handler=lambda text: handle_phone_command(brain, text),
            notification_handler=lambda app, title, text: handle_phone_notification(speaker, app, title, text),
            file_resolver=find_file,
        )
        if started:
            shutdown.on_shutdown(phone_bridge.stop)
    except Exception:
        logger.exception("Couldn't start the phone bridge (phone control won't be available)")


def _prefetch_briefing():
    """Builds today's automatic briefing on a thread (it makes several network calls) so it is ready
    by the time she has said "Listening." Returns a function that waits for the text (or None)."""
    if not getattr(config, "BRIEFING_ENABLED", True):
        return lambda: None
    holder = {}

    def work():
        try:
            import briefing
            holder["text"] = briefing.auto_briefing()
        except Exception:
            logger.exception("Automatic briefing failed (non-fatal)")

    thread = threading.Thread(target=work, name="briefing-prefetch", daemon=True)
    thread.start()

    def wait(timeout: float = 15.0):
        thread.join(timeout)
        if thread.is_alive():
            # Too slow (bad network): don't make her wait longer, and don't lose the day's briefing either -
            # hand the claim back so the next wake-up delivers it.
            logger.warning("Morning briefing took longer than %.0fs - skipped for now", timeout)
            try:
                import briefing
                briefing.release_today()
            except Exception:
                pass
            return None
        return holder.get("text")

    return wait


def _dictation_handle(transcript: str):
    """None = dictation is off (route normally); otherwise the text to speak ("" = say nothing)."""
    try:
        import dictation
        return dictation.handle(transcript)
    except Exception:
        logger.exception("Dictation handling failed (non-fatal)")
        return None


def _remember_reply(reply: str):
    """Lets "copy that" put her last answer on the clipboard."""
    try:
        import clipboard_tool
        clipboard_tool.remember_last_reply(reply)
    except Exception:
        pass


# --- conversation --------------------------------------------------------------

def run_awake_session(pa, transcriber, speaker, brain, wake_detector, pre_roll_frames, logger):
    """
    Runs the conversation loop while the assistant is "awake". Only returns
    when the user says an explicit sleep phrase (or shutdown is requested) - it
    waits indefinitely for speech otherwise, never auto-sleeping on silence.
    """
    brain.reset()
    confirmation.clear("new session")   # nothing carries over from an earlier session

    # Waking her counts as answering anything she said unprompted, and resets
    # initiative's "ignored twice, go quiet" bookkeeping.
    _engine("user_turn_started")
    _engine("note_wake")                 # (she was just called: "Forty minutes of nothing" would be silly now)
    _engine("set_busy", False)
    _avatar_state("listening")
    wait_briefing = _prefetch_briefing()     # the first wake of the day also gives the briefing
    speaker.speak(_say("listening"))
    interrupted_at_wake, wake_pre_roll = False, None
    briefing_text = wait_briefing()
    if briefing_text:
        logger.info("Reply (morning briefing): %s", briefing_text)
        _engine("set_busy", True)
        interrupted_at_wake, wake_pre_roll = speaker.speak_interruptible(briefing_text, pa)
        memory.append_to_session(brain.session_id, "assistant", briefing_text)
        _remember_reply(briefing_text)
        _engine("assistant_replied", briefing_text)

    # The very first turn already has pre_roll_frames from the wake word
    # trigger (None with the current wake_word.py), so a None here simply means
    # the first turn also goes through wait_for_voice. No ambient calibration
    # exists yet for a turn that skips it, so record_until_silence falls back to
    # its static default threshold until the next wait_for_voice call.
    pending_pre_roll = pre_roll_frames
    if interrupted_at_wake:
        pending_pre_roll = wake_pre_roll         # she was talked over during the briefing: listen right away
    current_ambient_level = None
    # "addressed": she was just called by name (the wake word, or "Raziel" over the music) or she has
    # just asked you something - the next thing you say is for her even if music is playing.
    addressed = True
    said_didnt_catch = False   # "Sorry, I didn't catch that" is said once; then she stays quiet (the TV...)
    last_asked = False         # her last reply was a question (its answer may be a word like "Brazil")

    while not shutdown.is_shutting_down():
        if pending_pre_roll is None:
            if not addressed and _media_playing():
                # Music / a video is playing: only her name counts until it stops.
                addressed = _wait_for_name_over_music(wake_detector, speaker)
                if shutdown.is_shutting_down():
                    return
                if not addressed and _media_playing():
                    continue            # (still playing: keep waiting for her name)
            logger.info("Waiting for you to speak...")
            _avatar_state("listening")
            with_music = addressed and (_media_ducked() or _media_playing())
            if with_music:
                # called by name over music: talk within a few seconds, or the music comes back up
                pending_pre_roll, current_ambient_level = wait_for_voice(pa, timeout=MEDIA_LISTEN_TIMEOUT)
            else:
                # open mic: music starting (sustained sound) ends this wait; any other app's sound at the
                # moment speech seems to start (a song's first bar, a 'ding') doesn't start a recording
                pending_pre_roll, current_ambient_level = wait_for_voice(
                    pa, stop_when=lambda: _media_playing(1.0), veto=_media_sound_now)
            if shutdown.is_shutting_down():
                return
            if pending_pre_roll is None and current_ambient_level is None:
                # (None, None): nothing said after "Raziel" (the music comes back up), or music just started.
                _media_unduck("nothing said")
                addressed = False
                continue

        # She is being spoken to: hold proactive speech off until the whole
        # turn (record -> transcribe -> route -> speak) is finished.
        _engine("set_busy", True)

        wav_path = record_until_silence(
            pa, pre_roll_frames=pending_pre_roll, ambient_level=current_ambient_level
        )
        pending_pre_roll = None
        current_ambient_level = None
        if shutdown.is_shutting_down():
            return
        _voice_check(wav_path)               # "was that you?" - in the background, for risky actions

        _avatar_state("thinking")
        logger.info("Transcribing...")
        media_now = _media_ducked() or _media_playing(2.0)
        was_addressed = addressed
        _media_call("touch")                 # (a long turn mustn't hit the "turned down too long" limit)
        _gestures_busy(True)
        try:
            transcript = transcriber.transcribe(wav_path)
        finally:
            _gestures_busy(False)

        if transcript and _not_your_voice(was_addressed, media_now):
            logger.info("Ignored %r - %s", transcript,
                        "music / a video was playing and it didn't sound like you." if media_now else
                        "that didn't sound like you (the TV, someone else talking?).")
            transcript = ""
            try:
                transcriber.forget_last_turn()        # its language mustn't steer the next sentence
            except Exception:
                pass
            if was_addressed and not media_now and not said_didnt_catch:
                speaker.speak(_say("didnt_catch"))   # you called her and are waiting: don't just go silent
                said_didnt_catch = True
            elif said_didnt_catch:
                addressed = False                    # asked once already: whatever this is, it isn't you
        if not transcript:
            logger.info("No speech detected - still listening.")
            _engine("set_busy", False)   # a rejected/empty turn is not a real turn
            continue

        logger.info("Transcript: %s", transcript)
        code = lang.set_current(lang.detect(transcript))     # the language of THIS turn (Devanagari = Hindi)
        if code == "hi":
            logger.info("Language of this turn: Hindi")
        _engine("user_turn_started")
        _engine("user_turn_ended", transcript)
        _engine("set_busy", True)        # still mid-turn: LLM / tool / speech to come

        # Dictation mode types every transcript, so it is checked before anything else (a dictated
        # sentence may contain "goodbye"): None = dictation is off.
        # ("Atlas, stop typing": the being's name is not part of what dictation hears)
        pre = _universe("split_name", transcript)
        dictated = _dictation_handle(pre[1] if pre and pre[0] and pre[1] else transcript)
        if dictated is not None:
            logger.info("Dictation handled the transcript%s", f" -> {dictated!r}" if dictated else "")
            if dictated:
                speaker.speak(dictated)
            _engine("set_busy", False)
            _avatar_state("listening")
            continue

        if is_name_only(transcript) and not (last_asked and "raz" not in transcript.lower()):
            speaker.speak(_say("yes"))            # "Raziel?" - "Yes?"  (she is already listening)
            addressed = True
            said_didnt_catch = False
            _engine("set_busy", False)
            continue

        # "Melody, play Kalyani": one of the beings of her universe takes it (universes.py)
        split = _universe("split_name", transcript)
        being_key = split[0] if split else None
        if being_key:
            if not split[1]:                          # "Melody." on its own: "Melody here." - the next sentence is hers
                speaker.speak(_universe("name_only", being_key) or _say("yes"))
                addressed, said_didnt_catch = True, False
                _engine("set_busy", False)
                continue
            transcript = split[1]
            logger.info("For %s: %s", being_key, transcript)

        if is_sleep_phrase(transcript):
            _clear_ai_question()                     # "What should I ask Gemini?" ends here too
            dropped = confirmation.has_pending()
            confirmation.clear("session ended")      # never run a parked action after goodbye
            speaker.speak(_say("goodbye_pending") if dropped else _say("goodbye"))
            brain.end_session()
            _engine("set_busy", False)
            return

        try:
            brain.last_turn_idle = False
        except Exception:
            pass
        _universe("begin_turn", being_key)
        try:
            with _turn_lock:
                reply = confirmation_reply(transcript, brain, speaker.speak)
                if reply is None:
                    reply = route_transcript(transcript, brain)
        except Exception:
            logger.exception("Handling that request failed")
            reply = _say("failed")

        if isinstance(reply, str) and not reply.strip():
            reply = ""                        # nothing to say (the model found no request in it)
            interrupted, barge_in_pre_roll = False, None
        elif isinstance(reply, str):
            logger.info("Reply: %s", reply)
            if confirmation.expects_answer(reply):
                # A yes/no question: she says all of it. Her own voice leaking into the
                # mic (laptop speakers) used to cut it off after a second, so the
                # question was never confirmed as spoken and "yes" did nothing.
                speaker.speak(reply)
                interrupted, barge_in_pre_roll = False, None
            else:
                interrupted, barge_in_pre_roll = speaker.speak_interruptible(reply, pa)
        else:
            # Streaming LLM reply: she starts talking after the first sentence.
            interrupted, barge_in_pre_roll, reply = speaker.speak_stream_interruptible(
                reply, pa, protect=confirmation.expects_answer)
            logger.info("Reply: %s", reply)
        if not (reply or "").strip() and not interrupted:
            # The model found nothing it could do with that (it used to say "Understood." to anything).
            # Called by name: say so; otherwise it was most likely someone else talking - stay quiet.
            if was_addressed and not said_didnt_catch:
                reply = _say("didnt_catch")
                speaker.speak(reply)
                said_didnt_catch = True
            else:
                logger.info("Nothing to do for that (probably not meant for her) - staying quiet.")
        elif (reply or "").strip():
            said_didnt_catch = False                 # a real answer: the next miss may be said again
        _universe("end_turn")                  # (a phone command after this is nobody's being)
        # Arm a parked action only if its question was REALLY spoken (not cut off by
        # a barge-in or a failed reply), then start the user's time to answer.
        confirmation.settle(reply)
        confirmation.restart_timer()
        _remember_reply(reply)
        _engine("assistant_replied", reply)   # also clears initiative's busy flag
        _media_call("touch")
        if interrupted and not _media_ducked() and _media_sound_now():
            # Her answer started a song (or a video autoplayed) and the speakers "interrupted" her: that
            # was the music, not you. Recording it would be 20 s of lyrics.
            logger.info("That interruption was music starting, not you - not recording it.")
            interrupted, barge_in_pre_roll = False, None
        last_asked = bool(confirmation.has_pending() or _asks_back(reply))
        if interrupted:
            logger.info("User interrupted mid-reply, listening immediately.")
            pending_pre_roll = barge_in_pre_roll
        elif last_asked:
            addressed = True             # she asked you something: your answer doesn't need her name
        else:
            addressed = False
            _media_unduck("answered")    # the music comes back up; while it plays she waits for her name


def run_voice_assistant(*_args):
    """
    The actual voice assistant loop. When the avatar is enabled, pywebview
    calls this itself in a background thread once its window is ready
    (pywebview needs the main thread for the GUI). When the avatar is
    disabled, main() just calls this directly instead.

    Anything that goes wrong here is logged to assistant.log with a traceback.
    pywebview swallows exceptions raised in this thread and prints them only to
    the console, which is how a startup crash once looked like "she just
    doesn't start" with nothing in the log.
    """
    logger.info("Starting voice assistant (Phase 2: LLM brain + conversation mode)")

    wake_detector = None
    pa = None
    try:
        # Brain first, and warm it up in the background right away: loading the
        # 8B model takes 10-15 s, and doing it while Whisper and Piper load
        # (instead of on the first question) is what removes the 12-17 s wait
        # the first answer used to have.
        brain = Brain()
        try:
            import universes
            brain.tool_listener = universes.note_tools     # the tool she used -> that being's galaxy lights up
        except Exception:
            logger.exception("universes: couldn't listen to the brain's tools")
        if getattr(config, "LLM_WARMUP", True):
            try:
                brain.warm_up_async()
            except Exception:
                logger.exception("Couldn't start the LLM warm-up (non-fatal)")

        wake_detector = WakeWordDetector()
        transcriber = Transcriber()
        speaker = Speaker()
        pa = pyaudio.PyAudio()

        # Pay Get-StartApps' 2-3 s PowerShell cost now, in the background,
        # instead of on the first "open X" of the session.
        warm_up_app_index()

        speaker.speak(_say("online"))

        # The engine always exists (wake_word.py / audio_recorder.py ask it to gate the microphone
        # while she speaks unprompted, and reminders wait on its busy flag); with INITIATIVE_ENABLED
        # False it just never speaks first.
        engine = initiative.Initiative(
            speak=speaker.speak,
            memory=memory,
            avatar=avatar_server,
            llm=brain,          # only used if use_llm=True; Brain has no quick_completion yet
            use_llm=False,      # templates first; flip once stable
            enabled=bool(getattr(config, "INITIATIVE_ENABLED", True)),
        )
        # The startup line above counts as her first unprompted speech, so
        # the global cooldown starts now (otherwise a "first contact today"
        # line fires seconds after "Systems online.").
        engine.last_speak_time = time.time()
        initiative.engine = engine      # lets wake_word.py / audio_recorder.py gate the mic
        engine.start()                  # registers its own shutdown hook

        start_reminder_scheduler(speaker)    # timers / reminders / alarms ring even when she is asleep
        _start_extras(speaker)               # quiet mode, Voice ID, AI-answer reading, file index
        start_phone_bridge(brain, speaker)   # phone <-> PC bridge (off until config.PHONE_BRIDGE_TOKEN is set)

        failures = 0
        while not shutdown.is_shutting_down():
            try:
                pre_roll_frames = wake_detector.wait_for_wake_word()
                if shutdown.is_shutting_down():
                    break
                _media_duck("wake word")          # music playing? turn it down while she listens
                try:
                    run_awake_session(
                        pa, transcriber, speaker, brain, wake_detector, pre_roll_frames, logger
                    )
                finally:
                    _media_unduck("asleep")
                    _universe("end_session")          # the camera flies back out to the whole universe
                failures = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                # One bad turn (device hiccup, odd transcript) must not end the
                # assistant - but a persistent fault must not spin forever.
                failures += 1
                logger.exception("Voice loop error (%d/5)", failures)
                _engine("set_busy", False)       # otherwise timers would wait for a turn that will never finish
                if failures >= 5:
                    raise
                shutdown.wait(1.0)
                _avatar_state("idle")

    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C received).")
    except Exception:
        logger.exception("Voice assistant crashed")
        shutdown.request_shutdown("voice loop crashed - see assistant.log")
    finally:
        if wake_detector is not None:
            try:
                wake_detector.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def _start_music_listener():
    """The orb avatar dances to music playing on the PC. Optional (needs PyAudioWPatch); never fatal."""
    if getattr(avatar_window, "STYLE", "vrm") != "orb" or not getattr(config, "AVATAR_MUSIC_REACT", True):
        return
    try:
        import music_listener
        music_listener.start()
    except Exception:
        logger.exception("Music listener couldn't start - the orb just won't dance to music")


def main():
    setup_logging()

    # One Raziel at a time: she may already be running in the background (started at login by
    # start_raziel.pyw), and a second copy would hear "Raziel" too and answer everything twice.
    if not instance_guard.acquire():
        logger.info("Raziel is already running - not starting a second copy.")
        print('\nRaziel is already running in the background - just say "Raziel" to wake her.\n'
              'To restart her, use the "Stop Raziel" shortcut first.\n', flush=True)
        return instance_guard.ALREADY_RUNNING_EXIT_CODE

    # Running in the background there's no console to press Ctrl+C in: the "Stop Raziel" shortcut
    # (stop_raziel.pyw) signals this instead, and she shuts down exactly as Ctrl+C would.
    instance_guard.start_stop_listener(lambda: shutdown.request_shutdown("Stop Raziel shortcut"))

    if config.AVATAR_ENABLED:
        import webview  # imported here so the assistant still runs fine
        # without pywebview installed if someone sets AVATAR_ENABLED = False

        avatar_server.start()
        avatar_window.create_window()   # also registers closing the window with shutdown
        logger.info("Starting avatar window (always-on-top, %s avatar).", getattr(avatar_window, "STYLE", "vrm"))
        _start_music_listener()
        if getattr(avatar_window, "STYLE", "vrm") == "orb":
            try:
                import cards
                cards.start()           # timer countdowns, the Spotify song, the weather under the orb
            except Exception:
                logger.exception("Couldn't start the orb's mini cards")

        # Registered last so our console handler runs FIRST - Windows calls
        # them in reverse order, and ours returns True, so pywebview's and
        # MKL's handlers never fire and the console stays clean.
        shutdown.install()

        # pywebview owns the main thread for its GUI event loop; our voice
        # loop runs in the background thread pywebview starts for `func`.
        webview.start(func=run_voice_assistant, http_server=True)
    else:
        shutdown.install()
        run_voice_assistant()


if __name__ == "__main__":
    sys.exit(main())
