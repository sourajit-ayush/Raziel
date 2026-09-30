"""
Central config for the voice assistant.
Change values here instead of hunting through the other files.

THIS IS A TEMPLATE. Copy it to config.py (which is gitignored and stays only
on your own PC) and fill in your own secrets/paths there. Never put real
API keys, tokens, or phone numbers in THIS file - it's the one that gets
committed to GitHub.
"""

import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Wake word (openWakeWord) ------------------------------------------------
# No account or API key needed - fully open-source and offline.
# Built-in pretrained models include: "hey_jarvis", "alexa", "hey_mycroft",
# "hey_rhasspy". Pick one (must match a model name from openwakeword).
#
# CUSTOM "Raziel" MODEL: once Raziel Training produces Raziel.onnx, copy it into
# this folder and change the line below to:
#       WAKE_WORD_MODEL = "Raziel.onnx"
# A bare filename is resolved against this folder (works from any working
# directory). If the file isn't there yet, wake_word.py logs a warning and falls
# back to "hey_jarvis" instead of crashing. Also lower/raise the threshold below
# (start at 0.5 for a custom model): false wakes -> raise it, ignored -> lower it.
WAKE_WORD_MODEL = "hey_jarvis"     # set to "Raziel.onnx" once you have your own trained model
WAKE_WORD_FALLBACK = "hey_jarvis"

# How confident the model must be (0.0-1.0) before we treat it as a real
# detection. Lower = more sensitive (more false triggers). Higher = you have
# to say it more clearly/loudly.
WAKE_WORD_THRESHOLD = 0.5
WAKE_WORD_THRESHOLD_MUSIC = 0.42  # used while other apps play music (the song masks your voice); None = off
MEDIA_DUCK_ENABLED = True        # music playing: she waits for her name, then turns other apps down while she listens
MEDIA_DUCK_LEVEL = 0.2           # 0.2 = 20 % of each app's own volume
MEDIA_DUCK_MAX_SECONDS = 120     # safety: never keep them turned down longer than this

# --- Audio recording ---------------------------------------------------------
SAMPLE_RATE = 16000          # openWakeWord requires 16kHz
FRAME_LENGTH = 1280          # openWakeWord's expected chunk size (80ms @ 16kHz)
RECORD_MAX_SECONDS = 20      # hard cap so it never records forever
SILENCE_RMS_THRESHOLD = 200   # tune from mic_test.py to your own mic/room
SILENCE_DURATION = 1.2       # seconds of continuous silence that end recording
MIN_RECORD_SECONDS = 0.6     # ignore silence detection until at least this long

# --- Adaptive voice detection (wait_for_voice) -------------------------------
AMBIENT_CALIBRATION_SECONDS = 0.4   # how long to sample background noise before listening
VOICE_TRIGGER_MARGIN = 400          # how much louder than ambient noise real speech needs to be
MIN_SUSTAINED_VOICE_MS = 200        # sound must stay loud this long to count as real speech
SILENCE_STOP_MARGIN = 250           # how much louder than ambient counts as "still talking"

# --- Speech-to-text (faster-whisper) -----------------------------------------
WHISPER_MODEL_SIZE = "small"    # tiny/base/small/medium/large-v3
WHISPER_DEVICE = "cpu"          # set to "cuda" if you have a supported NVIDIA GPU
WHISPER_COMPUTE_TYPE = "int8"   # int8 is fastest on CPU
WHISPER_CPU_THREADS = 0         # 0 = automatic
WHISPER_TEMPERATURES = (0.0, 0.2, 0.4)   # retry passes on a doubtful clip (fewer = never slow)
WHISPER_PLAYLIST_HINTS = 12      # your Spotify playlist names as spelling hints (0 = off)
WHISPER_APP_HINTS = True         # ChatGPT, Gemini, Spotify, WhatsApp, YouTube as spelling hints
WHISPER_FOLDER_HINTS = 15        # short, unusual folder names (COA, DBMS...) as spelling hints (0 = off)

# Names/terms Whisper tends to mishear. Add anything you say often that keeps
# getting transcribed wrong - artists, friends' names, brand names, etc.
CUSTOM_VOCABULARY = [
    # "Some Name",
]

