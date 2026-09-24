"""
The assistant's "brain" - sends transcripts to a local Ollama model,
handles tool calls, and keeps track of conversation history for the
duration of an awake session.

Requires Ollama installed and running locally (https://ollama.com/download),
with the model in config.OLLAMA_MODEL already pulled (e.g. `ollama pull llama3.2`).
"""

import logging
import re
import threading
import time

import ollama

import config
import confirmation
import lang
import memory
from routing import AskLLM
from tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

logger = logging.getLogger("voice_assistant")

# Safety net: some Ollama chat templates put reasoning directly in the
# visible content as <think>...</think> instead of the separate 'thinking'
# field. Strip it so it's never read aloud.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _localize(text: str) -> str:
    """English sentences returned by the older tools ("Opened notepad.") -> Hindi on a Hindi turn."""
    if not lang.is_hindi():
        return text
    try:
        import hindi_commands
        return hindi_commands.localize(text)
    except Exception:
        return text


_UNTRUSTED_REFUSAL = {"msg": {
    "en": "I won't run commands that come from copied text.",
    "hi": "मैं कॉपी किए गए टेक्स्ट में लिखे आदेश नहीं चलाऊँगी।"}}


def _strip_thinking(text: str) -> str:
    return _THINK_TAG_RE.sub("", text).strip()


