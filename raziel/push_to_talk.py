"""
push_to_talk.py - hold a key, speak, and Raziel types your words into the app in front.

Companion Deck's Phase 12 (its PHASE-12.md E, DECISIONS 2026-11-01 D1). The key is
config.PUSH_TO_TALK_KEY ("right_ctrl" by default, "right_alt", or None = off). Holding Ctrl+Win was
ruled out: Wispr Flow uses it.

How it works
------------
wake_word.py already reads the microphone every 80 ms while it waits for "Raziel". On each frame it
asks `Hold.poll()` whether the key is held:
  * the key goes down          -> nothing yet (it might be Right Ctrl + C)
  * held 0.3 s, no other key   -> "start": recording begins, with the frames since the press
  * another key while held     -> "cancel": it was a shortcut; whatever was recorded is thrown away
  * the key comes up           -> "release": main.py transcribes the recording and types it
The key's state is read with GetAsyncKeyState: no keyboard hook, and the key still reaches the app
(a lone Ctrl does nothing there). Push-to-talk works while Raziel waits for her name, not in the
middle of a spoken conversation with her.

Typing goes through dictation.type_now's checks: never into Raziel's own window, and a friendly
sentence if Windows refuses (an administrator window). Nothing typed is logged, only its length.
The notch shows "Listening..." while the key is held and "Typing..." while Whisper works
(avatar_server's state with mode "dictation").
"""

import ctypes
import time
from typing import Callable, Optional

import config

MIN_HOLD = 0.3          # seconds: a shorter press is just a Ctrl tap
MAX_HOLD = 60.0         # seconds: a key stuck down (or forgotten) stops recording here

KEYS = {"right_ctrl": 0xA3, "right_alt": 0xA5}
# The keys a held modifier may share the keyboard with, without being a shortcut: the modifiers
# themselves (GetAsyncKeyState reports VK_CONTROL along with VK_RCONTROL) and mouse buttons.
_IGNORED = {0x01, 0x02, 0x04, 0x05, 0x06, 0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def _key_down_win(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def configured_key() -> Optional[int]:
    """The key's virtual-key code, or None when push-to-talk is off."""
    name = getattr(config, "PUSH_TO_TALK_KEY", "right_ctrl")
    return KEYS.get(str(name).lower()) if name else None


class Hold:
    """Follows one key across microphone frames. `key_down(vk)` is injectable for tests."""

    def __init__(self, vk: int, key_down: Optional[Callable[[int], bool]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.vk = vk
        self._down = key_down or _key_down_win
        self._clock = clock
        self.pressed_at: Optional[float] = None
        self.recording = False
        self._spoiled = False       # another key was pressed: a shortcut, not push-to-talk
        self._already_down = set()  # keys down when the press began: not part of it

    def _keys_down(self) -> set:
        return {vk for vk in range(0x08, 0xFF) if vk not in _IGNORED and self._down(vk)}

    def _other_key(self) -> bool:
        # A key pressed after ours, or one that was down with it and comes up (Right Ctrl + C in
        # the same 80 ms frame), is a shortcut. A key that stays down throughout is not: on this
        # laptop Windows reports End as held all the time, and counting it spoiled every hold
        # (Companion Deck PHASE-12 E).
        return self._keys_down() != self._already_down

    def poll(self) -> Optional[str]:
        """Call once per microphone frame: None, "start", "cancel" or "release"."""
        now = self._clock()
        if not self._down(self.vk):
            if self.pressed_at is None:
                return None
            was_recording = self.recording
            self.pressed_at, self.recording, self._spoiled = None, False, False
            return "release" if was_recording else None
        if self.pressed_at is None:
            self.pressed_at = now
            self._already_down = self._keys_down()
        if self._spoiled:
            return None
        if self._other_key():
            self._spoiled = True
            if self.recording:
                self.recording = False
                return "cancel"
            return None
        if not self.recording and now - self.pressed_at >= MIN_HOLD:
            self.recording = True
            return "start"
        if self.recording and now - self.pressed_at >= MAX_HOLD:
            self.recording = False
            self._spoiled = True    # until the key comes up
            return "release"
        return None


def type_words(text: str) -> str:
    """Types a push-to-talk transcript. Returns "" when typed, else a sentence for the log/notch."""
    import dictation
    text = (text or "").strip()
    if not text:
        return "nothing heard"
    result = dictation.type_now(text)
    return "" if result == dictation._tr("typed") else result
