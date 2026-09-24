"""
Spoken yes/no confirmation for risky actions (Phase 3).

WHY IT IS BUILT THIS WAY
------------------------
A tool cannot simply "ask a question and wait for the answer" from inside
itself: in streaming mode tools run in the reply-producer thread of
speaker.speak_stream_interruptible() while the main thread holds the barge-in
microphone stream, and the answer has to go through the normal record ->
transcribe path anyway. So confirmation is a two-turn exchange:

  turn 1   "send Mom a WhatsApp saying I'm late"
             -> send_whatsapp_message() does NOT send. It calls request(),
                which parks the real action here and returns the question
                ("Send Mom this WhatsApp message: I'm late. Say yes to send,
                or no to cancel."). The brain speaks that verbatim.
  turn 2   "yes"  ->  main.py hands the transcript to handle_transcript(),
                      which runs the parked action and returns its result.
           "no"   ->  cancelled, nothing happens.

FAIL-CLOSED
-----------
Nothing runs unless the user clearly says yes:

  * An action is only ARMED once main.py has confirmed that its question was
    really spoken (settle()). If she was interrupted before the question, or
    the reply failed, it is dropped - a later "okay" can never trigger an
    action the user was never asked about.
  * Acceptance is strict: the WHOLE utterance must be an affirmation ("yes",
    "yeah go ahead", "yes please"). Whisper sometimes invents a short phrase
    from noise, so "thank you", "right", "so okay", "yes?" are all NOT a yes.
    A clear negative ("no", "cancel", "don't") anywhere in a short utterance
    means no, and wins over everything else.
  * An unclear answer never counts as yes; a pending action expires after
    config.CONFIRM_TIMEOUT_SECONDS, is dropped when the session ends
    ("goodbye") or a new session starts, and is removed from the queue BEFORE
    it runs so it can never run twice.

Only main.py, tools.py and llm_brain.py talk to this module. It has no
dependency on audio, Ollama or the OS, so it can be tested on its own.
"""

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple

import config
import lang

logger = logging.getLogger("voice_assistant")


# --- settings (all optional in config.py; these defaults apply if missing) ------

def _cfg(name: str, default):
    return getattr(config, name, default)


def needs_confirmation(kind: str) -> bool:
    """True if the action `kind` must be confirmed out loud before it runs."""
    if not _cfg("CONFIRM_RISKY_ACTIONS", True):
        return False
    return kind in _cfg("CONFIRM_ACTIONS", ("send_whatsapp_message", "overwrite_file"))


def _now() -> float:
    return time.monotonic()          # immune to the clock being changed mid-wait


# --- the framework's own sentences (English + Hindi) --------------------------------
# Hindi summaries are written as an infinitive/noun phrase: "माँ को संदेश भेजना".

_T = {
    "also_ask": {"en": "I'll also ask about that: {summary}",
                 "hi": "उसके बारे में भी पूछा जाएगा: {summary}"},
    "queued": {"en": "Understood. {announcement}, once we've settled the first one.",
               "hi": "ठीक है। {announcement}, पहले वाले के बाद।"},
    "reprompt": {"en": "I didn't catch that. Should I {summary}? Say yes or no.",
                 "hi": "मुझे साफ़ सुनाई नहीं दिया। क्या {summary} है? हाँ या नहीं बोलिए।"},
    "timed_out": {"en": "That request timed out, so I didn't {summary}. Ask me again if you still want it.",
                  "hi": "वह अनुरोध समय पर पूरा नहीं हुआ और रद्द हो गया: {summary}। ज़रूरत हो तो दोबारा कहिए।"},
    "cancelled": {"en": "Cancelled. I did not {summary}.",
                  "hi": "रद्द कर दिया: {summary}।"},
    "understood_not": {"en": "Understood. I did not {summary}.",
                       "hi": "ठीक है, रद्द कर दिया: {summary}।"},
    "next": {"en": "{text} Next: {question}", "hi": "{text} अगला: {question}"},
    "failed": {"en": "That failed: {error}", "hi": "यह नहीं हो पाया: {error}"},
    "done": {"en": "Done.", "hi": "हो गया।"},
    "and": {"en": " and ", "hi": " और "},
}


