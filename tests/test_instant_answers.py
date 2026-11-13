"""
Tests for the instant answers: what Raziel handles without the LLM (instant_replies.py, tools.py's
wrappers, hindi_commands.py) and the order main.py tries them in. These are the turns that take
~0.1 s instead of seconds, and they break silently - a missed phrasing just goes to the slow LLM.

Run: venv\\Scripts\\python.exe -m unittest test_instant_answers -v
Nothing is opened for real: the app / folder openers and the browser are stand-ins.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "raziel"))

import unittest
from datetime import datetime
from unittest import mock

import hindi_commands
import instant_replies as ir

NOW = datetime(2026, 11, 8, 14, 35)        # Sunday


class DateTime(unittest.TestCase):
    def test_time_phrasings(self):
        for said in ("what time is it", "What's the time?", "what is the time", "tell me the time",
                     "Raziel, what time is it?", "what's the time now"):
            self.assertEqual(ir.answer_datetime(said, NOW), "It's 2:35 PM.", said)

    def test_date_phrasings(self):
        for said in ("what's the date", "What is today's date?", "what's today's date", "today's date"):
            reply = ir.answer_datetime(said, NOW)
            self.assertIsNotNone(reply, said)
            self.assertIn("November 8, 2026", reply, said)

    def test_day(self):
        self.assertEqual(ir.answer_datetime("what day is it", NOW), "Today is Sunday.")

    def test_anything_more_goes_to_the_llm(self):
        for said in ("what time is it in London", "what's the date on Friday",
                     "what time does the match start", "set a timer for the time it takes",
                     "what is the date of Diwali this year", ""):
            self.assertIsNone(ir.answer_datetime(said, NOW), said)


class SmallTalkAndScreenshots(unittest.TestCase):
    def test_small_talk(self):
        self.assertEqual(ir.answer_smalltalk("thank you"), "Anytime.")
        self.assertEqual(ir.answer_smalltalk("okay thanks Raziel"), "Anytime.")
        self.assertEqual(ir.answer_smalltalk("hello"), "Hello. Standing by.")
        self.assertEqual(ir.answer_smalltalk("good morning"), "Good morning. Standing by.")
        for said in ("thank you for opening spotify yesterday and also", "hello can you open chrome"):
            self.assertIsNone(ir.answer_smalltalk(said), said)

    def test_screenshots(self):
        self.assertEqual(ir.parse_screenshot_request("take a screenshot"), "take")
        self.assertEqual(ir.parse_screenshot_request("show me the last screenshot"), "show")
        self.assertIsNone(ir.parse_screenshot_request("how do I take a screenshot on a mac"))


class OpenApps(unittest.TestCase):
    def setUp(self):
        import instant_matchers
        self.tools = instant_matchers          # the matchers; the openers they call are swapped out here
        self.opened = []
        for name in ("open_app", "open_folder"):
            p = mock.patch.object(instant_matchers, name,
                                  side_effect=lambda x, n=name: self.opened.append((n, x)) or f"Opened {x}.")
            p.start()
            self.addCleanup(p.stop)

    def test_known_apps_open_without_the_llm(self):
        for said, app in (("open notepad", "notepad"), ("Open Spotify.", "spotify"), ("launch chrome", "chrome")):
            self.opened.clear()
            self.assertTrue(self.tools.try_auto_open_app(said), said)
            self.assertEqual(self.opened, [("open_app", app)], said)

    def test_common_folders(self):
        self.tools.try_auto_open_app("open downloads")
        self.assertEqual(self.opened, [("open_folder", "downloads")])

    def test_more_than_opening_goes_to_the_llm(self):
        for said in ("open chrome and search cats", "open notes.txt", "open the door for me please now"):
            self.assertIsNone(self.tools.try_auto_open_app(said), said)
        self.assertEqual(self.opened, [])


class Hindi(unittest.TestCase):
    def test_time_date_and_day(self):
        with mock.patch.object(hindi_commands, "_enabled", return_value=True):
            time = hindi_commands.try_handle("समय क्या हुआ है", NOW)
            self.assertTrue(time.startswith("अभी") and time.endswith("हुए हैं।"), time)
            self.assertTrue(hindi_commands.try_handle("कितने बजे हैं", NOW).startswith("अभी"))
            self.assertTrue(hindi_commands.try_handle("आज की तारीख क्या है", NOW).startswith("आज "))
            self.assertEqual(hindi_commands.try_handle("आज कौन सा दिन है", NOW), "आज रविवार है।")

    def test_english_is_not_hindi(self):
        with mock.patch.object(hindi_commands, "_enabled", return_value=True):
            for said in ("what time is it", "open notepad", "play lo-fi"):
                self.assertIsNone(hindi_commands.try_handle(said, NOW), said)

    def test_open_an_app(self):
        import tools
        with mock.patch.object(hindi_commands, "_enabled", return_value=True), \
             mock.patch.object(tools, "open_app", return_value="नोटपैड खोल दिया।") as opened:
            self.assertEqual(hindi_commands.try_handle("नोटपैड खोलो", NOW), "नोटपैड खोल दिया।")
        opened.assert_called_once_with("notepad")


class HubCommand(unittest.TestCase):
    def test_phrasings(self):
        import hub_server
        with mock.patch.object(hub_server, "_server", object()), mock.patch("webbrowser.open") as browser:
            for said in ("open the hub", "Raziel, open my hub", "show me the hub", "open Raziel hub",
                         "open the dashboard", "हब खोलो"):
                browser.reset_mock()
                self.assertTrue(hub_server.try_handle(said), said)
                browser.assert_called_once()
            for said in ("open the hub of the wheel in blender", "what is a hub", "open github"):
                self.assertIsNone(hub_server.try_handle(said), said)

    def test_says_so_when_the_hub_is_off(self):
        import hub_server
        with mock.patch.object(hub_server, "_server", None), mock.patch("webbrowser.open") as browser:
            self.assertIn("off", hub_server.try_handle("open the hub"))
        browser.assert_not_called()


class Order(unittest.TestCase):
    """main.py tries the matchers in order: the specific ones must win over the broad ones."""

    def label(self, said):
        import instant_matchers
        import main
        brain = mock.Mock(session_id=None)
        with mock.patch.object(instant_matchers, "open_app", return_value="Opened."), \
             mock.patch.object(instant_matchers, "open_folder", return_value="Opened."), \
             mock.patch("webbrowser.open"), mock.patch.object(main.memory, "append_to_session"):
            hit = main._match_deterministic(said, brain, record_to_memory=False)
        return hit[0] if hit else None

    def test_routing(self):
        self.assertEqual(self.label("what time is it"), "date-time")
        self.assertEqual(self.label("thank you"), "small-talk")
        self.assertEqual(self.label("open the hub"), "hub")            # not "open-app"
        self.assertEqual(self.label("open notepad"), "open-app")
        self.assertIsNone(self.label("explain dijkstra's algorithm"))   # the LLM's job


if __name__ == "__main__":
    unittest.main()
