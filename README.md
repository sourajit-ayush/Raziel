# Raziel — a local, offline voice assistant

Say the wake word, then just talk. Raziel listens continuously, understands
English and Hindi, calls real tools instead of just describing what it would
do, and keeps a running conversation until you say "goodbye" or "go to
sleep". Everything - wake word detection, speech recognition, the language
model, and text-to-speech - runs locally. No subscription, no account
required for the core experience, and nothing risky (sending a message,
overwriting a file, shutting the PC down) ever runs without you saying yes
first.

## What it can do

- **Wake word → continuous conversation.** A custom wake word trained on
  your own voice (or any of openWakeWord's built-ins, e.g. "hey Jarvis"),
  then a real back-and-forth conversation with no need to repeat the wake
  word between turns.
- **A local LLM brain** (Ollama, `qwen3:8b` by default) that calls real
  tools - opening apps, searching the web, controlling the PC - rather than
  just talking about doing them.
- **Bilingual.** Understands and speaks English and Hindi, switching per
  sentence, including code-switched names and phrases.
- **Apps, files and the web.** Opens installed apps, files and folders by
  name, searches the web or a specific site, takes and describes
  screenshots, describes what's currently on your screen with a local
  vision model.
- **Messaging.** Sends WhatsApp messages and drafts emails (Gmail/Outlook) -
  nothing is ever sent without you confirming first.
- **Music.** Full Spotify playback control by voice (play, pause, skip,
  named playlists) - requires Spotify Premium.
- **Memory.** Remembers facts and conversations across restarts
  (`remember` / `recall` / `forget`), backed by a local SQLite database.
- **Timers, reminders and alarms** that fire even while it's back asleep.
- **PC control.** Volume, brightness, lock, sleep, switch window, close an
  app, shut down or restart - the destructive ones always ask first.
- **Calendar and mail.** Reads your Google Calendar and Gmail (read-only)
  and adds calendar events (with confirmation).
- **Daily extras.** A morning briefing on first wake of the day, a
  calculator with unit/currency conversion, dictation mode, notes and
  to-do/shopping lists, reading your clipboard aloud.
- **A 3D avatar** (optional) - a VRM face with lip sync and expression,
  shown in its own always-on-top window.
- **A phone bridge** (optional, Phase 6) - talk to Raziel from your Android
  phone from anywhere, and have it ring, text, or open an app on your phone
  in return. See `PHONE_BRIDGE_SETUP.md`.

## Prerequisites

- Windows 10/11
- Python 3.11 or 3.12 (not 3.13 yet — some audio libs lag behind new releases)
- A working microphone and speaker
- No account or API key needed for the core experience — wake word
  (openWakeWord), speech recognition (faster-whisper), the LLM brain
  (Ollama), and text-to-speech (Piper) all run fully offline and free
- [Ollama](https://ollama.com/download) installed
- 8GB+ RAM recommended (more with a GPU) for the default `qwen3:8b` model;
  a lighter model works on more modest hardware, see "Tuning" below

## Setup

### 1. Install dependencies

Open PowerShell in this folder:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`pyaudio` sometimes fails to install with plain pip on Windows. If it does:

```powershell
pip install pipwin
pipwin install pyaudio
```

### 2. Set up your config

Copy the template and fill in anything you want to use:

```powershell
copy config.example.py config.py
```

`config.py` is where every setting lives, and it's excluded from git on
purpose since it ends up holding your own API keys and contacts. Everything
inside is commented — most sections (Spotify, Google, the phone bridge) are
entirely optional and the assistant works fine without them.

### 3. Download the wake word model files (one-time)

openWakeWord ships pretrained models but needs to fetch them once:

```powershell
python -c "import openwakeword; openwakeword.utils.download_models()"
```

This downloads a small set of `.onnx` model files (a few MB total) and
caches them locally — no account, no key, nothing to sign up for. By
default `config.py` points at a custom-trained `Raziel.onnx` model with a
fallback to `hey_jarvis` if that file isn't present; if you haven't trained
your own, just change `WAKE_WORD_MODEL` to `"hey_jarvis"`.

### 4. Set up Ollama (the brain)

1. Install Ollama from https://ollama.com/download and run the installer
2. Ollama runs as a background service after install — you don't need to
   manually start it each time (it starts with Windows)
3. Pull the model this project uses (one-time, downloads ~5GB):

```powershell
ollama pull qwen3:8b
```

4. Quick sanity check it's working:

```powershell
ollama run qwen3:8b "say hello in 5 words"
```

If that replies, Ollama is ready.

### 5. Download the voice (Piper - free, local, neural TTS)

```powershell
python -m piper.download_voices en_GB-cori-high
```

This downloads two small files (`en_GB-cori-high.onnx` and `.onnx.json`)
into this project folder - no account, no key. To try a different voice,
browse https://github.com/rhasspy/piper/blob/master/VOICES.md, change
`PIPER_VOICE_NAME` in `config.py` to match, then re-run the download
command with the new name.

If you want Hindi replies too (`HINDI_ENABLED = True` in `config.py`, on by
default), the Hindi voice (`hi_IN-priyamvada-medium`, ~60MB) downloads
automatically the first time it's needed — no separate step.

### 6. Optional: Spotify playback

Requires a **Spotify Premium** account - the API can't control playback on
a free account.

1. Go to https://developer.spotify.com/dashboard and log in with your
   Spotify account
2. Click **Create app**. Fill in any name/description you want
3. For **Redirect URI**, enter exactly: `http://127.0.0.1:8888/callback`
   (Spotify no longer accepts `localhost` in redirect URIs — it must be the
   literal IP `127.0.0.1`)
4. Save the app, then open it and copy the **Client ID** and **Client
   Secret** into `config.py`:
   ```python
   SPOTIFY_CLIENT_ID = "your-client-id-here"
   SPOTIFY_CLIENT_SECRET = "your-client-secret-here"
   ```
5. The first time you ask it to play music, a browser window opens asking
   you to log in and authorize the app - approve it. This creates a local
   `.spotify_cache` file so you won't need to do this again.
6. Make sure you're logged into the **same Spotify account** in the desktop
   app that you used to create the developer app.

If you skip this, `play_music` just tells you it isn't set up yet -
everything else works fine.

### 7. Optional: Google Calendar + Gmail

One-time setup — see `SETUP_V3.md` for the full walkthrough, then run:

```powershell
python google_setup.py
```

and sign in in the browser. Nothing is ever sent or deleted: it reads mail
and adds calendar events only after you say yes.

### 8. Optional: the 3D avatar

```powershell
pip install pywebview websockets
```

Set `AVATAR_ENABLED = True` in `config.py` (on by default) and place a VRM
model file where `AVATAR_MODEL_PATH` points.

### 9. Optional: the phone bridge

Lets you talk to Raziel from your phone from anywhere, and have it trigger
actions (ring, text, open an app) on your phone in return. Full setup
(Tailscale + Tasker + Join, ~15 minutes, mostly free) is in
`PHONE_BRIDGE_SETUP.md`.

## Running it

```powershell
venv\Scripts\activate
python main.py
```

Say your wake word (**"Raziel"** by default, or **"Hey Jarvis"** if you
haven't trained a custom model). It'll say it's listening — now just talk
normally, no need to repeat the wake word between turns. Say **"goodbye"**
or **"go to sleep"** (also understood in Hindi) when you're done, and it
returns to listening for the wake word.

Try things like:
- "What's the capital of Japan?" (plain question, no tool needed)
- "Open notepad" / "Open my resume.pdf" / "Open Google and search for the
  weather in Tokyo"
- "Send a WhatsApp message to [contact] saying I'll be late" (requires
  `CONTACTS` set up in `config.py`)
- "Play Blinding Lights by The Weeknd" (requires Spotify setup)
- "Remember that I have an exam on Friday" / "What do you remember about my
  exam?" / "Forget about the exam"
- "Set a timer for 10 minutes" / "Remind me to call Mom at 6pm"
- "What's on my calendar today?" / "Do I have any new email?"
- "Turn the volume up" / "Lock my PC" / "What's on my screen?"
- "Start typing" (dictation mode) / "Take a note: buy milk"
- Ask it in Hindi — it answers in Hindi

Press `Ctrl+C` to stop.

## Tuning

All the knobs live in `config.py` (each one is commented in place). A few
worth knowing about:

- `WAKE_WORD_MODEL` / `WAKE_WORD_THRESHOLD` — swap the wake word or adjust
  sensitivity; lower the threshold if it's not triggering, raise it if it
  triggers randomly
- `SILENCE_RMS_THRESHOLD` — lower this if it cuts you off mid-sentence,
  raise it if it never stops recording in a noisy room; run
  `python mic_test.py` to measure your actual mic levels instead of guessing
- `WHISPER_MODEL_SIZE` — `tiny`/`base` are faster but less accurate,
  `small`/`medium` more accurate but slower on CPU
- `OLLAMA_MODEL` — `qwen3:8b` is the default; if it's too slow on your
  machine, try a lighter model like `llama3.2` (`ollama pull llama3.2`
  first, then update this setting)
- `HINDI_ENABLED` — set `False` for English-only
- `CONFIRM_RISKY_ACTIONS` / `CONFIRM_ACTIONS` — which actions ask for a
  spoken yes/no before running

## Adding more apps to `open_app`

`open_app` already searches Start Menu shortcuts (covering almost anything
installed) with website fallbacks for a few common ones. For anything it
still can't find, edit the `APP_COMMANDS` dictionary at the top of
`tools.py`:

```python
"discord": "discord.exe",
```

## Project docs

- `PROJECT.md` — the full development log and feature history
- `SETUP_V3.md` — setup for the Hindi/Google-knowledge/PC-control feature set
- `PHONE_BRIDGE_SETUP.md` — setup for talking to Raziel from your phone
- `HANDOFF.md` — internal notes on the codebase's structure and safety rules

## Troubleshooting

- **No sound on playback** — check Windows default output device; Piper's
  audio plays through whatever the default output is.
- **"No module named 'piper'" or voice file not found** — make sure you ran
  the download command from this exact project folder, since
  `PIPER_MODEL_PATH` looks for the `.onnx` file right next to the scripts.
- **First run is slow** — openWakeWord, faster-whisper, and the Ollama model
  all download files the first time they're used; cached after that.
- **Wake word never triggers** — lower `WAKE_WORD_THRESHOLD` in `config.py`,
  or double-check the model files downloaded successfully.
- **Wake word triggers randomly** — raise `WAKE_WORD_THRESHOLD`, or reduce
  background noise/TV/music near the mic.
- **It mishears you constantly** — run `python mic_test.py` to check your
  actual mic levels; also check Windows Sound settings for Microphone Boost
  set too high, which can make background noise look like speech.
- **"I'm having trouble reaching my local brain"** — Ollama isn't running.
  Check it's installed and try `ollama run qwen3:8b "hi"` in a terminal to
  confirm it responds.
- **Tool calls don't work reliably** — confirm `OLLAMA_ENABLE_THINKING =
  False` in `config.py` (thinking mode can interfere with clean tool-call
  output), and that you're running the full `qwen3:8b` model, not a smaller
  quantized variant.
- **Web search fails** — `ddgs` (DuckDuckGo search) doesn't need a key, but
  can occasionally rate-limit; wait a bit and try again.
