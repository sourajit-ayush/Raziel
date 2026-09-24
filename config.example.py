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

# --- Avatar (3D VRM face) --------------------------------------------------
# Requires: pip install pywebview websockets
AVATAR_ENABLED = True
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
SLEEP_PHRASES = ["goodbye", "go to sleep", "अलविदा", "गुडबाय", "गुड बाय", "सो जाओ", "सो जाइए"]

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
