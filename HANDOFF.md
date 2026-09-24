# Voice Assistant "Raziel" — Project Handoff

Paste this as the first message of a new chat to resume with full context.

---

## 1. What this is

A fully local, free, offline voice assistant on **Windows**, built in Python.
Wake word → continuous conversation → executes real system/web/media actions →
persists memory across restarts → 3D VRM avatar face with lip sync + emotions.

Project folder: `C:\Users\Ayush\Desktop\Voice Agent`
User: Ayush. Student at VIT-AP. Prefers **cost-free/local** solutions throughout.

---

## 2. Stack (all free, all local)

| Layer | Choice | Notes |
|---|---|---|
| Wake word | **openWakeWord** (`hey_jarvis`) | Switched from Porcupine — it demanded a company email |
| STT | **faster-whisper** (`small`, CPU, int8) | `medium` tested, too slow for marginal gain |
| LLM brain | **Ollama** `qwen3:8b` | Chosen over Gemini for privacy/offline |
| TTS | **Piper** `en_GB-cori-high` | Replaced pyttsx3 (see §6) |
| Memory | **SQLite** (`memory.db`) | Facts + full session transcripts |
| Avatar | **three.js + @pixiv/three-vrm** in **pywebview** | Raziel-1.vrm, VRM 1.0 |

---

## 3. File map

```
main.py             Entry. Wake/sleep state machine. pywebview owns main thread;
                    voice loop runs in its background thread.
config.py           ALL tunables. _SCRIPT_DIR makes paths cwd-independent.
wake_word.py        openWakeWord + 0.5s pre-roll buffer (prevents clipped first word)
audio_recorder.py   wait_for_voice() (adaptive ambient calibration) +
                    record_until_silence()
transcriber.py      faster-whisper + CUSTOM_VOCABULARY biasing via initial_prompt
llm_brain.py        Ollama chat, tool-call loop, system prompt (persona + routing),
                    session persistence, history trimming
tools.py            ~20 tools + deterministic bypass matchers (see §5)
memory.py           SQLite: facts (keyed + freeform), sessions, search
speaker.py          Piper synth + interruptible chunked playback + barge-in
emotion.py          Keyword classifier → VRM expression (deliberately NOT the LLM)
viseme_map.py       eSpeak IPA phonemes → VRM's 5 visemes (aa/ih/ou/ee/oh)
avatar_server.py    WebSocket bridge (asyncio loop in bg thread; sync API)
avatar_window.py    pywebview always-on-top window
avatar.html         3D viewer: VRM load, lip sync, emotions, blinking, auto-reconnect
mic_test.py         Diagnostic: measures real ambient vs speech RMS
Raziel-1.vrm        The avatar model (16.7MB)
```

---

## 4. Persona (encoded in `llm_brain.py` BASE_SYSTEM_PROMPT)

Name **Raziel**. Total certainty, no hedging. No filler words. Analytical phrasing
("Probability of precipitation is high", not "I think it might rain"). Cool
acknowledgments: "Understood." / "Acknowledged." / "Processing request."
Concise, no pleasantries. Voice is flat/measured; **face is fully expressive**
(user explicitly wanted both).

Hardcoded lines: startup "Systems online.", wake "Listening.", sleep "Goodbye."

---

## 5. CRITICAL ARCHITECTURAL LESSON

**qwen3:8b is unreliable at picking the right tool.** Prompt-tuning was tried 3+
times and kept failing. The working pattern is **deterministic regex/keyword
matching in `main.py` BEFORE the LLM ever sees the transcript**:

- `match_quick_command()` — "pause"/"next"/"skip"/"go back" → direct tool call
- `try_auto_remember_favorite()` / `try_auto_recall_favorite()` — "my favorite X
  is Y" / "what is my favorite X" → direct SQLite (keyed by `favorite_<category>`)
- `try_auto_open_site_action()` — "open SITE and play/search X" → right tool

All strip filler prefixes ("Okay,", "So,") and truncate at first `?` to survive
Whisper garbling. **When something is flaky, add a deterministic matcher — don't
tune the prompt again.**