# --- Transcript guard (transcript_guard.py) ---------------------------------
GUARD_ENABLED = True
GUARD_MIN_PEAK_RMS = 150
GUARD_MIN_VOICED_MS = 200
GUARD_SEGMENT_NO_SPEECH = 0.6
GUARD_SEGMENT_LOGPROB = -1.0
GUARD_HARD_LOGPROB = -1.6
GUARD_MAX_COMPRESSION = 2.4
GUARD_REJECT_LOGPROB = -1.25
GUARD_CONF_MAX_NO_SPEECH = 0.4
GUARD_CONF_MIN_LOGPROB = -0.8
GUARD_CONF_MIN_VOICED_MS = 250

# --- Proactive speech (initiative.py) ---------------------------------------
INITIATIVE_ENABLED = True

# --- Text-to-speech (Piper - local neural TTS, free, offline) ---------------
# One-time setup: download the voice model from this project folder:
#   python -m piper.download_voices en_GB-cori-high
# Browse other voices: https://github.com/rhasspy/piper/blob/master/VOICES.md
PIPER_VOICE_NAME = "en_GB-cori-high"
PIPER_MODEL_PATH = os.path.join(_SCRIPT_DIR, f"{PIPER_VOICE_NAME}.onnx")
PIPER_CONFIG_PATH = os.path.join(_SCRIPT_DIR, f"{PIPER_VOICE_NAME}.onnx.json")
PIPER_LENGTH_SCALE = 1.15
TTS_VOLUME = 1.0     # 0.0 to 1.0

# --- Avatar ------------------------------------------------------------------
# Requires: pip install pywebview websockets
AVATAR_ENABLED = True
# "orb" = a living orb of glowing particles that turns into a dragon, sword, knight, butterfly,
#         planet, heart or galaxy when asked ("become a dragon", "roar", "back to the orb") and
#         dances to music playing on the PC. "vrm" = the 3D character below.
AVATAR_STYLE = "orb"
AVATAR_REMEMBER_PLACE = True     # reopen where you dragged her, at the size you gave her (Ctrl + wheel)
AVATAR_START_X = None            # her spot when nothing is saved; None = right side of the main screen
AVATAR_START_Y = None
AVATAR_ORB_WINDOW_WIDTH = 420
AVATAR_ORB_WINDOW_HEIGHT = 560
AVATAR_ORB_LOOK = "sphere"        # "sphere" = inside a glass crystal ball, "float" = bare particles on the desktop
AVATAR_PARTICLES = 24000         # fewer (e.g. 14000) = lighter on the CPU
AVATAR_MUSIC_REACT = True        # dance to the PC's music (needs: pip install PyAudioWPatch)
AVATAR_MUSIC_GAIN_DB = 0.0       # raise (e.g. 6) if she barely reacts to quiet music

# --- Her universe (universes.py) ---------------------------------------------------------------
# Raziel is the Nexus in the middle; six beings each look after one part of what she can do. Say a
# name first: "Melody, play Kalyani", "Hermes, message Fazal", "Athena, explain paging", "Atlas,
# open my COA folder", "Chronos, remind me at 6", "Sentinel, lock my laptop". "Show me the
# universe" opens the map big; "who lives in your universe?" introduces them.
UNIVERSES_ENABLED = True         # False = just Raziel, as before
UNIVERSE_HOME = "universe"       # her resting look: "universe" (the galaxy map) or "orb"
UNIVERSE_BIG_SECONDS = 40        # "show me the universe" stays big this long ("close the universe" ends it)
# UNIVERSE_NAMES = {"music": "Melody", "messages": "Hermes", "knowledge": "Athena", "forge": "Atlas",
#                   "time": "Chronos", "shield": "Sentinel"}   # rename any of them
AVATAR_MODEL_PATH = os.path.join(_SCRIPT_DIR, "Raziel-1.vrm")
AVATAR_HTML_PATH = os.path.join(_SCRIPT_DIR, "avatar.html")
AVATAR_WINDOW_TITLE = "Raziel"
AVATAR_WINDOW_WIDTH = 480
AVATAR_WINDOW_HEIGHT = 640
AVATAR_WS_PORT = 8765
AVATAR_TRANSPARENCY = "native"   # "native" or "none"

