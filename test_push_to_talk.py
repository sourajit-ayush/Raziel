"""
Tests for push-to-talk (push_to_talk.py), Companion Deck's PHASE-12 E3: the key's hold, the
shortcut filter, and typing through dictation's safety checks.

Run: venv\\Scripts\\python.exe -m unittest test_push_to_talk -v
No microphone, keyboard or Whisper needed: the key state and the clock are stand-ins.
"""

import unittest
from unittest import mock

import dictation
import push_to_talk
from push_to_talk import MAX_HOLD, MIN_HOLD, Hold

RCTRL = 0xA3
CTRL = 0x11
C_KEY = 0x43


class Keys:
    """A stand-in keyboard and clock."""

    def __init__(self):
        self.down = set()
        self.now = 0.0

    def key_down(self, vk):
        return vk in self.down

    def clock(self):
        return self.now


def _hold(keys):
    return Hold(RCTRL, key_down=keys.key_down, clock=keys.clock)


class HoldTests(unittest.TestCase):
    def test_a_short_tap_does_nothing(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL, CTRL}           # Windows reports VK_CONTROL with VK_RCONTROL
        self.assertIsNone(h.poll())
        keys.now += MIN_HOLD / 2
        self.assertIsNone(h.poll())
        keys.down = set()
        self.assertIsNone(h.poll())
        self.assertFalse(h.recording)

    def test_hold_starts_and_release_ends(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL, CTRL}
        self.assertIsNone(h.poll())
        keys.now += MIN_HOLD
        self.assertEqual(h.poll(), "start")
        keys.now += 2.0
        self.assertIsNone(h.poll())
        keys.down = set()
        self.assertEqual(h.poll(), "release")
        self.assertIsNone(h.poll())

    def test_a_shortcut_before_the_hold_never_starts(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL, CTRL, C_KEY}    # Right Ctrl + C, both seen in the same frame
        self.assertIsNone(h.poll())
        keys.down = {RCTRL, CTRL}           # C comes up: it was a shortcut
        self.assertIsNone(h.poll())
        keys.now += 2 * MIN_HOLD
        self.assertIsNone(h.poll())        # still the same press: spoiled until it comes up
        keys.down = set()
        self.assertIsNone(h.poll())

    def test_a_key_pressed_while_recording_cancels(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL}
        h.poll()
        keys.now += MIN_HOLD
        self.assertEqual(h.poll(), "start")
        keys.down = {RCTRL, C_KEY}
        self.assertEqual(h.poll(), "cancel")
        keys.down = set()
        self.assertIsNone(h.poll())        # nothing to type

    def test_a_key_stuck_down_before_the_press_doesnt_spoil_it(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {0x23}                  # End, reported held all the time on this laptop
        self.assertIsNone(h.poll())
        keys.down = {0x23, RCTRL, CTRL}
        h.poll()
        keys.now += MIN_HOLD
        self.assertEqual(h.poll(), "start")
        keys.down = {0x23}
        self.assertEqual(h.poll(), "release")

    def test_mouse_buttons_dont_spoil_it(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL, 0x01}           # holding the left mouse button too
        h.poll()
        keys.now += MIN_HOLD
        self.assertEqual(h.poll(), "start")

    def test_a_key_stuck_down_stops_recording(self):
        keys = Keys()
        h = _hold(keys)
        keys.down = {RCTRL}
        h.poll()
        keys.now += MIN_HOLD
        self.assertEqual(h.poll(), "start")
        keys.now += MAX_HOLD
        self.assertEqual(h.poll(), "release")
        keys.now += 5
        self.assertIsNone(h.poll())
        keys.down = set()
        self.assertIsNone(h.poll())

    def test_the_key_setting(self):
        with mock.patch.object(push_to_talk.config, "PUSH_TO_TALK_KEY", "right_alt", create=True):
            self.assertEqual(push_to_talk.configured_key(), 0xA5)
        with mock.patch.object(push_to_talk.config, "PUSH_TO_TALK_KEY", None, create=True):
            self.assertIsNone(push_to_talk.configured_key())


class TypeWordsTests(unittest.TestCase):
    def test_types_into_another_app(self):
        with mock.patch.object(dictation, "_foreground", return_value={"title": "Notes - Notepad", "exe": "notepad.exe"}), \
             mock.patch.object(dictation, "_type", return_value=11) as typed:
            self.assertEqual(push_to_talk.type_words(" hello world "), "")
            typed.assert_called_once_with("hello world")

    def test_never_types_into_raziel_herself(self):
        with mock.patch.object(dictation, "_foreground", return_value={"title": "x", "exe": "pythonw.exe"}), \
             mock.patch.object(dictation, "_type") as typed:
            self.assertNotEqual(push_to_talk.type_words("hello"), "")
            typed.assert_not_called()

    def test_nothing_heard_types_nothing(self):
        with mock.patch.object(dictation, "_type") as typed:
            self.assertEqual(push_to_talk.type_words(""), "nothing heard")
            typed.assert_not_called()


class _Seg:
    def __init__(self, text):
        self.text = text


class _Model:
    """A stand-in Whisper model: detects `detected` (with `probs`), and writes `language`'s text."""

    def __init__(self, detected, probs, name="small"):
        self.detected, self.probs, self.name, self.calls = detected, probs, name, []

    def transcribe(self, path, language=None, **kw):
        self.calls.append(language)
        lang = language or self.detected
        info = mock.Mock(language=lang, language_probability=self.probs.get(lang, 0.0),
                         all_language_probs=sorted(self.probs.items(), key=lambda kv: -kv[1]))
        return iter([_Seg(f"{self.name}:{lang}")]), info


class DictationLanguageTests(unittest.TestCase):
    """transcribe_dictation: only English or Hindi is ever typed, never Whisper's other guesses."""

    def _run(self, model, hindi_model=None, duration=3.0):
        import transcriber
        t = transcriber.Transcriber.__new__(transcriber.Transcriber)
        t.model, t.hindi_model, t.last_language, t._prev_language = model, hindi_model, "en", "en"
        stats = mock.Mock(duration_s=duration)
        with mock.patch.object(transcriber.transcript_guard, "load_wav", return_value=([], 16000)), \
             mock.patch.object(transcriber.transcript_guard, "check_audio", return_value=(True, stats, "")), \
             mock.patch.object(t, "_language_mode", return_value="auto"), \
             mock.patch.object(t, "_hindi_wanted", return_value=True):
            return t.transcribe_dictation("x.wav"), t.last_language

    def test_english_mistaken_for_urdu_is_typed_in_english(self):
        model = _Model("ur", {"ur": 0.5, "en": 0.3, "hi": 0.2})
        self.assertEqual(self._run(model), ("small:en", "en"))
        self.assertEqual(model.calls, [None, "en"])

    def test_english_needs_no_second_pass(self):
        model = _Model("en", {"en": 0.9, "hi": 0.05})
        self.assertEqual(self._run(model), ("small:en", "en"))
        self.assertEqual(model.calls, [None])

    def test_clear_hindi_goes_to_the_hindi_model(self):
        model, hindi = _Model("hi", {"hi": 0.9, "en": 0.05}), _Model("hi", {"hi": 1.0}, name="big")
        self.assertEqual(self._run(model, hindi), ("big:hi", "hi"))

    def test_unsure_hindi_is_typed_in_english(self):
        model = _Model("hi", {"hi": 0.5, "en": 0.4})
        self.assertEqual(self._run(model), ("small:en", "en"))

    def test_one_word_stays_english(self):
        model = _Model("hi", {"hi": 0.9, "en": 0.05})
        self.assertEqual(self._run(model, duration=0.5), ("small:en", "en"))


if __name__ == "__main__":
    unittest.main()