BASE_SYSTEM_PROMPT = (
    "You are Raziel, a voice assistant running on the user's computer. "
    "Your responses will be read aloud, so write for speech, not text: no "
    "markdown, no lists, no formatting. "
    "\n\n"
    "PERSONA - follow this strictly in every reply: "
    "Speak with total certainty - never hedge. Don't say 'I think', "
    "'maybe', 'probably', or 'it seems like' unless a piece of information "
    "is genuinely uncertain; state things as fact. "
    "Never use filler words or phrases - no 'well', 'um', 'let's see', "
    "'so', or 'actually'. Begin directly with the substance of the answer. "
    "Prefer precise, analytical phrasing over casual phrasing - say "
    "'Probability of precipitation is high' rather than 'I think it might "
    "rain'; say 'Battery level is at twelve percent' rather than 'looks "
    "like your battery's getting low'. "
    "When acknowledging a command, use short, cool acknowledgments - "
    "'Understood.', 'Acknowledged.', 'Processing request.' - never "
    "enthusiastic phrasing like 'Okay, I'll do that right now!'. "
    "Be concise. Do not add pleasantries, small talk, or offers of further "
    "help unless directly relevant. Answer in one or two short sentences "
    "unless the user explicitly asks for detail, a list, or a story. "
    "\n\n"
    "IMPORTANT: Whenever the user asks you to open, launch, start, run, or "
    "go to any application, program, game, or website by name - such as "
    "Notepad, Steam, Spotify, YouTube, Chrome, or anything similar - you "
    "MUST call the open_app tool with that name. Never respond with manual "
    "instructions like 'you can open it by searching in the Start menu' - "
    "always attempt the tool call yourself first. "
    "\n\n"
    "If the user instead asks to open a specific FOLDER (like Downloads, "
    "Documents, Desktop, Pictures, or a named folder/path) rather than an "
    "application, call open_folder instead of open_app. "
    "\n\n"
    "If the user asks to open a specific FILE, document, PDF, or image by "
    "name, call open_file. "
    "\n\n"
    "If the user asks to send a WhatsApp message to someone, call "
    "send_whatsapp_message directly - never call open_app for WhatsApp, "
    "since send_whatsapp_message already opens WhatsApp itself as part "
    "of sending. If the user didn't say what message to send, ask them "
    "what they'd like to say before calling the tool - don't guess or "
    "invent message content. If it says no phone number is saved, tell "
    "the user they need to add that contact to CONTACTS in config.py first. "
    "Sending and overwriting a file are confirmed out loud first: when a "
    "tool result is a question ending 'Say yes...', it has NOT been done "
    "yet - just say it, and never claim something was sent or saved until "
    "a result says so. "
    "\n\n"
    "If the user asks you to write, draft, or send an email, call "
    "compose_email with a subject and the full body written in the user's "
    "voice (and the recipient if they named one). It only opens a draft "
    "for them to review and send - never say the email was sent. If they "
    "didn't say what the email should say, ask first. "
    "\n\n"
    "If the user asks to open a website AND search or look something up on "
    "it (e.g. 'open Google and search X', 'open YouTube and search for Y'), "
    "call open_website_search with the site and query - do not call "
    "open_app, since that only opens the homepage without searching. Set "
    "play=true only when they said 'play X' (they want it playing right "
    "away, not just a list of results) - leave it false/omitted for "
    "'search for X' or 'look up X'. "
    "\n\n"
    "If the user asks to play a song, artist, or 'some music' on Spotify - "
    "or just says 'play X' with no platform mentioned - call play_music "
    "with the song/artist as the query (or an empty query for generic "
    "music). Don't call open_app for this - play_music opens Spotify "
    "itself as part of playing. For example, 'open Spotify and play "
    "Billie Jean' means: call play_music with query 'Billie Jean'. It does "
    "NOT mean call open_app for Spotify. "
    "\n\n"
    "IMPORTANT: play_music and play_playlist are ONLY for Spotify. If the "
    "user explicitly mentions YouTube (e.g. 'open YouTube and play X', "
    "'open YouTube and search for Y', 'play X on YouTube'), you MUST call "
    "open_website_search with site 'youtube' and the song/query instead - "
    "never call play_music for a YouTube request, and never call open_app "
    "for YouTube either. Set play=true for 'play X on YouTube' so it "
    "actually starts playing instead of just showing search results. "
    "\n\n"
    "If the user names one of their own Spotify playlists specifically "
    "(e.g. 'play my Midnight Melodies playlist'), call play_playlist with "
    "that playlist name instead of play_music. "
    "\n\n"
    "For controlling music already playing on Spotify - pause, resume/"
    "continue, skip/next song, or go back/previous song - call pause_music, "
    "resume_music, next_track, or previous_track respectively. "
    "\n\n"
    "Whenever the user asks what the date, day, or time is, call the "
    "get_current_datetime tool rather than guessing or saying you don't "
    "have access to it. "
    "\n\n"
    "Whenever the user asks you to take, capture, or save a screenshot, "
    "call the take_screenshot tool. If they then ask to see, view, open, or "
    "show that screenshot, call show_last_screenshot. "
    "\n\n"
    "If the user tells you a fact, preference, or plan about themselves - "
    "even without saying the word 'remember' (e.g. 'my favorite musician "
    "is X', 'I have a meeting at 5', 'I don't like spicy food') - call the "
    "remember tool to save it. Do this for ANY personal fact they share, "
    "not just when they explicitly say 'remember that'. "
    "\n\n"
    "CRITICAL: Never say or imply that you've noted, saved, or remembered "
    "something unless you actually called the remember tool in this same "
    "turn. Do not just acknowledge a fact conversationally - always call "
    "the tool. "
    "\n\n"
    "Before answering ANY question about the user's preferences, facts, or "
    "things they've told you, first check the KNOWN FACTS section in this "
    "system prompt (if present) - if the answer is there, use it directly "
    "without needing a tool. Only call recall or say you don't know if "
    "it's genuinely not listed there. If the user asks what you remember, "
    "or references a past conversation ('what did we talk about', 'do you "
    "remember when...'), and KNOWN FACTS doesn't answer it, call recall "
    "with a search query. If they ask you to forget something, call "
    "forget with a keyword to match. "
    "\n\n"
    "LANGUAGE: the user speaks English or Hindi. Always answer in the language of "
    "their latest message. For Hindi, write natural spoken Hindi in Devanagari "
    "script (app names, contacts and English loanwords may stay in English "
    "letters). Give tool arguments for apps, contacts, songs and places in "
    "English letters; notes, messages and reminder text stay in the language the "
    "user said them in. "
    "\n\n"
    "MORE TOOLS - always call the matching tool, never guess: set_reminder "
    "(timers, alarms, reminders) and manage_reminders (list, cancel, snooze); "
    "system_control (volume, mute, brightness, battery, lock, sleep, shutdown, "
    "close an app, switch window); calculate (any arithmetic, unit or currency "
    "conversion - never do maths yourself); get_weather; get_news (for ANY news "
    "request, every time, never answer news from memory); manage_notes (notes, "
    "to-do and shopping lists); clipboard_action; calendar_action and "
    "email_action (Google Calendar; Gmail, read only); describe_screen (what is "
    "on the screen); daily_briefing; dictation_control (typing what the user "
    "says); control_phone (ring the user's phone to help find it, send an SMS "
    "from it, open an app or link on it, or set its clipboard - only for that "
    "fixed list of actions, and only when the user is talking about their "
    "PHONE specifically, e.g. 'ring my phone', 'text Mom from my phone I'm "
    "running late', 'open Spotify on my phone'; never use it for anything on "
    "this PC, and never invent an action it doesn't support). "
    "\n\n"
    "For anything current or that may have changed - sports scores, prices, who "
    "holds an office, recent events, or anything you are not sure about - call "
    "web_search (it searches Google). For everything else, just answer directly "
    "and briefly."
)


