# Voice Assistant — Phase 1 + Phase 2

Wake word → conversation with a local LLM brain that can open apps, save
files, and search the web — until you say "goodbye" or "go to sleep".

## 1. Prerequisites

- Windows 10/11
- Python 3.11 or 3.12 (not 3.13 yet — some audio libs lag behind new releases)
- A working microphone and speaker
- No account or API key needed anywhere — wake word (openWakeWord), speech
  recognition (faster-whisper), and the LLM brain (Ollama) all run fully
  offline and free
- [Ollama](https://ollama.com/download) installed (for Phase 2's brain)

## 2. Set up the project

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

## 3. Download the wake word model files (one-time)

openWakeWord ships pretrained models but needs to fetch them once:

```powershell
python -c "import openwakeword; openwakeword.utils.download_models()"
```

This downloads a small set of `.onnx` model files (a few MB total) and
caches them locally — no account, no key, nothing to sign up for.

## 4. Set up Ollama (Phase 2's brain)

1. Install Ollama from https://ollama.com/download and run the installer
2. Ollama runs as a background service after install — you don't need to
   manually start it each time (it starts with Windows)
3. Pull the model this project uses (one-time, downloads ~2GB):

```powershell
ollama pull qwen3:8b
```

4. Quick sanity check it's working:

```powershell
ollama run qwen3:8b "say hello in 5 words"
```

If that replies, Ollama is ready.

## 5. Download the voice (Piper - free, local, neural TTS)

```powershell
python -m piper.download_voices en_GB-cori-high
```

This downloads two small files (`en_GB-cori-high.onnx` and `.onnx.json`)
into this project folder - no account, no key. To try a different voice,
browse https://github.com/rhasspy/piper/blob/master/VOICES.md, change
`PIPER_VOICE_NAME` in `config.py` to match, then re-run the download
command with the new name.

## 6. Set up Spotify playback (optional)

Requires a **Spotify Premium** account - the API can't control playback on
a free account.

1. Go to https://developer.spotify.com/dashboard and log in with your
   Spotify account
2. Click **Create app**. Fill in any name/description you want
3. For **Redirect URI**, enter exactly: `http://127.0.0.1:8888/callback`
   (Spotify no longer accepts `localhost` in redirect URIs as of late 2025 -
   it must be the literal IP `127.0.0.1`)
4. Save the app, then open it and copy the **Client ID** and **Client
   Secret**
5. Paste them into `config.py`:
   ```python
   SPOTIFY_CLIENT_ID = "your-client-id-here"
   SPOTIFY_CLIENT_SECRET = "your-client-secret-here"
   ```
6. The first time you ask it to play music, a browser window will open
   asking you to log in and authorize the app - approve it. This creates a
   local `.spotify_cache` file so you won't need to do this again
7. Make sure you're logged into the **same Spotify account** in the
   desktop app that you used to create the developer app

If you skip this, `play_music` will just tell you it isn't set up yet -
everything else in the project still works fine.

## 7. Run it

```powershell
venv\Scripts\activate
python main.py
```

You should see:
```
Voice assistant ready.
Listening for wake word 'hey_jarvis'...
```

Say **"Hey Jarvis"**. It'll say "I'm listening" — now just talk normally,
no need to repeat the wake word between turns. Say **"goodbye"** or
**"go to sleep"** when you're done, and it returns to listening for the
wake word.

Try things like:
- "What's the capital of Japan?" (plain question, no tool needed)
- "Open notepad" (uses the `open_app` tool)
- "Search the web for today's weather in Kadapa" (uses `web_search`)
- "Write a file called todo.txt with a shopping list" (uses `write_file`,
  saved to the `generated_files` folder)
- "Take a screenshot" (uses `take_screenshot`, saved to the `screenshots`
  folder)
- "Open my resume.pdf" (uses `open_file`, searches Desktop/Downloads/etc.)
- "Open Google and search for the weather in Tokyo" (uses
  `open_website_search`, actually performs the search)
- "Send a WhatsApp message to Fazal saying I'll be late" (uses
  `send_whatsapp_message` - requires CONTACTS set up in `config.py`)
- "Play Blinding Lights by The Weeknd" (uses `play_music` - requires
  Spotify setup in step 5 above)
- "Remember that I have an exam on Friday" (uses `remember` - persists
  across sleep/wake and even restarting the script, via `memory.db`)
- "What do you remember about my exam?" or "What did we talk about
  yesterday?" (uses `recall` - searches saved facts and past conversations)
- "Forget about the exam" (uses `forget`)

Press `Ctrl+C` to stop.

## 8. Tuning

All the knobs live in `config.py`:

- `WAKE_WORD_MODEL` — built-in options include `hey_jarvis`, `alexa`,
  `hey_mycroft`, `hey_rhasspy`
- `WAKE_WORD_THRESHOLD` — lower it if it's not triggering when you say the
  wake word; raise it if it triggers randomly
- `SILENCE_RMS_THRESHOLD` — lower this if it cuts you off mid-sentence,
  raise it if it never stops recording in a noisy room. Run `python
  mic_test.py` to measure your actual mic levels instead of guessing.
- `WHISPER_MODEL_SIZE` — `tiny`/`base` are faster but less accurate,
  `small`/`medium` more accurate but slower on CPU
- `PIPER_VOICE_NAME` — swap voices anytime; re-run the download command
  from step 5 with the new name after changing this
- `PIPER_LENGTH_SCALE` — speaking pace. `1.0` is the voice's natural speed,
  higher is slower/more deliberate, lower is faster
- `OLLAMA_MODEL` — `qwen3:8b` is the default: better tool-calling than
  smaller models, but needs decent hardware (8GB+ RAM, more with a GPU) and
  will be slower per response on CPU-only machines. If it's too slow on
  your machine, try a lighter model like `llama3.2` (3B) —
  `ollama pull llama3.2` first, then update this setting
- `OLLAMA_ENABLE_THINKING` — Qwen3 models reason step-by-step by default
  before answering, which adds latency. This is set to `False` for faster,
  more direct voice replies. Set to `True` if you want it to reason more
  carefully on complex requests, at the cost of speed
- `SLEEP_PHRASES` — the phrases that end a conversation session
- `MAX_CONSECUTIVE_SILENCE` — how many silent turns in a row before it
  auto-returns to sleep (safety net for walking away mid-conversation)

## 9. Adding more apps to `open_app`

Edit the `APP_COMMANDS` dictionary at the top of `tools.py` — add a friendly
name mapped to the actual executable name, e.g.:

```python
"discord": "discord.exe",
```

## Troubleshooting

- **No sound on playback** — check Windows default output device; Piper's
  audio plays through whatever pyaudio picks as the default output.
- **"No module named 'piper'" or voice file not found** — make sure you ran
  the download command in step 5 from this exact project folder, since
  `PIPER_MODEL_PATH` looks for the `.onnx` file right next to the scripts.
- **First run is slow** — openWakeWord, faster-whisper, and the Ollama model
  all download files the first time they're used; cached after that.
- **Wake word never triggers** — lower `WAKE_WORD_THRESHOLD` in `config.py`,
  or double-check step 3 ran successfully (model files must be downloaded).
- **Wake word triggers randomly** — raise `WAKE_WORD_THRESHOLD`, or reduce
  background noise/TV/music near the mic.
- **It mishears you constantly** — run `python mic_test.py` to check your
  actual mic levels; also check Windows Sound settings for Microphone Boost
  set too high, which can make background noise look like speech.
- **"I'm having trouble reaching my local brain"** — Ollama isn't running.
  Check it's installed and try `ollama run qwen3:8b "hi"` in a terminal to
  confirm it responds.
- **Tool calls don't work reliably** (e.g. it just talks about opening
  Notepad instead of actually doing it) — this can happen with local models.
  Make sure `OLLAMA_ENABLE_THINKING = False` in `config.py` (thinking mode
  can interfere with clean tool-call output), and confirm you're running
  the full `qwen3:8b` model, not a smaller quantized variant.
- **Responses are slow** — `qwen3:8b` is a bigger model; if your machine
  doesn't have a GPU, try `llama3.2` in `config.py` (`ollama pull llama3.2`
  first) for noticeably faster replies at some cost to tool-calling accuracy.
- **Web search fails** — `ddgs` (DuckDuckGo search) doesn't need a key, but
  can occasionally rate-limit; wait a bit and try again.