# --- Barge-in (interrupting the assistant mid-sentence) ---------------------
BARGE_IN_GRACE_PERIOD = 0.6
BARGE_IN_RMS_THRESHOLD = 4500
BARGE_IN_CONSECUTIVE_FRAMES = 3

# --- Misc ---------------------------------------------------------------------
TEMP_AUDIO_PATH = "temp_recording.wav"
LOG_FILE = "assistant.log"

# --- LLM brain (Ollama - local, free, no API key) ----------------------------
# Requires Ollama installed and running locally: https://ollama.com/download
# Then pull a model once: `ollama pull qwen3:8b`
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"
OLLAMA_ENABLE_THINKING = False

# --- Speed tuning --------------------------------------------------------
FAST_TOOL_REPLIES = True
SLOW_PATH_TOOLS = {"recall", "web_search"}
MAX_HISTORY_MESSAGES = 12
OLLAMA_MAX_TOKENS = 160
OLLAMA_NUM_CTX = 10240
LLM_WARMUP = True
LLM_STREAMING = True
OLLAMA_KEEP_ALIVE = "4h"

# --- Conversation session (Phase 2) ------------------------------------------
SLEEP_PHRASES = ["goodbye", "go to sleep", "go back to sleep", "अलविदा", "गुडबाय", "गुड बाय", "सो जाओ", "सो जाइए"]

GENERATED_FILES_DIR = "generated_files"
SCREENSHOTS_DIR = "screenshots"

# --- Memory (Phase 4) -----------------------------------------------------
MEMORY_DB_PATH = os.path.join(_SCRIPT_DIR, "memory.db")
MAX_REMEMBERED_FACTS_IN_PROMPT = 15

# --- WhatsApp messaging -------------------------------------------------
# Name (lowercase) -> phone number with country code, no spaces or dashes,
# e.g. "+919876543210". We can't read your phone's contact list directly,
# so add people you want to message by name via voice here.
CONTACTS = {
    # "fazal": "+91XXXXXXXXXX",
}

WHATSAPP_SEND_DELAY = 12
WHATSAPP_MODE = "app"   # "app" | "web" | "auto"
WHATSAPP_APP_DELAY = 6
WHATSAPP_APP_TIMEOUT = 20

# --- Confirmation step for risky actions (Phase 3) ---------------------------
CONFIRM_RISKY_ACTIONS = True
CONFIRM_ACTIONS = ("send_whatsapp_message", "overwrite_file", "shutdown_pc", "restart_pc", "close_app",
                   "clear_notes", "clear_list", "add_calendar_event", "send_phone_sms")
CONFIRM_TIMEOUT_SECONDS = 60
CONFIRM_MAX_REPROMPTS = 1
CONFIRM_KEEP_BACKUP_ON_OVERWRITE = True

# --- Email (Phase 3) ----------------------------------------------------------
EMAIL_DRAFT_MODE = "gmail"   # "gmail" | "outlook" | "mailto"
EMAIL_MAX_LINK_CHARS = 2000
EMAIL_GMAIL_ACCOUNT = ""
EMAIL_CONTACTS = {
    # "mom": "mom@example.com",
}

# --- Spotify playback -----------------------------------------------------
# Requires a Spotify Premium account and a free Spotify Developer app.
# Create one at https://developer.spotify.com/dashboard - leave blank to
# disable the play_music tool.
SPOTIFY_CLIENT_ID = ""
SPOTIFY_CLIENT_SECRET = ""
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"


# ===== Raziel v3 features ==========================================================
# Everything below is OPTIONAL: each setting has a sensible default inside the code, so
# deleting this whole block gives the defaults. Change values here, then restart Raziel.

# --- Hindi (understand AND speak) ---------------------------------------------------
HINDI_ENABLED = True
HINDI_COMMANDS_ENABLED = True
WHISPER_LANGUAGE = "auto"            # "auto" | "en" | "hi"
HINDI_WHISPER_MODEL = "medium"
HINDI_MIN_PROBABILITY = 0.6
HINDI_SWITCH_PROBABILITY = 0.75     # ...and to switch from English to Hindi (higher = fewer English
                                    # sentences sent to the slow Hindi model by mistake)
