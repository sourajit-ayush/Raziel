"""
Maps eSpeak IPA phoneme symbols (as produced by Piper's phoneme alignment
output) to VRM's 5 standard lip-sync viseme shapes: aa, ih, ou, ee, oh.

This is a coarse approximation - real speech has far more distinct mouth
shapes than 5 - but it matches exactly what VRM's standard viseme system
supports, and looks convincing in practice. Consonants default to a small,
mostly-closed shape rather than getting their own category, since VRM's
5-viseme system is vowel-shape-based by design (this mirrors how most
basic viseme lip-sync implementations handle consonants).
"""

_VISEME_MAP = {
    # Open vowels -> aa
    "a": "aa", "ɑ": "aa", "ʌ": "aa", "æ": "aa", "ɐ": "aa",
    "aɪ": "aa", "aʊ": "aa", "ə\u02c8": "aa",
    # Mid/front vowels -> ih
    "ɪ": "ih", "ɛ": "ih", "e": "ih", "ə": "ih", "ɜ": "ih", "ɚ": "ih",
    # Rounded back vowels -> ou
    "u": "ou", "ʊ": "ou", "w": "ou", "oʊ": "ou", "aʊ": "ou",
    # Close front vowels -> ee
    "i": "ee", "iː": "ee", "j": "ee", "ɨ": "ee",
    # Rounded mid/back vowels -> oh
    "ɔ": "oh", "o": "oh", "ɒ": "oh", "ɔɪ": "oh",
}

DEFAULT_VISEME = "ih"  # consonants: a small, neutral mouth shape
SILENCE_VISEME = None  # mouth fully closed/neutral


# Modifiers that do not change the mouth shape much: length (aː), nasalisation (ã), stress,
# aspiration (pʰ), palatalisation. Hindi uses them a lot ("aː", "ɛː", "ũ").
_MODIFIERS = "\u02d0\u0303\u02c8\u02cc\u02b0\u02b2\u02b7\u0329\u032f\u0325"


def phoneme_to_viseme(phoneme: str) -> str:
    """Returns one of 'aa'/'ih'/'ou'/'ee'/'oh' for a given eSpeak phoneme symbol."""
    cleaned = phoneme.strip()
    if not cleaned:
        return DEFAULT_VISEME
    found = _VISEME_MAP.get(cleaned)
    if found:
        return found
    base = "".join(ch for ch in cleaned if ch not in _MODIFIERS)
    if base in _VISEME_MAP:
        return _VISEME_MAP[base]
    # a diphthong / sequence the table doesn't list: judge it by its first sound
    return _VISEME_MAP.get(base[:1], DEFAULT_VISEME) if base else DEFAULT_VISEME
