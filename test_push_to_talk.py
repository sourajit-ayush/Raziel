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


if __name__ == "__main__":
    unittest.main()