HINDI_VOICE_NAME = "hi_IN-priyamvada-medium"
HINDI_MODEL_PATH = os.path.join(_SCRIPT_DIR, f"{HINDI_VOICE_NAME}.onnx")
HINDI_CONFIG_PATH = os.path.join(_SCRIPT_DIR, f"{HINDI_VOICE_NAME}.onnx.json")
HINDI_LENGTH_SCALE = 1.05

# --- Google: live news, weather and "ask anything" ----------------------------------
# NEWS and WEATHER need no key at all. For "ask me anything" answers straight from
# Google Search, paste a free Gemini API key from https://aistudio.google.com/apikey
# Leave "" to keep every question local (DuckDuckGo search + the local model instead).
GEMINI_API_KEY = ""
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"]
DEFAULT_LOCATION = ""                # e.g. "Kolkata" - empty = guessed from your IP
NEWS_COUNT = 5
NEWS_REGION = "IN"
NEWS_LANG_EN = "en-IN"
NEWS_SPEAK_SOURCE = False
WEBINFO_TIMEOUT = 8

# --- Google Calendar + Gmail (read-only mail) ----------------------------------------
# One-time setup: see SETUP_V3.md, then run `py google_setup.py` once and sign in.
GOOGLE_ENABLED = True
GOOGLE_CREDENTIALS_PATH = ""         # "" = google_credentials.json next to this file
GOOGLE_TOKEN_PATH = ""               # "" = google_token.json next to this file
CALENDAR_DEFAULT_MINUTES = 60
TIMEZONE = ""                        # "" = this PC's time zone, or e.g. "Asia/Kolkata"

# --- Timers, reminders, alarms -------------------------------------------------------
REMINDERS_ENABLED = True
REMINDER_BEEP = True
REMINDER_LATE_SECONDS = 120
TIMER_MAX_SECONDS = 172800

# --- PC controls: volume, brightness, lock, sleep, close app, shutdown ---------------
SYSTEM_CONTROL_ENABLED = True
VOLUME_STEP = 10
BRIGHTNESS_STEP = 10
SYSTEM_ACTION_DELAY_SECONDS = 2.0
SHUTDOWN_DELAY_SECONDS = 30

# --- Dictation ("start typing") -------------------------------------------------------
DICTATION_IDLE_TIMEOUT = 300

# --- Morning briefing ------------------------------------------------------------------
BRIEFING_ENABLED = True
BRIEFING_AUTO = True
BRIEFING_INCLUDE_NEWS = True

# --- "What's on my screen?" (local vision model, nothing leaves this PC) ---------------
VISION_ENABLED = True
VISION_MODEL = "qwen3.5:4b"
VISION_FALLBACK_MODELS = ("qwen3-vl:4b", "qwen2.5vl:3b", "gemma3:4b")
VISION_KEEP_ALIVE = 0
VISION_TIMEOUT = 120
VISION_MAX_WIDTH = 1280

# --- Phone bridge (Phase 6): talk to Raziel from your phone, and let her trigger actions on it ----
# See PHONE_BRIDGE_SETUP.md for the full setup: Tailscale (phone -> PC reachability) +
# Tasker (phone -> PC: send a command, forward a notification) + Join (PC -> phone: ring
# it, send an SMS, open an app). Both directions stay OFF until you fill these in.
PHONE_BRIDGE_ENABLED = True
PHONE_BRIDGE_PORT = 8765
PHONE_BRIDGE_TOKEN = ""              # REQUIRED to start the server - generate with:
                                      #   py -c "import secrets; print(secrets.token_hex(32))"
JOIN_API_KEY = ""
JOIN_DEVICE_ID = ""

# --- Typing into AI chat websites (ChatGPT, Gemini, Claude, Perplexity, Copilot) ------------------
# "open chatgpt and type/ask <question>" opens the site (or its installed app, same as "open X")
# and types the question into its message box once the window actually has the keyboard focus -
# see tools.type_into_ai_site(). If it keeps saying the site "never came to the front", raise
# AI_CHAT_FOCUS_TIMEOUT (a slow PC/browser can need longer); if it types before the page is ready,
# raise AI_CHAT_TYPE_DELAY.
AI_CHAT_FOCUS_TIMEOUT = 15   # seconds to wait for the site to get keyboard focus before giving up
AI_CHAT_TYPE_DELAY = 3       # extra seconds after focus, for the page to finish loading its message box
AI_CHAT_READ_ANSWER = True   # after typing, wait for the site's answer and read it out (ai_answers.py)
AI_CHAT_READ_FULL_WORDS = 70 # answers up to this many words are read in full; longer ones get a short
                             # summary from the local model, then "say 'read it all'" for the whole thing