# --- streaming helpers ---------------------------------------------------------

def _g(obj, key, default=None):
    """Reads a field from an Ollama response, whether it is a dict or a model object."""
    if obj is None:
        return default
    try:
        value = obj.get(key, default)
    except AttributeError:
        value = getattr(obj, key, default)
    except Exception:
        value = default
    return default if value is None else value


_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc", "ltd",
    "e.g", "i.e", "a.m", "p.m", "no", "approx", "fig",
}
_SPEECH_JUNK_RE = re.compile(r"[*`#]+")


def _speakable(text: str) -> str:
    """Drops markdown characters a TTS engine would read out loud."""
    return _SPEECH_JUNK_RE.sub("", text).strip()


class _ThinkFilter:
    """
    Removes <think>...</think> from a token stream, even when a tag is split
    across chunks. With think=False Ollama normally never emits one; this is the
    same safety net as _strip_thinking(), for the streaming path.
    """

    _OPEN, _CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = []
        while True:
            lowered = self.buf.lower()
            tag = self._CLOSE if self.inside else self._OPEN
            idx = lowered.find(tag)
            if idx >= 0:
                if not self.inside:
                    out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # No complete tag: keep back any tail that could be the start of one.
            keep = 0
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(lowered[-n:]):
                    keep = n
                    break
            emit_upto = len(self.buf) - keep
            if not self.inside:
                out.append(self.buf[:emit_upto])
            self.buf = self.buf[emit_upto:]
            return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class _SentenceSplitter:
    """
    Turns a stream of text fragments into speakable sentences as soon as each is
    complete, so speech can begin after the FIRST sentence instead of after the
    whole reply. Conservative: no split inside "3.28", "p.m.", "Dr.", "J. Smith".
    """

    def __init__(self, min_len: int = 8, max_len: int = 200):
        self.buf = ""
        self.min_len = min_len
        self.max_len = max_len

    def _boundary(self):
        text = self.buf
        n = len(text)
        for i, ch in enumerate(text):
            if ch == "\n" and text[:i].strip():
                return i + 1
            if ch not in ".!?।":
                continue
            j = i + 1
            while j < n and text[j] in "\"')]":
                j += 1
            if j >= n or not text[j].isspace():
                continue                        # end of buffer or "3.28": not confirmed
            head = text[:i].rstrip()
            if len(head) < self.min_len - 1:
                continue
            if ch == ".":
                last = re.split(r"\s+", head)[-1].lower().strip("\"'([")
                if last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
                    continue
            return j
        if n >= self.max_len:                   # run-on with no punctuation
            for sep in (", ", "; ", ": ", " - ", " "):
                k = text.rfind(sep, self.min_len, self.max_len)
                if k > 0:
                    return k + len(sep)
        return None

    def feed(self, piece: str):
        self.buf += piece
        out = []
        while True:
            cut = self._boundary()
            if cut is None:
                break
            sentence, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
            sentence = _speakable(sentence)
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        rest, self.buf = _speakable(self.buf), ""
        return rest