# --- pending actions ----------------------------------------------------------------

# state: "new"    registered this turn, question not yet confirmed as spoken
#        "queued" waiting behind another action; only announced so far
#        "armed"  its question was spoken: a clear "yes" now runs it
@dataclass
class PendingAction:
    kind: str                        # e.g. "send_whatsapp_message", "overwrite_file"
    summary: str                     # short spoken description: "send Mom a WhatsApp message"
    question: str                    # what she asks out loud
    execute: Callable[[], str]       # runs the real action, returns the sentence to speak
    ack: str = ""                    # spoken right after "yes", before a slow action runs
    yes_phrases: Tuple[str, ...] = ()  # extra affirmations valid for THIS action ("send it")
    announcement: str = ""           # what is said when it is queued behind another
    state: str = "new"
    expires_at: float = 0.0
    reprompts: int = 0
    id: int = 0
    lang: str = "en"                 # language the question was asked in (answers come back in it)


@dataclass
class Outcome:
    """What main.py should do with a transcript while a confirmation is open."""
    reply: Optional[str] = None      # text to speak (None = nothing to say)
    then_route: bool = False         # True = ALSO handle the transcript as a normal command
    handled: bool = False            # True = the transcript was consumed as an answer
    note: str = ""                   # for the LLM's history when then_route drops an action


_lock = threading.RLock()
_queue: Deque[PendingAction] = deque()
_expired: Optional[PendingAction] = None     # last one that timed out (to explain a late "yes")
_expired_at = 0.0
_next_id = 1
_EXPIRED_MEMORY_S = 300.0                    # a "yes" hours later is just a "yes"


def _timeout() -> float:
    return float(_cfg("CONFIRM_TIMEOUT_SECONDS", 60))


def request(kind: str, question: str, execute: Callable[[], str], summary: str = "",
            ack: str = "", yes_phrases: Tuple[str, ...] = ()) -> str:
    """
    Parks a risky action and returns the sentence to speak to the user.

    The action stays inert until main.py calls settle() with what was actually
    spoken. Several actions can be waiting at once (e.g. "message Mom and Dad"):
    the first one's question is spoken now, and the next is asked after the user
    answers the first. Every action gets its own yes/no.

    `ack` (optional) is a short line spoken the moment the user says yes, for
    actions that take a while (WhatsApp waits ~12 s) so it isn't silent meanwhile.
    `yes_phrases` are extra whole-utterance affirmations valid only for this
    action ("send it" for a message, "overwrite it" for a file).
    """
    global _next_id, _expired
    with _lock:
        _drop_expired()
        _expired = None
        summary = summary or kind.replace("_", " ")
        first = not _queue
        code = lang.current()
        action = PendingAction(
            kind=kind, summary=summary, question=question, execute=execute, ack=ack,
            yes_phrases=tuple(yes_phrases), expires_at=_now() + _timeout(), id=_next_id,
            announcement="" if first else lang.tr(_T, "also_ask", code, summary=summary),
            lang=code,
        )
        _next_id += 1
        _queue.append(action)
        logger.info("Confirmation requested (#%d, %s): %s", action.id, kind, question)
        if first:
            return question
        return lang.tr(_T, "queued", code, announcement=action.announcement)


def settle(spoken: str):
    """
    Call after every spoken reply with the text that was ACTUALLY spoken.
    An action whose question was spoken becomes armed (answerable); one whose
    question never got said (barge-in before it, a failed reply, ...) is dropped.
    """
    spoken = spoken or ""
    with _lock:
        _drop_expired()
        keep = []
        for a in _queue:
            if a.state == "armed":
                keep.append(a)
            elif a.question in spoken:
                a.state = "armed"
                a.expires_at = _now() + _timeout()      # the user's time starts NOW
                logger.info("Confirmation #%d armed (question was spoken)", a.id)
                keep.append(a)
            elif a.state == "new" and a.announcement and a.announcement in spoken:
                a.state = "queued"
                keep.append(a)
            elif a.state == "queued":
                keep.append(a)
            else:
                logger.info("Confirmation #%d dropped: its question was never spoken", a.id)
        _queue.clear()
        _queue.extend(keep)


