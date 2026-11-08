"""
instant_matchers.py - the deterministic "instant" matchers: requests answered in code, before the LLM.

main.py's DETERMINISTIC_MATCHERS runs these in order; each returns the finished reply when the whole
sentence matched, else None (and nothing happened). Most act through the tool functions in tools.py:
"my favorite X is Y", "open SITE and play X", "open ChatGPT and ask X", "send it and read me the answer",
"add X to the queue", "message X saying Y", "open X" / "open my X folder", date and time, thanks / hello,
screenshots and "play X". The pure phrase patterns for the last four are in instant_replies.py.

Split out of tools.py (2026-11-08) so that file holds the tools themselves; tools.py never imports this
module, so there is no import cycle. Tests that swap a tool out patch it here (instant_matchers.open_app).
"""

import difflib
import logging
import re
import time

import config
import instant_replies
import spotify_queue
from tools import (  # the tools these matchers act through, and their shared tables
    AI_CHAT_SITES,
    APP_COMMANDS,
    FULL_PATH_CANDIDATES,
    KNOWN_FOLDERS,
    PROTOCOL_APPS,
    WEBSITE_URLS,
    _AI_DISPLAY,
    _AI_SITE_WORDS,
    _ai_state,
    _folder_key,
    _get_shortcut_index,
    _get_start_apps,
    _resolve_contact,
    _spoken_app_name,
    canonical_ai_site,
    next_track,
    open_app,
    open_folder,
    open_website_search,
    play_music,
    play_playlist,
    playlist_names,
    previous_track,
    queue_music,
    send_whatsapp_message,
    show_last_screenshot,
    take_screenshot,
    type_into_ai_site_and_read,
)

logger = logging.getLogger("voice_assistant")


# --- Deterministic "favorite X" memory handling ------------------------------
# Relying on the LLM to reliably call remember/recall for stated preferences
# proved inconsistent with an 8B local model - it would sometimes claim to
# remember something without calling the tool, or fail to notice a fact
# already in its context. These patterns are matched directly, in code,
# before anything reaches the LLM, so "my favorite X is Y" and "what's my
# favorite X" work every single time regardless of model behavior.

_FAVORITE_STATEMENT_RE = re.compile(
    r"^(?:my )?favorite (.+?) is (.+)$", re.IGNORECASE
)
_FAVORITE_QUESTION_RE = re.compile(
    r"^(?:who|what) is my favorite (.+?)\??$", re.IGNORECASE
)

# Filler words people naturally say before the actual sentence ("Okay, my
# favorite musician is...") - stripped before pattern matching, since the
# patterns above are anchored to the start of the sentence and would
# otherwise miss anything with a filler prefix, silently falling through to
# the less reliable LLM-driven path instead.
_FILLER_PREFIX_RE = re.compile(
    r"^(?:okay|ok|so|alright|well|umm?|uhh?|hey|please|now|actually)[,.]?\s+",
    re.IGNORECASE,
)


def _strip_filler_prefix(text: str) -> str:
    for _ in range(3):  # handle stacked fillers like "Okay, so, ..."
        new_text = _FILLER_PREFIX_RE.sub("", text, count=1)
        if new_text == text:
            break
        text = new_text
    return text


def _favorite_key(category: str) -> str:
    return "favorite_" + re.sub(r"\s+", "_", category.strip().lower())


