"""
Deterministic (non-LLM) emotion classification for the avatar's facial
expressions.

This project has repeatedly found that asking the local 8B LLM to reliably
produce an extra piece of structured output (a tool call, a tag, a format)
alongside its normal reply is inconsistent. Classifying the reply's emotion
by keyword/punctuation instead is fast, free, and 100% consistent - it
won't always be psychologically "correct", but it's never flaky.

Returns one of the VRM expression presets actually present in Raziel-1.vrm:
happy, angry, sad, relaxed, surprised, neutral.
"""

import re

_HAPPY_WORDS = {
    "great", "excellent", "wonderful", "success", "successful",
    "completed", "opened", "playing", "sent", "saved", "found",
    "correct", "nice", "good",
}
_SAD_WORDS = {
    "sorry", "unfortunately", "failed", "failure", "wrong", "missing",
    "unable", "trouble", "lost", "sad",
}
_ANGRY_WORDS = {
    "never", "forbidden", "denied", "refuse", "refused", "won't", "wont",
}
_SURPRISED_MARKERS = {"wow", "incredible", "unexpected", "surprising", "whoa"}

# Phrases this project's own tools/system actually produce for failures -
# checked first, since they're common and unambiguous.
_FAILURE_PHRASES = (
    "couldn't", "can't", "cannot", "i don't have", "i don't know",
    "error", "trouble reaching", "not found", "not installed",
)


def classify(text: str) -> str:
    """Returns one of: happy, sad, angry, surprised, relaxed, neutral."""
    if not text:
        return "neutral"

    lowered = text.lower()
    words = set(re.findall(r"[a-z']+", lowered))

    if any(phrase in lowered for phrase in _FAILURE_PHRASES):
        return "sad"
    if "!" in text or (words & _SURPRISED_MARKERS):
        return "surprised"
    if words & _ANGRY_WORDS:
        return "angry"
    if words & _SAD_WORDS:
        return "sad"
    if words & _HAPPY_WORDS:
        return "happy"
    if "?" in text:
        return "relaxed"
    return "neutral"