def _reprompt_text(action: PendingAction) -> str:
    return lang.tr(_T, "reprompt", action.lang, summary=action.summary)


def expects_answer(text: str) -> bool:
    """
    True if `text` is a yes/no question about a parked action - the original
    question, or the "I didn't catch that" re-ask. main.py plays such a sentence
    WITHOUT barge-in: the user must hear it to the end before answering, and on
    laptop speakers her own voice leaking into the mic used to cut the question
    off after a second (then the action was never confirmed and "yes" did nothing).
    """
    if not text:
        return False
    with _lock:
        _drop_expired()
        for a in _queue:
            if a.question in text or _reprompt_text(a) in text:
                return True
    return False


def has_pending() -> bool:
    """True while any action is registered (armed or not)."""
    with _lock:
        _drop_expired()
        return bool(_queue)


def pending_count() -> int:
    with _lock:
        _drop_expired()
        return len(_queue)


def restart_timer():
    """Gives every ARMED action a fresh full window (never revives an expired one)."""
    with _lock:
        _drop_expired()
        deadline = _now() + _timeout()
        for action in _queue:
            if action.state == "armed":
                action.expires_at = deadline


def clear(reason: str = ""):
    """Drops everything that is waiting (session ended, shutdown, ...)."""
    global _expired
    with _lock:
        if _queue:
            logger.info("Confirmation cleared (%d dropped)%s", len(_queue),
                        f": {reason}" if reason else "")
        _queue.clear()
        _expired = None


def _drop_expired():
    """Removes timed-out actions. Must be called with _lock held."""
    global _expired, _expired_at
    now = _now()
    if not any(a.expires_at < now for a in _queue):
        return
    keep = []
    for action in _queue:
        if action.expires_at < now:
            _expired, _expired_at = action, now
            logger.info("Confirmation #%d (%s) timed out - not executed", action.id, action.kind)
        else:
            keep.append(action)
    _queue.clear()
    _queue.extend(keep)


# --- understanding the answer -------------------------------------------------------
#
# Everything below works on tokens with apostrophes removed ("don't" -> "dont").
# Hindi (Devanagari) and romanised Hindi ("haan", "nahi") are understood as well.
# Devanagari is compared in its folded spelling (lang.fold_hindi): हाँ = हां = हा.

def _f(words: str) -> set:
    """A set of folded Hindi tokens from a space-separated string."""
    return {lang.fold_hindi(w) for w in words.split()}


