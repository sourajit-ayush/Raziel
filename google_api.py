"""
google_api.py - Google Calendar and Gmail (read-only) for Raziel, English and Hindi.

No Google client libraries: the OAuth "installed app" loopback flow is implemented by hand (stdlib
http.server + PKCE) and the REST APIs are called through netutil with a Bearer token, so nothing has to
be pip-installed. Set it up once with `py google_setup.py` (see that file for the Google Cloud steps).

Voice features (all decided in code, no LLM - `try_handle(transcript)`):

Calendar view  "what's on my calendar today / tomorrow / this week / on Friday", "what's my next meeting",
               "do I have any meetings today", "what do I have on Friday".
               Hindi: "आज मेरे कैलेंडर में क्या है", "कल मेरी कौन सी मीटिंग है", "मेरी अगली मीटिंग कब है".
Calendar add   "add dentist to my calendar tomorrow at 5 pm", "schedule a meeting with Rahul on Monday at 3 pm
               for an hour", "create an event called lunch at 1". Hindi: "कल शाम 5 बजे कैलेंडर में डेंटिस्ट जोड़ो".
               Connected: she asks "Add dentist to your calendar tomorrow at 5 PM? Say yes or no." (confirmation
               kind `add_calendar_event`) and inserts only after the yes. Not connected: she opens a pre-filled
               Google Calendar page (no setup needed) and you press Save.
Gmail          "do I have any new emails", "how many unread emails do I have", "read my latest email",
               "do I have an email from Rahul". Hindi: "मेरे नए ईमेल पढ़ो", "कितने अनरीड ईमेल हैं".
               READ ONLY (scope gmail.readonly): she says the unread count and sender - subject of the newest
               three. Only message metadata + Gmail's own snippet is fetched; email content is never sent to any
               AI service and never written to the log.

Deliberately NOT handled (other modules / the LLM): "open Gmail", "open the calendar app", "compose an email
to X", "send an email", "read my WhatsApp messages", "remind me ...".

Hooks (never raise, "" when not connected or nothing to say): calendar_briefing_line(), email_briefing_line().
Tool functions: calendar_action(action, when, title), email_action(action, sender).
Time phrases ("tomorrow at 5 pm", "कल शाम 5 बजे") are understood by timeparse.py (imported lazily, one seam:
_timeparse()); the day ranges of the calendar VIEW ("today", "tomorrow", "this week", "Friday") are handled here.

Settings (config.py, all optional): GOOGLE_CREDENTIALS_PATH, GOOGLE_TOKEN_PATH, TIMEZONE, CALENDAR_DEFAULT_MINUTES,
GOOGLE_ENABLED. Secrets (client secret, tokens) are never logged.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

import config
import confirmation
import lang
import netutil

logger = logging.getLogger("voice_assistant")

_HERE = os.path.dirname(os.path.abspath(__file__))

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.events"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = (SCOPE_CALENDAR, SCOPE_GMAIL)
CAL_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CONFIRM_KIND = "add_calendar_event"

MAX_SPOKEN_EVENTS = 5
MAX_SPOKEN_EMAILS = 3
SUBJECT_MAX_CHARS = 70
SNIPPET_MAX_CHARS = 110
NEXT_EVENT_DAYS = 30

# ------------------------------------------------------------------ strings (English / Hindi)

_T: Dict[str, Dict[str, str]] = {
    # --- problems (spoken)
    "not_setup": {"en": "Please run google_setup.py once so I can connect to your Google account.",
                  "hi": "मुझे आपके Google अकाउंट से जोड़ने के लिए google_setup.py एक बार चला दीजिए।"},
    "bad_credentials": {"en": "The google_credentials.json file looks wrong. Please download it again from Google Cloud Console.",
                        "hi": "google_credentials.json फ़ाइल सही नहीं लग रही। कृपया उसे Google Cloud Console से दोबारा डाउनलोड कीजिए।"},
    "bad_client_type": {"en": "That credentials file is for a web application. Please create a Desktop app client instead and download it again.",
                        "hi": "यह क्रेडेंशियल फ़ाइल वेब ऐप की है। कृपया Desktop app क्लाइंट बनाकर उसे दोबारा डाउनलोड कीजिए।"},
    "expired": {"en": "Google sign-in expired. Please run google_setup.py again.",
                "hi": "Google का साइन-इन एक्सपायर हो गया है। कृपया google_setup.py फिर से चलाइए।"},
    "net": {"en": "I couldn't reach Google right now.",
            "hi": "अभी मैं Google तक नहीं पहुँच पा रही हूँ।"},
    "api_off": {"en": "Google says the Calendar or Gmail API isn't turned on for your project. Please enable both in Google Cloud Console.",
                "hi": "Google कह रहा है कि आपके प्रोजेक्ट में Calendar या Gmail API चालू नहीं है। कृपया Google Cloud Console में दोनों चालू कीजिए।"},
    "denied": {"en": "Google didn't allow that. Please run google_setup.py again and allow both permissions.",
               "hi": "Google ने इसकी इजाज़त नहीं दी। कृपया google_setup.py फिर से चलाकर दोनों अनुमतियाँ दीजिए।"},
    "sorry": {"en": "Sorry, something went wrong with Google.",
              "hi": "माफ़ कीजिए, Google के साथ कुछ गड़बड़ हो गई।"},
    # --- sign-in flow (google_setup.py)
    "auth_denied": {"en": "You didn't allow access, so nothing was connected.",
                    "hi": "आपने अनुमति नहीं दी, इसलिए कुछ भी जुड़ा नहीं।"},
    "auth_state": {"en": "The sign-in answer didn't match this request, so I ignored it. Please try again.",
                   "hi": "साइन-इन का जवाब इस अनुरोध से मेल नहीं खाया, इसलिए मैंने उसे छोड़ दिया। कृपया फिर से कोशिश कीजिए।"},
    "auth_timeout": {"en": "The sign-in took too long, so I stopped waiting. Please try again.",
                     "hi": "साइन-इन में बहुत देर लगी, इसलिए मैंने इंतज़ार बंद कर दिया। कृपया फिर से कोशिश कीजिए।"},
    "auth_no_refresh": {"en": "Google didn't give a long-lived sign-in. Remove Raziel at myaccount.google.com/permissions and try again.",
                        "hi": "Google ने लंबे समय वाला साइन-इन नहीं दिया। myaccount.google.com/permissions से Raziel को हटाकर फिर कोशिश कीजिए।"},
    "auth_failed": {"en": "Google refused the sign-in. Check that the credentials file is a Desktop app client and try again.",
                    "hi": "Google ने साइन-इन मना कर दिया। जाँचिए कि क्रेडेंशियल फ़ाइल Desktop app क्लाइंट की है, फिर कोशिश कीजिए।"},
    "auth_save": {"en": "I signed in but couldn't save the token file. Check that the folder is writable.",
                  "hi": "साइन-इन हो गया लेकिन टोकन फ़ाइल सेव नहीं हो पाई। जाँचिए कि फ़ोल्डर में लिखा जा सकता है।"},
    # --- calendar
    "cal_none": {"en": "Nothing on your calendar {label}.",
                 "hi": "{label} आपके कैलेंडर में कुछ नहीं है।"},
    "cal_one": {"en": "You have 1 event {label}: {items}.",
                "hi": "{label} आपका 1 इवेंट है: {items}।"},
    "cal_some": {"en": "You have {n} events {label}: {items}.",
                 "hi": "{label} आपके {n} इवेंट हैं: {items}।"},
    "cal_many": {"en": "You have {n} events {label}. The first {k} are: {items}.",
                 "hi": "{label} आपके {n} इवेंट हैं। पहले {k} ये हैं: {items}।"},
    "it_timed": {"en": "{t} at {time}", "hi": "{t} {time}"},
    "it_allday": {"en": "{t} all day", "hi": "{t} पूरे दिन"},
    "it_timed_day": {"en": "{t} {dl} at {time}", "hi": "{dl} {t} {time}"},
    "it_allday_day": {"en": "{t} {dl}, all day", "hi": "{dl} {t} पूरे दिन"},
    "untitled": {"en": "an untitled event", "hi": "बिना नाम का इवेंट"},
    "next_timed": {"en": "Your next event is {t}, {dl} at {time}.", "hi": "आपका अगला इवेंट {t} है, {dl} {time}।"},
    "next_allday": {"en": "Your next event is {t}, {dl}, all day.", "hi": "आपका अगला इवेंट {t} है, {dl} पूरे दिन।"},
    "next_none": {"en": "You have nothing coming up in the next {days} days.",
                  "hi": "अगले {days} दिनों में आपके कैलेंडर में कुछ नहीं है।"},
    "which_day": {"en": "Which day should I check?", "hi": "किस दिन का देखूँ?"},
    "need_title": {"en": "What should I call the event?", "hi": "इवेंट का नाम क्या रखूँ?"},
    "need_time": {"en": "What time should I put it at?", "hi": "इसे कितने बजे के लिए रखूँ?"},
    "past": {"en": "That time has already passed. What time should I put it at?",
             "hi": "वह समय निकल चुका है। इसे कितने बजे के लिए रखूँ?"},
    "add_q": {"en": "Add {title} to your calendar {when}{dur}? Say yes or no.",
              "hi": "क्या मैं {title} को आपके कैलेंडर में {when}{dur} जोड़ दूँ? हाँ या ना कहिए।"},
    "dur": {"en": " for {d}", "hi": ", {d} के लिए,"},
    "add_summary": {"en": "add {title} to your calendar", "hi": "आपके कैलेंडर में {title} जोड़ना"},
    "add_ack": {"en": "Adding it now.", "hi": "जोड़ रही हूँ।"},
    "added": {"en": "Added {title} to your calendar {when}.",
              "hi": "मैंने {title} को आपके कैलेंडर में {when} के लिए जोड़ दिया है।"},
    "template_opened": {"en": "I opened Google Calendar with the event filled in - press Save to add it.",
                        "hi": "मैंने Google Calendar में इवेंट भरकर खोल दिया है, उसे जोड़ने के लिए सेव दबाइए।"},
    "browser_fail": {"en": "I couldn't open the browser.", "hi": "मैं ब्राउज़र नहीं खोल पाई।"},
    "d_today": {"en": "today", "hi": "आज"},
    "d_tomorrow": {"en": "tomorrow", "hi": "कल"},
    "d_yesterday": {"en": "yesterday", "hi": "कल"},
    "d_recently": {"en": "recently", "hi": "हाल ही में"},
    "d_day2": {"en": "the day after tomorrow", "hi": "परसों"},
    "d_on": {"en": "on {d}", "hi": "{d} को"},
    "d_week": {"en": "this week", "hi": "इस हफ्ते"},
    "d_nextweek": {"en": "next week", "hi": "अगले हफ्ते"},
    "d_weekend": {"en": "this weekend", "hi": "इस वीकेंड"},
    "cal_b1": {"en": "Today you have {items}.", "hi": "आज आपके कैलेंडर में {items} है।"},
    "cal_bn": {"en": "Today you have {items}.", "hi": "आज आपके कैलेंडर में {items} हैं।"},
    "cal_bmany": {"en": "Today you have {n} events, starting with {items}.",
                  "hi": "आज आपके {n} इवेंट हैं, जिनमें सबसे पहले {items} हैं।"},
    # --- gmail
    "mail_none": {"en": "You have no unread emails.", "hi": "आपका कोई अनरीड ईमेल नहीं है।"},
    "mail_one": {"en": "You have 1 unread email: {items}.", "hi": "आपका 1 अनरीड ईमेल है: {items}।"},
    "mail_some": {"en": "You have {n} unread emails. The newest are {items}.",
                  "hi": "आपके {n} अनरीड ईमेल हैं। सबसे नए ये हैं: {items}।"},
    "mail_some1": {"en": "You have {n} unread emails. The newest is {items}.",
                   "hi": "आपके {n} अनरीड ईमेल हैं। सबसे नया यह है: {items}।"},
    "mail_item": {"en": "from {who} - {subj}", "hi": "{who} से - {subj}"},
    "no_subject": {"en": "no subject", "hi": "बिना विषय"},
    "mail_latest": {"en": "Your latest email is from {who} - {subj}.",
                    "hi": "आपका सबसे नया ईमेल {who} से है - {subj}।"},
    "mail_says": {"en": " It says: {snip}", "hi": " उसमें लिखा है: {snip}"},
    "mail_empty": {"en": "Your inbox is empty.", "hi": "आपका इनबॉक्स खाली है।"},
    "mail_from": {"en": "Yes. The latest email from {who} is {subj}, {when}.",
                  "hi": "हाँ। {who} का सबसे नया ईमेल {when} आया था - {subj}।"},
    "mail_from_none": {"en": "I couldn't find any emails from {q} in your inbox.",
                       "hi": "आपके इनबॉक्स में मुझे {q} का कोई ईमेल नहीं मिला।"},
    "mail_b1": {"en": "You have 1 unread email.", "hi": "आपका 1 अनरीड ईमेल है।"},
    "mail_bn": {"en": "You have {n} unread emails.", "hi": "आपके {n} अनरीड ईमेल हैं।"},
}


def _tr(key: str, l: Optional[str] = None, **kw) -> str:
    return lang.tr(_T, key, lang=l, **kw)


# ------------------------------------------------------------------ settings

def _cfg(name: str, default):
    value = getattr(config, name, default)
    return default if value is None else value


def _enabled() -> bool:
    return bool(_cfg("GOOGLE_ENABLED", True))


def credentials_path() -> str:
    return str(_cfg("GOOGLE_CREDENTIALS_PATH", "") or os.path.join(_HERE, "google_credentials.json"))


def token_path() -> str:
    return str(_cfg("GOOGLE_TOKEN_PATH", "") or os.path.join(_HERE, "google_token.json"))


def _default_minutes() -> int:
    try:
        return max(5, int(_cfg("CALENDAR_DEFAULT_MINUTES", 60)))
    except (TypeError, ValueError):
        return 60


def _timezone_name() -> str:
    return str(_cfg("TIMEZONE", "") or "").strip()


# ------------------------------------------------------------------ errors

class GoogleError(Exception):
    """Something to tell the user. str(e) is the speakable sentence in the language of that moment;
    `.key` lets a caller re-render it (speak(l))."""

    def __init__(self, key: str, **kw):
        self.key = key
        self.kw = kw
        super().__init__(_tr(key, **kw))

    def speak(self, l: Optional[str] = None) -> str:
        return _tr(self.key, l, **self.kw)


class GoogleAuthError(GoogleError):
    """Not set up, revoked, expired or not allowed: the fix is `google_setup.py`."""


class GoogleNetError(GoogleError):
    """Google could not be reached (or answered with a server error)."""


class GoogleApiError(GoogleError):
    """Google answered with an error that is not about sign-in."""


# ------------------------------------------------------------------ token store (small functions tests can patch)

_token_lock = threading.RLock()


def _now_ts() -> float:
    return time.time()


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_token() -> dict:
    return _read_json_file(token_path())


def _save_token(tok: dict) -> None:
    path = token_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=2)
    try:
        os.chmod(tmp, 0o600)          # owner only, where the OS supports it
    except OSError:
        pass
    os.replace(tmp, path)


def is_configured() -> bool:
    """True if the OAuth client file (google_credentials.json) exists."""
    try:
        return os.path.isfile(credentials_path())
    except Exception:  # noqa: BLE001
        return False


def is_connected() -> bool:
    """True if google_setup.py has been run: the token file holds a refresh token."""
    try:
        return bool(_load_token().get("refresh_token"))
    except Exception:  # noqa: BLE001
        return False


def _load_credentials() -> dict:
    path = credentials_path()
    if not os.path.isfile(path):
        raise GoogleAuthError("not_setup")
    data = _read_json_file(path)
    block = data.get("installed")
    if not isinstance(block, dict):
        if isinstance(data.get("web"), dict):
            raise GoogleAuthError("bad_client_type")
        raise GoogleAuthError("bad_credentials")
    if not block.get("client_id"):
        raise GoogleAuthError("bad_credentials")
    auth_uri, token_uri = str(block.get("auth_uri") or ""), str(block.get("token_uri") or "")
    return {
        "client_id": str(block["client_id"]),
        "client_secret": str(block.get("client_secret") or ""),
        "auth_uri": auth_uri if auth_uri.startswith("https://") else AUTH_URI,
        "token_uri": token_uri if token_uri.startswith("https://") else TOKEN_URI,
    }


# ------------------------------------------------------------------ HTTP (all network goes through these three)

def _http_get(url: str, params: Optional[dict], headers: dict):
    return netutil.get_json(url, params=params, headers=headers)


def _http_post_json(url: str, payload: dict, headers: dict):
    return netutil.post_json(url, payload, headers=headers)


def _http_post_form(url: str, data: dict):
    return netutil.post_form(url, data)


def _error_code(body: str) -> str:
    try:
        data = json.loads(body or "")
    except ValueError:
        return ""
    err = data.get("error") if isinstance(data, dict) else ""
    if isinstance(err, dict):
        return str(err.get("status") or err.get("message") or "")
    return str(err or "")


def _map_net_error(e: "netutil.NetError") -> GoogleError:
    status = getattr(e, "status", 0) or 0
    body = getattr(e, "body", "") or ""
    logger.warning("google: HTTP problem (status %s, %s)", status, _error_code(body)[:80])
    if status == 401:
        return GoogleAuthError("expired")
    if status == 403:
        low = body.lower()
        if "accessnotconfigured" in low or "has not been used" in low or "is disabled" in low \
                or "service_disabled" in low:
            return GoogleAuthError("api_off")
        return GoogleAuthError("denied")
    if status == 0 or status == 429 or status >= 500:
        return GoogleNetError("net")
    return GoogleApiError("sorry")


# ------------------------------------------------------------------ access token

def get_access_token(force_refresh: bool = False) -> str:
    """A valid access token, refreshed a minute before it expires. Raises GoogleAuthError (speakable)."""
    with _token_lock:
        tok = _load_token()
        refresh = tok.get("refresh_token")
        if not refresh:
            raise GoogleAuthError("not_setup")
        access = tok.get("access_token")
        try:
            expiry = float(tok.get("expiry") or 0)
        except (TypeError, ValueError):
            expiry = 0.0
        if access and not force_refresh and expiry - 60 > _now_ts():
            return str(access)

        creds = _load_credentials()
        try:
            resp = _http_post_form(creds["token_uri"], {
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
                "refresh_token": refresh, "grant_type": "refresh_token"})
        except netutil.NetError as e:
            code = _error_code(getattr(e, "body", ""))
            logger.warning("google: token refresh failed (status %s, %s)", e.status, code[:60])
            if code in ("invalid_client", "unauthorized_client"):
                raise GoogleAuthError("bad_credentials") from None
            if e.status in (400, 401):
                raise GoogleAuthError("expired") from None      # invalid_grant: revoked / 7-day Testing expiry
            raise GoogleNetError("net") from None
        new_access = (resp or {}).get("access_token") if isinstance(resp, dict) else None
        if not new_access:
            raise GoogleAuthError("expired")
        try:
            lifetime = float(resp.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        tok["access_token"] = new_access
        tok["expiry"] = _now_ts() + lifetime
        if resp.get("refresh_token"):
            tok["refresh_token"] = resp["refresh_token"]
        try:
            _save_token(tok)
        except OSError as e:
            logger.warning("google: couldn't save the refreshed token (%s)", e)
        logger.info("google: access token refreshed (valid %d s)", int(lifetime))
        return str(new_access)


def _auth_headers(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _api_request(method: str, url: str, params: Optional[dict] = None, payload: Optional[dict] = None):
    def once(tok: str):
        if method == "POST":
            return _http_post_json(url + ("?" + urllib.parse.urlencode(params) if params else ""),
                                   payload or {}, _auth_headers(tok))
        return _http_get(url, params, _auth_headers(tok))

    token = get_access_token()
    try:
        return once(token)
    except netutil.NetError as e:
        if e.status == 401:                 # token revoked or clock skew: one forced refresh, one retry
            token = get_access_token(force_refresh=True)
            try:
                return once(token)
            except netutil.NetError as e2:
                raise _map_net_error(e2) from None
        raise _map_net_error(e) from None


def _api_get(url: str, params: Optional[dict] = None):
    return _api_request("GET", url, params)


def _api_post(url: str, payload: dict):
    return _api_request("POST", url, None, payload)


# ------------------------------------------------------------------ sign-in (loopback + PKCE)

def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(auth_uri: str, client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }, quote_via=urllib.parse.quote)
    return auth_uri + ("&" if "?" in auth_uri else "?") + query


_PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>Raziel</title></head>"
         "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'><h2>{title}</h2><p>{body}</p></body></html>")


def authorize_interactive(open_browser: Callable[[str], object] = webbrowser.open, timeout: float = 180) -> dict:
    """
    The whole sign-in: loopback server on 127.0.0.1 (free port), browser consent page, code -> token exchange,
    token file written. Returns the saved token dict. Raises GoogleAuthError (denied / state mismatch /
    timeout / refused) or GoogleNetError. `open_browser(url)` is injectable for tests and the setup script.
    """
    creds = _load_credentials()
    verifier = secrets.token_urlsafe(64)                       # 86 chars, within RFC 7636's 43-128
    challenge = _pkce_challenge(verifier)
    state = secrets.token_urlsafe(24)
    outcome: Dict[str, str] = {}
    done = threading.Event()
    decide = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Raziel"
        timeout = 10            # browsers open idle "preconnect" sockets: never let one hold a thread forever

        def log_message(self, fmt, *args):                    # the URL carries the code: never log it
            return

        def _send(self, status: int, title: str, body: str) -> None:
            data = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def do_GET(self):  # noqa: N802
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in params and "error" not in params:
                self._send(404, "Not found", "This address is only used for the Raziel sign-in.")
                return
            with decide:
                if done.is_set():
                    self._send(200, "Already done", "You can close this tab.")
                    return
                if not secrets.compare_digest(params.get("state", "").encode("utf-8"), state.encode("utf-8")):
                    outcome["error"] = "state"
                    self._send(400, "Sign-in failed", "The sign-in answer did not match. Please go back to Raziel and try again.")
                elif "error" in params:
                    outcome["error"] = "denied" if params["error"] == "access_denied" else "failed"
                    self._send(200, "Not connected", "No access was granted. You can close this tab.")
                else:
                    outcome["code"] = params["code"]
                    self._send(200, "Connected", "Raziel is connected to your Google account. You can close this tab.")
                done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              name="google-oauth", daemon=True)
    thread.start()
    try:
        url = build_auth_url(creds["auth_uri"], creds["client_id"], redirect_uri, challenge, state)
        logger.info("google: sign-in started (loopback port %d)", port)
        try:
            open_browser(url)
        except Exception as e:  # noqa: BLE001 - the user can still open the printed link
            logger.warning("google: couldn't open the browser (%s)", e)
        if not done.wait(timeout):
            logger.warning("google: sign-in timed out after %s s", timeout)
            raise GoogleAuthError("auth_timeout")
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()
        thread.join(2)

    err = outcome.get("error")
    if err == "state":
        raise GoogleAuthError("auth_state")
    if err == "denied":
        raise GoogleAuthError("auth_denied")
    if err or not outcome.get("code"):
        raise GoogleAuthError("auth_failed")

    try:
        resp = _http_post_form(creds["token_uri"], {
            "code": outcome["code"], "client_id": creds["client_id"], "client_secret": creds["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    except netutil.NetError as e:
        logger.warning("google: code exchange failed (status %s, %s)", e.status, _error_code(e.body)[:60])
        if e.status in (400, 401, 403):
            raise GoogleAuthError("auth_failed") from None
        raise GoogleNetError("net") from None
    if not isinstance(resp, dict) or not resp.get("access_token"):
        raise GoogleAuthError("auth_failed")
    if not resp.get("refresh_token"):
        raise GoogleAuthError("auth_no_refresh")
    try:
        lifetime = float(resp.get("expires_in") or 3600)
    except (TypeError, ValueError):
        lifetime = 3600.0
    scopes = str(resp.get("scope") or "").split() or list(SCOPES)
    tok = {"refresh_token": resp["refresh_token"], "access_token": resp["access_token"],
           "expiry": _now_ts() + lifetime, "scopes": scopes, "token_type": "Bearer"}
    try:
        with _token_lock:
            _save_token(tok)
    except OSError as e:
        logger.error("google: couldn't save the token file (%s)", e)
        raise GoogleAuthError("auth_save") from None
    logger.info("google: sign-in complete, token saved (scopes: %s)", len(scopes))
    return tok


def missing_scopes(tok: Optional[dict] = None) -> List[str]:
    """Scopes we need that the user did not grant (they can untick one on the consent screen)."""
    granted = set((tok if tok is not None else _load_token()).get("scopes") or [])
    return [s for s in SCOPES if s not in granted]


def get_profile_email() -> str:
    """The signed-in Gmail address (users/me/profile), for the setup script's confirmation line."""
    data = _api_get(GMAIL_BASE + "/profile")
    return str((data or {}).get("emailAddress") or "")


