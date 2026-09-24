# Raziel v3 - setup guide

Everything below is **optional** except step 1. Raziel starts and works without any key; each feature that needs a key or a
download tells you so out loud the first time you use it.

## 1. Install the new packages (2 minutes)

Open a terminal in the Voice Agent folder, activate the venv you always use, then:

```
py -m pip install -r requirements.txt
```

New in `requirements.txt`: `pycaw` + `comtypes` (volume), `screen-brightness-control` (brightness), `psutil` (close app).
If one of them fails to install, skip it - only that one feature (volume / brightness / close app) says "I can't do that
on this computer"; everything else is unaffected.

## 2. Hindi - nothing to do, two automatic downloads

* **Hindi voice** (Piper `hi_IN-priyamvada-medium`, ~60 MB): downloaded the first time Raziel starts, in the background.
  Until it is ready she speaks Hindi in romanised letters with the English voice, so she is never silent.
* **Hindi listening model** (Whisper `medium`, ~1.5 GB): downloaded and loaded in the background at the first start.
  On a slow PC set `HINDI_WHISPER_MODEL = "small"` in `config.py` (about 3x faster, a little less accurate).
* Whisper decides **per sentence** whether you spoke English or Hindi. If English sentences ever come out as Hindi,
  raise `HINDI_MIN_PROBABILITY` (0.6 -> 0.7). To switch Hindi off completely: `HINDI_ENABLED = False`.

Try: "नमस्ते", "नोटपैड खोलो", "समय क्या हुआ है", "10 मिनट का टाइमर लगाओ", "मौसम कैसा है", "ताज़ा ख़बरें सुनाओ",
"फ़ज़ल को व्हाट्सएप पर संदेश भेजो कि मैं देर से आऊँगा" (she reads it back in Hindi and waits for "हाँ" / "नहीं").

## 3. Google search for "anything I want to know" (free Gemini key, 1 minute)

**News and weather need no key** - every "what's the news?" fetches Google News fresh, every time.
For open questions ("who won the match last night?", "what's the price of gold?", "google the population of India")
she asks Gemini with Google Search grounding, which needs a free key:

1. Open https://aistudio.google.com/apikey and sign in with your Google account.
2. Press **Create API key**, copy it.
3. In `config.py` paste it: `GEMINI_API_KEY = "AIza..."` (keep the quotes), restart Raziel.

Privacy: on Google's free tier, the questions you ask this way may be used by Google to improve its products. Your
microphone audio, files, mail and everything else never leave the PC. With no key, she uses DuckDuckGo + the local
model instead (slower, less accurate for live facts).

## 4. Google Calendar + Gmail (optional, ~5 minutes once)

Calendar view/add and read-only Gmail need a free Google "OAuth client". Without it, "add ... to my calendar" still works
by opening a pre-filled Google Calendar page for you to press Save; only reading needs the sign-in.

1. https://console.cloud.google.com/ -> create a project (name it Raziel).
2. **APIs & Services -> Library**: enable **Google Calendar API** and **Gmail API**.
3. **Google Auth platform** (OAuth consent screen): app name Raziel, audience **External**. Either press **Publish app**
   (sign-in never expires; you will see an "unverified app" warning, click *Advanced -> Go to Raziel*), or leave it in
   *Testing*, add your own Gmail as a test user and re-run step 5 every 7 days.
4. **Clients -> Create client -> Desktop app -> Download JSON**. Save it in the Voice Agent folder as
   `google_credentials.json`.
5. Run once: `py google_setup.py` and sign in in the browser. `py google_setup.py --check` tests it later.

She can **read** mail (sender + subject of the newest three, never the body sent anywhere) and **add** events only after
you say yes. She can never send, change or delete mail or events.

## 5. "What's on my screen?" (optional, one download, ~3 GB)

```
ollama pull qwen3.5:4b
```

Runs entirely on this PC. The model is loaded only for the question and unloaded straight after so it never competes with
the main model for memory. If that tag is not available for you, pull one of: `qwen3-vl:4b`, `qwen2.5vl:3b`, `gemma3:4b`
(she tries them automatically).

## 6. Things worth knowing

* **Shutdown / restart**: she asks "yes or no", then gives you 30 seconds ("say cancel the shutdown"). Windows **force-closes
  open apps** when that time runs out - unsaved work is lost. Change `SHUTDOWN_DELAY_SECONDS` if you want longer.
* **Sleep**: "put the computer to sleep" uses Windows sleep; on PCs with Modern Standby / hibernation enabled it may hibernate.
* **Morning briefing**: the first time you wake her each day (time, weather, calendar, unread mail, three headlines).
  It can take a few seconds; `BRIEFING_AUTO = False` turns the automatic one off ("give me my briefing" still works).
* **Timers / reminders** ring even when she is asleep (three beeps, then her voice). They wait while you are talking to her.
* **Dictation**: "start typing" types every sentence into the window you are in (Hindi too); "stop typing" ends it. She keeps
  typing even if you say "goodbye" - say "stop typing" first.
* **Everything is in `config.py`** in the block "Raziel v3 features" (each line is commented). Delete the block to get the
  built-in defaults.
* A copy of every file that was replaced is in `_backup_before_phase5features` (restore = copy the files back).
* This version was tested with automated checks (thousands of them) but **not yet on your real PC**: if any command
  misbehaves, look at `assistant.log` for the exact error line - that's the fastest way to track it down.
