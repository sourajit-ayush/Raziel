"""
Tests for what Companion Deck's notch uses: the token-locked request channel on the avatar socket
(avatar_server.py), window-only vision's box (vision.py) and "show me" (show_me.py).

Run: venv\\Scripts\\python.exe -m unittest test_notch_api -v
No network and no Ollama needed; the socket runs on a spare local port with a temporary token.
"""

import asyncio
import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

import websockets

import avatar_server
import show_me


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ShowMeMatch(unittest.TestCase):
    def test_requests_it_takes(self):
        cases = {
            "what does the Eiffel Tower look like": "eiffel tower",
            "Raziel, show me a picture of a red panda": "red panda",
            "show me the Taj Mahal": "taj mahal",
            "picture of Mount Fuji?": "mount fuji",
            "can you show me photos of axolotls": "axolotls",
        }
        for said, subject in cases.items():
            self.assertEqual(show_me.match(said), subject, said)

    def test_requests_it_leaves_alone(self):
        for said in ("show me my calendar", "show me the weather", "show me the universe",
                     "what does it look like", "what time is it", "show me how to cook rice",
                     "show me a picture of " + "very " * 10 + "long thing"):
            self.assertIsNone(show_me.match(said), said)

    def test_first_sentence(self):
        text = "The red panda (Ailurus fulgens) is a small mammal. It lives in the Himalayas."
        self.assertEqual(show_me._first_sentence(text), "The red panda is a small mammal.")
        nested = ("The Taj Mahal (TAHJ mə-HAHL; lit. 'Crown of the Palace', "
                  "pronounced [taːdʒ ˈmɛɦ(ɛ)l]) is an ivory-white "
                  "marble mausoleum. It was built in 1632.")
        self.assertEqual(show_me._first_sentence(nested), "The Taj Mahal is an ivory-white marble mausoleum.")

    def test_pictures_are_shrunk_to_fit(self):
        from PIL import Image
        noisy = Image.effect_noise((2000, 1500), 100).convert("RGB")
        buf = io.BytesIO()
        noisy.save(buf, format="PNG")
        jpeg = show_me.to_jpeg(buf.getvalue())
        self.assertIsNotNone(jpeg)
        self.assertLessEqual(len(jpeg), show_me.MAX_IMAGE_BYTES)
        self.assertLessEqual(max(Image.open(io.BytesIO(jpeg)).size), show_me.MAX_SIDE)
        self.assertIsNone(show_me.to_jpeg(b"not an image"))

    def test_nothing_on_wikipedia_says_so_and_offline_lets_the_llm_answer(self):
        with mock.patch.object(show_me, "find", return_value=None):
            self.assertEqual(show_me.try_handle("show me the zorblax"),
                             "I couldn't find a picture of zorblax.")
        with mock.patch.object(show_me, "find", side_effect=OSError("offline")):
            self.assertIsNone(show_me.try_handle("show me the Taj Mahal"))

    def test_a_found_picture_goes_to_the_socket(self):
        with mock.patch.object(show_me, "find", return_value=("Taj Mahal", "It is a tomb.", b"\xff\xd8jpeg")), \
                mock.patch.object(avatar_server, "show_card") as shown:
            said = show_me.try_handle("show me the Taj Mahal")
        self.assertEqual(said, "Here's Taj Mahal. It is a tomb.")
        card = shown.call_args[0][0]
        self.assertEqual((card["kind"], card["title"], card["source"]), ("image", "Taj Mahal", "Wikipedia"))
        self.assertTrue(card["image"].startswith("data:image/jpeg;base64,"))