# ------------------------------------------------------------------ time helpers (one seam for tests)

_LOCAL_TZ = None          # tests / advanced: a tzinfo to use instead of the system time zone


def _now() -> datetime:
    """Aware local 'now'."""
    return datetime.now(_LOCAL_TZ) if _LOCAL_TZ else datetime.now().astimezone()


def _to_local(dt: datetime) -> datetime:
    return dt.astimezone(_LOCAL_TZ) if _LOCAL_TZ else dt.astimezone()


def _aware(dt: datetime) -> datetime:
    """A naive datetime is local wall-clock time; make it aware."""
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_LOCAL_TZ) if _LOCAL_TZ else dt.astimezone()


def _naive(dt: datetime) -> datetime:
    return _to_local(dt).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _rfc3339(dt: datetime) -> str:
    return _aware(dt).isoformat(timespec="seconds")


def _parse_rfc3339(s: str) -> datetime:
    s = (s or "").strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(re.sub(r"\.\d+", "", s))


def _day_start(d: date) -> datetime:
    return _aware(datetime.combine(d, dtime(0, 0)))


def _open_url(url: str) -> bool:
    """Opens a link in the default browser (tests replace this)."""
    return bool(webbrowser.open(url))


# ------------------------------------------------------------------ text folding with an index map

