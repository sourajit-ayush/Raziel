# Voice Assistant Project

## Vision
A voice-controlled assistant that starts as a system-control tool on Windows
(writing emails, creating websites, managing files, etc.). Smart home control
(Home Assistant) was considered and dropped - the user does not want it.

## Target environment
- **OS:** Windows
- **STT/TTS:** Local (privacy-first, no cloud API cost)
  - STT: `faster-whisper` (local Whisper implementation, runs well on CPU/GPU)
  - TTS: `pyttsx3` (uses Windows SAPI voices, fully offline) — can upgrade to
    a nicer local model (e.g. Piper) later if quality isn't good enough
- **Wake word:** openWakeWord — fully open-source, offline, no account/API
  key needed (switched from Porcupine, which required a company email to
  sign up)
- **LLM brain:** Ollama running locally (`qwen3:8b` by default) — free,
  offline, no API key. Chosen over cloud LLM options to
  keep the whole stack private and cost-free, even though local models are
  less reliable at tool-calling than cloud ones
- **Language:** Python 3.11+

## Phase 1 — Voice pipeline only — ✅ COMPLETE
Wake word (openWakeWord) → record until silence → transcribe (faster-whisper)
→ speak back (pyttsx3). Tested extensively, tuned for the user's actual mic
via `mic_test.py` (found and fixed a Microphone Boost/enhancements issue
that was corrupting the audio signal). Reliable across 20+ test rounds.

## Phase 2 — LLM brain + conversation mode — ✅ COMPLETE (and expanded well beyond original scope)

### What actually got built
1. Local LLM brain via Ollama (`llm_brain.py`), model `qwen3:8b`, with
   `OLLAMA_KEEP_ALIVE`/`OLLAMA_NUM_CTX` tuning for speed
2. Conversation session state machine in `main.py`:
   - Wake word → "awake" mode → loop (wait for real speech → record →
     transcribe → respond), no wake word needed between turns
   - Sleep phrases ("goodbye", "go to sleep") checked locally, no LLM call
   - No auto-sleep timeout (by design, per user preference) - waits
     indefinitely for speech once awake
   - Barge-in: talking over the assistant mid-reply stops it and
     immediately starts listening (`speaker.py`, best with headphones)
   - Quick-command shortcuts (`tools.py: QUICK_COMMANDS`) bypass the LLM
     entirely for common exact phrases (pause/next/previous track) for
     near-instant response
