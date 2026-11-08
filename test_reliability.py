"""
Tests for starting Ollama when it isn't running (ollama_guard.py, llm_brain.py) and for the rotating
assistant.log (main.py).

Run: venv\\Scripts\\python.exe -m unittest test_reliability -v
Nothing is started for real: whether Ollama answers, the launcher and the clock are stand-ins.
"""

import logging.handlers
import os
import tempfile
import unittest
from unittest import mock

import ollama_guard


class _World:
    """Ollama comes up `up_after` seconds after it is launched (None = never)."""

    def __init__(self, up=False, up_after=2.0):
        self.now, self.up_now, self.up_after = 0.0, up, up_after
        self.launched = []

    def up(self):
        return self.up_now

    def launch(self, cmd):
        self.launched.append(cmd)
        if self.up_after is not None:
            self.ready_at = self.now + self.up_after

    def sleep(self, s):
        self.now += s
        if self.up_after is not None and self.launched and self.now >= self.ready_at:
            self.up_now = True

    def ensure(self, cmds=(["ollama app.exe"], ["ollama.exe", "serve"])):
        return ollama_guard.ensure_running(wait=10, up=self.up, launch=self.launch,
                                           commands=lambda: [list(c) for c in cmds],
                                           clock=lambda: self.now, sleep=self.sleep)


class EnsureRunning(unittest.TestCase):
    def setUp(self):
        ollama_guard._last_attempt[0] = None
        patch = mock.patch.object(ollama_guard.config, "OLLAMA_HOST", "http://localhost:11434")
        patch.start()
        self.addCleanup(patch.stop)

    def test_already_running_starts_nothing(self):
        world = _World(up=True)
        self.assertTrue(world.ensure())
        self.assertEqual(world.launched, [])

    def test_starts_the_tray_app_and_waits_for_it(self):
        world = _World(up_after=3.0)
        self.assertTrue(world.ensure())
        self.assertEqual(world.launched, [["ollama app.exe"]])
        self.assertGreaterEqual(world.now, 3.0)

    def test_falls_back_to_ollama_serve(self):
        world = _World(up_after=None)
        launched = []

        def launch(cmd):
            launched.append(cmd)
            world.launched.append(cmd)
            if cmd[-1] == "serve":
                world.up_after, world.ready_at = 1.0, world.now + 1.0
        world.launch = launch
        self.assertTrue(world.ensure())
        self.assertEqual(launched, [["ollama app.exe"], ["ollama.exe", "serve"]])

    def test_gives_up_and_doesnt_retry_right_away(self):
        world = _World(up_after=None)
        self.assertFalse(world.ensure())
        self.assertEqual(len(world.launched), 2)
        world.now += 5
        self.assertFalse(world.ensure())                   # within RETRY_AFTER: not launched again
        self.assertEqual(len(world.launched), 2)
        world.now += ollama_guard.RETRY_AFTER
        world.ensure()
        self.assertEqual(len(world.launched), 4)

    def test_never_starts_a_remote_ollama(self):
        world = _World()
        with mock.patch.object(ollama_guard.config, "OLLAMA_HOST", "http://192.168.1.20:11434"):
            self.assertFalse(world.ensure())
        self.assertEqual(world.launched, [])

    def test_can_be_turned_off(self):
        world = _World()
        with mock.patch.object(ollama_guard.config, "OLLAMA_AUTOSTART", False, create=True):
            self.assertFalse(world.ensure())
        self.assertEqual(world.launched, [])

    def test_which_errors_mean_not_running(self):
        import ollama
        self.assertTrue(ollama_guard.is_connection_error(ConnectionError("Failed to connect to Ollama.")))
        self.assertFalse(ollama_guard.is_connection_error(ollama.ResponseError("model not found", 404)))


class AskAgainOnceStarted(unittest.TestCase):
    """llm_brain: a request that can't connect starts Ollama and is asked once more."""

    def _brain(self, client):
        import llm_brain
        brain = llm_brain.Brain.__new__(llm_brain.Brain)
        brain.client, brain.messages, brain.last_turn_idle = client, [{"role": "user", "content": "hi"}], False
        brain._log_timing = lambda *a, **k: None
        brain._finish_turn = lambda reply: reply
        brain._chat_options = lambda: {}
        brain._may_retry = lambda: False
        return llm_brain, brain

    def test_started_then_answered(self):
        client = mock.Mock()
        client.chat.side_effect = [ConnectionError("Failed to connect to Ollama."),
                                   {"message": {"content": "Hello there."}}]
        llm_brain, brain = self._brain(client)
        with mock.patch.object(llm_brain.ollama_guard, "is_up", return_value=False), \
             mock.patch.object(llm_brain.ollama_guard, "ensure_running", return_value=True) as ensure:
            self.assertEqual(brain._respond_blocking(), "Hello there.")
        ensure.assert_called_once()
        self.assertEqual(client.chat.call_count, 2)

    def test_not_started_says_so(self):
        client = mock.Mock()
        client.chat.side_effect = ConnectionError("Failed to connect to Ollama.")
        llm_brain, brain = self._brain(client)
        with mock.patch.object(llm_brain.ollama_guard, "is_up", return_value=False), \
             mock.patch.object(llm_brain.ollama_guard, "ensure_running", return_value=False):
            self.assertIn("trouble reaching", brain._respond_blocking())
        self.assertEqual(client.chat.call_count, 1)

    def test_a_timeout_while_running_is_not_asked_twice(self):
        client = mock.Mock()
        client.chat.side_effect = TimeoutError("timed out")
        llm_brain, brain = self._brain(client)
        with mock.patch.object(llm_brain.ollama_guard, "is_up", return_value=True), \
             mock.patch.object(llm_brain.ollama_guard, "ensure_running") as ensure:
            brain._respond_blocking()
        ensure.assert_not_called()
        self.assertEqual(client.chat.call_count, 1)