def try_auto_remember_favorite(transcript: str):
    """If transcript states a favorite (e.g. 'my favorite musician is Arijit Singh'),
    saves it deterministically and returns a confirmation, else returns None."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!")
    m = _FAVORITE_STATEMENT_RE.match(text)
    if not m:
        return None

    import memory

    category, value = m.group(1).strip(), m.group(2).strip()
    key = _favorite_key(category)
    content = f"User's favorite {category} is {value}"
    memory.add_fact(content, key=key)
    import obsidian_memory
    obsidian_memory.add_fact(content, replace_prefix=f"User's favorite {category} is")
    logger.info("Auto-remembered (deterministic): %s", content)
    return f"Got it, I'll remember your favorite {category} is {value}."


def try_auto_recall_favorite(transcript: str):
    """If transcript asks about a favorite (e.g. 'what is my favorite musician'),
    looks it up deterministically and returns an answer, else returns None."""
    # Truncate at the first '?' - if transcription hallucinated repeated or
    # garbled trailing content after the actual question (a known Whisper
    # quirk on longer/noisier recordings), this drops the garbage instead of
    # capturing it into the category and producing a nonsensical reply.
    text = transcript.split("?", 1)[0]
    text = _strip_filler_prefix(text.strip()).rstrip(".!")
    m = _FAVORITE_QUESTION_RE.match(text)
    if not m:
        return None

    import memory

    category = m.group(1).strip()
    key = _favorite_key(category)
    content = memory.get_fact_by_key(key)
    if content:
        return content

    # Fall back to a broader search across ALL remembered facts, including
    # ones saved via the LLM's own remember tool (which doesn't use keys) -
    # so a fact is still findable even if it wasn't saved through this exact
    # deterministic path.
    facts = memory.get_recent_facts(limit=50)
    matches = [
        f for f in facts
        if category.lower() in f.lower() and "favorite" in f.lower()
    ]
    if matches:
        return matches[-1]

    return f"I don't know your favorite {category} yet - tell me and I'll remember it."


# --- Deterministic "open SITE and play/search X" handling -------------------
# Same reasoning as the favorite-fact handling above: relying on the LLM to
# reliably pick the right tool for this specific phrasing pattern proved
# inconsistent (it would sometimes just say "I can't open YouTube" instead
# of calling any tool at all). Matched directly in code before anything
# reaches the LLM, so this works every time regardless of model behavior.

_OPEN_SITE_ACTION_RE = re.compile(
    r"^open (youtube|google|spotify|bing|wikipedia|amazon) and "
    r"(play|search(?: for)?|look up) (.+)$",
    re.IGNORECASE,
)

_GENERIC_QUERY_RE = re.compile(r"^(?:any|some) ", re.IGNORECASE)


def try_auto_open_site_action(transcript: str):
    """If transcript matches 'open SITE and play/search X', handles it
    deterministically and returns the result, else returns None."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!?")
    m = _OPEN_SITE_ACTION_RE.match(text)
    if not m:
        return None

    site = m.group(1).strip().lower()
    verb = m.group(2).strip().lower()
    query = m.group(3).strip()
    query = _GENERIC_QUERY_RE.sub("", query)  # "any music" -> "music"
    play = verb == "play"

    logger.info("Deterministic open-site-action matched: site=%s verb=%s query=%s", site, verb, query)

    if site == "spotify":
        return play_music(query)
    return open_website_search(site, query, play=play)


# --- Deterministic "open AI_SITE and type/ask X" handling ---------------------
# Matches the phrase in code and calls type_into_ai_site (defined earlier, alongside the
# other v3 tool implementations) directly, before anything reaches the LLM - same reasoning
# as try_auto_open_site_action just above: relying on the LLM to pick the right tool for
# this exact phrasing proved unreliable.

_AI_CHAT_ACTION_RE = re.compile(
    rf"^(?:open|launch|go to|start)\s+(?:the\s+)?({_AI_SITE_WORDS})(?:\s+app)?\s*(?:,|and|and then|then)\s+"
    r"(?:type|ask(?: it)?|say|tell it|write|search|type in|enter)(?:\s+that|\s+in|\s+it(?=[\s,:.]|$))?[\s,:.]*(.*)$",
    re.IGNORECASE,
)
# "ask Gemini what AI is" / "ask ChatGPT about black holes"
_AI_ASK_RE = re.compile(rf"^ask\s+(?:the\s+)?({_AI_SITE_WORDS})\s+(?:about\s+|that\s+|,\s*)?(.+)$", re.IGNORECASE)


def try_auto_type_into_ai_site(transcript: str):
    """If transcript matches 'open SITE and type/ask X' for a known AI chat site,
    handles it deterministically and returns the result, else returns None.

    Deliberately does NOT rstrip trailing '.!?' the way the other deterministic
    matchers above do: there, the trailing punctuation sits after a short keyword
    (a search term, an app name) and stripping it is harmless or even helpful. Here
    the captured group is the user's actual message to type - often a spoken
    question - so a trailing '?' is part of what they want typed, not noise to
    discard. The message group is "(.+)$" anchored to the true end of the string
    either way, so skipping the rstrip only changes what ends up IN that group."""
    text = _strip_filler_prefix(transcript.strip())
    m = _AI_CHAT_ACTION_RE.match(text) or _AI_ASK_RE.match(text)
    if not m:
        return None
    site = canonical_ai_site(m.group(1)) or m.group(1).strip().lower()
    message = m.group(2).strip()
    logger.info("Deterministic AI-chat-site request matched: site=%s message=%r", site, message)
    return type_into_ai_site_and_read(site, message)


