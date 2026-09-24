"""
phone_control.py - Phase 6: lets Raziel trigger actions on the user's Android phone, via the
free "Join" app (https://joaoapps.com/join/). Join relays a small push to the phone over
Google's own push infrastructure, so the phone never has to run a server or open a port - this
works from anywhere the phone has any data/Wi-Fi connection at all, unlike the OTHER direction
(phone -> PC, see phone_bridge.py), which needs the phone to actually reach this PC (Tailscale).

One-time setup (see PHONE_BRIDGE_SETUP.md next to this file for the full walkthrough):
  1. Install "Join" from the Play Store on the phone to be controlled.
  2. Open Join -> tap that phone's device card -> "Join API" - it shows an API key and this
     device's ID.
  3. Put both in config.py: JOIN_API_KEY, JOIN_DEVICE_ID.

Every action here is a small, fixed, outbound-only request - nothing here can read anything OFF
the phone (contacts, messages, location, files, ...). Sending an SMS goes through the same
spoken yes/no confirmation as a WhatsApp message (confirmation.py, config.CONFIRM_ACTIONS).
"""

import difflib
import logging

import config
import confirmation
import lang
import netutil

logger = logging.getLogger("voice_assistant")

# Documented at https://joaoapps.com/join/api/ ; independently confirmed against the endpoint
# used by the (unrelated, open-source) "notifiers" Python library's Join integration.
JOIN_PUSH_URL = "https://joinjoaomgcd.appspot.com/_ah/api/messaging/v1/sendPush"
JOIN_TIMEOUT = 8.0

_T = {
    "not_set_up": {
        "en": "Phone control isn't set up yet - add JOIN_API_KEY and JOIN_DEVICE_ID to config.py "
              "from the Join app on your phone.",
        "hi": "फ़ोन कंट्रोल अभी सेट अप नहीं है - Join ऐप से JOIN_API_KEY और JOIN_DEVICE_ID config.py में जोड़िए।",
    },
    "unreachable": {"en": "I couldn't reach your phone through Join just now - is it online?",
                    "hi": "अभी Join के ज़रिए आपके फ़ोन तक नहीं पहुँच पाई - क्या वह ऑनलाइन है?"},
    "ringing": {"en": "Ringing your phone now, even if it's on silent.",
                "hi": "आपका फ़ोन अभी बजा रही हूँ, भले ही वह साइलेंट पर हो।"},
    "opened_app": {"en": "Asked your phone to open {app}.", "hi": "आपके फ़ोन पर {app} खोलने को कहा।"},
    "opened_url": {"en": "Asked your phone to open that link.", "hi": "आपके फ़ोन पर वह लिंक खोलने को कहा।"},
    "no_app": {"en": "Which app should I open on your phone?", "hi": "आपके फ़ोन पर कौन सा ऐप खोलूँ?"},
    "no_url": {"en": "What link should I open on your phone?", "hi": "आपके फ़ोन पर कौन सा लिंक खोलूँ?"},
    "clip_set": {"en": "Put that on your phone's clipboard.", "hi": "वह आपके फ़ोन के क्लिपबोर्ड पर रख दिया।"},
    "no_clip_text": {"en": "What should I put on your phone's clipboard?",
                      "hi": "आपके फ़ोन के क्लिपबोर्ड पर क्या रखूँ?"},
    "sms_no_contact": {"en": "I don't have a phone number saved for {name}. Add them to CONTACTS "
                             "in config.py first, e.g. \"{ckey}\": \"+91XXXXXXXXXX\".",
                       "hi": "{name} का फ़ोन नंबर मेरे पास सेव नहीं है। पहले config.py के CONTACTS में जोड़िए।"},
    "sms_ambiguous": {"en": "I have more than one contact that could be {name}: {names}. Which one do you mean?",
                      "hi": "{name} नाम से कई संपर्क हो सकते हैं: {names}। आपका मतलब किससे है?"},
    "sms_what": {"en": "What should the text to {who} say?", "hi": "{who} को टेक्स्ट में क्या लिखूँ?"},
    "sms_question": {"en": "Send {who} this SMS from your phone: {msg} Say yes to send, or no to cancel.",
                     "hi": "{who} को यह SMS आपके फ़ोन से भेजूँ: {msg} भेजने के लिए हाँ कहिए, रद्द करने के लिए नहीं।"},
    "sms_summary": {"en": "send {who} that SMS", "hi": "{who} को वह SMS भेजना"},
    "sms_ack": {"en": "Sending it now.", "hi": "अभी भेज रही हूँ।"},
    "sms_sent": {"en": "Sent your text to {who}.", "hi": "{who} को टेक्स्ट भेज दिया।"},
    "sms_failed": {"en": "I couldn't reach your phone through Join, so I did not text {who}.",
                  "hi": "मैं Join के ज़रिए आपके फ़ोन तक नहीं पहुँच पाई, इसलिए {who} को कुछ नहीं भेजा।"},
    "unknown_action": {"en": "I don't know how to do that on your phone yet.",
                       "hi": "मैं अभी आपके फ़ोन पर यह नहीं कर सकती।"},
}


def _t(key: str, **kw) -> str:
    return lang.tr(_T, key, **kw)