AI_CHAT_ANSWER_TIMEOUT = 120 # seconds to wait for the answer to finish before giving up
AI_CHAT_VISION_FALLBACK = True  # if the page text can't be read, describe the screen instead (needs VISION_*)
AI_CHAT_VISION_WAIT = 25     # ...after waiting this long for the answer
AI_CHAT_QUESTION_WAIT = 45      # 'open Gemini and type' with no question: the next sentence is it, for this long
AI_CHAT_READ_ON_SEND = True     # 'send it' / 'press enter' in ChatGPT or Gemini also reads the answer

# --- Several commands in one sentence (multi_command.py) --------------------------------------------
# "open Spotify, play lo-fi and set the volume to 30" is split into its parts, and each part goes
# through the same exact commands as when said on its own. Parts nothing recognises go to the
# model together. Things that take the rest of the sentence ("remind me to call mum and dad",
# "type hello and goodbye") are never split.
MULTI_COMMAND_ENABLED = True

# --- Voice ID: risky actions only for your voice (voice_id.py) ---------------------------------------
# Learns your voice from the recordings in my_voice/ (the wake-word training clips) the first time,
# then checks every command while Whisper is transcribing it. Only the actions in VOICE_ID_GUARDED
# need your voice: shutting down, sending messages, deleting notes... Anyone can still ask the
# time, the weather or play music. Phone commands always count as you (they carry your token).
# Needs models/voice_id_resnet34.onnx (install_new_features.bat downloads it). Every check is
# logged in assistant.log as "Voice ID: 0.63 (...)" - you are usually 0.5-0.8, other people
# below 0.35. If she refuses you too often, lower VOICE_ID_THRESHOLD a little (e.g. 0.40); if a
# TV voice gets through, raise it. After changing your mic, delete voice_profile.npz to relearn.
OWNER_NAME = "Ayush"         # used in "Sorry, I only do that for Ayush."
VOICE_ID_ENABLED = True
VOICE_ID_THRESHOLD = 0.45    # this similarity or more = you
VOICE_ID_REJECT_BELOW = 0.33 # below this = someone else; in between = "say it again, closer to the mic"
VOICE_ID_MIN_SPEECH = 0.5    # seconds of speech needed to judge at all
VOICE_ID_ADAPT = True        # clear matches (0.6+) slowly update your voice profile (new mic, a cold...)
VOICE_ID_UNSURE_DROP_SPEECH = 2.5  # a 'not sure' this many seconds long (or longer) is someone else's
                                   # talking and is ignored (unless you just called her); 'someone
                                   # else' is always ignored. Short commands are kept.
# VOICE_ID_LENIENT = ("lock_pc", "close_app")  # guarded actions a 'not sure' voice may still do
# VOICE_ID_GUARDED = ("shutdown_pc", "restart_pc", ...)   # to change the list; see voice_id.DEFAULT_GUARDED

# --- Game and call mode (quiet_mode.py) --------------------------------------------------------------
# A full-screen game (or video) running: she never speaks on her own - timers, reminders and
# notifications become Windows pop-ups - but still answers when you call her. Another app using
# the microphone (Zoom, Discord, Teams, Meet in a browser...): fully silent, the wake word is off
# too, until the call ends. "Do not disturb" / "quiet mode on" does the same by hand; "you can
# talk again" lets her speak during a game for QUIET_OVERRIDE_SECONDS.
QUIET_MODE_ENABLED = True
QUIET_GAME_MODE = True
QUIET_CALL_MODE = True
QUIET_CALL_BLOCKS_WAKE_WORD = True
QUIET_POPUP_TOAST = True     # Windows pop-ups (a card under the orb is shown too)
QUIET_OVERRIDE_SECONDS = 1800
QUIET_MIC_IGNORE = ()        # apps that use the mic but aren't calls, e.g. ("obs64.exe", "Voicemeeter")