Also: `SLOW_PATH_TOOLS = {"recall", "web_search"}` — these bypass
`FAST_TOOL_REPLIES` because their raw output must be LLM-summarized, not spoken
verbatim.

---

## 6. Hard-won bug fixes (do not regress these)

| Bug | Root cause | Fix |
|---|---|---|
| Silent TTS failures | `pyttsx3.init()` **caches/reuses one engine** globally; `.stop()` corrupts it → "run loop already started" | Replaced pyttsx3 entirely with Piper (stateless) |
| Garbled/hallucinated transcripts | Mic Boost too high → noise ≈ speech amplitude | User lowered Windows Mic Boost + disabled enhancements. Verified via `mic_test.py` (ambient 10264→0.6) |
| Apps "not found" though installed | `subprocess(shell=True)` always "succeeds"; UWP apps have no .exe/.lnk | `open_app` tries: full-path candidates → **`Get-StartApps`** (PowerShell, cached) → .lnk scan → PATH → `os.startfile` |
| Spotify crash | Spotify API returns **null entries** in search results (known upstream bug) | Filter `[i for i in items if i]`, request limit=5 |
| Whisper repetition loops | Long/quiet tails | `condition_on_previous_text=False`, `vad_filter=False` (VAD stripped real speech) |
| Slow responses | Ollama unloads model between calls | `keep_alive="30m"`, `num_ctx=2048`, `FAST_TOOL_REPLIES` |

---

## 7. Key config values (tuned from real measurements)

```python
WAKE_WORD_THRESHOLD = 0.45
VOICE_TRIGGER_MARGIN = 400      # additive above measured ambient, NOT multiplicative
SILENCE_STOP_MARGIN = 250       # lower than trigger — don't cut off mid-sentence
MIN_SUSTAINED_VOICE_MS = 200    # filters clicks/coughs
RECORD_MAX_SECONDS = 20
AMBIENT_CALIBRATION_SECONDS = 0.4
BARGE_IN_RMS_THRESHOLD = 1200   # needs headphones; speakers cause self-triggering
PIPER_LENGTH_SCALE = 1.15
CUSTOM_VOCABULARY = ["Arijit Singh"]   # add misheard names here
```

Note: `wait_for_voice()` measures the room's ambient RMS each turn and requires
speech to exceed `ambient + VOICE_TRIGGER_MARGIN`. **Additive, not multiplicative**
— a multiplier was tried and was mathematically wrong (speaking volume doesn't
scale with room noise).

---

## 8. Tools implemented

`open_app`, `open_folder`, `open_file`, `open_website_search`, `write_file`,
`web_search` (Gemini + Google Search when a key is set, else ddgs), `get_current_datetime`, `take_screenshot`,
`show_last_screenshot`, `send_whatsapp_message`, `play_music`, `play_playlist`,
`pause_music`, `resume_music`, `next_track`, `previous_track`, `remember`,
`recall`, `forget`, plus (v3, see §12) `set_reminder`, `manage_reminders`, `system_control`, `calculate`, `get_weather`,
`get_news`, `manage_notes`, `clipboard_action`, `calendar_action`, `email_action`, `describe_screen`, `daily_briefing`,
`dictation_control` (34 tools in all)

---

## 9. Setup / required user actions

```powershell
pip install -r requirements.txt
python -c "import openwakeword; openwakeword.utils.download_models()"
python -m piper.download_voices en_GB-cori-high
ollama pull qwen3:8b
python main.py
```

- **Spotify**: needs Premium + free dev app. Redirect URI **must** be
  `http://127.0.0.1:8888/callback` (Spotify banned `localhost` in 2025).
  Client ID/secret go in `config.py`. `.spotify_cache` persists the login.
- **WhatsApp**: add numbers to `config.CONTACTS`; must be logged into WhatsApp Web.
- **Preserve on config.py updates**: `SPOTIFY_CLIENT_ID/SECRET`, `CONTACTS`,
  `CUSTOM_VOCABULARY`.