class RequestChannel(unittest.TestCase):
    """A real avatar socket on a spare port, with the token in a temporary APPDATA."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"APPDATA": cls.tmp.name})
        cls.env.start()
        cls.port = _free_port()
        cls.port_patch = mock.patch.object(avatar_server, "PORT", cls.port)
        cls.port_patch.start()
        avatar_server.set_request_handlers(ask=lambda text: f"you asked: {text}",
                                           look=lambda: "a text editor",
                                           say=lambda text: f"said: {text}")
        assert avatar_server.start()
        with open(avatar_server.token_path(), encoding="ascii") as f:
            cls.token = f.read()

    @classmethod
    def tearDownClass(cls):
        avatar_server.stop()
        cls.port_patch.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def run_client(self, steps, origin=None):
        async def go():
            url = f"ws://127.0.0.1:{self.port}"
            kwargs = {"origin": origin} if origin else {}
            got = []
            async with websockets.connect(url, **kwargs) as ws:
                for send in steps:
                    await ws.send(json.dumps(send))
                    while True:
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                        except websockets.ConnectionClosed as closed:
                            got.append(("closed", closed.rcvd.code if closed.rcvd else None))
                            return got
                        if msg.get("type") in ("hello_ok", "reply"):
                            got.append(msg)
                            break  # broadcasts (state replays) are skipped
            return got
        return asyncio.run(go())

    def test_token_is_written_for_this_run(self):
        self.assertGreaterEqual(len(self.token), 40)

    def test_ask_and_look_after_hello(self):
        got = self.run_client([
            {"type": "hello", "token": self.token},
            {"type": "ask", "id": "1", "text": "what time is it"},
            {"type": "look", "id": "2"},
        ])
        self.assertEqual(got[0], {"type": "hello_ok"})
        self.assertEqual(got[1], {"type": "reply", "id": "1", "ok": True, "text": "you asked: what time is it"})
        self.assertEqual(got[2], {"type": "reply", "id": "2", "ok": True, "text": "a text editor"})

    def test_say_after_hello(self):
        got = self.run_client([
            {"type": "hello", "token": self.token},
            {"type": "say", "id": "3", "text": "It's 5 pm."},
            {"type": "say", "id": "4", "text": "  "},
            {"type": "say", "id": "5", "text": "x" * 1001},
        ])
        self.assertEqual(got[1], {"type": "reply", "id": "3", "ok": True, "text": "said: It's 5 pm."})
        self.assertEqual([g["ok"] for g in got[2:]], [False, False])

    def test_saying_without_hello_is_closed(self):
        got = self.run_client([{"type": "say", "id": "1", "text": "hello"}])
        self.assertEqual(got, [("closed", 4001)])

    def test_wrong_token_is_closed(self):
        got = self.run_client([{"type": "hello", "token": "nope"}])
        self.assertEqual(got, [("closed", 4001)])

    def test_asking_without_hello_is_closed(self):
        got = self.run_client([{"type": "ask", "id": "1", "text": "shut down"}])
        self.assertEqual(got, [("closed", 4001)])

    def test_a_web_page_cannot_use_it(self):
        got = self.run_client([{"type": "hello", "token": self.token}], origin="https://evil.example")
        self.assertEqual(got, [("closed", 4001)])

    def test_empty_or_long_questions_are_refused(self):
        got = self.run_client([
            {"type": "hello", "token": self.token},
            {"type": "ask", "id": "1", "text": "   "},
            {"type": "ask", "id": "2", "text": "x" * 501},
        ])
        self.assertEqual([g["ok"] for g in got[1:]], [False, False])

    def test_before_she_is_online_it_says_so(self):
        with mock.patch.dict(avatar_server._handlers, clear=True):
            got = self.run_client([{"type": "hello", "token": self.token},
                                   {"type": "ask", "id": "7", "text": "what time is it"}])
        self.assertEqual((got[1]["ok"], got[1]["text"]), (False, "I'm still starting up. Ask me again in a moment."))

    def test_a_failing_handler_still_answers(self):
        with mock.patch.dict(avatar_server._handlers, {"look": lambda: 1 / 0}):
            got = self.run_client([{"type": "hello", "token": self.token}, {"type": "look", "id": "9"}])
        self.assertEqual((got[1]["id"], got[1]["ok"]), ("9", False))


class BusyRequest(unittest.TestCase):
    """main.handle_phone_command with lock_timeout: a notch request that can't start in time is
    dropped, never run later after the notch was told it failed."""

    def test_gives_up_without_running_while_a_turn_holds_the_lock(self):
        import main
        with mock.patch.object(main, "route_transcript") as route, \
             mock.patch.object(main, "confirmation_reply", return_value=None):
            main._turn_lock.acquire()
            try:
                reply = main.handle_phone_command(object(), "open notepad", lock_timeout=0.1)
            finally:
                main._turn_lock.release()
            self.assertEqual(reply, main._say("busy"))
            route.assert_not_called()
        self.assertFalse(main._turn_lock.locked())

    def test_runs_when_free_and_releases_the_lock(self):
        import main
        with mock.patch.object(main, "route_transcript", return_value="Opened Notepad."), \
             mock.patch.object(main, "confirmation_reply", return_value=None), \
             mock.patch.object(main, "_remember_reply"), mock.patch.object(main, "_engine"):
            self.assertEqual(main.handle_phone_command(object(), "open notepad", lock_timeout=0.1),
                             "Opened Notepad.")
        self.assertFalse(main._turn_lock.locked())

    def test_saying_waits_for_a_turn_and_releases_the_lock(self):
        import main
        with mock.patch.object(main, "announce_unprompted") as announce:
            main._turn_lock.acquire()
            try:
                self.assertEqual(main.say_for_notch(object(), "hi", lock_timeout=0.1), main._say("busy"))
            finally:
                main._turn_lock.release()
            announce.assert_not_called()
            self.assertEqual(main.say_for_notch("spk", "hi", lock_timeout=0.1), "Done.")
            announce.assert_called_once_with("spk", "hi")
        self.assertFalse(main._turn_lock.locked())


if __name__ == "__main__":
    unittest.main()