_DROP = {0x0901, 0x0902, 0x093C, 0x200C, 0x200D}          # chandrabindu, anusvara, nukta, ZWNJ, ZWJ
_DEV_DIGITS = "०१२३४५६७८९"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(_DEV_DIGITS)}


def _fold_pat(p: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", p) if ord(ch) not in _DROP)


def _c(p: str) -> "re.Pattern":
    return re.compile(_fold_pat(p), re.S)


def _fold_map(text: str) -> Tuple[str, List[int]]:
    """Folded text and, for every folded char, its index in `text`."""
    out: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(text):
        if ord(ch) in _DROP:
            continue
        if ch in "।॥":
            ch = " "
        ch = ch.translate(_DIGIT_MAP).lower()
        for c in ch:
            out.append(c)
            idx.append(i)
    return "".join(out), idx


class _U:
    """One utterance: the cleaned original, a folded copy for matching and the way back."""

    def __init__(self, orig: str):
        self.orig = orig
        self.f, self.m = _fold_map(orig)

    def _o(self, i: int) -> int:
        return self.m[i] if i < len(self.m) else len(self.orig)

    def span(self, s: int, e: int) -> str:
        return self.orig[self._o(s):self._o(e)]

    def grp(self, mt, name: str) -> str:
        return "" if mt.group(name) is None else self.span(mt.start(name), mt.end(name))

    def without(self, spans: List[Tuple[int, int]]) -> str:
        """The original text with the given folded-coordinate spans cut out."""
        cuts = sorted((self._o(s), self._o(e)) for s, e in spans)
        out, pos = [], 0
        for a, b in cuts:
            if a > pos:
                out.append(self.orig[pos:a])
            pos = max(pos, b)
        out.append(self.orig[pos:])
        return re.sub(r"\s+", " ", " ".join(out)).strip()


# ------------------------------------------------------------------ timeparse.py seam

def _timeparse():
    try:
        import timeparse
        return timeparse
    except ImportError:
        return None


def _extract_when(text: str, now: datetime) -> Tuple[Optional[datetime], str]:
    tp = _timeparse()
    if tp is None:
        return None, text
    try:
        dt, rest = tp.extract_when(text, now=_naive(now))
    except Exception as e:  # noqa: BLE001
        logger.warning("google: extract_when failed (%s)", e)
        return None, text
    return dt, (rest if isinstance(rest, str) else text)


def _parse_when(text: str, now: datetime) -> Optional[datetime]:
    tp = _timeparse()
    if tp is None:
        return None
    try:
        return tp.parse_when(text, now=_naive(now))
    except Exception as e:  # noqa: BLE001
        logger.warning("google: parse_when failed (%s)", e)
        return None


def _parse_duration_seconds(text: str) -> Optional[float]:
    tp = _timeparse()
    if tp is None:
        return None
    try:
        secs = tp.parse_duration(text)
        return float(secs) if secs else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ day ranges for the calendar view

_WEEKDAYS_EN = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
                "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
                "fri": 4, "sat": 5, "sun": 6}
_MONTHS_EN = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
              "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
              "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_ALT = "|".join(sorted(_MONTHS_EN, key=len, reverse=True))
_MD_1 = re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?(?: of)? (?P<m>" + _MONTH_ALT + r")\b")
_MD_2 = re.compile(r"\b(?P<m>" + _MONTH_ALT + r") (?P<d>\d{1,2})(?:st|nd|rd|th)?\b")
_MD_3 = re.compile(r"\bthe (?P<d>\d{1,2})(?:st|nd|rd|th)\b")


@dataclass
class _Scope:
    start: date
    end: date            # inclusive
    kind: str            # today | tomorrow | day2 | weekday | date | week | nextweek | weekend
    multi: bool = False


def _fold(text: str) -> str:
    return _fold_map(unicodedata.normalize("NFC", text or ""))[0]


def _fixed_date(month: int, day: int, today: date) -> Optional[date]:
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


_TODAY_W = {"today", "tonight", "आज", "aaj"}
_TOMORROW_W = {"tomorrow", "कल", "kal"}
_DAY2_W = {_fold("परसों"), "parso", "parson"}
_PH_WEEK = ("this week", _fold("इस हफ्ते"), _fold("इस हफ़्ते"), _fold("इस सप्ताह"))
_PH_NEXTWEEK = ("next week", _fold("अगले हफ्ते"), _fold("अगले हफ़्ते"), _fold("अगले सप्ताह"))
_PH_WEEKEND = ("this weekend", "weekend", _fold("इस वीकेंड"), _fold("वीकेंड"))
_PH_TODAY = ("this morning", "this afternoon", "this evening")
_PH_DAY2 = ("day after tomorrow",)