# --- "send it and read me the answer" / "read me the answer" (ChatGPT / Gemini in front) -------------------
# The log: "Okay, type. What is AI?" typed the question into Gemini, then "Send it and read me the answer."
# had no handler at all - the model repeated an old error. The question is typed, so: Enter, then the
# answer is read exactly as for "open Gemini and ask X".
_AI_POLITE = r"^(?:(?:ok|okay|now|and|please|raziel|then|so|alright|yes|yeah)[\s,]+)*"
_AI_READ_TAIL = (r"(?:read (?:me |out )?(?:the |its |it'?s |that )?(?:answer|reply|response|result)(?: out)?(?: to me)?|"
                 r"read (?:it|that)(?: out)?(?: to me)?|tell me (?:the answer|what it says|what it said)|"
                 r"what does it say)")
_AI_SEND_RE = re.compile(
    _AI_POLITE + r"(?:(?P<send>send it|send that|send (?:the |this )?(?:message|question)|send|press enter|hit enter|"
    r"press (?:the )?enter(?: key| button)?|submit(?: it| that)?|enter|click send)"
    r"|(?P<misheard>peter|inter|entre|center))"
    rf"(?:\s*(?:,|and|then|and then)?\s+(?P<read>{_AI_READ_TAIL}))?(?:[\s,]+(?:please|now))*[.!?]*$", re.IGNORECASE)
_AI_READ_RE = re.compile(_AI_POLITE + _AI_READ_TAIL + r"(?:[\s,]+(?:please|now))*[.!?]*$", re.IGNORECASE)


def _ai_site_in_front():
    """(site_key, hwnd) of the ChatGPT / Gemini / ... window in front, or (None, 0)."""
    try:
        import winutil
        w = winutil.foreground_window()
    except Exception:  # noqa: BLE001
        return None, 0
    title = str(w.get("title") or "").lower()
    for key, words in AI_CHAT_SITES.items():
        if any(k in title for k in words):
            return ("chatgpt" if key == "chat gpt" else key), int(w.get("hwnd") or 0)
    return None, 0