3. Full toolset (`tools.py`), far beyond the original 3:
   - `open_app` — searches Start Menu shortcuts (covers virtually anything
     installed, including Store/UWP apps), plus protocol handlers, known
     install-path fallbacks, and website detection
   - `open_folder` / `open_file` — opens folders or files by name, searching
     common locations
   - `open_website_search` — opens a site with a search already performed
   - `write_file` / `take_screenshot` / `show_last_screenshot`
   - `web_search` — DuckDuckGo via `ddgs`, no key needed
   - `get_current_datetime`
   - `send_whatsapp_message` — via WhatsApp Web + simulated Enter keypress,
     contacts mapped in `config.CONTACTS`
   - `play_music` / `play_playlist` / `pause_music` / `resume_music` /
     `next_track` / `previous_track` — full Spotify Web API integration
     (requires free Spotify Developer app + user's own Premium account)

### Was still open after Phase 2 (both now done in Phase 3, see below)
- ~~No confirmation/safety step for destructive actions~~ -> Phase 3
- ~~No email tool yet~~ -> Phase 3 (opens a pre-filled draft; she never sends)
- No persistent memory across separate wake/sleep sessions -> Phase 4 (core built)

### Success test - passed
Wake word → conversation → plain questions, app/file/folder opening,
website search, WhatsApp messages, full Spotify control (including named
playlists) → "goodbye" → back to sleep. Tested extensively across many
real-world sessions with bugs found and fixed along the way (see chat
history for the full debugging log - mic calibration, pyttsx3 engine
caching, Spotify's null search results, tool-routing prompt tuning, etc.)


## Key interaction requirement (implemented in Phase 2, see above)
The assistant supports a **continuous conversation mode**, not single
wake-word-per-command — this shaped Phase 2's design from the start rather
than being bolted on afterward.

## Also noted: wake word is swappable
Wake word is fully swappable via `WAKE_WORD_MODEL` in `config.py` — either a
built-in openWakeWord name or a path to a custom-trained `.onnx` file (e.g.
a future "Isabelle wake up" model trained via the openWakeWord Colab
notebook). No other code changes needed when swapping.

## Phase 3 — Confirmation step + email tool — ✅ BUILT (2026-09-19/20), first live test done, fixes below

### What got built
1. **Spoken confirmation for risky actions** (`confirmation.py`, new). Two-turn
   exchange: the tool does NOT act, it parks the action and she asks out loud;
   the next transcript is checked by `confirmation.handle_transcript()` (called
   from `main.py` before normal routing).
   - Covers `send_whatsapp_message` (reads the whole message back, then sends
     only on "yes") and `write_file` **when it would replace an existing file with
     different content** (new files are just created; a confirmed overwrite keeps
     the old one as `name.ext.bak`).
   - **Fail-closed:** an action is only "armed" once main.py confirms its question
     was really spoken; only a whole-utterance affirmation ("yes", "yeah go
     ahead", "yes please", plus "send it" for a message / "overwrite it" for a
     file) runs it; "no"/"cancel"/"don't" anywhere in a short answer cancels;
     unclear = asked once more, then cancelled; timeout 60 s; dropped on
     "goodbye" and at the start of a new session; never runs twice.
   - After "yes" she says a short acknowledgement (WhatsApp takes ~12 s) and the
     model's history gets the outcome (`Brain.note_exchange` / `note_assistant`).
   - Switches in `config.py`: `CONFIRM_RISKY_ACTIONS`, `CONFIRM_ACTIONS`,
     `CONFIRM_TIMEOUT_SECONDS`, `CONFIRM_MAX_REPROMPTS`,
     `CONFIRM_KEEP_BACKUP_ON_OVERWRITE`.
2. **Email tool** `compose_email(subject, body, to="")` — opens a pre-filled
   DRAFT (Gmail in the browser by default; `outlook` or `mailto` via
   `EMAIL_DRAFT_MODE`). No password, no SMTP, nothing is sent: pressing Send in
   the draft is the confirmation, so it needs no spoken yes/no. Recipient = a
   name from `EMAIL_CONTACTS`, or an address said aloud ("john at example dot
   com"). Windows can't open links over ~2,000 characters, so a longer email is
   saved in `generated_files`, copied to the clipboard, and the draft opens with
   only recipient + subject.

### Fixes after the first live test (2026-09-20)
The first real-PC test found three problems (all visible in `assistant.log`):
- **Her own voice cut the question off.** On laptop speakers her voice leaks into
  the mic; 15 of 27 replies that day were "barged in" ~0.7 s after she started
  talking. A yes/no question is now protected: `confirmation.expects_answer()`
  marks it, `main.py` speaks it with `speaker.speak()` (or passes
  `protect=` to `speak_stream_interruptible`), and nothing can interrupt it. Normal
  replies are still interruptible. `assistant.log` now shows the mic level on
  every barge-in and the peak level while she spoke (`Mic level while speaking:
  peak N (barge-in threshold 1200)`) - if N is above the threshold, use headphones,
  lower `TTS_VOLUME`, or raise `BARGE_IN_RMS_THRESHOLD` in `config.py`.
- **Contact names.** Whisper said "Fazal Singh" for the contact `"fazal"`. Contacts
  are now matched loosely (`tools._resolve_contact`: "Fazal Singh", "my friend
  Fazal", surname only, close misspellings). She always says the SAVED name in
  the question ("I think you mean Fazal. Send Fazal this WhatsApp message: ..."),
  and asks "which one?" when two contacts fit.
- **The LLM skipped the tool** (and took 4-5 s). "Send a message to Fazal saying
  hello (in WhatsApp)", "message Mom that I'm late", "WhatsApp Fazal saying hi" are
  now matched in code (`tools.try_auto_whatsapp`, in `DETERMINISTIC_MATCHERS`).
  It still never sends by itself: it calls `send_whatsapp_message`, which asks yes/no.
  Only intercepts when the contact is saved or WhatsApp is named; anything else
  goes to the LLM as before.
- **WhatsApp desktop app instead of WhatsApp Web** (2nd live-test finding). She now
  opens `whatsapp://send?phone=<digits>&text=<message>` (Windows hands it to the
  installed WhatsApp app), waits until a WhatsApp window really has the keyboard
  focus, waits `WHATSAPP_APP_DELAY` seconds for the chat to load, then presses Enter.
  If WhatsApp never comes to the front, or focus moves away, she does NOT press
  Enter and says so. `WHATSAPP_MODE` in `config.py`: `"app"` (default, never falls
  back to the browser silently), `"web"`, or `"auto"` (app, Web only if no app).
  The phone number is now sent as digits only (your saved `"+91 72689 26862"`
  had spaces and a `+`, which is not safe inside a link).
Backups of the Phase 3 versions: `_backup_before_livefix\` (before the WhatsApp-app
change: `_backup_before_whatsapp_app\` = config.py, tools.py, PROJECT.md).

### Tests
Stub-based, on Linux: 272 checks for Phase 3 + 113 for the live-test fixes, plus all 7 earlier suites (no
regressions). An independent adversarial review found 2 fail-open paths and
several sharp edges; all were fixed and are covered by tests. NOT yet tried on
the real PC (audio, Windows shell, real WhatsApp/Gmail).

### Known limits
- A lone "Yes." that Whisper invents from noise while a question is open could
  still approve it (the transcript guard's audio gate is the only defence).
- If speaking the question fails silently at the audio-device level, main.py
  cannot tell and will treat it as spoken.
- Only one WhatsApp contact per message; several parked actions are asked one
  after another.
- "Send a message to Fazal" (no text yet) -> she asks what it should say, but the
  reply to that goes through the LLM, which may not call the tool; say the whole
  sentence in one go.
- The model may still say something like "Sure, sending that" BEFORE the
  question (prompt-only mitigation).

## Wake word status (2026-09-20)
`Raziel.onnx` was retrained on the user's own voice (synthetic-only training
never woke him). Threshold `WAKE_WORD_THRESHOLD = 0.5` in `config.py`; do not
raise it above ~0.6.

## v3 - Hindi + Google knowledge + 12 new features - BUILT (2026-09-22), NOT yet tried on the real PC

Asked for by the user after the Phase 5 (smart home) removal: "add all 12 suggested features", "full access to Google so
she can tell me the latest news or anything I want to know, every time I ask", "understand and speak Hindi". Setup steps
(one pip install, optional Gemini key, optional Google sign-in, optional `ollama pull qwen3.5:4b`) are in `SETUP_V3.md`.

### What got built
1. **Hindi, understood and spoken.** Whisper detects English/Hindi per sentence (`transcriber.py`; bigger Hindi model
   `HINDI_WHISPER_MODEL`, loaded in the background); Hindi replies use the Piper voice `hi_IN-priyamvada-medium`
   (auto-downloaded, ~60 MB) with per-script code-switching (English names inside a Hindi sentence use the English
   voice) and a romanised fallback while the voice loads or if it breaks (`speaker.py`). Hindi yes/no answers, Hindi
   goodbye, Hindi contact names ("फ़ज़ल" -> Fazal) and a Hindi command layer (`hindi_commands.py`); `lang.py` holds the shared
   helpers (`lang.set_current()` per turn, `lang.tr(TABLE, key)` for en/hi text). Switch off: `HINDI_ENABLED = False`.
2. **Live Google knowledge** (`webinfo.py`, `netutil.py`). "What's the news" is fetched fresh from Google News every time
   (no key). Weather: Open-Meteo (no key). "Google X", scores, prices, "who is ..." use Gemini with Google Search
   grounding when `GEMINI_API_KEY` is set (free key), else DuckDuckGo + the local model. Finished Gemini answers are spoken
   as they are (`webinfo.Answer.final`).
3. **The 12 features** (all matched in code before the LLM, all with tool schemas for the LLM as a fallback - 34 tools now):
   timers/reminders/alarms that ring even while she sleeps (`reminders.py`, `timeparse.py`, `main.announce_unprompted`);
   volume / mute / brightness / battery (`sysctl.py`, `winutil.py`); calculator + units + currency (`calc.py`); weather;
   clipboard read / summarise / save (`clipboard_tool.py`); notes + to-do/shopping lists (`notes.py`); dictation mode
   (`dictation.py`); PC controls - lock, sleep, close app, switch window, shutdown, restart (`sysctl.py`; shutdown, restart,
   close-app and "clear notes/list" ask a spoken yes/no first); morning briefing on the first wake of the day
   (`briefing.py`); "what's on my screen?" with a local vision model (`vision.py`); Google Calendar view + add (add asks
   yes/no) and read-only Gmail (`google_api.py`, `google_setup.py`; hand-rolled OAuth, no Google libraries).
4. **Routing** (`main.DETERMINISTIC_MATCHERS`, lazy, in priority order): reminders, favorite-fact, open-site-action,
   queue-music, whatsapp, calendar-mail, briefing, hindi-commands, system-control, dictation, quick-command, date-time,
   screenshot, screen-vision, small-talk, calculator, weather, news, notes, clipboard, play-music, open-app, web-search,
   live-question. A matcher that crashes 3 times in a row is switched off for the run (logged), never the assistant.
   A matcher may return `routing.AskLLM(prompt)` (e.g. "summarise my clipboard"): the model then writes the answer from
   text the code fetched - and such a turn is **untrusted**: no tool can run during it (prompt-injection guard).
5. **Confirmation step extended** (`confirmation.py`): Hindi/Hinglish answers, answers in the language the question was
   asked in, kinds `shutdown_pc`, `restart_pc`, `close_app`, `clear_notes`, `clear_list`, `add_calendar_event` added to
   `CONFIRM_ACTIONS`. A timer/reminder never speaks while a yes/no question is open (it could receive the "yes").

### Config / context budget
`config.py` got one new block "Raziel v3 features" (every setting commented; delete it to get defaults).
`OLLAMA_NUM_CTX` is now 10240: system prompt + 34 tool schemas are ~5,400 tokens before any conversation.
Do not shrink it, or Ollama truncates the prompt on every request.

### Tests
24 stub-based suites (Linux), ~7,350 checks, all passing, plus an independent adversarial review whose findings
(timer answering a shutdown question, prompt injection through the clipboard, Hindi "कैंसल" not cancelling a
shutdown, clipboard write owner window, beep not muted, briefing timeout, ...) were fixed and are covered by tests.
Backups of every replaced file: `_backup_before_phase5features\`.

### Known limits (real-PC behaviour not yet observed)
- All Windows-specific code (volume via pycaw, brightness, SendInput typing, clipboard, window listing/closing,
  `shutdown`, LockWorkStation) was written from the Microsoft docs and never run on Windows.
- `shutdown /t 30` force-closes open apps when the timer ends; sleep may hibernate on Modern Standby PCs.
- Hindi quality depends on Whisper (`medium`) and the Piper voice; the first start downloads ~1.5 GB + ~60 MB.
- Free-tier Gemini questions may be used by Google to improve its products (mentioned in `SETUP_V3.md` and `config.py`).
- Not done: Hindi keywords in `emotion.py`, Hindi lines in `initiative.py`, undo for "delete my last note".

## Roadmap (for context only — do not build ahead of current phase)
- **Phase 2:** ✅ Complete — see above (LLM brain, conversation mode, full
  toolset including Spotify, WhatsApp, file/app/web access)
- **Phase 3 (current):** ✅ Confirmation step for risky actions (overwrite/send)
  and email tool (draft) built — see above; live PC test + "anything else the
  user wants next" still open
- **Phase 4:** ✅ Core memory built - `memory.py` (SQLite), `remember`/
  `recall`/`forget` tools, facts persist across sessions and restarts,
  full session transcripts searchable via `recall`
- **v3:** Hindi + live Google news/knowledge + 12 features (timers, volume/brightness, calculator, weather,
  clipboard, notes/lists, dictation, PC controls, briefing, screen description, Calendar, Gmail) built - see above;
  real-PC test still open
- **Phase 5:** Polish — error handling, logs dashboard, voice-profile
  security, PIN confirmation for sensitive actions
- **Phase 6 (current):** ✅ Phone bridge built (2026-09-23) - talk to Raziel from your phone from
  anywhere (Tailscale + Tasker: `POST /command`, `POST /notification`, `GET /file`, all
  token-authenticated) and have her trigger actions on your phone (Join: ring it, send an SMS -
  asks yes/no first, same as WhatsApp - open an app/link, set its clipboard). New modules
  `phone_bridge.py` (the HTTP server) and `phone_control.py` (the Join client); `main.py` gained
  a shared `_turn_lock` so a phone command and a spoken one can never run at once; `tools.py`
  gained `find_file()` (shared by `open_file` and the phone bridge's file endpoint) and the
  `control_phone` tool. New config: `PHONE_BRIDGE_ENABLED/PORT/TOKEN`, `JOIN_API_KEY/DEVICE_ID`.
  See `PHONE_BRIDGE_SETUP.md` for the phone-side setup (Tailscale + Tasker + Join) - the
  phone-side steps are still pending on the real device.

## Working agreement for AI coding sessions
- At the start of every session, read this file and the existing code before
  making changes.
- Only build what's listed under "Current phase" — do not jump ahead.
- After each working milestone, note it here and suggest a git commit.
- New tools/features should follow the same code pattern as existing ones.
- Any action that deletes, sends, or modifies something outside the project
  folder must have a confirmation step before executing.
