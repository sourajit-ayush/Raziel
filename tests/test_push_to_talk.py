"""
Tests for push-to-talk (push_to_talk.py), Companion Deck's PHASE-12 E3: the key's hold, the
shortcut filter, and typing through dictation's safety checks.

Run: venv\\Scripts\\python.exe -m unittest test_push_to_talk -v
No microphone, keyboard or Whisper needed: the key state and the clock are stand-ins.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "raziel"))

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

    def test_hindi_before_the_hindi_model_is_loaded(self):
        # On demand: the normal model writes this sentence in Hindi, and the Hindi model starts loading.
        import transcriber
        model = _Model("hi", {"hi": 0.9, "en": 0.05})
        with mock.patch.object(transcriber.Transcriber, "_start_hindi_loader") as load, \
             mock.patch.object(transcriber.config, "HINDI_WHISPER_ON_DEMAND", True, create=True):
            self.assertEqual(self._run(model), ("small:hi", "hi"))
        load.assert_called_once()


class SpokenTurnLanguageTests(unittest.TestCase):
    """transcribe() (a spoken turn): English or Hindi is read from the transcription pass itself, and
    only a pass in the wrong language is run again."""

    def _run(self, model, hindi_model=None, duration=3.0, last="en"):
        import transcriber
        t = transcriber.Transcriber.__new__(transcriber.Transcriber)
        t.model, t.hindi_model, t.last_language, t._prev_language = model, hindi_model, last, last
        stats = mock.Mock(duration_s=duration)
        with mock.patch.object(transcriber.config, "GUARD_ENABLED", True, create=True), \
             mock.patch.object(transcriber.transcript_guard, "load_wav", return_value=([], 16000)), \
             mock.patch.object(transcriber.transcript_guard, "check_audio", return_value=(True, stats, "")), \
             mock.patch.object(transcriber.transcript_guard, "check_segments",
                               side_effect=lambda segs, **kw: mock.Mock(ok=True, text=" ".join(s.text for s in segs))), \
             mock.patch.object(transcriber, "_build_vocabulary_prompt", return_value=""), \
             mock.patch.object(t, "_language_mode", return_value="auto"), \
             mock.patch.object(t, "_hindi_wanted", return_value=True), \
             mock.patch.object(t, "_language_probs", side_effect=AssertionError("no separate language check")):
            return t.transcribe("x.wav"), t.last_language

    def test_english_takes_one_pass(self):
        model = _Model("en", {"en": 0.9, "hi": 0.05})
        self.assertEqual(self._run(model), ("small:en", "en"))
        self.assertEqual(model.calls, [None])

    def test_english_mistaken_for_urdu_is_run_again_in_english(self):
        model = _Model("ur", {"ur": 0.5, "en": 0.3, "hi": 0.2})
        self.assertEqual(self._run(model), ("small:en", "en"))
        self.assertEqual(model.calls, [None, "en"])

    def test_clear_hindi_goes_to_the_hindi_model(self):
        model, hindi = _Model("hi", {"hi": 0.9, "en": 0.05}), _Model("hi", {"hi": 1.0}, name="big")
        self.assertEqual(self._run(model, hindi), ("big:hi", "hi"))

    def test_a_hindi_conversation_stays_hindi_on_a_mixed_sentence(self):
        # last sentence was Hindi, this one is in between: stays Hindi (as before)
        model, hindi = _Model("hi", {"hi": 0.45, "en": 0.45}), _Model("hi", {"hi": 1.0}, name="big")
        self.assertEqual(self._run(model, hindi, last="hi"), ("big:hi", "hi"))


class HindiModelMemoryTests(unittest.TestCase):
    def _transcriber(self):
        import transcriber
        t = transcriber.Transcriber.__new__(transcriber.Transcriber)
        t.model, t.hindi_model, t._hindi_used_at = object(), object(), 0.0
        return transcriber, t

    def test_let_go_after_being_unused(self):
        transcriber, t = self._transcriber()
        with mock.patch.object(transcriber.time, "sleep"), \
             mock.patch.object(transcriber.time, "monotonic", return_value=11 * 60.0), \
             mock.patch.object(transcriber.config, "HINDI_WHISPER_UNLOAD_MINUTES", 10, create=True):
            t._unload_hindi_when_idle()
        self.assertIsNone(t.hindi_model)

    def test_kept_while_in_use(self):
        transcriber, t = self._transcriber()
        hindi = t.hindi_model
        times = iter([5 * 60.0, 5 * 60.0 + 30])          # used at 5 min, checked 30 s later
        checks = []

        def sleep(_s):
            checks.append(1)
            if len(checks) > 1:
                t.hindi_model = None                      # end the loop on the second check
        with mock.patch.object(transcriber.time, "sleep", side_effect=sleep), \
             mock.patch.object(transcriber.time, "monotonic", side_effect=lambda: next(times)), \
             mock.patch.object(transcriber.config, "HINDI_WHISPER_UNLOAD_MINUTES", 10, create=True):
            self.assertIs(t._hindi(), hindi)              # a Hindi sentence at 5 min
            t._unload_hindi_when_idle()
        self.assertEqual(len(checks), 2)                  # still there at the first check

    def test_never_lets_go_of_the_shared_model(self):
        transcriber, t = self._transcriber()
        t.hindi_model = t.model                           # HINDI_WHISPER_MODEL == WHISPER_MODEL_SIZE
        with mock.patch.object(transcriber.time, "sleep"), \
             mock.patch.object(transcriber.time, "monotonic", return_value=1e9):
            t._unload_hindi_when_idle()
        self.assertIs(t.hindi_model, t.model)


if __name__ == "__main__":
    unittest.main()