def try_auto_ai_send(transcript: str):
    t = re.sub(r"\s+", " ", (transcript or "").strip())
    m = _AI_SEND_RE.match(t)
    read_only = None if m else _AI_READ_RE.match(t)
    if not m and not read_only:
        return None
    if m and m.group("misheard") and not m.group("read"):
        return None                                   # "Peter" on its own is just a name
    site, hwnd = _ai_site_in_front()
    if site is None:
        if read_only or not m.group("read"):
            return None                               # "send" / "read me the answer" about something else
        recent = _ai_state["site"] and time.time() - _ai_state["at"] < 900
        if not recent:
            return None
        return f"Switch to {_AI_DISPLAY.get(_ai_state['site'], 'the chat')} first, then say 'send it'."
    try:
        import ai_answers
        import winutil
    except Exception:  # noqa: BLE001
        return None
    prompt = ""
    try:
        import dictation
        last = getattr(dictation, "last_typed", {}) or {}
        if time.time() - float(last.get("at", 0)) < 600:
            prompt = str(last.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    if read_only:
        logger.info("Deterministic AI-chat read request: %s", site)
        return ai_answers.read_current(site, hwnd, prompt)
    logger.info("Deterministic AI-chat send request: %s (read: %s)", site, bool(m.group("read")))
    _ai_state.update(site=site, at=time.time())
    return ai_answers.send_and_read(site, hwnd, prompt, lambda: winutil.tap_key(winutil.VK_RETURN),
                                    read=bool(m.group("read")) or getattr(config, "AI_CHAT_READ_ON_SEND", True))


def try_auto_ai_followup(transcript: str):
    """After "Gemini is open. What should I ask it?": the next sentence is the question."""
    if not _ai_state["pending_site"] or time.time() > _ai_state["pending_until"]:
        _ai_state["pending_site"] = ""
        return None
    site = _ai_state["pending_site"]
    t = (transcript or "").strip()
    low = re.sub(r"[^\w\s']", "", t.lower()).strip()
    if low in ("cancel", "never mind", "nevermind", "nothing", "no", "stop", "forget it"):
        _ai_state["pending_site"] = ""
        return "Okay."
    _ai_state["pending_site"] = ""
    question = re.sub(r"^(?:ok(?:ay)?|so|type|ask(?: it)?|ask (?:him|her)|write|search(?: for)?)[\s,:.]+", "", t,
                      flags=re.IGNORECASE).strip() or t
    logger.info("The question for %s: %r", site, question)
    return type_into_ai_site_and_read(site, question)


# --- Deterministic "add X to the queue" handling ------------------------------
# There was no queue tool at all, so the LLM routed "add X to the queue" to
# play_music, which plays it immediately. The regex lives in spotify_queue.py.

def try_auto_queue_music(transcript: str):
    """If transcript is 'add X to the queue' / 'queue X', queues it and returns
    the reply, else returns None."""
    query = spotify_queue.parse_queue_request(transcript)
    if query is None:
        return None
    logger.info("Deterministic queue request matched: query=%r", query)
    return queue_music(query)


# --- Deterministic "message X saying Y" handling (WhatsApp) -------------------
# Live log: "Send a message to Fazal saying hello in whatsapp" took 4-5 s to reach
# the model and it once answered "I don't have a phone number saved" from its own
# earlier reply without calling the tool at all. Same remedy as everywhere else:
# match the sentence in code. This never SENDS anything - it only calls
# send_whatsapp_message(), which reads the message back and waits for a "yes".

_WA_WORD = r"what'?s\s?app"
_WA_PREP = r"(?:in|on|via|using|through|over)"
_WA_SAY = (r"(?:\s+(?:(?:saying|says|say|that says|which says|and says?|to say|"
           r"telling (?:him|her|them))(?:\s+that)?|that|with (?:the )?message)\b|\s*:)")
_WA_MSG_NOUN = r"(?:message|text|msg)"
_WA_ON = rf"(?:\s+{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?)?"
_WA_POLITE_RE = re.compile(
    r"^(?:raziel[,]?\s+|(?:can|could|would|will) you\s+(?:please\s+)?|"
    r"i (?:want|need|would like) (?:you )?to\s+|i'd like you to\s+)", re.IGNORECASE)
_WA_TAIL_RE = re.compile(rf"[\s,]*{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?(?:\s+please)?\s*$", re.IGNORECASE)
_WA_LEAD_RE = re.compile(rf"^{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?[,]?\s+", re.IGNORECASE)

# "send a (whatsapp) message to X (on whatsapp) saying Y"   /   "... to X"  (no text yet)
_WA_TO_RE = re.compile(
    rf"^(?:send|write|drop|shoot)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+|quick\s+|short\s+)?(?:whatsapp\s+)?"
    rf"{_WA_MSG_NOUN}\s+(?:to|for)\s+(?P<name>.+?){_WA_ON}(?:{_WA_SAY}\s*(?P<msg>.+))?$",
    re.IGNORECASE)
# "send X a (whatsapp) message saying Y"
_WA_SEND_RE = re.compile(
    rf"^(?:send|write|drop|shoot)\s+(?!(?:a|an|the|new|quick|short)\s)(?P<name>[^,:]+?)\s+"
    rf"(?:a\s+|an\s+)?(?:new\s+|quick\s+|short\s+)?(?:whatsapp\s+)?{_WA_MSG_NOUN}{_WA_ON}"
    rf"{_WA_SAY}\s*(?P<msg>.+)$", re.IGNORECASE)
# "message X saying Y" / "text X that Y" / "whatsapp X saying Y"
_WA_VERB_RE = re.compile(
    rf"^(?:whatsapp|message|text|msg)\s+(?P<name>[^,:]+?){_WA_ON}{_WA_SAY}\s*(?P<msg>.+)$",
    re.IGNORECASE)


def try_auto_whatsapp(transcript: str):
    """
    If the transcript is a "message X saying Y" request, calls send_whatsapp_message()
    (which asks yes/no before sending) and returns her reply; else None.

    Only intercepts when it is sure it is a WhatsApp message: X is a saved contact,
    or WhatsApp was named out loud. Anything else falls through to the LLM.
    """
    text = _strip_filler_prefix(transcript.strip()).rstrip(" .")
    text = _WA_POLITE_RE.sub("", text, count=1)
    text = _strip_filler_prefix(text)
    named_whatsapp = bool(re.search(_WA_WORD, text, re.IGNORECASE))
    text = _WA_LEAD_RE.sub("", text, count=1)
    text = _WA_TAIL_RE.sub("", text, count=1).strip()

    for pattern in (_WA_TO_RE, _WA_SEND_RE, _WA_VERB_RE):
        m = pattern.match(text)
        if m:
            break
    else:
        return None

    name = re.sub(r"\s+(?:please|now)$", "", m.group("name").strip(" ,"), flags=re.IGNORECASE)
    message = (m.groupdict().get("msg") or "").strip()
    message = message.strip('"“” ').strip()
    if not name or len(name.split()) > 4:
        return None
    if not _resolve_contact(name)[0] and not named_whatsapp:
        return None                       # not a known contact and not WhatsApp: the LLM can decide
    logger.info("Deterministic WhatsApp request matched: to=%r message=%r", name, message)
    return send_whatsapp_message(name, message)


# --- Deterministic "open X" handling (app-launch latency, issue #5) ---------
# Measured from assistant.log: open_app() itself takes 5-100 ms. What made
# "open Spotify" feel slow was the ~4 s (3.8-4.5 s, every single time) between
# the transcript and the tool call - qwen3:8b deciding to call open_app - plus
# 2-3 s of PowerShell on the first launch of a session (now prewarmed). Same
# remedy as every other flaky/slow path in this project (handoff section 5):
# match it in code before the LLM ever sees it.
#
# The matcher only INTERCEPTS when it can positively identify the target -
# a known app/site/folder, or something actually in the Start Menu. Anything
# else ("open the file called report", "open a new tab", "open the door")
# returns None and falls through to the LLM exactly as before.

_OPEN_APP_RE = re.compile(
    r"^(?P<verb>open|launch|start|run|fire up)(?:\s+up)?\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_APP_LEAD_RE = re.compile(r"^(?:the|my|an?)\s+", re.IGNORECASE)
_APP_TAIL_RE = re.compile(r"\s+(?:app|application|program|please|for me|now)$", re.IGNORECASE)
_MULTI_STEP_RE = re.compile(r"\s(?:and|then|to|with|in|on|from|for|about|of)\s", re.IGNORECASE)
# First words that mean "this is a file / web page / window, not an app":
# leave to the LLM's open_file / open_website_search / etc.
_NOT_AN_APP_WORDS = {
    "file", "files", "document", "doc", "pdf", "image", "photo", "picture", "folder",
    "directory", "website", "site", "page", "link", "tab", "window", "email",
    "message", "new", "last", "recent", "screenshot",
}


# How Whisper writes some app names (compared with everything but letters removed): "open what's app"
# never matched anything and went to the model, which answered "Understood.".
_ALWAYS_OPENABLE = {"whatsapp"}      # opened through the app, or WhatsApp Web when it isn't installed


def _known_app_key(key: str):
    """True if key is in one of the hand-maintained app/site tables."""
    return (key in WEBSITE_URLS or key in PROTOCOL_APPS or key in _ALWAYS_OPENABLE
            or key in APP_COMMANDS or key in FULL_PATH_CANDIDATES)


def _resolve_installed_app(key: str, loose: bool) -> bool:
    """
    Is `key` something actually installed? Exact Start Menu name always counts.
    With loose=True, also: every word of key appears in an app name
    ("code" -> "Visual Studio Code"), or a close fuzzy match (>= 0.8).
    """
    names = [n.lower() for n, _id in _get_start_apps()]
    names += list(_get_shortcut_index().keys())
    if key in names:
        return True
    if not loose or not names:
        return False
    words = key.split()
    for name in names:
        name_words = set(re.findall(r"[a-z0-9]+", name))
        if all(w in name_words for w in words):
            return True
    return bool(difflib.get_close_matches(key, names, n=1, cutoff=0.8))


_OPEN_FOLDER_RE = re.compile(
    r"^(?:open|show(?: me)?|go to|launch|take me to)\s+(?:up\s+)?(?:(?:my|the|our)\s+)?(?P<name>.+?)\s+"
    r"(?:folder|directory)(?:\s+(?:in|on|with|using)\s+(?:the\s+)?(?:file (?:manager|explorer)|explorer|"
    r"windows explorer|this pc))?(?:\s+(?:please|for me|now))?$", re.IGNORECASE)


def try_auto_open_folder(transcript: str):
    """ "open my COA folder" / "open the Personal Documents folder in file manager" -> opens it now
    (it went to the model before, which is slower and guessed names)."""
    text = _strip_filler_prefix((transcript or "").strip()).rstrip(".!?").strip()
    m = _OPEN_FOLDER_RE.match(text)
    if not m:
        return None
    name = m.group("name").strip(" ,")
    if not name or len(name.split()) > 5 or re.search(
            r"\b(?:and|then|in|inside|of|from|what|what's|whats|which|where|files?|everything|contents?|"
            r"parent|previous|last|recent|current|this|that)\b", name, re.IGNORECASE):
        return None                                   # "show me the files in my documents folder" -> the model
    if _folder_key(name) in ("", "the", "a", "my", "our", "new", "that", "this", "it", "any", "some", "which", "same",
                             "that same", "new empty", "your", "his", "her", "their"):
        return None                                   # "open a new folder" / "open that folder": not a name
    logger.info("Deterministic open-folder request matched: %r", name)
    return open_folder(name)


def try_auto_open_app(transcript: str):
    """If transcript is 'open/launch X' and X is positively identifiable as an
    app, site or common folder, opens it directly and returns the reply.
    Otherwise returns None (falls through to the LLM)."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!?").strip()
    m = _OPEN_APP_RE.match(text)
    if not m:
        return None

    verb = m.group("verb").lower()
    target = m.group("target").strip()
    whole = _spoken_app_name(re.sub(r"^(?:the|my)\s+", "", target.lower()).strip())
    if whole in ("whatsapp", "chatgpt", "gemini"):
        target = whole                                # "what's app" (its "app" isn't the word app)
    for _ in range(2):
        target = _APP_LEAD_RE.sub("", target, count=1)
    for _ in range(2):
        target = _APP_TAIL_RE.sub("", target, count=1)
    key = re.sub(r"\s+", " ", target).strip().lower()

    if len(key) < 2 or len(key.split()) > 4:
        return None
    key = _spoken_app_name(key)
    if _MULTI_STEP_RE.search(f" {key} "):
        return None                       # "open chrome and search cats" -> LLM
    if re.search(r"\.[a-z0-9]{2,4}$", key):
        return None                       # "open notes.txt" -> open_file via LLM

    # 1. Known app / website / URI-protocol app (includes "file manager").
    if _known_app_key(key):
        logger.info("Deterministic open-app matched (known): %s", key)
        return open_app(key)

    # 2. Common folder ("open downloads", "open my documents folder"). Only for
    # open/launch: "start music" means play music, not open the Music folder.
    folder_key = re.sub(r"\s+(?:folder|directory)$", "", key)
    if verb in ("open", "launch") and folder_key in KNOWN_FOLDERS:
        logger.info("Deterministic open-folder matched: %s", folder_key)
        return open_folder(folder_key)

    if key.split()[0] in _NOT_AN_APP_WORDS:
        return None

    # 3. Installed app. "open"/"launch" allow fuzzy/word matching; the more
    # ambiguous "start"/"run" (start music, run a search...) need an exact name.
    if _resolve_installed_app(key, loose=verb in ("open", "launch")):
        logger.info("Deterministic open-app matched (installed): %s", key)
        return open_app(key)

    return None


# --- Instant answers: date/time, thanks/hello, screenshots, "play X" ----------
# From assistant.log: "what is today's date?" took 17 s (a cold 8B model
# loading, then a tool call) to produce something datetime.now() knows in
# microseconds; a bare "Thank you" cost 4.5 s. The pure matchers live in
# instant_replies.py (strict, whole-utterance, unit-tested); these wrappers do
# the acting. Anything that doesn't match exactly still goes to the LLM.

def try_auto_datetime(transcript: str):
    reply = instant_replies.answer_datetime(transcript)
    if reply:
        logger.info("Instant date/time answer: %s", reply)
    return reply


def try_auto_smalltalk(transcript: str):
    reply = instant_replies.answer_smalltalk(transcript)
    if reply:
        logger.info("Instant small-talk reply: %s", reply)
    return reply


def try_auto_screenshot(transcript: str):
    kind = instant_replies.parse_screenshot_request(transcript)
    if kind == "take":
        logger.info("Deterministic screenshot request")
        return take_screenshot()
    if kind == "show":
        logger.info("Deterministic show-screenshot request")
        return show_last_screenshot()
    return None


def try_auto_play_music(transcript: str):
    """'play X' (Spotify, no other platform named) -> straight to Spotify."""
    name = instant_replies.parse_playlist_request(transcript, known=playlist_names())
    if name:
        logger.info("Deterministic playlist request matched: %r", name)
        return play_playlist(name)
    parsed = instant_replies.parse_play_request(transcript)
    if parsed is None:
        return None
    kind, arg = parsed
    logger.info("Deterministic play request matched: %s %r", kind, arg)
    if kind == "next":
        return next_track()
    if kind == "previous":
        return previous_track()
    if kind == "playlist":
        return play_playlist(arg)
    return play_music(arg)