class Brain:
    def __init__(self):
        self.client = ollama.Client(host=config.OLLAMA_HOST)
        memory.init_db()
        self.session_id = None
        self.reset()

    def reset(self):
        """
        Starts a fresh conversation: clears in-session history and opens a
        new session row in the memory database. Remembered facts (from
        past sessions, even past restarts) are injected into the system
        prompt here so the assistant carries that knowledge forward.
        """
        facts = memory.get_recent_facts(limit=config.MAX_REMEMBERED_FACTS_IN_PROMPT)
        prompt = BASE_SYSTEM_PROMPT
        if facts:
            facts_block = "\n".join(f"- {f}" for f in facts)
            prompt += (
                "\n\n=== KNOWN FACTS ABOUT THE USER (already confirmed true - "
                "use these directly to answer questions, don't say you don't "
                "know something that's listed here) ===\n"
                f"{facts_block}\n"
                "=== END KNOWN FACTS ==="
            )
        self.messages = [{"role": "system", "content": prompt}]
        self.session_id = memory.start_session()

    def end_session(self):
        """Call when the user says goodbye/go to sleep, to close out this session's record."""
        memory.end_session(self.session_id)

    def _finish_turn(self, reply: str) -> str:
        """Logs the assistant's reply to the persistent session transcript, then returns it."""
        memory.append_to_session(self.session_id, "assistant", reply)
        return reply

    def note_exchange(self, user_text: str, reply: str):
        """
        Records a turn that was handled WITHOUT the model - a spoken yes/no to a
        confirmation (confirmation.py) - in the conversation history and the
        saved transcript. Otherwise the model would see her question but never
        the answer or the outcome ("did that go through?").
        """
        self.messages.append({"role": "user", "content": user_text})
        self.messages.append({"role": "assistant", "content": reply})
        memory.append_to_session(self.session_id, "user", user_text)
        memory.append_to_session(self.session_id, "assistant", reply)
        self._trim_history()

    def note_assistant(self, text: str):
        """Adds a note in her own voice to the history (not spoken), e.g. that a
        pending confirmation was cancelled, so the model doesn't think it is still open."""
        if not text:
            return
        self.messages.append({"role": "assistant", "content": text})
        memory.append_to_session(self.session_id, "assistant", text)

    def _trim_history(self):
        """
        Keeps only the system prompt plus the most recent N messages, so
        the model isn't reprocessing an ever-growing conversation on every
        turn of a long session (which gets noticeably slower over time).
        """
        if len(self.messages) > config.MAX_HISTORY_MESSAGES + 1:
            system = self.messages[0]
            recent = self.messages[-config.MAX_HISTORY_MESSAGES:]
            self.messages = [system] + recent

    # ------------------------------------------------------------------ plumbing

    def _chat_options(self) -> dict:
        # Identical in the warm-up and in every real request: a different
        # num_ctx makes Ollama unload and reload the model.
        return {
            "num_predict": config.OLLAMA_MAX_TOKENS,
            "num_ctx": config.OLLAMA_NUM_CTX,
        }

    def _log_timing(self, response, label: str, started: float = None):
        """
        Logs Ollama's own timing for a call, so "why was that slow" has an
        answer in assistant.log: model load vs reading the prompt vs writing.
        Also warns when the prompt is close to filling the context window,
        which makes Ollama silently truncate it.
        """
        try:
            ns = 1e9
            load = _g(response, "load_duration", 0) / ns
            p_tok = int(_g(response, "prompt_eval_count", 0))
            p_dur = _g(response, "prompt_eval_duration", 0) / ns
            e_tok = int(_g(response, "eval_count", 0))
            e_dur = _g(response, "eval_duration", 0) / ns
            total = _g(response, "total_duration", 0) / ns
            if not total and started is not None:
                total = time.time() - started
            rate = f"{e_tok / e_dur:.0f} tok/s" if e_dur > 0 else "n/a"
            logger.info(
                "LLM timing (%s): total %.1fs | model load %.1fs | read %d tok in %.1fs | "
                "wrote %d tok in %.1fs (%s)",
                label, total, load, p_tok, p_dur, e_tok, e_dur, rate,
            )
            if load > 2.0:
                logger.info("LLM: the model had to be loaded into memory for that request "
                            "(%.1fs). LLM_WARMUP does this at start-up instead.", load)
            if p_tok + e_tok >= 0.9 * config.OLLAMA_NUM_CTX:
                logger.warning(
                    "LLM prompt nearly fills the context window (%d of %d tokens) - Ollama "
                    "will truncate it. Raise OLLAMA_NUM_CTX in config.py.",
                    p_tok + e_tok, config.OLLAMA_NUM_CTX,
                )
        except Exception as e:                      # logging must never break a turn
            logger.debug("couldn't log LLM timing: %s", e)

    # ---------------------------------------------------------------- warm-up

    def warm_up(self):
        """
        Loads the model into memory (and lets Ollama cache the system prompt +
        tool schemas) with a 1-token request, so the user's first real question
        doesn't pay for it. Measured cold cost: 12-17 s on the first question
        after every start-up. Safe to call from a background thread; changes
        no conversation state.
        """
        started = time.time()
        logger.info("Warming up the language model (loading %s into memory)...",
                    config.OLLAMA_MODEL)
        try:
            options = self._chat_options()
            options["num_predict"] = 1
            response = self.client.chat(
                model=config.OLLAMA_MODEL,
                messages=list(self.messages) + [{"role": "user", "content": "hi"}],
                tools=TOOL_SCHEMAS,
                think=config.OLLAMA_ENABLE_THINKING,
                options=options,
                keep_alive=config.OLLAMA_KEEP_ALIVE,
            )
            self._log_timing(response, "warm-up", started)
            logger.info("Language model ready (%.1fs).", time.time() - started)
        except Exception as e:
            logger.warning("LLM warm-up failed (%s) - the first question will be slower. "
                           "Is Ollama running?", e)

    def warm_up_async(self):
        thread = threading.Thread(target=self.warm_up, name="llm-warmup", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------ tool running

    def _run_tool_calls(self, tool_calls):
        """Executes the model's tool calls. Returns (results, called_tool_names)."""
        results = []
        called_tool_names = []
        self._final_flags = []
        self._asked_llm = False
        for call in tool_calls:
            func_name = call["function"]["name"]
            func_args = call["function"].get("arguments", {})
            logger.info("Tool call: %s(%s)", func_name, func_args)
            called_tool_names.append(func_name)

            func = TOOL_FUNCTIONS.get(func_name)
            if getattr(self, "_turn_untrusted", False):
                # The model is reading text nobody vouched for (the clipboard, ...). Whatever that text says,
                # it cannot make her do anything: no tool runs during such a turn.
                logger.warning("Tool call %s refused: this turn works on untrusted text", func_name)
                result = lang.tr(_UNTRUSTED_REFUSAL, "msg")
            elif func is None:
                result = f"Unknown tool: {func_name}"
            else:
                try:
                    result = func(**func_args)
                except Exception as e:
                    logger.error("Tool '%s' raised an error: %s", func_name, e)
                    result = f"That tool failed: {e}"

            final = bool(getattr(result, "final", False))
            if isinstance(result, AskLLM):
                # e.g. "summarize what I copied": the code fetched the data, the model writes the answer
                self._asked_llm = True
                result = result.prompt
            self._final_flags.append(final)
            logger.info("Tool result: %s", result)
            results.append(str(result))
            self.messages.append(
                {"role": "tool", "content": str(result), "tool_name": func_name}
            )
        return results, called_tool_names

    def _needs_synthesis(self, called_tool_names) -> bool:
        # Info-retrieval tools return raw data meant for the LLM to read and
        # summarize - not meant to be spoken verbatim (recall in particular
        # can return chunks of old conversation transcripts, which sounds
        # bizarre read aloud word-for-word). These always get the natural
        # language follow-up regardless of FAST_TOOL_REPLIES.
        # ...unless the tool returned a FINISHED answer (webinfo.Answer.final: Gemini's Google-grounded
        # answer) - that is spoken verbatim - or a tool handed text back for the model to work on (AskLLM).
        finals = getattr(self, "_final_flags", [])
        if getattr(self, "_asked_llm", False):
            return True
        for i, name in enumerate(called_tool_names):
            if name in config.SLOW_PATH_TOOLS and not (i < len(finals) and finals[i]):
                return True
        return False

    # ---------------------------------------------------------------- one turn

    def _begin_turn(self, user_text: str, model_text: str = None, allow_tools: bool = True,
                    untrusted: bool = False):
        """`user_text` is what the user said (saved in the transcript); `model_text`, when given, is what
        the model reads instead (e.g. "summarize this text: ..." built by a matcher from the clipboard)."""
        self._turn_tools = bool(allow_tools)
        self._turn_untrusted = bool(untrusted)     # tools stay in the request (Ollama's cache) but never run
        content = model_text if model_text else user_text
        if lang.is_hindi():
            # The system prompt (and so Ollama's prompt cache) must never change between turns, so the
            # language hint travels with the user's message instead; it is not saved in the transcript.
            content = f"{content}\n[Reply in Hindi, in Devanagari script.]"
        self.messages.append({"role": "user", "content": content})
        memory.append_to_session(self.session_id, "user", user_text)
        self._trim_history()

    def process_turn(self, user_text: str, model_text: str = None, allow_tools: bool = True,
                     untrusted: bool = False) -> str:
        """
        Sends the user's message to the model, executes any tool calls it
        requests, and returns the final natural-language reply. (Blocking:
        nothing is available until the whole reply is done. stream_turn() is
        the version that lets speech start early.)
        """
        self._begin_turn(user_text, model_text, allow_tools, untrusted)
        return self._respond_blocking()

    def _respond_blocking(self) -> str:
        started = time.time()
        try:
            response = self.client.chat(
                model=config.OLLAMA_MODEL,
                messages=self.messages,
                **({"tools": TOOL_SCHEMAS} if getattr(self, "_turn_tools", True) else {}),
                think=config.OLLAMA_ENABLE_THINKING,
                options=self._chat_options(),
                keep_alive=config.OLLAMA_KEEP_ALIVE,
            )
        except Exception as e:
            logger.error("Ollama request failed: %s", e)
            return self._finish_turn(
                "I'm having trouble reaching my local brain right now. "
                "Make sure Ollama is running."
            )
        self._log_timing(response, "chat", started)

        message = response["message"]
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            reply = _strip_thinking(message.get("content", ""))
            self.messages.append({"role": "assistant", "content": reply})
            return self._finish_turn(reply or "Acknowledged.")

        # Model wants to use one or more tools - execute them.
        self.messages.append(message)
        results, called_tool_names = self._run_tool_calls(tool_calls)

        if confirmation.has_pending() or (
                config.FAST_TOOL_REPLIES and not self._needs_synthesis(called_tool_names)):
            # (A pending confirmation is always spoken word for word: the
            # question must reach the user exactly as written.)
            # Skip the second LLM call entirely - just speak the tool's own
            # result directly. Cuts a full model inference off every tool
            # action's response time, at the cost of a slightly less
            # conversational confirmation message.
            reply = _localize(" ".join(results))
            self.messages.append({"role": "assistant", "content": reply})
            return self._finish_turn(reply or "Task complete.")

        # Ask the model to turn the tool result(s) into a natural reply.
        started = time.time()
        try:
            followup = self.client.chat(
                model=config.OLLAMA_MODEL,
                messages=self.messages,
                think=config.OLLAMA_ENABLE_THINKING,
                options=self._chat_options(),
                keep_alive=config.OLLAMA_KEEP_ALIVE,
            )
        except Exception as e:
            logger.error("Ollama follow-up request failed: %s", e)
            return self._finish_turn("I did that, but had trouble summarizing the result.")
        self._log_timing(followup, "follow-up", started)

        reply = _strip_thinking(followup["message"].get("content", ""))
        self.messages.append({"role": "assistant", "content": reply})
        return self._finish_turn(reply or "Task complete.")

    # --------------------------------------------------------------- streaming

    def _stream_once(self, use_tools: bool, spoken: list):
        """
        One streaming chat call. A generator: yields each finished sentence as
        soon as it exists (also recording it in `spoken`), and RETURNS
        (full_text, tool_calls) - use it with `yield from`. Nothing is added to
        self.messages here.
        """
        started = time.time()
        kwargs = dict(
            model=config.OLLAMA_MODEL,
            messages=self.messages,
            think=config.OLLAMA_ENABLE_THINKING,
            options=self._chat_options(),
            keep_alive=config.OLLAMA_KEEP_ALIVE,
            stream=True,
        )
        if use_tools and getattr(self, "_turn_tools", True):
            kwargs["tools"] = TOOL_SCHEMAS

        stream = self.client.chat(**kwargs)
        splitter = _SentenceSplitter()
        think_filter = _ThinkFilter()
        full, tool_calls, last = [], [], None
        first_out = False

        def emit(sentence):
            nonlocal first_out
            if not first_out:
                first_out = True
                logger.info("LLM first sentence after %.1fs", time.time() - started)
            spoken.append(sentence)
            return sentence

        try:
            for chunk in stream:
                last = chunk
                message = _g(chunk, "message")
                calls = _g(message, "tool_calls")
                if calls:
                    tool_calls.extend(calls)
                piece = think_filter.feed(_g(message, "content", "") or "")
                if not piece:
                    continue
                full.append(piece)
                for sentence in splitter.feed(piece):
                    yield emit(sentence)

            # End of stream: whatever the think-filter was holding back goes
            # through the splitter, then the splitter's own remainder.
            leftover = think_filter.flush()
            if leftover:
                full.append(leftover)
            sentences = splitter.feed(leftover) if leftover else []
            rest = splitter.flush()
            if rest:
                sentences.append(rest)
            for sentence in sentences:
                yield emit(sentence)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._log_timing(last, "stream", started)
        return "".join(full).strip(), tool_calls

    def stream_turn(self, user_text: str, model_text: str = None, allow_tools: bool = True,
                    untrusted: bool = False):
        """
        Like process_turn(), but a generator that yields the reply sentence by
        sentence AS THE MODEL WRITES IT, so speech starts after the first
        sentence (~2-3 s) instead of after the whole reply (up to 17 s in the
        log). Tool calls run exactly as in process_turn(). If the consumer
        stops early (the user barged in), whatever was already produced is kept
        in the conversation history.
        """
        self._begin_turn(user_text, model_text, allow_tools, untrusted)
        spoken = []
        completed = False
        try:
            try:
                content, tool_calls = yield from self._stream_once(True, spoken)
            except Exception as e:
                if spoken:
                    raise
                # Streaming itself failed before any output: fall back to the
                # blocking path once, so a streaming quirk never loses a reply.
                logger.warning("Streaming failed (%s) - retrying without streaming.", e)
                reply = self._respond_blocking()
                spoken.append(reply)
                completed = True
                yield reply
                return

            if not tool_calls:
                reply = " ".join(spoken).strip() or content
                if not reply:
                    reply = "Acknowledged."
                    spoken.append(reply)
                    yield reply
                self.messages.append({"role": "assistant", "content": reply})
                completed = True
                self._finish_turn(reply)
                return

            self.messages.append(
                {"role": "assistant", "content": content, "tool_calls": tool_calls}
            )
            results, called_tool_names = self._run_tool_calls(tool_calls)

            if confirmation.has_pending() or (
                    config.FAST_TOOL_REPLIES and not self._needs_synthesis(called_tool_names)):
                reply = _localize(" ".join(results)) or "Task complete."
                self.messages.append({"role": "assistant", "content": reply})
                spoken.append(reply)
                completed = True
                self._finish_turn(reply)
                yield reply
                return

            follow = []
            try:
                content2, _ = yield from self._stream_once(False, follow)
            except Exception as e:
                logger.error("Ollama follow-up request failed: %s", e)
                if follow:
                    raise
                reply = "I did that, but had trouble summarizing the result."
                spoken.append(reply)
                completed = True
                self._finish_turn(reply)
                yield reply
                return
            reply = " ".join(follow).strip() or content2
            if not reply:
                reply = "Task complete."
                yield reply
            spoken.append(reply)
            self.messages.append({"role": "assistant", "content": reply})
            completed = True
            self._finish_turn(reply)
        finally:
            if not completed:
                # Consumer stopped early (barge-in) or something broke: keep what
                # was actually produced so the next turn's context is coherent.
                partial = " ".join(spoken).strip()
                if partial:
                    self.messages.append({"role": "assistant", "content": partial})
                    try:
                        memory.append_to_session(self.session_id, "assistant", partial)
                    except Exception:
                        pass