def _scope_from_text(text: str, today: date) -> Optional[_Scope]:
    """Which day(s) does this text talk about? None if it names none."""
    f = " " + _fold(text) + " "
    if any(" " + p + " " in f for p in _PH_NEXTWEEK):
        start = today + timedelta(days=7 - today.weekday())
        return _Scope(start, start + timedelta(days=6), "nextweek", True)
    if any(" " + p + " " in f for p in _PH_WEEK):
        return _Scope(today, today + timedelta(days=6 - today.weekday()), "week", True)
    if any(" " + p + " " in f for p in _PH_WEEKEND):
        if today.weekday() >= 5:
            start, end = today, today + timedelta(days=6 - today.weekday())
        else:
            start = today + timedelta(days=5 - today.weekday())
            end = start + timedelta(days=1)
        return _Scope(start, end, "weekend", True)
    tokens = f.split()
    if any(p in f for p in _PH_DAY2) or any(t in _DAY2_W for t in tokens):
        d = today + timedelta(days=2)
        return _Scope(d, d, "day2")
    if any(t in _TOMORROW_W for t in tokens):
        d = today + timedelta(days=1)
        return _Scope(d, d, "tomorrow")
    if any(t in _TODAY_W for t in tokens) or any(p in f for p in _PH_TODAY):
        return _Scope(today, today, "today")
    for i, tok in enumerate(tokens):                     # weekdays, "next friday"
        wd = _WEEKDAYS_EN.get(tok)
        if wd is None:
            wd = lang.hindi_weekday_index(tok)
        if wd is not None:
            ahead = (wd - today.weekday()) % 7
            if ahead == 0 and i > 0 and tokens[i - 1] == "next":
                ahead = 7
            d = today + timedelta(days=ahead)
            return _Scope(d, d, "weekday" if ahead > 0 else "today")
    for rx in (_MD_1, _MD_2):                            # "on March 5", "5 September"
        mt = rx.search(f)
        if mt:
            d = _fixed_date(_MONTHS_EN[mt.group("m")], int(mt.group("d")), today)
            if d:
                return _Scope(d, d, "date")
    mt = _MD_3.search(f)                                 # "the 25th"
    if mt:
        day = int(mt.group("d"))
        for add_month in (0, 1):
            year, month = today.year, today.month + add_month
            if month > 12:
                year, month = year + 1, 1
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d >= today:
                return _Scope(d, d, "date")
    for i, tok in enumerate(tokens[:-1]):                # Hindi: "25 सितंबर"
        if tok.isdigit():
            month = lang.hindi_month_number(tokens[i + 1])
            if month:
                d = _fixed_date(month, int(tok), today)
                if d:
                    return _Scope(d, d, "date")
    return None


def _day_phrase(d: date, today: date, l: str) -> str:
    """'today' / 'tomorrow' / 'on Friday' / 'on Sunday, September 27' (Hindi: आज / कल / शुक्रवार को)."""
    delta = (d - today).days
    if delta == 0:
        return _tr("d_today", l)
    if delta == 1:
        return _tr("d_tomorrow", l)
    dt = datetime.combine(d, dtime(0, 0))
    name = (lang.WEEKDAYS_HI if l == "hi" else lang.WEEKDAYS_EN)[d.weekday()] if 0 < delta < 7 \
        else lang.fmt_date(dt, l)
    return _tr("d_on", l, d=name)


def _scope_label(scope: _Scope, today: date, l: str) -> str:
    key = {"today": "d_today", "tomorrow": "d_tomorrow", "day2": "d_day2", "week": "d_week",
           "nextweek": "d_nextweek", "weekend": "d_weekend"}.get(scope.kind)
    if key:
        return _tr(key, l)
    return _day_phrase(scope.start, today, l)


# ------------------------------------------------------------------ text hygiene for speech

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_RE_PREFIX = re.compile(r"^(?:\s*(?:re|fwd?|fw|aw|sv)\s*:\s*)+", re.I)