class GameFreesTheLLM(unittest.TestCase):
    """A game in front: quiet_mode reports it, the LLM is unloaded, and it is loaded again afterwards."""

    def _quiet(self, full, mics=()):
        import quiet_mode
        clock = [0.0]
        q = quiet_mode.QuietMode(fullscreen=lambda: full[0], mics=lambda: list(mics), now=lambda: clock[0])
        return quiet_mode, q, clock

    def test_listeners_hear_a_game_come_and_go(self):
        full = [False]
        quiet_mode, q, clock = self._quiet(full)
        heard = []
        with mock.patch.object(quiet_mode, "_fullscreen_listeners", [heard.append]):
            q.poll()
            full[0] = True
            q.poll()
            q.poll()                                    # still in the game: no second call
            full[0] = False
            clock[0] = 1.0
            q.poll()                                    # just left: game mode lingers a little
            clock[0] = 1.0 + quiet_mode.EXIT_AFTER_SECONDS + 1
            q.poll()
        self.assertEqual(heard, [True, False])

    def test_a_call_during_a_game_still_counts_as_the_game(self):
        full = [True]
        quiet_mode, q, _clock = self._quiet(full, mics=["Discord.exe"])
        heard = []
        with mock.patch.object(quiet_mode, "_fullscreen_listeners", [heard.append]):
            q.poll()
        self.assertEqual(q.mode, "call")
        self.assertEqual(heard, [True])

    def test_keep_alive_is_short_while_gaming(self):
        import llm_brain
        with mock.patch.object(llm_brain.config, "OLLAMA_KEEP_ALIVE", "4h"), \
             mock.patch.object(llm_brain.config, "GAME_LLM_KEEP_ALIVE", "2m", create=True), \
             mock.patch.object(llm_brain.config, "GAME_UNLOAD_LLM", True, create=True), \
             mock.patch("quiet_mode.fullscreen", return_value=True):
            self.assertEqual(llm_brain.keep_alive(), "2m")
        with mock.patch.object(llm_brain.config, "OLLAMA_KEEP_ALIVE", "4h"), \
             mock.patch("quiet_mode.fullscreen", return_value=False):
            self.assertEqual(llm_brain.keep_alive(), "4h")
        with mock.patch.object(llm_brain.config, "OLLAMA_KEEP_ALIVE", "4h"), \
             mock.patch.object(llm_brain.config, "GAME_UNLOAD_LLM", False, create=True), \
             mock.patch("quiet_mode.fullscreen", return_value=True):
            self.assertEqual(llm_brain.keep_alive(), "4h")

    def test_no_warm_up_while_a_game_is_in_front(self):
        import llm_brain
        brain = llm_brain.Brain.__new__(llm_brain.Brain)
        brain.client = mock.Mock()
        with mock.patch.object(llm_brain.ollama_guard, "ensure_running", return_value=True), \
             mock.patch("quiet_mode.fullscreen", return_value=False), \
             mock.patch("quiet_mode.fullscreen_now", return_value=True):
            brain.warm_up()
        brain.client.chat.assert_not_called()

    def test_unloaded_for_the_game_and_loaded_after(self):
        import llm_brain
        import quiet_mode
        brain = llm_brain.Brain.__new__(llm_brain.Brain)
        brain.client = mock.Mock()
        listeners = []
        with mock.patch.object(quiet_mode, "_fullscreen_listeners", listeners), \
             mock.patch.object(brain, "warm_up_async") as warm:
            brain.follow_games()
            listeners[0](True)
            brain.client.generate.assert_called_once_with(model=llm_brain.config.OLLAMA_MODEL, keep_alive=0)
            warm.assert_not_called()
            listeners[0](False)
            warm.assert_called_once()


class RotatingLog(unittest.TestCase):
    def test_log_starts_over_at_the_size_limit(self):
        import main
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "assistant.log")
            with mock.patch.object(main.config, "LOG_FILE", path), \
                 mock.patch.object(main.config, "LOG_MAX_MB", 0.001, create=True), \
                 mock.patch.object(main.config, "LOG_BACKUPS", 2, create=True):
                handler = main._log_file_handler()
            try:
                self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
                log = logging.getLogger("test_rotating_log")
                log.propagate = False
                log.addHandler(handler)
                for i in range(200):
                    log.warning("line %d %s", i, "x" * 40)
                self.assertTrue(os.path.exists(path + ".1"))
                self.assertFalse(os.path.exists(path + ".3"))
                self.assertLessEqual(os.path.getsize(path), 1100)
            finally:
                logging.getLogger("test_rotating_log").removeHandler(handler)
                handler.close()

    def test_zero_keeps_one_growing_file(self):
        import main
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(main.config, "LOG_FILE", os.path.join(d, "a.log")), \
                 mock.patch.object(main.config, "LOG_MAX_MB", 0, create=True):
                handler = main._log_file_handler()
            handler.close()
            self.assertNotIsInstance(handler, logging.handlers.RotatingFileHandler)


if __name__ == "__main__":
    unittest.main()