def _fp(phrases: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(lang.fold_hindi(p) for p in phrases)


# Unambiguous refusals: anywhere in a short utterance they mean NO.
_STRONG_NO = {
    "no", "nope", "nah", "nay", "cancel", "canceled", "cancelled", "abort", "negative",
    "dont", "doesnt", "disagree", "decline",
} | _f("नहीं नही नो नहि मत कैंसिल कैंसल कैन्सल केंसल रद्द रद्दी नाह एबॉर्ट") | {
    "nahi", "nahin", "nahee", "nai", "mat", "nahi",
}
# Words that only mean "no" when they ARE the answer ("stop", "hold on"), not when
# they start a new command ("stop the music").
_WEAK_NO = {
    "not", "stop", "wait", "hold", "never", "nevermind", "forget", "skip", "undo", "quit", "wrong",
    "incorrect", "leave", "mind",
} | _f("ना रुको रुकिए रुकिये रुक ठहरो ठहरिए छोड़ो छोड़ छोड़िए रहने जाने भूल बस स्टॉप") | {
    "ruko", "rukiye", "chhodo", "chodo", "rehne", "jaane", "bas",
}
_NO_FILL = {
    "i", "im", "ill", "it", "that", "thats", "this", "please", "thanks", "thank", "you", "raziel",
    "do", "send", "sending", "message", "just", "actually", "now", "yet", "sorry", "mean",
    "meant", "want", "to", "but", "on",
} | _f("अभी इसे इसको यह ये है हूँ दो दीजिए दीजिये करो मैं को मुझे चाहिए जाओ जाइए प्लीज कृपया रज़ील "
       "जी भेजना करना") | {"do", "abhi", "ise", "hai", "please"}

_YES_CORE = {
    "yes", "yeah", "yea", "yep", "yup", "yah", "aye", "sure", "okay", "ok", "okey", "confirm",
    "confirmed", "affirmative", "absolutely", "definitely", "certainly", "proceed", "correct",
    "alright", "agreed",
} | _f("हाँ हां हा जी जरूर ज़रूर बिल्कुल बिलकुल ठीक ओके यस येस सही पक्का बेशक अवश्य") | {
    "haan", "han", "haa", "haanji", "ji", "jee", "zaroor", "zarur", "jarur", "jaroor", "bilkul",
    "theek", "thik", "pakka", "sahi",
}
# Whole-phrase affirmations valid for any action. Action-specific ones ("send it",
# "overwrite it") come from the action itself.
_YES_PHRASES = ("go ahead", "do it", "go for it", "of course", "please do", "sounds good", "go on") + _fp((
    "ठीक है", "ठीक रहेगा", "सही है", "कर दो", "कर दीजिए", "कर दीजिये", "कर दें", "करो", "कीजिए",
    "कीजिये", "भेज दो", "भेजो", "भेज दीजिए", "भेज दीजिये", "भेज दें", "आगे बढ़ो", "चलो", "चलेगा",
    "हाँ जी", "जी हाँ", "बिल्कुल ठीक", "ओ के",
)) + ("theek hai", "thik hai", "kar do", "kar dijiye", "bhej do", "bhej dijiye", "haan ji", "ji haan")
_YES_FILL = {"please", "raziel", "and", "now", "thanks", "thank", "you"} | _f("प्लीज कृपया और अभी शुक्रिया धन्यवाद रज़ील ना")
_YES_LAST = {"please", "now", "thanks", "thank", "you", "raziel"} | _f("प्लीज कृपया अभी शुक्रिया धन्यवाद रज़ील जी ना")
_YES_FIRST = {"raziel", "please"} | _f("प्लीज कृपया रज़ील")
_MAX_YES_TOKENS = 12
_MAX_NO_TOKENS = 8
_MAX_WEAK_NO_TOKENS = 5


def normalize(text: str) -> str:
    """Lower-cased, Devanagari-safe, punctuation-free. Keeps "?" (a question mark means unsure)."""
    text = lang.fold_hindi((text or "").replace("’", "'"))
    text = re.sub(r"[^a-z0-9'?\u0900-\u097F ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(norm: str):
    return norm.replace("'", "").replace("?", " ").split()


def parse_answer(transcript: str, extra_yes_phrases: Tuple[str, ...] = ()) -> Optional[str]:
    """
    Returns "yes", "no", or None (unclear). Strict on purpose: only a whole
    utterance that IS an affirmation counts as yes, and negatives win.
    """
    norm = normalize(transcript)
    tokens = _tokens(norm)
    if not tokens:
        return None

    # --- no ---
    if len(tokens) <= _MAX_NO_TOKENS and any(t in _STRONG_NO for t in tokens):
        return "no"
    if (len(tokens) <= _MAX_WEAK_NO_TOKENS and any(t in _WEAK_NO for t in tokens)
            and all(t in (_STRONG_NO | _WEAK_NO | _NO_FILL | _YES_CORE) for t in tokens)):
        return "no"

    # --- yes ---
    if "?" in norm:                       # "Yes?" / "Okay?" - she is unsure: ask again
        return None
    if len(tokens) > _MAX_YES_TOKENS:
        return None
    text = f" {' '.join(tokens)} "
    extra = tuple(lang.fold_hindi(p) for p in extra_yes_phrases)
    for phrase in sorted(tuple(_YES_PHRASES) + extra, key=len, reverse=True):
        text = text.replace(f" {phrase.replace(chr(39), '')} ", " PH ")
    rest = text.split()
    yes_words = set(_YES_CORE) | {"PH"}
    if not any(t in yes_words for t in rest):
        return None
    if rest[0] not in (yes_words | _YES_FIRST) or rest[-1] not in (yes_words | _YES_LAST):
        return None
    if any(t not in (yes_words | _YES_FILL) for t in rest):
        return None
    return "yes"


def _n_tokens(transcript: str) -> int:
    return len(_tokens(normalize(transcript)))


# --- the turn handler used by main.py ------------------------------------------------

def handle_transcript(transcript: str, announce: Optional[Callable[[str], None]] = None) -> Outcome:
    """
    Call with every transcript BEFORE normal routing.

    Outcome.handled False  -> nothing was waiting; route the transcript normally.
    Outcome.handled True   -> speak Outcome.reply (the transcript was an answer);
                              if Outcome.then_route is also True, route it normally too.

    `announce(text)` (e.g. speaker.speak) is used to say the action's `ack` line
    right after "yes", before a slow action starts.
    """
    global _expired
    to_run: Optional[PendingAction] = None
    with _lock:
        _drop_expired()
        head = _queue[0] if _queue else None
        if head is None or head.state != "armed":
            expired, _expired = _expired, None
            if (expired is not None and _now() - _expired_at < _EXPIRED_MEMORY_S
                    and parse_answer(transcript, expired.yes_phrases) is not None):
                # A late "yes"/"no" for something that already timed out. Say so
                # instead of letting the LLM guess what "yes" refers to.
                lang.set_current(expired.lang)
                return Outcome(reply=lang.tr(_T, "timed_out", expired.lang, summary=expired.summary),
                               handled=True)
            return Outcome()

        action = head
        answer = parse_answer(transcript, action.yes_phrases)
        n_tokens = _n_tokens(transcript)

        if answer == "yes":
            _queue.popleft()                       # removed BEFORE running: never twice
            logger.info("Confirmation #%d (%s) approved by the user", action.id, action.kind)
            to_run = action

        elif answer == "no":
            _queue.popleft()
            logger.info("Confirmation #%d (%s) declined by the user", action.id, action.kind)
            lang.set_current(action.lang)
            return Outcome(reply=_with_next(lang.tr(_T, "cancelled", action.lang, summary=action.summary)),
                           handled=True)

        elif n_tokens <= 3 and action.reprompts < int(_cfg("CONFIRM_MAX_REPROMPTS", 1)):
            action.reprompts += 1
            action.expires_at = _now() + _timeout()
            logger.info("Confirmation #%d: unclear answer %r - asking again", action.id, transcript)
            lang.set_current(action.lang)
            return Outcome(
                reply=_reprompt_text(action),
                handled=True,
            )

        else:
            # Unclear twice, or a whole new sentence: treat it as "no" (fail-closed).
            dropped = list(_queue)
            _queue.clear()
            logger.info("Confirmation cancelled (%d dropped): unclear answer %r",
                        len(dropped), transcript)
            summaries = lang.tr(_T, "and", "en").join(a.summary for a in dropped)
            if n_tokens >= 2:
                # Most likely a new request: drop the old action(s), handle the new one.
                return Outcome(reply=None, then_route=True, handled=True,
                               note=f"(Cancelled: I did not {summaries}.)")
            code = dropped[0].lang
            lang.set_current(code)
            joined = lang.tr(_T, "and", code).join(a.summary for a in dropped)
            return Outcome(reply=lang.tr(_T, "understood_not", code, summary=joined), handled=True)

    # Approved: run it OUTSIDE the lock (WhatsApp takes ~12 s to send). The result is
    # worded in the language the question was asked in, even if the answer was "yes".
    lang.set_current(to_run.lang)
    if to_run.ack and announce is not None:
        try:
            announce(to_run.ack)
        except Exception:
            logger.exception("Couldn't speak the acknowledgement (non-fatal)")
    result = _run(to_run)
    with _lock:
        return Outcome(reply=_with_next(result), handled=True)


def _run(action: PendingAction) -> str:
    try:
        result = action.execute()
    except Exception as e:  # noqa: BLE001 - a failing action must never crash the loop
        logger.exception("Confirmed action #%d (%s) failed", action.id, action.kind)
        return lang.tr(_T, "failed", action.lang, error=e)
    return str(result) if result else lang.tr(_T, "done", action.lang)


def _with_next(text: str) -> str:
    """Appends the next queued question (if any) to what she is about to say.
    Must be called with _lock held. The next action becomes answerable only
    once main.py confirms (settle) that this question was really spoken."""
    if _queue:
        _queue[0].expires_at = _now() + _timeout()
        return lang.tr(_T, "next", _queue[0].lang, text=text, question=_queue[0].question)
    return text