def _speakable(text: str, limit: int = 0) -> str:
    """HTML entities decoded, URLs / emoji / zero-width junk removed, whitespace collapsed, cut at a word."""
    text = html.unescape(text or "")
    text = _URL_RE.sub(" ", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\ufe0e\ufe0f":
            continue
        if cat in ("Zs", "Zl", "Zp") or ch in "\t\r\n":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Co", "Cs", "Cn", "So", "Sk"):
            continue
        else:
            out.append(ch)
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        if " " in cut and text[limit] != " ":
            cut = cut[:cut.rfind(" ")]
        text = cut.rstrip(" ,.;:-")
    return text


# ------------------------------------------------------------------ calendar: events

@dataclass
class _Event:
    title: str
    all_day: bool
    start: datetime          # aware, local
    end: datetime            # aware, local (exclusive for all-day)
    location: str = ""

    @property
    def start_date(self) -> date:
        return self.start.date()


def _parse_event(item: dict) -> Optional[_Event]:
    try:
        if item.get("status") == "cancelled" or item.get("eventType") == "workingLocation":
            return None
        for a in item.get("attendees") or []:
            if a.get("self") and a.get("responseStatus") == "declined":
                return None
        s, e = item.get("start") or {}, item.get("end") or {}
        title = _speakable(str(item.get("summary") or ""), 80)
        if s.get("dateTime"):
            start = _to_local(_parse_rfc3339(s["dateTime"]))
            end = _to_local(_parse_rfc3339(e["dateTime"])) if e.get("dateTime") else start
            return _Event(title, False, start, end, str(item.get("location") or ""))
        if s.get("date"):
            d0 = date.fromisoformat(s["date"])
            d1 = date.fromisoformat(e["date"]) if e.get("date") else d0 + timedelta(days=1)
            return _Event(title, True, _day_start(d0), _day_start(d1), str(item.get("location") or ""))
    except (ValueError, KeyError, TypeError) as ex:
        logger.warning("google: skipped an event I couldn't read (%s)", ex)
    return None


def _fetch_events(t0: datetime, t1: datetime, limit: int = 25) -> List[_Event]:
    data = _api_get(CAL_EVENTS_URL, {
        "timeMin": _rfc3339(t0), "timeMax": _rfc3339(t1), "singleEvents": "true", "orderBy": "startTime",
        "maxResults": limit,
        "fields": "items(summary,status,eventType,start,end,location,attendees(self,responseStatus))"})
    events = [ev for ev in (_parse_event(i) for i in (data or {}).get("items", []) or []) if ev]
    events.sort(key=lambda ev: ev.start)          # Google already sorts; all-day events sit at 00:00
    logger.info("google: calendar list %s .. %s -> %d event(s)", _rfc3339(t0), _rfc3339(t1), len(events))
    return events


def _event_item(ev: _Event, l: str, today: Optional[date] = None, show_day: bool = False,
                range_start: Optional[date] = None) -> str:
    title = ev.title or _tr("untitled", l)
    if show_day and today is not None:
        d = max(ev.start_date, range_start) if range_start else ev.start_date
        dl = _day_phrase(d, today, l)
        if ev.all_day:
            return _tr("it_allday_day", l, t=title, dl=dl)
        return _tr("it_timed_day", l, t=title, dl=dl, time=lang.fmt_time(ev.start, l))
    if ev.all_day:
        return _tr("it_allday", l, t=title)
    return _tr("it_timed", l, t=title, time=lang.fmt_time(ev.start, l))


def _join_items(items: List[str], l: str) -> str:
    if len(items) <= 1:
        return "".join(items)
    if l == "hi":
        return lang.join_list(items, "hi")
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _view_scope(scope: _Scope, l: str) -> str:
    now = _now()
    today = now.date()
    events = _fetch_events(_day_start(scope.start), _day_start(scope.end + timedelta(days=1)))
    label = _scope_label(scope, today, l)
    n = len(events)
    if n == 0:
        return _tr("cal_none", l, label=label)
    shown = events[:MAX_SPOKEN_EVENTS]
    items = _join_items([_event_item(e, l, today, scope.multi, scope.start) for e in shown], l)
    if n == 1:
        return _tr("cal_one", l, label=label, items=items)
    if n > MAX_SPOKEN_EVENTS:
        return _tr("cal_many", l, label=label, n=n, k=len(shown), items=items)
    return _tr("cal_some", l, label=label, n=n, items=items)


def _next_event(l: str) -> str:
    now = _now()
    today = now.date()
    events = _fetch_events(now, now + timedelta(days=NEXT_EVENT_DAYS))
    pick = next((e for e in events if not e.all_day and e.start > now), None)
    if pick is None:
        pick = next((e for e in events if e.all_day and e.start_date >= today), None)
    if pick is None:
        return _tr("next_none", l, days=NEXT_EVENT_DAYS)
    title = pick.title or _tr("untitled", l)
    dl = _day_phrase(max(pick.start_date, today), today, l)
    if pick.all_day:
        return _tr("next_allday", l, t=title, dl=dl)
    return _tr("next_timed", l, t=title, dl=dl, time=lang.fmt_time(pick.start, l))


# ------------------------------------------------------------------ calendar: adding events

_NUM_WORDS = {"an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45,
              "forty-five": 45, "fortyfive": 45, "sixty": 60, "ninety": 90, "half an": 0.5, "half a": 0.5,
              "a couple of": 2}
_DUR_EN = re.compile(
    r"\bfor\s+(?:about\s+|around\s+|roughly\s+)?"
    r"(?P<n>a couple of|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|"
    r"forty[- ]?five|sixty|ninety|\d+(?:\.\d+)?)"
    r"(?P<half>\s+and\s+a\s+half)?\s*(?P<u>hours?|hrs?|minutes?|mins?)\b(?P<half2>\s+and\s+a\s+half\b)?", re.I)
_DUR_HI = re.compile(
    r"(?P<n>\d+(?:\.\d+)?|आधे|आधा|डेढ़|डेढ|ढाई)\s*(?P<u>घंटे|घंटा|घण्टे|घण्टा|मिनट)\s*(?:के\s+लिए|तक)")
_HI_FRACTIONS = {"आधे": 0.5, "आधा": 0.5, "डेढ़": 1.5, "डेढ": 1.5, "ढाई": 2.5}


def _duration_minutes(n: str, half: bool, unit: str) -> Optional[float]:
    n = n.lower().strip()
    if n in _HI_FRACTIONS:
        val = _HI_FRACTIONS[n]
    elif n in _NUM_WORDS:
        val = float(_NUM_WORDS[n])
    else:
        try:
            val = float(n)
        except ValueError:
            return None
    if half:
        val += 0.5
    return val * (60 if unit.lower().startswith(("h", "घ")) else 1)


def _pull_duration(text: str) -> Tuple[Optional[int], str]:
    """'... at 3 pm for an hour' -> (60, '... at 3 pm'). (None, text) when no duration was said."""
    minutes: Optional[float] = None
    m = _DUR_EN.search(text)
    if m:
        minutes = _parse_duration_seconds(m.group(0)[3:].strip())
        minutes = minutes / 60 if minutes else _duration_minutes(
            m.group("n"), bool(m.group("half") or m.group("half2")), m.group("u"))
        rest = text[:m.start()] + " " + text[m.end():]
    else:
        t2 = lang.hindi_to_ascii_numbers(text)
        m = _DUR_HI.search(t2)
        if not m:
            return None, text
        minutes = _duration_minutes(m.group("n"), False, m.group("u"))
        rest = t2[:m.start()] + " " + t2[m.end():]
    rest = re.sub(r"\s+", " ", rest).strip()
    if not minutes or minutes < 1 or minutes > 24 * 60:
        return None, rest
    return int(round(minutes)), rest


_LEAD_GLUE_EN = {"a", "an", "the", "my", "new", "called", "named", "titled", "for", "on", "at", "to", "and",
                 "then", "of", "in", "by", "from", "about", "regarding", "is", "that"}
_TRAIL_GLUE_EN = {"on", "at", "for", "to", "in", "by", "from", "and", "then", "of", "a", "an", "the", "my",
                  "starting", "every", "is", "that"}
_GLUE_HI = {_fold(w) for w in ("को", "में", "पर", "का", "की", "के", "से", "और", "तक", "एक", "नया", "नई", "नयी",
                               "अपना", "अपनी", "मेरा", "मेरी", "मेरे", "लिए", "फिर", "जो", "है")}
_TITLE_NOUN_LEAD = re.compile(
    r"^(?:(?:calendar\s+)?(?:event|entry)(?:\s+(?:called|named|titled|for|about|regarding))?\s+"
    r"|(?:meeting|appointment)\s+(?:called|named|titled)\s+)", re.I)
_PRONOUN_TITLES = {"it", "this", "that", "something", "them", "these", "those", "one", "यह", "ये", "इसे", "वो",
                   "वह", "उसे"}


def _clean_title(s: str) -> str:
    s = (s or "").strip(" \t,.;:-–—\"'")
    for _ in range(8):
        before = s
        s = _TITLE_NOUN_LEAD.sub("", s)
        words = s.split()
        if words and (words[0].lower() in _LEAD_GLUE_EN or _fold(words[0]) in _GLUE_HI):
            words = words[1:]
        if words and (words[-1].lower() in _TRAIL_GLUE_EN or _fold(words[-1]) in _GLUE_HI):
            words = words[:-1]
        if len(words) >= 2 and _fold(words[-2] + " " + words[-1]) == _fold("के लिए"):
            words = words[:-2]
        s = " ".join(words).strip(" \t,.;:-–—\"'")
        if s == before:
            break
    return s[:100].strip()


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _describe(dt: datetime, now: datetime, l: str) -> str:
    """'tomorrow at 5 PM' (timeparse.describe_when, with a fallback of our own)."""
    tp = _timeparse()
    if tp is not None:
        try:
            text = tp.describe_when(_naive(dt), _naive(now), l)
            if isinstance(text, str) and text.strip():
                return text.strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("google: describe_when failed (%s)", e)
    local = _to_local(dt)
    day = _day_phrase(local.date(), _to_local(now).date(), l)
    return f"{day} at {lang.fmt_time(local, l)}" if l != "hi" else f"{day} {lang.fmt_time(local, l)}"


def _template_url(title: str, start: datetime, end: datetime) -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    dates = _aware(start).astimezone(timezone.utc).strftime(fmt) + "/" + _aware(end).astimezone(timezone.utc).strftime(fmt)
    query = urllib.parse.urlencode({"action": "TEMPLATE", "text": _cap(title), "dates": dates,
                                    "details": "Added by Raziel"},
                                   quote_via=urllib.parse.quote, safe="/")
    return "https://calendar.google.com/calendar/render?" + query


def _insert_event(title: str, start: datetime, end: datetime) -> dict:
    body = {"summary": _cap(title), "description": "Added by Raziel",
            "start": {"dateTime": _rfc3339(start)}, "end": {"dateTime": _rfc3339(end)}}
    tz = _timezone_name()
    if tz:
        body["start"]["timeZone"] = tz
        body["end"]["timeZone"] = tz
    result = _api_post(CAL_EVENTS_URL, body)
    logger.info("google: calendar event inserted (%s)", _rfc3339(start))
    return result if isinstance(result, dict) else {}


def _add_event(title: str, start_naive_or_aware: datetime, minutes: Optional[int], l: str) -> str:
    """Connected: ask first, insert on 'yes'. Not connected: open the pre-filled Calendar page."""
    now = _now()
    start = _aware(start_naive_or_aware)
    minutes = int(minutes or _default_minutes())
    end = start + timedelta(minutes=minutes)
    when = _describe(start, now, l)

    if not is_connected():
        url = _template_url(title, start, end)
        logger.info("google: not connected - opening the Calendar template page for %r", title)
        try:
            opened = _open_url(url)
        except Exception as e:  # noqa: BLE001
            logger.error("google: couldn't open the browser (%s)", e)
            opened = False
        return _tr("template_opened", l) if opened else _tr("browser_fail", l)

    def execute() -> str:
        try:
            _insert_event(title, start, end)
        except GoogleError as e:
            return e.speak(l)
        return _tr("added", l, title=title, when=when)

    dur = ""
    if minutes != _default_minutes():
        d = lang.fmt_duration(minutes * 60, l)
        dur = _tr("dur", l, d=d)
    question = _tr("add_q", l, title=title, when=when, dur=dur)
    logger.info("google: asking to confirm adding %r at %s", title, _rfc3339(start))
    if not _cfg("CONFIRM_RISKY_ACTIONS", True):
        return execute()
    return confirmation.request(kind=CONFIRM_KIND, question=question, execute=execute,
                                summary=_tr("add_summary", l, title=title), ack=_tr("add_ack", l))


def _finish_add(title: str, dt: Optional[datetime], minutes: Optional[int], l: str) -> str:
    title = _clean_title(title)
    if not title or title.lower() in _PRONOUN_TITLES:
        return _tr("need_title", l)
    if dt is None:
        return _tr("need_time", l)
    if _aware(dt) < _now() - timedelta(seconds=90):
        logger.info("google: the time %s is in the past", _rfc3339(dt))
        return _tr("past", l)
    return _add_event(title, dt, minutes, l)


def _add_from_core(core: str, l: str) -> str:
    """`core` = title + time words in any order ('dentist tomorrow at 5 pm for an hour')."""
    minutes, core = _pull_duration(core)
    dt, rest = _extract_when(core, _now())
    logger.info("google: add event - time %s, duration %s, leftover %r", dt.isoformat() if dt else None,
                minutes, rest[:60])
    return _finish_add(rest, dt, minutes, l)


# ------------------------------------------------------------------ gmail

def _decode_hdr(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001 - unknown charset etc.
        return value or ""


def _sender_name(raw: str) -> str:
    name, addr = parseaddr(raw or "")
    name = _speakable(_decode_hdr(name).strip().strip('"'), 40)
    if not name or "@" in name:
        local = (addr or raw or "").split("@")[0]
        name = _speakable(re.sub(r"[._+-]+", " ", local), 40) or _speakable(raw or "", 40)
    return name


def _subject(raw: str, l: str) -> str:
    text = _RE_PREFIX.sub("", _decode_hdr(raw or ""))
    return _speakable(text, SUBJECT_MAX_CHARS) or _tr("no_subject", l)


def _parse_message(msg: dict, l: str) -> dict:
    hdrs = {}
    for h in ((msg.get("payload") or {}).get("headers") or []):
        hdrs[str(h.get("name", "")).lower()] = str(h.get("value", ""))
    ts: Optional[datetime] = None
    try:
        if msg.get("internalDate"):
            ts = _to_local(datetime.fromtimestamp(int(msg["internalDate"]) / 1000.0, tz=timezone.utc))
        elif hdrs.get("date"):
            ts = parsedate_to_datetime(hdrs["date"])
            ts = _to_local(ts) if ts.tzinfo else _aware(ts)
    except (ValueError, TypeError, OverflowError):
        ts = None
    return {
        "who": _sender_name(hdrs.get("from", "")),
        "subject": _subject(hdrs.get("subject", ""), l),
        "snippet": _speakable(msg.get("snippet", ""), SNIPPET_MAX_CHARS),
        "unread": "UNREAD" in (msg.get("labelIds") or []),
        "ts": ts,
    }


def _gmail_ids(query: str, n: int) -> List[str]:
    data = _api_get(GMAIL_BASE + "/messages", {"q": query, "maxResults": n})
    return [str(m["id"]) for m in ((data or {}).get("messages") or []) if m.get("id")]


def _gmail_meta(message_id: str) -> dict:
    return _api_get(f"{GMAIL_BASE}/messages/{message_id}",
                    {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}) or {}


def _gmail_messages(query: str, n: int, l: str) -> List[dict]:
    ids = _gmail_ids(query, n)
    if not ids:
        return []
    if len(ids) == 1:
        metas = [_gmail_meta(ids[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(ids))) as pool:
            metas = list(pool.map(_gmail_meta, ids))
    return [_parse_message(m, l) for m in metas]


def _unread_count() -> Optional[int]:
    """Gmail's own unread counter for the inbox (None if the answer had no counter)."""
    data = _api_get(GMAIL_BASE + "/labels/INBOX")
    try:
        return max(0, int((data or {})["messagesUnread"]))
    except (KeyError, TypeError, ValueError):
        return None


def _mail_item(m: dict, l: str) -> str:
    return _tr("mail_item", l, who=m["who"] or "?", subj=m["subject"])


def _mail_unread(l: str) -> str:
    count = _unread_count()
    msgs = _gmail_messages("is:unread in:inbox", MAX_SPOKEN_EMAILS, l) if count != 0 else []
    if count is None:
        count = len(msgs)
    logger.info("google: gmail %d unread, %d header(s) fetched", count, len(msgs))
    if count == 0:
        return _tr("mail_none", l)
    items = _join_items([_mail_item(m, l) for m in msgs], l)
    if count == 1 and msgs:
        return _tr("mail_one", l, items=items)
    if not msgs:
        return _tr("mail_bn", l, n=count)
    return _tr("mail_some1" if len(msgs) == 1 else "mail_some", l, n=count, items=items)


def _mail_latest(l: str) -> str:
    msgs = _gmail_messages("in:inbox", 1, l)
    logger.info("google: gmail latest -> %d message(s)", len(msgs))
    if not msgs:
        return _tr("mail_empty", l)
    m = msgs[0]
    reply = _tr("mail_latest", l, who=m["who"] or "?", subj=m["subject"])
    if m["snippet"]:
        reply += _tr("mail_says", l, snip=m["snippet"])
    return reply


def _when_of_mail(ts: Optional[datetime], l: str) -> str:
    if ts is None:
        return _tr("d_recently", l)
    today = _now().date()
    d = ts.date()
    delta = (today - d).days
    if delta == 0:
        return _tr("d_today", l)
    if delta == 1:
        return _tr("d_yesterday", l)
    if 1 < delta < 7:
        return _tr("d_on", l, d=(lang.WEEKDAYS_HI if l == "hi" else lang.WEEKDAYS_EN)[d.weekday()])
    return _tr("d_on", l, d=lang.fmt_date(datetime.combine(d, dtime(0, 0)), l, with_weekday=False))


def _sender_variants(name: str) -> List[str]:
    name = re.sub(r"[\"(){}:\\<>]", " ", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    variants = [name] if name else []
    if name and lang.has_devanagari(name):
        latin = lang.to_latin(name).strip()
        collapsed = re.sub(r"aa", "a", re.sub(r"ee", "i", re.sub(r"oo", "u", latin)))
        for v in (latin, collapsed):
            if v and v not in variants:
                variants.append(v)
    return variants


def _from_query(name: str) -> str:
    variants = _sender_variants(name)
    if not variants:
        return "in:inbox"
    if len(variants) > 1 and all(" " not in v for v in variants):
        return "in:inbox from:(" + " OR ".join(variants) + ")"
    return "in:inbox from:(" + variants[0] + ")"


def _mail_from(sender: str, l: str) -> str:
    msgs = _gmail_messages(_from_query(sender), 1, l)
    logger.info("google: gmail from-search -> %d message(s)", len(msgs))
    if not msgs:
        return _tr("mail_from_none", l, q=sender)
    m = msgs[0]
    return _tr("mail_from", l, who=m["who"] or sender, subj=m["subject"], when=_when_of_mail(m["ts"], l))


# ------------------------------------------------------------------ guard: every action ends in one sentence

def _guard(fn: Callable[[], str], l: str) -> str:
    try:
        return fn()
    except GoogleError as e:
        logger.info("google: told the user: %s", e.key)
        return e.speak(l)
    except netutil.NetError:
        return _tr("net", l)
    except Exception:  # noqa: BLE001 - a voice assistant never shows a traceback
        logger.exception("google: unexpected error")
        return _tr("sorry", l)


def _need_google(l: str) -> Optional[str]:
    """The one sentence to say when there is no sign-in yet (None when connected)."""
    if is_connected():
        return None
    logger.info("google: not connected - asking the user to run google_setup.py")
    return _tr("not_setup", l)


def _s(v) -> str:
    return "" if v is None else str(v).strip()


# ------------------------------------------------------------------ tool functions (LLM schemas) and hooks

def calendar_action(action: str = "view", when: str = "", title: str = "", **_ignored) -> str:
    """Google Calendar. action: view (a day or range, `when` = 'today', 'tomorrow', 'friday', 'this week'),
    next (the next event) or add (`title` + `when` like 'tomorrow at 5 pm for an hour')."""
    l = lang.current()
    act = _s(action).lower() or "view"
    when, title = _s(when), _s(title)

    def go() -> str:
        if act in ("add", "create", "schedule", "new", "insert", "book"):
            if not title:
                return _tr("need_title", l)
            if not when:
                return _tr("need_time", l)
            return _add_parts(title, when, l)
        missing = _need_google(l)
        if missing:
            return missing
        if act in ("next", "upcoming", "next_event"):
            return _next_event(l)
        today = _now().date()
        scope = _scope_from_text(when, today) if when else _Scope(today, today, "today")
        if scope is None:
            dt = _parse_when(when, _now())
            if dt is None:
                return _tr("which_day", l)
            d = _aware(dt).date()
            scope = _Scope(d, d, "date" if d != today else "today")
        return _view_scope(scope, l)

    return _guard(go, l)


def _add_parts(title: str, when: str, l: str) -> str:
    minutes, when = _pull_duration(when)
    dt, _rest = _extract_when(when, _now())
    if dt is None:
        dt = _parse_when(when, _now())
    return _finish_add(title, dt, minutes, l)


def email_action(action: str = "unread", sender: str = "", **_ignored) -> str:
    """Gmail, read only. action: unread (count + newest three), latest (the newest email and its preview),
    from (the newest email from `sender`)."""
    l = lang.current()
    act = _s(action).lower() or "unread"
    sender = _s(sender)

    def go() -> str:
        missing = _need_google(l)
        if missing:
            return missing
        if act in ("from", "sender", "search") or (sender and act in ("unread", "check", "read", "")):
            return _mail_from(sender, l) if sender else _mail_unread(l)
        if act in ("latest", "last", "newest", "recent", "read_latest"):
            return _mail_latest(l)
        return _mail_unread(l)

    return _guard(go, l)


def calendar_briefing_line() -> str:
    """For the morning briefing: 'Today you have standup at 10 AM and dentist at 5 PM.' ('' if none / not connected)."""
    try:
        if not (_enabled() and is_connected()):
            return ""
        l = lang.current()
        now = _now()
        today = now.date()
        events = _fetch_events(_day_start(today), _day_start(today + timedelta(days=1)))
        events = [e for e in events if e.all_day or e.end > now]
        if not events:
            return ""
        if len(events) > 4:
            items = lang.join_list([_event_item(e, l) for e in events[:2]], l)
            return _tr("cal_bmany", l, n=len(events), items=items)
        items = lang.join_list([_event_item(e, l) for e in events], l)
        return _tr("cal_b1" if len(events) == 1 else "cal_bn", l, items=items)
    except Exception as e:  # noqa: BLE001
        logger.warning("google: calendar briefing skipped (%s)", e)
        return ""


def email_briefing_line() -> str:
    """For the morning briefing: 'You have 4 unread emails.' ('' if none / not connected)."""
    try:
        if not (_enabled() and is_connected()):
            return ""
        n = _unread_count()
        if not n:
            return ""
        return _tr("mail_b1" if n == 1 else "mail_bn", lang.current(), n=n)
    except Exception as e:  # noqa: BLE001
        logger.warning("google: email briefing skipped (%s)", e)
        return ""


_FILLER = _c(
    r"(?:(?:hey|hi|hello|ok|okay|so|well|um+|uh+|alright|all right|please|kindly|raziel|razil|razeel|"
    r"actually|now|and|just|also|then)\s+"
    r"|(?:can|could|would|will) you(?: please)?\s+"
    r"|(?:i|we) (?:want|need|would like|'d like) you to\s+|i'd like you to\s+|go ahead and\s+|you should\s+"
    r"|कृपया\s+|प्लीज़\s+|ज़रा\s+|जरा\s+|रज़ील\s+|रेज़ील\s+|अच्छा\s+|सुनो\s+|अरे\s+|क्या आप\s+|क्या तुम\s+|आप\s+|तुम\s+|"
    r"ठीक है\s+|मेरे लिए\s+|मुझे\s+|मुझको\s+)+")
_TRAIL = _c(
    r"(?:\s+(?:please|thanks|thank you|for me|प्लीज़|कृपया|मेरे लिए|ज़रा|जरा|सकते हैं|सकती हैं|सकते हो|"
    r"सकती हो|सकते|सकती))+$")
_PUNCT_TO_SPACE = re.compile(r"[,;!?\"“”«»()\[\]]+")


def _prep(transcript) -> Optional[_U]:
    if not isinstance(transcript, str):
        return None
    t = unicodedata.normalize("NFC", transcript)
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = _PUNCT_TO_SPACE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[\s.:।॥]+$", "", t)
    if not t or len(t) > 400:
        return None
    u = _U(t)
    for _ in range(3):
        before = u.orig
        mt = _FILLER.match(u.f)
        if mt and mt.end():
            u = _U(u.orig[u._o(mt.end()):].strip())
        mt = _TRAIL.search(u.f)
        if mt:
            u = _U(u.orig[:u._o(mt.start())].strip())
        if u.orig == before or not u.orig:
            break
    return u if u.f.strip() else None


# ------------------------------------------------------------------ matchers: shared pieces

_GATE = _c(r"calendar|schedule|agenda|meeting|appointment|event|interview|mail|inbox|gmail|कैल|शेड्यूल|शेड्युल|शैड्यूल|"
           r"मीटिंग|अपॉइ|अपोइ|इवेंट|मेल|इनबॉक्स|एजेंडा|इंटरव्यू|calender|kalender|"
           r"what do i have|do i have anything|am i (?:free|busy|booked)")
_NOT_CAL = _c(r"\b(?:remind\w*|alarms?|timers?|notification)\b|रिमाइंडर|याद दिला|अलार्म|टाइमर")

_WD_EN = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MON_EN = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_DAYP = ("(?:today|tonight|tomorrow(?: morning| afternoon| evening| night)?|(?:the )?day after tomorrow|"
         "this (?:morning|afternoon|evening|week|weekend)|next week|"
         "(?:this |next |coming )?(?:" + _WD_EN + ")|the \\d{1,2}(?:st|nd|rd|th)|"
         "(?:" + _MON_EN + ") \\d{1,2}(?:st|nd|rd|th)?|\\d{1,2}(?:st|nd|rd|th)? (?:of )?(?:" + _MON_EN + "))")
_D = "(?: (?:on |for )?" + _DAYP + ")"            # optional day: " tomorrow", " on friday", " for today"
_D1 = "(?: (?:on |for )?" + _DAYP + ")"           # same, used where the day is required (no '?')
_CAL = "(?:calendar|schedule|agenda|diary)"
_NOUN = "(?:meetings?|events?|appointments?)"

_HDAY = ("(?:आज(?: रात| सुबह| शाम)?|कल(?: सुबह| शाम)?|परसों|इस हफ्ते|इस सप्ताह|अगले हफ्ते|अगले सप्ताह|इस वीकेंड|"
         "(?:सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|बृहस्पतिवार|वीरवार|इतवार)(?: को)?)")
_HNOUN = "(?:मीटिंग(?:्स|स)?|इवेंट(?:्स|स)?|अप[ॉो]इ[ंन्]*टमे[ंन्]*ट(?:्स|स)?|कार्यक्रम|इंटरव्यू)"
_CALH = "(?:कैल[ेै]?[ंन्ण]*डर|श[ेै]ड्?य[ूु]ल|एजे[ंन्ण]*डा)"
_HMY = "(?:(?:मेरे|मेरा|मेरी|हमारे|अपने)\\s+)"
_HREAD = ("(?:पढ़ो|पढ़ दो|पढ़िए|पढ़कर सुनाओ|पढ़ के सुनाओ|पढ़कर बताओ|पढ़कर सुना दो|सुनाओ|सुना दो|बताओ|बता दो|"
          "दिखाओ|दिखा दो|चेक करो|चेक कर दो|चेक करें|देखो|देख लो|देखिए)")


# ------------------------------------------------------------------ matchers: calendar view

_VIEW_EN = [_c(p) for p in (
    "what(?:'s| is) (?:on )?my " + _CAL + _D + "?",
    "what(?:'s| is) on the " + _CAL + _D + "?",
    "what(?:'s| is) the " + _CAL + _D1,                    # 'what is the calendar' alone is a definition question
    "what(?:'s| is) (?:on )?(?:my |the )?" + _CAL + " like" + _D + "?",
    "what do i have(?: (?:scheduled|planned|on|coming up))?" + _D1,
    "what do i have (?:on|in) (?:my )?" + _CAL + _D + "?",
    "what do i have (?:scheduled|planned) (?:on|in) (?:my )?" + _CAL + _D + "?",
    "(?:what|which) " + _NOUN + " do i have" + _D + "?",
    "(?:what|which) " + _NOUN + " (?:am i|are) (?:having|scheduled)" + _D + "?",
    "how many " + _NOUN + " do i have" + _D + "?",
    "(?:do i have|have i got|am i having) (?:any |a |an |some )?" + _NOUN + "(?: (?:scheduled|planned|coming up))?" + _D + "?",
    "do i have anything(?: scheduled| planned| on(?: my " + _CAL + ")?| in my " + _CAL + ")?" + _D1,
    "do i have anything (?:scheduled|planned|on my " + _CAL + "|in my " + _CAL + ")",
    "(?:show|read|tell|give|check|list)(?: me)?(?: my| the)? " + _CAL + _D + "?",
    "(?:what are|what is|what's|show me|tell me|read me|read)(?: my| the)? (?:today's|tomorrow's) (?:calendar|schedule|agenda|meetings|events|appointments)",
    "am i (?:free|busy|booked)" + _D1,
)]
_NEXT_EN = [_c(p) for p in (
    "(?:what(?:'s| is)|when(?:'s| is)|tell me|show me)? ?(?:my |the )?next (?:calendar )?(?:meeting|event|appointment)",
    "what(?:'s| is) next on my " + _CAL,
    "when(?:'s| is) my next (?:calendar )?(?:meeting|event|appointment)",
)]
_VIEW_HINGLISH = [_c(p) for p in (
    "(?:aaj|kal|parso) (?:mere |meri )?(?:calendar|schedule) (?:mein|me) kya (?:hai|hain)",
    "(?:mere |meri )?(?:calendar|schedule) (?:mein|me) (?:aaj|kal) kya (?:hai|hain)",
    "(?:aaj|kal) (?:meri|mere) (?:kaun si|kaunsi|kitni) (?:meeting|meetings|events?) (?:hai|hain)",
)]
_NEXT_HINGLISH = [_c(p) for p in (
    "(?:meri |mera )?(?:agli|agla|next) (?:meeting|event|appointment) kab (?:hai|hain|hogi)",
)]

_V = "(?:है|हैं|हो|होगा|होगी|होंगे|होंगी|लिखा है|लिखा हुआ है)"
_VIEW_HI = [_c(p) for p in (
    "(?:क्या )?(?:" + _HDAY + " )?" + _HMY + "?(?:" + _CALH + ")(?: में| पर| का| की| के)?(?: " + _HDAY + ")?(?: में)? "
    "(?:क्या क्या|क्या कुछ|क्या|कौन कौन सी|कौन कौन सा|कौन सी|कौन सा|कोई|कुछ) " + _V + "(?: क्या)?",
    "(?:क्या )?(?:" + _HDAY + " )?(?:मेरे पास )?(?:" + _HDAY + " )?(?:मेरी |मेरे |मेरा )?(?:" + _HDAY + " )?"
    "(?:कौन कौन|कौन|कितनी|कितने|कोई|कुछ)(?: सी| से| सा)? " + _HNOUN + "(?: " + _HDAY + ")? " + _V + "(?: क्या)?",
    "(?:" + _HMY + ")?(?:" + _HDAY + " (?:का|की|के) )?(?:" + _CALH + ")(?: " + _HDAY + ")? " + _HREAD,
    _HDAY + " (?:का|की|के) " + _HNOUN + " " + _HREAD,
)]
_NEXT_HI = [_c(p) for p in (
    "(?:मेरी |मेरा |मेरे )?(?:अगली|अगला|अगले|आने वाली|आने वाला|नेक्स्ट) " + _HNOUN + "(?: कब| कहाँ| कितने बजे| क्या| कौन सी| कौन सा)?"
    "(?: है| होगी| होगा| हो)?(?: क्या)?(?: " + _HREAD + ")?",
    "(?:क्या )?" + _HMY + "?(?:" + _HNOUN + ") कब है",
)]


def _match_view(u: "_U", l: str) -> Optional[Callable[[], str]]:
    f = u.f
    if not f.strip():
        return None
    if any(rx.fullmatch(f) for rx in _NEXT_EN + _NEXT_HI + _NEXT_HINGLISH):
        logger.info("google: matched calendar 'next event'")
        return lambda: _guard(lambda: _need_google(l) or _next_event(l), l)
    if any(rx.fullmatch(f) for rx in _VIEW_EN + _VIEW_HI + _VIEW_HINGLISH):
        today = _now().date()
        scope = _scope_from_text(f, today) or _Scope(today, today, "today")
        logger.info("google: matched calendar view (%s, %s..%s)", scope.kind, scope.start, scope.end)
        return lambda: _guard(lambda: _need_google(l) or _view_scope(scope, l), l)
    return None


# ------------------------------------------------------------------ matchers: calendar add

_A_VERB = "(?:add|put|schedule|create|make|book|save|insert|enter|set up|pencil in)"
_CAL_W = "(?:google\\s+)?(?:calendar|schedule|agenda|diary)"
_ADD_EN_1 = _c(_A_VERB + "\\s+(?P<body>.+?)\\s+(?:to|on|in|into|onto)\\s+(?:my|the|our)\\s+" + _CAL_W + "(?:\\s+(?P<rest>.+))?")
_ADD_EN_3 = _c("(?:add|put|create)\\s+(?:this\\s+|it\\s+)?(?:to|on|in)\\s+(?:my\\s+)?" + _CAL_W + "\\s*(?P<body>.*)")
_ADD_EN_4 = _c("(?:on|in|to)\\s+my\\s+" + _CAL_W + "\\s+(?:add|put|create)\\s+(?P<body>.+)")
_ADD_EN_5 = _c("new\\s+(?:calendar\\s+)?(?:event|appointment)\\s*(?P<body>.*)")
_ADD_EN_2 = _c("(?P<verb>add|create|make|schedule|book|set up|arrange|plan)\\s+(?:me\\s+)?(?:(?:an?|the|new)\\s+)*"
               "(?:calendar\\s+)?(?P<noun>event|appointment|meeting|entry|call|interview|session)(?P<rest>(?:\\s+.*)?)")
_REST_OK = _c("\\s+(?:with|called|named|titled|for|about|regarding|at|on|tomorrow|today|tonight|next|this|from|by|"
              "around|after|before|every|\\d|in \\d|in an? (?:hour|few))")
_REST_HAS_TIME = _c("\\b(?:tomorrow|today|tonight|at \\d|\\d{1,2}(?::\\d\\d)? ?(?:a\\.?m|p\\.?m)\\b|"
                    "on (?:" + _WD_EN + ")|next (?:" + _WD_EN + ")|this (?:morning|afternoon|evening))")
_CODE_EVENT = _c("\\s*(?:listeners?|handlers?|loops?|emitters?|bus|driven|queues?|streams?|logs?|sourcing|callbacks?|hooks?)\\b")
_ADD_NAMED = _c("^(?:called|named|titled)\\s+")

_ADDV = ("(?:जोड़ो|जोड़(?:\\s+(?:दो|दीजिए|दीजिये|दे|लो))?|जोड़ें|जोड़िए|जोड़िये|डालो|डाल(?:\\s+(?:दो|दीजिए|दीजिये|दे))?|"
         "रखो|रख(?:\\s+(?:दो|दीजिए|दीजिये))?|बनाओ|बना(?:\\s+(?:दो|दीजिए|दीजिये|दे))?|"
         "(?:शेड्यूल|शेड्युल|शैड्यूल|सेट|बुक|ऐड|एड|सेव|क्रिएट|फिक्स|प्लान|अरेंज)"
         "(?:\\s+(?:कर(?:ो|ें|िए)?|कीजिए|कीजिये|दो|दीजिए|दीजिये|दे|लो))*|लिखो|लिख(?:\\s+(?:दो|दीजिए|दीजिये))?)")
_H_ADDV = _c("(?<![ऀ-ॿa-z])" + _ADDV + "(?=\\s|$)")
_H_ADDV_END = _c("\\s+" + _ADDV + "\\s*$")
_H_DUR_TAIL = _c("\\s+\\S+\\s+(?:घंटे|घंटा|घण्टे|घण्टा|मिनट)\\s+(?:के\\s+लिए|तक)\\s*$")
_H_CALPHRASE = _c("(?:(?:मेरे|मेरा|मेरी|हमारे|अपने|आपके)\\s+)?" + _CALH + "(?:\\s+(?:में|पर|को|से))?")
_H_CAL_IN = _c(_CALH + "\\s+(?:में|पर|को|से)")
_H_EVENT_NOUN = _c("(?<![ऀ-ॿ])(?:मीटिंग|अप[ॉो]इ[ंन्]*टमे[ंन्]*ट|इवेंट|इंटरव्यू)(?![ऀ-ॿ])")


def _match_add(u: "_U", l: str) -> Optional[Callable[[], str]]:
    f = u.f
    if len(f) > 300 or _NOT_CAL.search(f):
        return None
    core: Optional[str] = None

    mt = _ADD_EN_1.fullmatch(f)
    if mt:
        core = (u.grp(mt, "body") + " " + u.grp(mt, "rest")).strip()
    if core is None:
        for rx in (_ADD_EN_3, _ADD_EN_4, _ADD_EN_5):
            mt = rx.fullmatch(f)
            if mt:
                core = u.grp(mt, "body")
                break
    if core is None:
        mt = _ADD_EN_2.fullmatch(f)
        if mt:
            verb, noun = mt.group("verb"), mt.group("noun")
            rest_f = mt.group("rest") or ""
            ok_verb = verb in ("schedule", "book", "set up", "arrange") if noun in ("call", "interview", "session") \
                else not (noun == "event" and _CODE_EVENT.match(rest_f))       # 'add an event listener at 5' is code talk
            if ok_verb and (not rest_f.strip() or _REST_OK.match(rest_f) or _REST_HAS_TIME.search(rest_f)):
                rest = u.grp(mt, "rest").strip()
                named = _ADD_NAMED.search(_fold(rest))
                if named:
                    rest = rest[named.end():].strip()
                    core = rest if noun in ("event", "entry") else (noun + " " + rest).strip()
                elif noun in ("event", "entry"):
                    core = re.sub(r"^(?:for|about|regarding)\s+", "", rest, flags=re.I)
                else:
                    core = (noun + " " + rest).strip()
    if core is None and lang.has_devanagari(u.orig):
        cal = _H_CAL_IN.search(f)
        verbs = list(_H_ADDV.finditer(f))
        if cal and verbs:                                      # "... कैलेंडर में डेंटिस्ट जोड़ो ..."
            v = verbs[-1]
            spans = [(v.start(), v.end())]
            cp = _H_CALPHRASE.search(f)
            spans.append((cp.start(), cp.end()) if cp else (cal.start(), cal.end()))
            core = u.without(spans)
        elif _H_EVENT_NOUN.search(f) and not cal:     # "... राहुल के साथ मीटिंग शेड्यूल करो"
            dur = _H_DUR_TAIL.search(f)                       # "... मीटिंग शेड्यूल करो एक घंटे के लिए"
            v = _H_ADDV_END.search(f[:dur.start()] if dur else f)
            if v:
                core = u.without([(v.start(), v.end())])
    if core is None:
        return None
    logger.info("google: matched calendar add (core %r)", core[:60])
    return lambda: _guard(lambda: _add_from_core(core, l), l)


# ------------------------------------------------------------------ matchers: email

_EMW = "(?:e[- ]?mails?|mails?)"
_NEWW = "(?:new|unread|unopened|recent)"
_MYW = "(?:my |the |all my |all the |all of my )?"
_INB = "(?: (?:in|on) (?:my )?(?:inbox|gmail|g mail|google mail))?"
_TIMEW = "(?: (?:today|right now|now|so far|this morning|tonight|lately|recently|since yesterday))?"
_Q = "(?:any |an? |some )?"
_RV = "(?:read|check|show|list|go through|look at|tell me about|tell me|give me)"
_NAME = "(?P<who>[a-z][a-z.'-]*(?: [a-z][a-z.'-]*){0,2})"

_MAIL_UNREAD_EN = [_c(p) for p in (
    "(?:do i have|have i got|have i received|did i get|did i receive|is there|are there|got) " + _Q + "(?:" + _NEWW + " )*" + _EMW + _INB + _TIMEW,
    "(?:any|got any) (?:" + _NEWW + " )*" + _EMW + _INB + _TIMEW,
    "anything new in (?:my )?(?:inbox|e[- ]?mail|gmail)",
    "how many (?:" + _NEWW + " )*" + _EMW + "(?: (?:do i have|have i got|are there|are waiting|are in my inbox|have i received|did i get|did i receive|came in))?"
    + _INB + "(?: " + _NEWW + ")?" + _TIMEW,
    "(?:what(?:'s| is) )?(?:my |the )?(?:unread|new) " + _EMW + " count",
    _RV + "(?: me)? " + _MYW + "(?:" + _NEWW + " )*" + _EMW + _INB + _TIMEW,
    _RV + "(?: me)? " + _MYW + "(?:inbox|(?:gmail|g mail|google mail)(?: inbox)?)" + _TIMEW,
    "check for (?:any )?(?:" + _NEWW + " )*" + _EMW + _INB,
    "what(?:'s| is) in " + _MYW + "(?:inbox|gmail)",
    "(?:my|the) (?:new|unread) " + _EMW,
)]
_MAIL_LATEST_EN = [_c(p) for p in (
    "(?:" + _RV + "|what(?:'s| is| was)|what(?:'s| is| was) in)(?: me)? " + _MYW + "(?:latest|last|newest|most recent)(?: unread)? " + _EMW + _INB + _TIMEW,
)]
_MAIL_FROM_EN = [_c(p) for p in (
    "(?:do i have|have i got|did i get|did i receive|is there|are there|got|have i received) " + _Q + "(?:" + _NEWW + " )*" + _EMW + " from " + _NAME + _TIMEW,
    "(?:any|got any)(?: " + _NEWW + ")* " + _EMW + " from " + _NAME + _TIMEW,
    "(?:did|has|have|does) " + _NAME + " (?:e-?mail(?:ed)?|mail(?:ed)?|write|wrote|written|send|sent)(?: to me| me)?(?: an? (?:e-?mail|mail))?" + _TIMEW + "(?: yet)?",
    "(?:read|check|show|list|get)(?: me)? " + _MYW + "(?:" + _NEWW + " )*" + _EMW + " from " + _NAME,
    "(?:what(?:'s| is| was)|read|show|tell me)(?: me)? " + _MYW + "(?:latest|last|newest|most recent) " + _EMW + " from " + _NAME,
    "check for (?:any )?" + _EMW + " from " + _NAME,
)]
_MAIL_HINGLISH = [_c(p) for p in (
    "(?:mere )?(?:naye|naya|new|unread) e?-?mails? (?:padho|padh do|batao|sunao)",
    "kitne (?:unread |naye )?e?-?mails? (?:hain|hai|aaye hain)",
)]

_EMH = "(?:(?:ई|इ|जी) ?)?मेल(?:्स|स)?"
_NEWH = "(?:नए|नये|नई|नया|नयी|अनरीड|अनरेड|अन रीड|अपठित|बिना पढ़े)"
_MAIL_UNREAD_HI = [_c(p) for p in (
    "(?:मेरे |मेरा |मेरी |सारे |सभी |मेरे सारे )?(?:" + _NEWH + " )*" + _EMH + " " + _HREAD,
    "(?:मेरा |मेरे )?इनबॉक्स " + _HREAD,
    "(?:मेरे पास |मेरे )?कितने (?:" + _NEWH + " )?" + _EMH + "(?: " + _NEWH + ")?(?: (?:आए|आये|आई|आया))?(?: (?:हैं|है|हुए हैं|बचे हैं))?(?: मेरे पास)?",
    "(?:क्या )?(?:मेरे पास |मेरे )?(?:कोई )?(?:" + _NEWH + " )?" + _EMH + " (?:आया|आए|आये|आई)(?: है| हैं| हुआ है| हुए हैं)?(?: क्या)?",
    "(?:क्या )?(?:मेरे )?(?:कोई )?" + _NEWH + " " + _EMH + "(?: आया| आए| आये| आई)? (?:है|हैं)",
)]
_MAIL_LATEST_HI = [_c(p) for p in (
    "(?:मेरा |मेरी |मेरे )?(?:आखिरी|पिछला|सबसे नया|सबसे नई|नवीनतम|लेटेस्ट|ताज़ा) " + _EMH + " (?:" + _HREAD + "|क्या है)",
)]
_MAIL_FROM_HI = [_c(p) for p in (
    "(?:क्या )?(?P<who>[ऀ-ॿA-Za-z]+(?: [ऀ-ॿA-Za-z]+)?) (?:का|की|के|ने) (?:कोई |कोई भी )?(?:" + _NEWH + " )?" + _EMH
    + "(?: आया| आई| आए| आये| भेजा| भेजी| किया)?(?: है| हैं| हुआ है| हुई है| था)?(?: क्या)?",
)]

_ANYONE = {"anyone", "anybody", "someone", "somebody", "everyone", "any one"}
_NOT_NAME = {"the", "a", "an", "me", "you", "i", "we", "they", "he", "she", "it", "this", "that", "these", "those",
             "there", "here", "today", "yesterday", "tomorrow", "now", "last", "next", "every", "all", "any", "some",
             "no", "more", "new", "unread", "recent", "week", "month", "morning", "tonight", "lately", "recently"}
_NAME_TAIL_RAW = re.compile(r"(?:\s+(?:today|yesterday|now|recently|lately|yet|please|this morning|tonight|so far))+$", re.I)
_NOT_NAME_HI = {_fold(w) for w in ("मेरे", "मेरा", "मेरी", "कोई", "क्या", "आज", "कल", "आपका", "आपके", "किसी", "सारे", "सभी",
                                    "नए", "नई", "नया", "मेरे पास", "आप", "तुम", "मैं")}


def _match_email(u: "_U", l: str) -> Optional[Callable[[], str]]:
    f = u.f
    if any(rx.fullmatch(f) for rx in _MAIL_LATEST_EN + _MAIL_LATEST_HI):
        logger.info("google: matched email 'latest'")
        return lambda: email_action("latest")
    for rx in _MAIL_FROM_EN:
        mt = rx.fullmatch(f)
        if mt:
            raw = re.sub(r"^my\s+", "", _NAME_TAIL_RAW.sub("", u.grp(mt, "who")).strip(), flags=re.I)
            who = _fold(raw).strip()
            if who in _ANYONE:
                logger.info("google: matched email 'unread' (anyone)")
                return lambda: email_action("unread")
            if not who or who.split()[0] in _NOT_NAME:
                return None
            spoken = raw
            logger.info("google: matched email 'from' (%s)", who)
            return lambda: email_action("from", sender=spoken)
    for rx in _MAIL_FROM_HI:
        mt = rx.fullmatch(f)
        if mt:
            who_f = mt.group("who").strip()
            if not who_f or any(w in _NOT_NAME_HI for w in who_f.split()):
                continue
            spoken = u.grp(mt, "who").strip()
            logger.info("google: matched email 'from' (Hindi)")
            return lambda: email_action("from", sender=spoken)
    if any(rx.fullmatch(f) for rx in _MAIL_UNREAD_EN + _MAIL_UNREAD_HI + _MAIL_HINGLISH):
        logger.info("google: matched email 'unread'")
        return lambda: email_action("unread")
    return None


# ------------------------------------------------------------------ the matcher

def try_handle(transcript: str) -> Optional[str]:
    """The sentence to speak if `transcript` is a calendar or Gmail request (already done), else None."""
    try:
        if not _enabled():
            return None
        u = _prep(transcript)
        if u is None or not _GATE.search(u.f):
            return None
        l = lang.current()
        plan = None
        for matcher in (_match_add, _match_view, _match_email):
            plan = matcher(u, l)
            if plan is not None:
                break
        if plan is None:
            return None
    except Exception:  # noqa: BLE001 - matching must never break the loop
        logger.exception("google: matcher failed on %r", (transcript or "")[:80])
        return None
    try:
        return plan()
    except Exception:  # noqa: BLE001
        logger.exception("google: action failed")
        return _tr("sorry", lang.current())
