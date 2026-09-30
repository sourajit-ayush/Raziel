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
- **Phase 5:** Polish — error handling, logs dashboard, ✅ voice-profile
  security (Voice ID, 2026-09-29), PIN confirmation for sensitive actions
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
- **Bugfix (2026-09-27):** "Open chatgpt/gemini/claude/... and type/ask X" now actually types X
  into the site instead of just opening it and stopping. New `tools.type_into_ai_site()` (opens
  the site or its installed app via `open_app()`, waits for it to really get keyboard focus, then
  types with `winutil.type_text()` and presses Enter) plus a deterministic `"open-ai-chat"` phrase
  matcher, right after `"open-site-action"` in `main.DETERMINISTIC_MATCHERS`. Gemini, Claude,
  Perplexity and Copilot were also added to `WEBSITE_URLS`/`APP_PREFERRED_WEBSITES` (ChatGPT
  already had website support; now all five try their installed app first, same as YouTube/
  Netflix). New config: `AI_CHAT_FOCUS_TIMEOUT`/`AI_CHAT_TYPE_DELAY`. Delivered and verified on
  the device; full test suite plus a new dedicated `test_ai_chat_site.py` all pass with 0 failures.
- **Auto-start (2026-09-28):** Raziel no longer needs a command prompt. `install_autostart.bat`
  (run once) puts a shortcut in the Windows Startup folder, so she starts by herself ~15 s after
  login, in the background with no console window, and adds "Start Raziel" / "Stop Raziel"
  shortcuts to the Desktop (`uninstall_autostart.bat` removes all three). New `launcher.py`
  (start/stop/install/uninstall/status; runs `venv\Scripts\python.exe main.py` with
  CREATE_NO_WINDOW - deliberately not pythonw, so programs she starts don't flash windows - and
  sends anything printed outside assistant.log to `raziel_console.log`), thin wrappers
  `start_raziel.pyw` / `stop_raziel.pyw`, and `instance_guard.py`: a named mutex so only one
  Raziel runs at a time (a second start exits with code 3 and says she's already running) and a
  named stop event that the Stop shortcut sets, which triggers the normal
  `shutdown.request_shutdown()` path. `main.py`: single-instance check + stop listener at the top
  of `main()`, no duplicate console log handler when started in the background
  (`RAZIEL_BACKGROUND=1`). New `test_launcher.py`; full suite passes.
- **Orb avatar (2026-09-28):** the default avatar is now a living orb of 24,000 glowing particles
  (`avatar_orb.html`, `config.AVATAR_STYLE = "orb"`; `"vrm"` brings back the 3D character in
  `avatar.html`). It speaks the same websocket protocol as the VRM page (dims while she sleeps,
  purple and faster while thinking, pulses and sends out rings with her lip-sync timeline, takes the
  colour of her emotion) plus new messages: `form` (orb, dragon, sword, knight, butterfly, planet,
  heart, galaxy - the particles fly from one shape to the next), `action` (dragon: roar / fire /
  fly / stay; sword: swing; knight: attack / block / raise - the page switches to the right shape
  first and queues actions), `music` and `dance`. New `avatar_forms.py`: the `"avatar-forms"`
  deterministic matcher (after `"open-ai-chat"`), English + Hindi + Hinglish, whole-sentence only,
  compound "become a dragon and roar"; replies via `lang.tr`. New `music_listener.py`: optional
  PyAudioWPatch WASAPI loopback of the default speakers (never the mic), a browser-identical
  48-band analyser, and a music-vs-speech detector (steady loudness, few dips; her own speech gated
  via `avatar_server.is_speaking()`); the page picks waves / equalizer ring / spiky pulse from the
  song's intensity and keeps the beat in any shape. `avatar_server.py` gained `set_form`,
  `send_action`, `send_music`, `set_dance`, `current_form`, `is_speaking`, and marks replayed
  messages; `avatar_window.py` picks the page and window size (420x560) by style and passes
  `?n=` (particles) / `?bg=`. See-through mode: the final pass writes only fully transparent or fully
  opaque pixels, because pywebview's transparency is a red colour key that can't blend soft edges.
  New config: `AVATAR_STYLE`, `AVATAR_ORB_WINDOW_WIDTH/HEIGHT`, `AVATAR_PARTICLES`,
  `AVATAR_MUSIC_REACT`, `AVATAR_MUSIC_GAIN_DB`; `requirements.txt` gained `PyAudioWPatch`
  (Windows only, optional). Sound effects were left out on purpose (the mic would hear them).
  New tests `test_avatar_forms.py`, `test_music_listener.py`, `test_orb_page.py` (headless
  Chromium, incl. the real websocket), updated `test_avatar_window.py` / `test_v3_integration.py`;
  full suite passes. Not yet seen on the real PC.
- **Six new features (2026-09-29):** all optional, each off by itself if its setup step fails.
  Setup: `install_new_features.bat` (pip `uiautomation` + `pypdf`, the Voice ID model into
  `models\voice_id_resnet34.onnx` with a sha256 check, `python voice_id.py` to learn the voice,
  `ollama pull nomic-embed-text`).
  1. *Reading AI chat answers aloud* - `ai_answers.py`: before typing, `tools._type_into_ai_site`
     snapshots the page text through Windows UI Automation (`uiautomation`: TextPattern of the
     document, or a tree walk); afterwards the page is re-read every 1.2 s, the new lines minus the
     question and UI labels are the answer, finished once unchanged for 3 s (6 s when it looks cut
     off). Short answers are read in full, long ones summarised by the local model (`think=False`)
     with "read it all" (`"ai-answer"` matcher) for the rest; code lines are skipped. "open X and
     ask Y" streams (`type_into_ai_site_and_read`); the LLM tool reads it in the background and
     announces it (`read_later` + `main._announce_when_quiet`); phone commands don't wait
     (`ai_answers.no_wait()`). No UI Automation -> a screenshot through `vision.py`.
  2. *Several commands in one sentence* - `multi_command.py`: `split()` cuts only where the next
     part starts a new command (a command verb after ", " / "and" / "then" / ". "), never inside
     payload commands (remind / type / message / search ...), keeps "open youtube and play X"
     pairs, supplies implied verbs ("open Spotify and Chrome", "volume to 30 and brightness to 50",
     "turn off wifi and bluetooth"), drops a leading "Raziel," / "Okay,", leaves "... and where is
     it" questions whole, and splits Hindi / Hinglish at और / aur after an imperative.
     `run()` sends each part through `main._match_deterministic`, waits after "open X" until a new
     window is in front, says yes/no questions last and hands the unknown parts to the model
     together. `main.route_transcript` tries it first (`MULTI_COMMAND_ENABLED`).
  3. *Voice ID* - `voice_id.py`: WeSpeaker ResNet34 ONNX (sherpa-onnx release), numpy Kaldi
     fbank (hamming, CMN), energy VAD; the profile is the mean embedding of `my_voice\` clips
     (`voice_profile.npz`, adapted slowly from clear matches). Each recording is judged in the
     background while Whisper runs (`main._voice_check`); `guard(kind)` refuses kinds in
     `VOICE_ID_GUARDED` for "someone else" (< 0.33) and asks to repeat for "not sure" (including no
     verdict), `answer_refusal()` cancels a "yes" in another voice. Hooks: `confirmation.request()`
     and its "yes", sysctl lock/sleep, notes delete, tools e-mail / WhatsApp / overwrite (also with
     confirmation off), phone_control, calendar, file search (only a clear other voice). Phone
     commands are trusted (`voice_id.trusted()`). Measured on the user's clips: held-out clips of
     him 0.38-0.8 (88% accepted, none "someone else"), 125 other voices at most 0.35 (none accepted).
  4. *Mini cards under the orb* - `cards.py` (timers from the reminders DB every second, the
     Spotify song every 5 s with a no-login-prompt spotipy client, else the Spotify window title;
     weather handed over by `webinfo.py`), `avatar_server.show_card/hide_card/cards()` (replayed to
     a reconnecting window), page part `p45_cards.js`: opaque square cards on a 4 px grid (no
     partial alpha in see-through mode), the orb's render area shrinks and moves up (`uOffY`).
  5. *Game and call mode* - `quiet_mode.py`: full screen = `SHQueryUserNotificationState` or a
     borderless non-maximised window covering the monitor; call = another running app with
     `LastUsedTimeStop == 0` in the microphone ConsentStore (incl. NonPackaged). Game: no unprompted
     speech (reminders -> Windows toast + a card, no beeps, no small talk), still answers when
     called. Call: the same, and the wake word is off (`wake_word._mic_gated`). "Do not disturb" /
     "quiet mode off" / "you can talk again" (30-minute override). Status chip under the orb.
  6. *Ask your own files* - `file_index.py`: Documents / Downloads / Desktop (known-folder paths;
     the project folder, program folders, hidden files and OneDrive cloud-only contents skipped);
     text from PDF (pypdf), docx / pptx / xlsx / odt (zip XML), txt / md / csv / html / rtf; ~1000-char
     pieces + a name piece per file; `nomic-embed-text` through Ollama on the CPU by default;
     `file_index.db` (SQLite, float16 vectors) + numpy cosine plus word matches; words only while
     Ollama is down. Background thread, newest files first, paused while she talks or in game mode,
     rescans every 20 min. `"file-search"` matcher: find / open / ask (AskLLM with the excerpts as
     untrusted data) / "open it" / "open the second one" / "show it in the folder" / index / status;
     LLM tool `search_my_files`.
  New tests: `test_multi_command.py`, `test_ai_answers.py`, `test_voice_id.py` (real model + clips),
  `test_quiet_mode.py`, `test_cards.py`, `test_file_index.py`, `test_new_features_main.py`, card
  checks in `test_orb_page.py`; full suite passes. Not yet seen on the real PC.
- **Avatar window remembers its place and size (2026-09-29):** the window no longer always opens
  at the hardcoded x=1420, y=180. New `avatar_place.py`: every move / resize (pywebview
  `events.moved` / `events.resized`) is read back with `GetWindowRect` and saved to
  `avatar_place.json` (per style: orb / vrm) a second after the last one (and at shutdown); at start
  `avatar_window.create()` restores it (`fit_rect` pulls it back onto the nearest screen, shrunk if
  needed, when that monitor is gone; nothing saved -> the right-hand side of the main screen, or
  `AVATAR_START_X/_Y`). Everything is in the window's own screen coordinates, so a place round-trips
  exactly at any display scaling; the size handed to pywebview is divided by the DPI scale it applies,
  and a check on `events.shown` corrects any difference. Resizing: Ctrl + mouse wheel over her (the
  pages call `window.pywebview.api.scale()` through a one-method `js_api`) and the `"avatar-place"`
  matcher (before `"avatar-forms"`): "make yourself bigger / a bit smaller / much bigger", "move to
  the top left corner / the right / the middle", "reset your size / position", Hindi "बड़ी / छोटी हो
  जाओ". New config `AVATAR_REMEMBER_PLACE`, `AVATAR_START_X/_Y`; `.gitignore` gained
  `avatar_place.json`. New `test_avatar_place.py`, wheel checks in `test_orb_page.py`.
- **Hand gestures (2026-09-29):** new `gestures.py`. A camera thread (OpenCV, DirectShow first, 640x480,
  mirrored, ~15 processed frames a second, 6 when no hand has been seen for 10 s) feeds MediaPipe 1.0.1's
  GestureRecognizer (VIDEO mode, 2 hands, `models\gesture_recognizer.task`); `GestureEngine` turns the 21
  landmarks + canned pose per hand into gestures (poses debounced over 3 frames; held poses fire once per
  hold; swipes only from a hand that has been in view 0.3 s, away from the picture's edge, >= 0.22 of the
  frame within 0.5 s, 1 s cooldown, vertical ones confirmed by the hand staying in view; knob = the
  knuckle line's angle, 20 degrees for the first step then 12, ends after 4 s unturned; one-hand / two-hand
  pinch with hysteresis). `GestureActions` (own thread): palm -> media play/pause key; swipe right/left ->
  next/previous track, or Page Down/Up (PowerPoint, PDF readers, a PDF in a browser) / arrow keys (Google
  Slides) when those are in front; swipe up/down -> volume +-VOLUME_STEP, down while she talks ->
  `speaker.skip_current()`; fist knob -> volume +-2 %, two-finger knob -> brightness +-4 % (applied at
  most 4x a second); thumbs up -> Spotify Liked Songs (no-prompt client; `tools._spotify_scope()` adds
  user-library-* only once gestures are set up, so the next Spotify command asks for it once); pinch drag /
  two-hand spread -> `avatar_place.Controller` + `note()`. Feedback: a "gesture" status card under the orb.
  The camera is let go in a call (`quiet_mode`), while another app uses the webcam (new
  `quiet_mode.camera_users()`), while Zoom / Teams / a Meet tab / the Camera app is in front, and while the PC
  is locked; "stop watching" / "watch my hands" (the `"gestures"` matcher, persisted in
  `gestures_state.json`). `speaker.py`: `is_playing()` counts whole replies (gaps too), `skip_current()` cuts
  the playing piece or the next one (never a yes/no question), long `speak_interruptible` texts are spoken
  piece by piece through the stream path. `install_gestures.bat` (mediapipe 1.0.1 + opencv-contrib-python
  4.10.0.84 with numpy pinned at 1.26.4, the model, a load + camera check), `check_gestures.bat` / `python
  gestures.py --check` (live preview, nothing done). Config `GESTURES_*`. New `test_gestures.py` (synthetic
  hands at 15 fps, jitter, fake camera / recogniser / backends). The recogniser itself could not be run here
  (the model host is blocked), so thresholds are untested on real hands.

- **Polish + fixes (2026-09-29, later):** (1) *Crystal sphere look:* in the see-through window
  (`AVATAR_TRANSPARENCY = "native"`, colour key = binary alpha only) the orb renders inside a glass
  sphere (`?look=sphere`, default; `AVATAR_ORB_LOOK = "float"` = old bare particles): the final pass
  (`OUT_FS` in `avatar_orb.html`) draws a disc - deep-space gradient + mood-tinted nebula, the scene
  (now with stars and the glow disc, like the dark tile) "screened" over it through a slight ball-lens
  warp, edge darkening, mood-coloured rim glow, a gloss band, a crescent reflection, a bottom caustic,
  a 1.5 px rim line; outside the disc alpha 0. World scale: `look.sphereWorld` 1.22 units to the rim.
  Cards are rounded glass pills painted on a `#cardbg` 2D canvas whose alpha is snapped to 0/255;
  new `hold` card kind (a bar filling from `start` over `total` s). (2) *Music while talking:* new
  `media_duck.py` (pycaw audio sessions on a COM worker thread: per-app peak meters -> `playing()`,
  `silent_for()`; `duck()` sets every other app's session volume to `MEDIA_DUCK_LEVEL` 0.2 of its own,
  `unduck()` restores unless the slider was moved; apps that close while ducked and crash leftovers
  are kept in `media_duck.json` and fixed when seen again; `MEDIA_DUCK_MAX_SECONDS` safety with a
  `touch()` heartbeat). `main.run_awake_session` tracks `addressed` (just woken / "Raziel" over music /
  she asked a question or a yes/no is pending): not addressed + music playing -> `wake_word.wait_for_name`
  (stops when other apps are silent 3 s); addressed + music -> ducked, `wait_for_voice(timeout=8)`;
  open mic -> `wait_for_voice(stop_when=playing(1.0), veto=sound in the last 0.45 s)`; the wake loop
  ducks on the wake word and unducks in a `finally`; after a non-question reply it unducks; a barge-in
  while music just started (not ducked) is ignored; with music, a Voice ID "someone else" (or "not
  sure" when not addressed) drops the transcript. `WAKE_WORD_THRESHOLD_MUSIC` 0.42 needs two frames in
  a row. Whisper: `WHISPER_TEMPERATURES` (0, .2, .4) caps retry passes; Spotify playlist names
  (`spotify_playlists.json`, saved by `play_playlist`, refreshed at start with the cached token only)
  go into `initial_prompt` (`WHISPER_PLAYLIST_HINTS` 12) but not into the guard's vocabulary; the guard
  rejects transcripts that are nothing but hint names, 3+ or repeated (prompt echo over music).
  Hand gestures pause (`gestures.set_busy`) while Whisper runs. (3) *Playlists:* `instant_replies.
  parse_playlist_request` ("open my X playlist [and play any music from that]", "play something from
  the X playlist", "start playing the X playlist"; without "my" the name must be a known playlist),
  used first by `try_auto_play_music`; `parse_play_request` never searches "from that";
  `multi_command.split` keeps "... playlist and play any music from that" whole. (4) *Stricter
  gestures:* a pose counts only when shown (palm centre above `GESTURES_RAISED` 0.8, upright within
  `GESTURES_UPRIGHT_DEGREES` 35, >= 3 fingertips above their knuckles for a palm), model + fingers must
  agree, palm hold 1.0 s with a hold-bar card from 0.4 s (`hold` / `hold_cancel` events), swipes only
  from "ready" samples (a shown palm held still 0.25 s), straightness 2.5, vertical swipes confirmed
  only if the hand is still a shown palm, pinch held still 0.4 s before a drag, knob needs a raised
  upright fist 0.6 s and 24 degrees for the first step, "Paused"/"Playing" cards, `knob_start` card,
  `gestures_debug.jsonl` (2.5 s of landmarks before each gesture that acted; `GESTURES_DEBUG_LOG`),
  `--check` shows COUNTS / ignored with the lean and height. (5) Shutdown: `webview.destroy()` doesn't
  exist in pywebview 5 - each window is destroyed. Tests: new `test_media_listen.py`; updated
  `test_gestures.py`, `test_orb_page.py`, `test_avatar_window.py`, `test_stream.py` and the loop fakes
  in five suites (`wait_for_voice(pa, **kw)`). 42 files, 0 failures. An independent review found the
  closed-app volume loss, question answers needing the name, the dropped-hand swipe, playlist names
  in the guard, parser false positives, the music barge-in and a GLSL `pow` of a negative base: fixed.
- **Fixes from the first day of real use (2026-09-29, evening):** (1) *"Understood." to everything:*
  the persona asked for it; now she confirms by saying what was done, never claims an action without a
  tool call, and answers "(nothing)" to words that aren't a request. `llm_brain`: `is_idle_reply`
  ("(nothing)" / empty / a bare "Understood."), `is_bare_ack`, `claims_action` (short "Playing music."
  sentences, not "Opening hours are ..."); `_stream_held` holds an opening acknowledgement or claim (and
  everything after a claim) until the model is done. A bare "Understood." or a claim without a tool call
  is asked again once (`_ACK_NUDGE` / `_NO_TOOL_NUDGE`, removed from the history afterwards; not for
  untrusted turns); still a claim -> "I didn't manage to do that"; nothing to do -> `last_turn_idle`,
  nothing said and no trace in the history. main.py then says "Sorry, I didn't catch that" once when she
  was called (`said_didnt_catch`), else stays quiet. Her name alone gets "Yes?"
  (`main.is_name_only`). Voice ID on every turn: "someone else" is dropped, "not sure" when not
  addressed and either music plays or it is >= `VOICE_ID_UNSURE_DROP_SPEECH` 2.5 s of talking;
  `lock_pc` / `close_app` are allowed on "not sure" (`VOICE_ID_LENIENT`). (2) *Sleep:* "go back to
  sleep" is her sleep (`_SLEEP_WHOLE_RE`); `system_control` sleep / shutdown / restart from the model is
  refused unless the sentence names the PC. (3) *AI chats:* `ai_sites.py` (JetGPT, chat gbt, Jemini...
  -> the real site, also in `multi_command`, `open_app` and the open-app matcher); "open Gemini and
  type" with no / a one-letter question opens it and asks for the question (`try_auto_ai_followup`,
  `AI_CHAT_QUESTION_WAIT`); `try_auto_ai_send` ("send it / enter / submit [and read me the answer]",
  "read me the answer") works when the window in front is ChatGPT / Gemini: Enter, then
  `ai_answers.send_and_read` / `read_current` (the question = `dictation.last_typed`). In a split
  sentence, parts that type / press after a part the model does go to the model too (in order).
  (4) *Galaxy:* more ways to say "become" ("turning to", "change into", ...). (5) *Folders:*
  `folder_names.json` (Desktop / Documents / Downloads / OneDrive to depth 3, other drives to 2, in the
  background at start): short unusual names are Whisper hints (`WHISPER_FOLDER_HINTS`), "open (my|the)
  X folder" is matched in code (`try_auto_open_folder`, "C O A" -> COA), a missing name gets "did you
  mean". App names are Whisper hints too (`WHISPER_APP_HINTS`); "what's app" / "whats up" -> WhatsApp.
  (6) *Spotify:* `spotify_auth.py` - background users of the saved login (music card, thumbs-up,
  playlist names) now keep its scope; spotipy writes the manager's scope into the cache on refresh,
  so the smaller scope made the next play command open the login page every time. `_pick_track`
  prefers the original over remix / lofi / slowed / cover unless asked, `_pick_device` this PC's app
  (host name, active, not a Web Player), `_confirm_playing` checks `current_playback` and retries once
  after `transfer_playback(force_play=True)`, else says it isn't playing; "play any music" plays a
  playlist, not a song called "Music". (7) *Gestures:* swipe distance 0.18 (config too), `3 x size`
  for far hands, straightness 2.0, ready after 0.15 s leaning up to 45 degrees, a swipe out of the
  picture counts (`_exit_swipe`: heading out of the picture and gone for 0.12 s), one blurred frame
  doesn't spoil it; the knob needs MediaPipe's own
  Closed_Fist / Victory and a hand >= 0.07 (every false knob in the debug log was a small far hand only
  the finger check called a fist) and shows its card with the first step; resize needs both pinches
  held still. The recognizer is closed before exit (no `GestureRecognizer.__del__` error). (8) Also:
  the mic's `OSError -9999` no longer counts as a loop error (a new PyAudio instance on the next open),
  no "forty minutes of nothing" right after a wake (`initiative.note_wake`), English / Hindi detection
  works on faster-whisper 1.0 (`_detect_language_1_0`: it failed on every turn; into Hindi only at
  `HINDI_SWITCH_PROBABILITY` 0.75, a dropped turn's language is forgotten), "play the third
  video" plays that result of the last YouTube search (only when the sentence says video / YouTube or
  YouTube is in front: "open the second one" stays the file search's). The pending "what should I ask
  it?" is checked before a sentence is split, and cleared by "go to sleep". Tests: new `test_fixes_0929.py`; updated
  `test_ai_chat_site.py`, `test_cards.py`, `test_gestures.py`, `test_media_listen.py`, `test_brain.py`,
  `test_voice_id.py`, `test_v3_integration.py`, `test_wiring.py`.
- **Her universe (2026-09-29, night):** after a video of a multi-agent "universe" (a Nexus with named
  beings, each waking by name), new `universes.py`. Raziel is the Nexus; six beings: Melody (music),
  Hermes (messages), Athena (knowledge), Atlas (the PC), Chronos (time), Sentinel (security), with
  colours, symbols (音 信 知 創 時 盾, 核 for the Nexus) and helpers. `split_name` takes a leading being name
  off the transcript (aliases for Whisper's spellings and Devanagari; after the name a pause or a request
  word, so "Atlas cycle price" / "melody of this song" are not for a being; "ask X ...", "tell X to ...",
  never "tell Melody I'm late" / "call Melody"); the name alone -> "Melody here." and the next sentence is
  that being's. A named turn: `Brain.stream_turn/process_turn(being=)` appends a persona hint naming the
  being's own tools to the user message; the tool list sent stays the full one (a different list would
  break Ollama's cached prefix: 10+ s). Without a name: matcher labels (`MATCHER_UNIVERSE`) and the
  model's tools (`TOOL_UNIVERSE`, via `Brain.tool_listener`) move the camera (never the shape the user
  chose; a named being shows the universe and the old shape comes back at sleep); `end_turn` after each
  spoken turn (phone commands are nobody's). The names are Whisper hints and in the guard's bare-name
  check; "Atlas, stop typing" reaches dictation without the name. `avatar_server.set_universe` (merged, replayed): beings, `focus`, `big`, `home`.
  Page: form `universe` (p3b_universe.js): Nexus star with two rings, six two-armed galaxies on a tilted
  orbit, helper rings / stars, dust; a camera that flies into the focused galaxy (zoom 2.6); colours via
  tone >= 2 = `uUni[k]` (8 colours, not blended in transitions). Labels (p46_labels.js) on a 2D canvas
  clipped to the glass, placed through the same projection and the lens inverse: names (+ titles when
  big), and in focus the symbol dial, name, title and helper names. "Show me the universe" grows the
  window to ~86 % of the screen for `UNIVERSE_BIG_SECONDS` (`avatar_place.suspend_saving`: never saved
  as her size; the re-enable timer is cancelled by a quick re-open). `UNIVERSE_HOME = "universe"`: the
  map is her resting look; music still switches her to the dance forms. Universe colours are tone >= 10
  (loud music forms reach tone 1.9, below it).
  Tests: new `test_universes.py` (64 checks), `test_orb_page.py` (the map, labels inside the glass only,
  colours kept apart, socket replay). An independent review's 9 findings were fixed.
## Working agreement for AI coding sessions
- At the start of every session, read this file and the existing code before
  making changes.
- Only build what's listed under "Current phase" — do not jump ahead.
- After each working milestone, note it here and suggest a git commit.
- New tools/features should follow the same code pattern as existing ones.
- Any action that deletes, sends, or modifies something outside the project
  folder must have a confirmation step before executing.