def _configured() -> bool:
    return bool(getattr(config, "JOIN_API_KEY", "") and getattr(config, "JOIN_DEVICE_ID", ""))


def _join_push(payload: dict) -> bool:
    """POSTs one action to the Join API. Returns True on success, False on any failure (never
    raises) - a network hiccup or a Join outage should read back as an honest 'couldn't reach
    your phone', not crash the turn."""
    params = {"apikey": config.JOIN_API_KEY, "deviceId": config.JOIN_DEVICE_ID, **payload}
    try:
        data = netutil.get_json(JOIN_PUSH_URL, params=params, timeout=JOIN_TIMEOUT)
    except Exception as e:
        logger.warning("Join push failed (%s): %s", payload, e)
        return False
    ok = bool(isinstance(data, dict) and data.get("success"))
    if not ok:
        logger.warning("Join push rejected (%s): %s", payload, (data or {}).get("errorMessage") if isinstance(data, dict) else data)
    return ok


# --- SMS: name -> number, reusing the same CONTACTS book as WhatsApp -----------------------

def _resolve_phone_contact(name: str):
    """Returns (list_of_matching_saved_keys, how) - 'exact' | 'fuzzy' | [] - matching
    send_whatsapp_message's own contact resolution in tools.py, kept local here to avoid the two
    modules depending on each other."""
    contacts = getattr(config, "CONTACTS", {}) or {}
    key = (name or "").strip().lower()
    if not key:
        return [], "none"
    if key in contacts:
        return [key], "exact"
    substring = [k for k in contacts if key in k or k in key]
    if substring:
        return substring, "fuzzy"
    close = difflib.get_close_matches(key, list(contacts.keys()), n=1, cutoff=0.6)
    return (close, "fuzzy") if close else ([], "none")


def _contact_display(saved_key: str) -> str:
    return saved_key.title() if saved_key == saved_key.lower() else saved_key


def _send_sms_now(who: str, number: str, message: str) -> str:
    ok = _join_push({"smsnumber": number, "smstext": message})
    return _t("sms_sent", who=who) if ok else _t("sms_failed", who=who)


def _send_sms(contact_name: str, message: str) -> str:
    contact_name = (contact_name or "").strip()
    if lang.has_devanagari(contact_name):
        contact_name = lang.hint_latin(contact_name)
    matches, how = _resolve_phone_contact(contact_name)
    if not matches:
        return _t("sms_no_contact", name=contact_name, ckey=contact_name.lower())
    if len(matches) > 1:
        names = lang.tr({"and": {"en": " and ", "hi": " और "}}, "and").join(_contact_display(m) for m in matches)
        return _t("sms_ambiguous", name=contact_name, names=names)
    saved_key = matches[0]
    number = config.CONTACTS[saved_key]
    who = _contact_display(saved_key)
    if not (message or "").strip():
        return _t("sms_what", who=who)

    if confirmation.needs_confirmation("send_phone_sms"):
        return confirmation.request(
            kind="send_phone_sms",
            summary=_t("sms_summary", who=who),
            question=_t("sms_question", who=who, msg=message.rstrip(".!?") + "."),
            execute=lambda: _send_sms_now(who, number, message),
            ack=_t("sms_ack"),
            yes_phrases=("send", "send it", "हाँ भेज दो", "भेजो"),
        )
    return _send_sms_now(who, number, message)


# --- The single tool entry point ------------------------------------------------------------

_ACTIONS_RING = {"ring", "find", "ring_phone", "find_phone", "locate"}
_ACTIONS_SMS = {"sms", "text", "send_sms", "send_text"}
_ACTIONS_APP = {"open_app", "app", "launch_app"}
_ACTIONS_URL = {"open_url", "url", "open_link"}
_ACTIONS_CLIP = {"clipboard", "set_clipboard", "copy_to_phone"}


def control_phone(action: str = "", contact: str = "", message: str = "", app_name: str = "",
                  url: str = "", text: str = "", **_ignored) -> str:
    """Triggers an action on the user's phone via Join. See the _ACTIONS_* sets above for the
    accepted `action` values."""
    if not _configured():
        return _t("not_set_up")

    key = (action or "").strip().lower().replace(" ", "_")

    if key in _ACTIONS_RING:
        ok = _join_push({"find": "true"})
        return _t("ringing") if ok else _t("unreachable")

    if key in _ACTIONS_SMS:
        return _send_sms(contact, message or text)

    if key in _ACTIONS_APP:
        app_name = (app_name or text or "").strip()
        if not app_name:
            return _t("no_app")
        ok = _join_push({"app": app_name})
        return _t("opened_app", app=app_name) if ok else _t("unreachable")

    if key in _ACTIONS_URL:
        target = (url or text or "").strip()
        if not target:
            return _t("no_url")
        ok = _join_push({"url": target})
        return _t("opened_url") if ok else _t("unreachable")

    if key in _ACTIONS_CLIP:
        clip_text = (text or message or "").strip()
        if not clip_text:
            return _t("no_clip_text")
        ok = _join_push({"clipboard": clip_text})
        return _t("clip_set") if ok else _t("unreachable")

    return _t("unknown_action")
