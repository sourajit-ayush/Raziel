"""
multi_command.py - several commands in one sentence.

    "open Spotify, play lo-fi and set the volume to 30"
    "set a timer for 10 minutes and what's the weather"
    "open notepad and type hello world"
    "स्पॉटिफाई खोलो और गाना बजाओ"          "spotify kholo aur lofi bajao"

split() cuts a sentence into clauses, but only where the next part clearly starts a NEW command
(a command verb right after ", " / "and" / "then"): "play rock and roll", "add milk and eggs to my list"
and "search for salt and pepper" stay whole. A few phrasings keep the rest of the sentence as their
own text and are never split after: "remind me to ...", "type ...", "message X saying ...",
"open ChatGPT and ask ...". "open Spotify and Chrome" becomes "open Spotify" + "open Chrome".

run() sends every clause through the normal deterministic matchers, in order. Clauses no matcher
knows go to the LLM together, after the rest is done. If NO clause matched, it returns None and
main.py handles the whole sentence exactly as before. A clause that asks a yes/no question
("shut down the PC") is said last, so the question is the last thing you hear before answering.

Pure text logic plus two callbacks (the matcher loop and the LLM turn), so it tests without audio,
Ollama or Windows.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Iterable, Iterator, List, Optional, Tuple

import config
import ai_sites
import lang

logger = logging.getLogger("voice_assistant")

MAX_CLAUSES = 5
OPEN_SETTLE_SECONDS = 1.5        # after "open X", before a clause that may type into / use it
OPEN_WAIT_MAX_SECONDS = 6.0      # ...or, on Windows, until the new window is in front (at most this long)

# ------------------------------------------------------------------ English

_FILLER = r"(?:(?:please|pls|raziel|also|now|then|and|just|quickly|can you|could you)\s+)*"
_VERBS = (
    r"open|launch|start|run|close|quit|exit|kill|play|pause|resume|stop|skip|mute|unmute|set|turn|switch|"
    r"increase|decrease|raise|lower|reduce|put|search|google|look up|find|show|tell|read|remind|add|take|lock|"
    r"send|message|text|whatsapp|email|write|create|make|check|give|go|shut|restart|reboot|sleep|queue|type|"
    r"note|delete|clear|cancel|snooze|wake|minimi[sz]e|maximi[sz]e|screenshot|describe|calculate|convert|"
    r"translate|summari[sz]e|copy|dance|become|what|what's|whats|how|who|when|where|next|previous"
)
_NOUN_CMDS = r"(?:volume|brightness)\s+(?:up|down|to\b|\d)"
_START_RE = re.compile(rf"^{_FILLER}(?:(?:{_VERBS})\b|{_NOUN_CMDS})", re.IGNORECASE)
# These keep the rest of the sentence as their own content: nothing after them is split off.
_PAYLOAD_RE = re.compile(
    r"^(?:(?:please|raziel|can you|could you|now)\s+)*(?:remind|type|write|dictate|ask|say|message|text|send|"
    r"whats\s?app|email|note|remember|search|google|look up|start typing|set a reminder|take a note)\b",
    re.IGNORECASE)
# "open youtube and play X", "open chatgpt and ask X": the pair belongs together (their own matchers).
# (ai_sites.SITE_WORDS: "JetGPT", "chat gbt", "Jemini" - how Whisper writes them - count too.)
_OPEN_SITE_RE = re.compile(r"^(?:open|launch|go to)\s+(?:the\s+)?(youtube|google|spotify|bing|wikipedia|amazon|"
                           rf"{ai_sites.SITE_WORDS})$", re.IGNORECASE)
# "enter and read me the answer" / "press enter, then read it": one request (tools.try_auto_ai_send)
_SEND_READ_RE = re.compile(r"^(?:(?:ok|okay|now|please|so)[\s,]+)*(?:send|submit|press|hit|enter|peter|inter|center|"
                           r"entre|click send)\b.*\b(?:read|tell me)\b", re.IGNORECASE)
# Parts that act on whatever window is in front. Once an earlier part has gone to the model (which
# runs after the matched parts), these must wait for it too: "Open JetGPT and type Hey Hi" typed
# "Hey Hi" into the wrong window before the model had opened anything.
_FOCUS_RE = re.compile(r"^(?:(?:please|now|then)\s+)*(?:type|write|paste|press|hit|enter|click|select|"
                       r"send it|send that|submit|scroll|dictate|start typing)\b", re.IGNORECASE)
_PAIR_TAIL_RE = re.compile(r"^(?:play|search|look up|type|ask|say|tell it|write)\b", re.IGNORECASE)
_PAIR_ABSORBS_REST = re.compile(r"^(?:type|ask|say|tell it|write)\b", re.IGNORECASE)
_SEP_RE = re.compile(
    r"\s*[,;]\s*(?:and\s+then\s+|then\s+|and\s+|also\s+|after\s+that\s+)?"
    r"|(?<=[a-z0-9'\")])[.?!]\s+(?:and\s+then\s+|then\s+|and\s+|also\s+|after\s+that\s+)?"
    r"|\s+(?:and\s+then|and\s+also|and|then|also|after\s+that|plus)\s+",
    re.IGNORECASE)
# "Raziel, what's the time" / "Okay, open Spotify": a name or a filler word, not a command of its own.
_LEAD_FILLER_RE = re.compile(r"^(?:(?:raziel|okay|ok|hey|hi|hello|so|um+|uh+|well|please|yes|yeah|no|right|"
                             r"alright|now|listen|hmm+)\s*[,.!?]\s*)+", re.IGNORECASE)
_FILLER_ONLY_RE = re.compile(r"^(?:raziel|okay|ok|hey|hi|hello|so|um+|uh+|well|please|yes|yeah|no|right|alright|"
                             r"now|listen|hmm+|thanks|thank you)(?:\s+(?:raziel|okay|ok|hey|please|now|then))*$",
                             re.IGNORECASE)
# "when is my next meeting and where is it": the second question is about the first -> one sentence.
_REF_Q_RE = re.compile(r"^(?:what|when|where|who|whom|whose|how|why|which|is|are|was|were|does|do|did|can|could|"
                       r"will|would|should)\b.*\b(?:he|she|they|him|her|them|his|hers|their|its|it|that|there)\b",
                       re.IGNORECASE)
_DUMMY_IT_RE = re.compile(r"\b(?:what (?:time|day|date) is it|is it (?:going to )?(?:rain|raining|cold|hot|sunny|snowing|"
                          r"windy|humid))\b", re.IGNORECASE)
_SEP_CAP_RE = re.compile(f"({_SEP_RE.pattern})", re.IGNORECASE)
# "... and play any music from that" / "... and shuffle it": the music is the thing just named.
_MUSIC_THING_RE = re.compile(r"\b(?:playlists?|albums?|songs?|music|tracks?|artists?|spotify|radio|mix)\b", re.IGNORECASE)
_PLAY_BACKREF_RE = re.compile(r"^(?:play|shuffle|start playing|put on)\b(?:.*\b(?:from|in|off|out of)\s+"
                              r"(?:it|that|this|there|them|those)|\s+(?:it|that|this|them))\s*$", re.IGNORECASE)
_OPENISH_RE = re.compile(r"^(?:please\s+)?(open|launch|close|quit)\s+", re.IGNORECASE)
_SET_RE = re.compile(r"^(?:please\s+)?(set|turn(?:\s+(?:up|down))?|increase|decrease|raise|lower|reduce)\s+", re.IGNORECASE)
_TOGGLE_RE = re.compile(r"^(?:please\s+)?((?:turn|switch)\s+(?:on|off))\s+", re.IGNORECASE)
_TOGGLE_THING_RE = re.compile(r"^(?:the\s+)?(?:wi-?fi|bluetooth|night light|airplane mode|flight mode|dark mode|"
                              r"light mode|hotspot|mobile hotspot|focus assist|do not disturb|caps lock)\b", re.IGNORECASE)
_PREP_RE = re.compile(r"\b(?:to|in|on|with|for|about|of|from|at|by)\b", re.IGNORECASE)
_ABSORBS_REST_RE = re.compile(r"\b(?:and|then)\s+(?:ask|type|say|tell it|write)\b", re.IGNORECASE)
_SETTLE_RE = re.compile(r"^(?:please\s+)?(?:open|launch|start|run|go to)\b", re.IGNORECASE)

# ------------------------------------------------------------------ Hindi / Hinglish

_HI_SEP_RE = re.compile(r"\s*,\s*|\s+(?:और\s+फिर|और|फिर|तथा|aur\s+phir|aur\s+fir|aur|phir|fir)\s+", re.IGNORECASE)
# An imperative (or polite request) at the end of the part before the separator: खोलो, बजाओ, करो,
# लगाओ, दो, बताइए, कीजिए, चलाएं ... / Hinglish kholo, bajao, karo, lagao, do, batao.
_HI_IMPERATIVE_RE = re.compile(r"(?:ो|ओ|िए|िये|ीजिए|इए|ाएं|ाएँ|करें|खोलें|भेजें|रखें|लिखें|पढ़ें|सुनें|देखें|"
                               r"रोकें|चलें|बंद करें)$")
# ...but not these (postpositions, pronouns, "two"): "मम्मी को और पापा को मैसेज भेजो" is ONE command.
_HI_NOT_VERB = {"को", "में", "से", "पे", "पर", "तो", "हो", "सो", "जो", "वो", "यो", "हमें", "तुम्हें", "उन्हें",
                "इन्हें", "जिन्हें", "किन्हें", "आपको", "मुझको", "उसको", "इसको"}
_HI_NUMBERS = {"एक", "दो", "तीन", "चार", "पांच", "पाँच", "छह", "छः", "सात", "आठ", "नौ", "दस"}
_HL_IMPERATIVE_RE = re.compile(r"(?:o|do|dijiye|ijiye|iye|ye)$", re.IGNORECASE)
_HL_WORD_RE = re.compile(r"\b(?:kholo|chalao|bajao|lagao|karo|kardo|kar do|batao|dikhao|bando|band karo|"
                         r"badhao|ghatao|bhejo|likho|suno|rok do|roko)\b", re.IGNORECASE)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.strip(" .!?।")


def _starts_command(part: str) -> bool:
    return bool(_START_RE.match(part.strip()))


def _words(s: str) -> int:
    return len(s.split())


def split(transcript: str) -> Optional[List[str]]:
    """The sentence as 2+ separate commands, or None when it is one command (the usual case).
    A 1-item list means "one command, reworded": route that text instead of the original."""
    text = _clean(transcript)
    if not text or len(text) > 300:
        return None
    text = _LEAD_FILLER_RE.sub("", text).strip()
    if not text:
        return None
    if lang.has_devanagari(text) or _HL_WORD_RE.search(text):
        return _split_hindi(text)
    if _PAYLOAD_RE.match(text) or not _SEP_RE.search(text) or _SEND_READ_RE.match(text):
        return None

    pieces = _SEP_CAP_RE.split(text)            # [part, separator, part, separator, part, ...]
    clauses = [pieces[0].strip()]
    for k in range(1, len(pieces) - 1, 2):
        sep, part = pieces[k], pieces[k + 1].strip()
        cur = clauses[-1]
        if not part:
            continue
        if not cur:
            clauses[-1] = part
            continue
        if _PAYLOAD_RE.match(cur) and not _OPEN_SITE_RE.match(cur):
            clauses[-1] = cur + sep + part        # "type hello and goodbye": all of it is the text
            continue
        if _ABSORBS_REST_RE.search(cur):
            clauses[-1] = cur + sep + part        # "open chatgpt and ask X and why": all of it is the question
            continue
        sverb = _SET_RE.match(cur)
        if sverb and re.match(r"^(?:the\s+)?(?:volume|brightness)\b", part, re.I):
            clauses.append(f"{sverb.group(1).lower()} {part}")     # "... volume to 30 and brightness to 50"
            continue
        toggle = _TOGGLE_RE.match(cur)
        if toggle and _TOGGLE_THING_RE.match(part):
            clauses.append(f"{toggle.group(1).lower()} {part}")    # "turn off wifi and bluetooth"
            continue
        if _PLAY_BACKREF_RE.match(part) and _MUSIC_THING_RE.search(cur):
            return None                           # "open my X playlist and play any music from that": one request
        if _starts_command(part):
            if _REF_Q_RE.match(part) and not _DUMMY_IT_RE.search(part):
                return None                       # "... and where is it": one question, not two
            clauses.append(part)
            continue
        verb = _OPENISH_RE.match(cur)
        if verb and _words(part) <= 3 and _words(cur) <= 4 and not _PREP_RE.search(part) \
                and not _PREP_RE.search(cur[verb.end():]):
            clauses.append(f"{verb.group(1).lower()} {part}")      # "open Spotify and Chrome"
            continue
        clauses[-1] = cur + sep + part            # "play rock and roll": one command
    return _finish(clauses)


def _finish(clauses: List[str]) -> Optional[List[str]]:
    clauses = [_clean(c) for c in clauses if _clean(c)]
    # Re-join the pairs that have their own matchers: "open youtube" + "play X" -> one clause.
    merged: List[str] = []
    i = 0
    while i < len(clauses):
        c = clauses[i]
        if i + 1 < len(clauses) and _OPEN_SITE_RE.match(c) and _PAIR_TAIL_RE.match(clauses[i + 1]):
            if _PAIR_ABSORBS_REST.match(clauses[i + 1]):
                merged.append(c + " and " + " and ".join(clauses[i + 1:]))
                break
            merged.append(c + " and " + clauses[i + 1])
            i += 2
            continue
        merged.append(c)
        i += 1
    if len(merged) > MAX_CLAUSES:
        return None
    if len(merged) < 2:
        # One command after all - but "open google, search for cats" reads better to the matchers as
        # "open google and search for cats": hand that back so it is routed in its tidy form.
        return merged if merged and len(clauses) >= 2 else None
    return merged


def _split_hindi(text: str) -> Optional[List[str]]:
    seps = list(_HI_SEP_RE.finditer(text))
    if not seps:
        return None
    clauses, start = [], 0
    for m in seps:
        left = text[start:m.start()].strip()
        if not left or not text[m.end():].strip():
            continue
        words = left.split()
        last = words[-1]
        hindi_verb = (bool(_HI_IMPERATIVE_RE.search(last)) and last not in _HI_NOT_VERB
                      and not (last in _HI_NUMBERS and (len(words) < 2 or words[-2] in _HI_NUMBERS)))
        if hindi_verb or (_HL_IMPERATIVE_RE.search(last) and not lang.has_devanagari(last)
                          and _HL_WORD_RE.search(left)):
            clauses.append(left)
            start = m.end()
    clauses.append(text[start:].strip())
    clauses = [c for c in clauses if c]
    return clauses if 2 <= len(clauses) <= MAX_CLAUSES else None


# ------------------------------------------------------------------ running them

Matcher = Callable[[str], Optional[Tuple[str, object]]]     # clause -> (label, reply) or None


def _is_stream(reply) -> bool:
    return reply is not None and not isinstance(reply, (str, bytes)) and hasattr(reply, "__iter__") \
        and not hasattr(reply, "prompt")


def run(clauses: List[str], match: Matcher, brain_turn: Callable, *, record: Optional[Callable] = None,
        localize: Callable = lambda s: s, pending_count: Optional[Callable[[], int]] = None,
        sleep: Callable[[float], None] = time.sleep, foreground: Optional[Callable[[], object]] = None):
    """
    Runs every clause. Returns the reply (a str, or an iterator of sentences when an LLM part or a
    streamed reply is involved), or None when no clause matched a deterministic handler - then the
    whole sentence should be handled as before.

    match(clause)            -> (label, reply) | None; runs the action (main._match_deterministic)
    brain_turn(text, model_text=None) -> str | iterator; the LLM for clauses no matcher knew
    record(user, assistant)  -> saves a finished exchange to the conversation memory
    pending_count()          -> how many yes/no questions are waiting (confirmation.pending_count)
    foreground()             -> the window in front (Windows); after "open X" the next part waits for
                                it to change, i.e. for X to open, instead of a fixed pause
    """
    if pending_count is None:
        import confirmation
        pending_count = confirmation.pending_count

    said: List[str] = []            # replies, in order
    questions: List[str] = []       # replies that asked a yes/no question: said last
    streams: List[object] = []      # streamed replies (the AI-answer reader) and LLM hand-offs
    leftovers: List[str] = []       # clauses for the LLM
    matched = []

    for i, clause in enumerate(clauses):
        before = pending_count()
        opens = i + 1 < len(clauses) and bool(_SETTLE_RE.match(clause))
        front = _safe(foreground) if opens else None
        if leftovers and _FOCUS_RE.match(clause):
            leftovers.append(clause)                # typing / pressing after an unmatched part: in order
            continue
        try:
            hit = match(clause)
        except Exception:
            logger.exception("Multi-command: clause %r failed", clause)
            hit = ("error", lang.tr(_T, "failed", clause=clause))
        if not hit:
            if not _FILLER_ONLY_RE.match(clause.strip(" ,.!?")):
                leftovers.append(clause)            # ("okay" / "Raziel" on their own aren't a request)
            continue
        label, reply = hit
        matched.append(label)
        if hasattr(reply, "prompt"):                   # routing.AskLLM: the model writes this answer
            streams.append(("ask", clause, reply))
            continue
        if _is_stream(reply):
            streams.append(("stream", clause, reply))
            continue
        reply = localize(str(reply))
        if record is not None and label != "error":
            try:
                record(clause, reply)
            except Exception:
                logger.debug("Multi-command: recording failed", exc_info=True)
        (questions if pending_count() > before else said).append(reply)
        if opens:
            _wait_for_window(front, foreground, sleep)  # let the app open before the next step uses it

    if not matched:
        return None
    logger.info("Multi-command: %d clauses -> matched %s; for the model: %s", len(clauses), matched, leftovers)

    if not leftovers and not streams:
        return " ".join(said + questions)

    def pieces() -> Iterator[str]:
        for s in said:
            yield s
        if leftovers:
            yield from _as_pieces(brain_turn(" and ".join(leftovers), None))
        for kind, clause, reply in streams:
            if kind == "ask":
                yield from _as_pieces(brain_turn(clause, reply.prompt))
            else:
                yield from _as_pieces(reply)
        for q in questions:
            yield q

    return pieces()


def _safe(fn):
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _wait_for_window(front, foreground, sleep):
    """After "open X": until a different window is in front (X opened), at most OPEN_WAIT_MAX_SECONDS,
    plus a moment for it to be ready. Without a way to tell (not Windows), a fixed pause."""
    if foreground is None or front is None:
        sleep(OPEN_SETTLE_SECONDS)
        return
    waited = 0.0
    while waited < OPEN_WAIT_MAX_SECONDS:
        sleep(0.25)
        waited += 0.25
        now = _safe(foreground)
        if now is not None and now != front:
            sleep(0.8)
            return
    logger.info("Multi-command: no new window after %.0f s - going on anyway", OPEN_WAIT_MAX_SECONDS)


def _as_pieces(reply) -> Iterable[str]:
    if reply is None:
        return []
    if isinstance(reply, str):
        return [reply]
    return reply


_T = {
    "failed": {"en": "I couldn't do this part: {clause}.", "hi": "ये हिस्सा नहीं हो पाया: {clause}।"},
}