---

## 10. STATUS

**Working:** wake word, conversation mode, barge-in, all tools, memory across
restarts, Piper voice, persona.

**Built but UNTESTED — test this first:** the entire 3D avatar (§3 avatar files).
Never run on real hardware. Python-side WebSocket pipeline, emotion classifier,
and viseme mapper were unit-tested and pass; WebGL rendering + pywebview window
+ real lip sync have never been verified.

**BLOCKED — custom wake word "Raziel":**
openWakeWord Colab training. Hit ~10 consecutive incompatibilities because Colab
runs Python 3.13 and the notebook targets 3.10–3.12. Progress so far:
- Switched Colab runtime version → Python **3.12** (Runtime → Change runtime type
  → "Runtime version" dropdown). This resolved most issues.
- AudioSet dataset **restructured upstream** (tar → Parquet), so the notebook's
  `wget`/`tar` cell is dead. Must use `datasets.load_dataset("agkphysics/AudioSet",
  split="train", streaming=True)` AND then **write clips to disk** as `.wav` into
  `./audioset_16k` (train.py needs real files, not a stream).
- `piper-sample-generator` v3.x removed `generate_samples.py` → clone
  `--branch v2.0.0`.
- Remove `'./fma'` from `config["background_paths"]`.
- `!pip install "numpy<2.1"` (Numba conflict).
- Last known error: `FileNotFoundError: './audioset_16k'` — fixed by the
  write-clips-to-disk cell above; not yet confirmed run.

Once trained: drop `raziel.onnx` in the project folder and set
`WAKE_WORD_MODEL = "raziel.onnx"` in `config.py`. No other code changes needed.

**Alternative if Colab keeps failing:** train locally (user has Python 3.12 on
Windows).

---

## 11. Roadmap remaining

- v3 (Hindi, Google news/knowledge, 12 features) is built - needs a real-PC test (see §12)
- Phase 5: voice-profile security, logs dashboard (smart home / Home Assistant was dropped by the user)

---

## 12. v3 (2026-09-22) - Hindi, Google knowledge, 12 features

Read `PROJECT.md` section "v3" and `SETUP_V3.md`. Short version:

* **New modules:** `lang.py` (en/hi helpers, per-turn language), `netutil.py` (all HTTP), `winutil.py` (Windows ctypes: clipboard,
  SendInput, windows), `routing.py` (`AskLLM`), `timeparse.py`, `reminders.py`, `sysctl.py`, `calc.py`, `webinfo.py`,
  `notes.py`, `clipboard_tool.py`, `dictation.py`, `briefing.py`, `vision.py`, `google_api.py` + `google_setup.py`,
  `hindi_commands.py`. Modified: `main.py`, `tools.py`, `llm_brain.py`, `confirmation.py`, `transcriber.py`,
  `transcript_guard.py`, `speaker.py`, `initiative.py`, `memory.py`, `viseme_map.py`, `config.py`.
* **Pattern kept:** everything that can be decided in code is a strict, anchored, lazily-run matcher in
  `main.DETERMINISTIC_MATCHERS` (priority order); the LLM is the fallback. Tool replies are spoken verbatim.
* **Language:** `lang.set_current(lang.detect(transcript))` once per turn (Devanagari = Hindi); modules answer with
  `lang.tr(TABLE, key)`. The Hindi hint for the LLM rides in the user message (the system prompt never changes, so
  Ollama's prompt cache stays valid).
* **Safety rules that must not regress:** shutdown/restart/close-app/clear/calendar-add/WhatsApp go through
  `confirmation.request`; `_assistant_busy()` is True while a question is open; AskLLM turns are `untrusted=True`
  (no tool executes); Gmail is read-only; the Gemini key and OAuth tokens are never logged.
* **Context budget:** prompt + 34 schemas ~5,400 tokens -> `OLLAMA_NUM_CTX = 10240`.
* **Tests:** stub test suites aren't included in this folder; on the real PC the first thing to do is
  read `assistant.log` after trying a few commands from `SETUP_V3.md`.