# --- Mini cards under the orb (cards.py, orb avatar only) --------------------------------------------
# Running timers with a live countdown, the Spotify song with its album art, and today's weather
# while she reads the morning briefing. The orb glides up to make room.
CARDS_ENABLED = True
CARDS_TIMERS = True
CARDS_MUSIC = True           # album art needs the Spotify login Raziel already uses (.spotify_cache)
CARDS_WEATHER = True
CARDS_SPOTIFY_POLL_SECONDS = 5

# --- Ask your own files (file_index.py) -------------------------------------------------------------
# "find the PDF about the bank loan", "what does my resume say about Python?", "search my files
# for ...", then "open it". Reads Documents, Downloads and Desktop in the background with a small
# local model through Ollama - fully offline, kept in file_index.db. One-time:
# `ollama pull nomic-embed-text` (about 270 MB; install_new_features.bat does it). Without it
# the search still works by words (names and text), just not by meaning.
FILE_INDEX_ENABLED = True
FILE_INDEX_FOLDERS = []      # empty = Documents, Downloads, Desktop; or e.g. ["Documents", r"D:\Work"]
FILE_INDEX_EXCLUDE = []      # folder names or full paths to skip, e.g. ["Old stuff", r"D:\Work\secret"]
FILE_INDEX_EMBED_MODEL = "nomic-embed-text"
FILE_INDEX_EMBED_ON_CPU = True  # keeps the graphics card for the chat model; False = faster first
                                # indexing if your GPU has memory to spare (12 GB+)
FILE_INDEX_MIN_SCORE = 0.5   # how close a match must be (the scores are in assistant.log)
FILE_INDEX_MAX_FILE_MB = 30  # bigger files are found by name only
FILE_INDEX_RESCAN_MINUTES = 20
FILE_INDEX_START_DELAY = 60  # seconds after start-up before reading begins

# --- Hand gestures in front of the webcam (gestures.py) ---------------------------------------------
# Open palm (hold it still a moment) = play / pause. Swipe right / left = next / previous song, or
# next / previous slide when PowerPoint, a PDF or Google Slides is in front. Swipe up / down = volume
# (down while she's talking = skip ahead). Turn a fist like a knob = volume, two fingers up = screen
# brightness. Thumbs up while a song plays = Spotify Liked Songs. Pinch (thumb + first finger, other
# fingers open) and move = drag her window; pinch with both hands and pull apart / together = bigger
# / smaller. "Stop watching" / "watch my hands" switch it off / on (remembered); "what gestures do
# you know?". One-time setup: install_gestures.bat. Nothing is recorded; the camera light shows when
# she watches. To see what she sees: venv\Scripts\python.exe gestures.py --check
GESTURES_ENABLED = True
GESTURES_CAMERA = 0              # which webcam (0 = the first one)
GESTURES_PAUSE_IN_CALLS = True   # let go of the camera while Zoom / Discord / Teams is in a call
GESTURES_MUSIC = True            # palm, swipes and volume
GESTURES_KNOB = True             # fist / two fingers turned like a knob
GESTURES_LIKE = True             # thumbs up -> Liked Songs (asks Spotify for one more permission once)
GESTURES_SLIDES = True           # swipes turn slides / PDF pages when those are in front
GESTURES_SKIP = True             # swipe down while she talks = skip ahead
GESTURES_WINDOW = True           # pinch to move / resize her
GESTURES_HOLD_SECONDS = 1.0      # how long to hold the open palm still (raise if music pauses by accident)
GESTURES_SWIPE_DISTANCE = 0.18   # how far a swipe goes, as a share of the camera's width (0.22 before: a near hand left the picture first)
GESTURES_KNOB_DEGREES = 12       # turning this much = one step
GESTURES_VOLUME_STEP = 2         # % per knob step
GESTURES_BRIGHTNESS_STEP = 4     # % per knob step
GESTURES_RAISED = 0.8            # a palm counts only above this line (share of the picture's height; 1 = anywhere)
GESTURES_UPRIGHT_DEGREES = 35    # ...and pointing up, leaning at most this much
GESTURES_DEBUG_LOG = True        # keep the hand points around each gesture in gestures_debug.jsonl (numbers only)
