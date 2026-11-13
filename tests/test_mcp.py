"""
Tests for MCP support (mcp_client.py, mcp_tools.py) against mcp_fake_server.py - a real process
speaking MCP over stdio, but no packages and no network.

Run: venv\\Scripts\\python.exe -m unittest test_mcp -v
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "raziel"))

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import confirmation
import config
import mcp_tools
from mcp_client import McpError, McpServer, result_text

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_fake_server.py")


def fake_spec(**extra):
    spec = {"command": sys.executable, "args": ["-I", FAKE], "label": "Fake"}
    spec.update(extra)
    return spec


class Client(unittest.TestCase):
    def setUp(self):
        self.srv = McpServer("fake", sys.executable, ["-I", FAKE])
        self.srv.start(timeout=20)
        self.addCleanup(self.srv.close)

    def test_handshake_and_paged_tool_list(self):
        self.assertEqual(self.srv.server_info.get("name"), "fake")
        names = [t["name"] for t in self.srv.list_tools()]
        self.assertEqual(names, ["echo", "save_note", "fail", "slow"])      # both pages

    def test_calls(self):
        self.assertEqual(result_text(self.srv.call_tool("echo", {"text": "hello"})), "hello")
        failed = self.srv.call_tool("fail", {})
        self.assertTrue(failed.get("isError"))
        with self.assertRaises(McpError):
            self.srv.call_tool("no_such_tool", {})

    def test_parallel_calls_get_their_own_answers(self):
        out = {}

        def ask(word):
            out[word] = result_text(self.srv.call_tool("echo", {"text": word}))
        threads = [threading.Thread(target=ask, args=(w,)) for w in ("one", "two", "three", "four")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(out, {w: w for w in ("one", "two", "three", "four")})

    def test_timeout_then_still_usable(self):
        with self.assertRaises(McpError):
            self.srv.call_tool("slow", {"seconds": 1.5}, timeout=0.3)
        self.assertEqual(result_text(self.srv.call_tool("echo", {"text": "after"}, timeout=10)), "after")

    def test_a_dead_server_fails_at_once(self):
        self.srv.proc.kill()
        self.srv.proc.wait(5)
        started = time.time()
        with self.assertRaises(McpError):
            self.srv.call_tool("echo", {"text": "x"}, timeout=20)
        self.assertLess(time.time() - started, 5)
        self.assertFalse(self.srv.alive)

    def test_a_missing_program_is_an_error(self):
        with self.assertRaises(McpError):
            McpServer("nope", "this-program-does-not-exist-42").start(timeout=5)


class ResultText(unittest.TestCase):
    def test_blocks(self):
        self.assertEqual(result_text({"content": [{"type": "text", "text": "a"},
                                                  {"type": "image", "data": "...", "mimeType": "image/png"},
                                                  {"type": "text", "text": "b"}]}),
                         "a\n[an image - not shown]\nb")
        self.assertEqual(result_text({"content": [], "structuredContent": {"n": 1}}), '{"n": 1}')


class Tools(unittest.TestCase):
    """mcp_tools: what the LLM sees, and what happens when it calls something."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(mcp_tools, "CACHE_PATH", os.path.join(tmp, "cache.json")),
            mock.patch.object(mcp_tools, "_servers", {}),
            mock.patch.object(mcp_tools, "_ready", {}),
            mock.patch.object(mcp_tools, "_tools", {}),
            mock.patch.object(mcp_tools, "_started", False),
            mock.patch.object(confirmation, "_voice_refusal", return_value=None),
            mock.patch.object(confirmation, "_voice_answer_refusal", return_value=None),
            mock.patch.object(config, "CONFIRM_RISKY_ACTIONS", True, create=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(mcp_tools.shutdown)
        self.addCleanup(confirmation.clear)
        self.schemas = [{"type": "function", "function": {"name": "open_app"}}]
        self.functions = {"open_app": lambda **k: "opened"}

    def use(self, servers, max_tools=15):
        for name, value in (("MCP_SERVERS", servers), ("MCP_MAX_TOOLS", max_tools)):
            p = mock.patch.object(config, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        mcp_tools.install(self.schemas, self.functions)

    def connect(self, name="fake"):
        self.assertIsNotNone(mcp_tools._connect(name, mcp_tools.servers_config()[name]))

    def names(self):
        return [s["function"]["name"] for s in self.schemas]

    def test_no_servers_changes_nothing(self):
        self.use({})
        self.assertEqual(self.names(), ["open_app"])

    def test_tools_appear_after_the_built_in_ones(self):
        self.use({"fake": fake_spec()})
        self.connect()
        self.assertEqual(self.names(), ["open_app", "mcp_fake_echo", "mcp_fake_save_note",
                                        "mcp_fake_fail", "mcp_fake_slow"])
        echo = self.schemas[1]["function"]
        self.assertTrue(echo["description"].startswith("[Fake] Repeats"))
        self.assertNotIn("$schema", echo["parameters"])
        self.assertEqual(echo["parameters"]["required"], ["text"])

    def test_cached_tools_are_there_before_the_server_starts(self):
        self.use({"fake": fake_spec()})
        self.connect()
        mcp_tools.shutdown()
        first = list(self.schemas)
        with mock.patch.object(mcp_tools, "_tools", {}):
            self.schemas[:] = self.schemas[:1]
            mcp_tools.install(self.schemas, self.functions)
            self.assertEqual(self.schemas, first)                   # same list, same order: Ollama's cache holds
            self.assertEqual(mcp_tools._servers, {})                # and nothing was started for it

    def test_allow_list_and_cap(self):
        self.use({"fake": fake_spec(tools=["echo", "fail"])})
        self.connect()
        self.assertEqual(self.names()[1:], ["mcp_fake_echo", "mcp_fake_fail"])
        mcp_tools.shutdown()
        with mock.patch.object(config, "MCP_MAX_TOOLS", 1), mock.patch.object(mcp_tools, "_tools", {}):
            self.schemas[:] = self.schemas[:1]
            with mock.patch.object(mcp_tools, "CACHE_PATH", mcp_tools.CACHE_PATH + ".x"):
                self.connect()
            self.assertEqual(self.names()[1:], ["mcp_fake_echo"])

    def test_read_only_runs_at_once(self):
        self.use({"fake": fake_spec()})
        self.connect()
        out = self.functions["mcp_fake_echo"](text="hi there")
        self.assertIn("hi there", out)
        self.assertIn("not instructions", out)
        self.assertFalse(confirmation.has_pending())

    def test_changing_something_asks_first(self):
        self.use({"fake": fake_spec()})
        self.connect()
        question = self.functions["mcp_fake_save_note"](text="buy milk")
        self.assertEqual(question, "Should I use Fake to save note, with text buy milk? "
                                   "Say yes to go ahead, or no to cancel.")
        self.assertTrue(confirmation.has_pending())
        confirmation.settle(question)                              # she said it
        outcome = confirmation.handle_transcript("yes")
        self.assertIn("Saved note 1.", outcome.reply)

    def test_no_means_nothing_runs(self):
        self.use({"fake": fake_spec()})
        self.connect()
        question = self.functions["mcp_fake_save_note"](text="x")
        confirmation.settle(question)
        self.assertIn("Cancelled", confirmation.handle_transcript("no").reply)
        self.assertIn("Saved note 1.", mcp_tools.run("mcp_fake_save_note", {"text": "y"}))  # first real save

    def test_trusted_tools_skip_the_question(self):
        self.use({"fake": fake_spec(trusted=["save_note"])})
        self.connect()
        self.assertIn("Saved note 1.", self.functions["mcp_fake_save_note"](text="x"))

    def test_errors_are_spoken_plainly(self):
        self.use({"fake": fake_spec(trusted=["fail"])})
        self.connect()
        self.assertEqual(self.functions["mcp_fake_fail"](), "Fake couldn't do that: disk full")

    def test_a_server_that_wont_start(self):
        self.use({"broken": {"command": "this-program-does-not-exist-42", "label": "Broken"}})
        mcp_tools._tools["mcp_broken_x"] = {"server": "broken", "tool": "x", "label": "Broken",
                                            "read_only": True, "title": ""}
        self.assertEqual(mcp_tools.run("mcp_broken_x", {}), "I can't reach Broken right now.")

    def test_restarts_a_server_that_stopped(self):
        self.use({"fake": fake_spec()})
        self.connect()
        srv = mcp_tools._servers["fake"]
        srv.proc.kill()
        srv.proc.wait(5)
        time.sleep(0.2)
        self.assertIn("again", self.functions["mcp_fake_echo"](text="again"))
        self.assertIsNot(mcp_tools._servers["fake"], srv)


class Paths(unittest.TestCase):
    """What qwen3:8b really sent for "my Documents" (and the like) ends up inside the folder."""
    ROOT = r"C:\Users\Ayush\Documents"

    def test_what_the_model_sends(self):
        r = self.ROOT
        for sent, want in [
            (".", r), ("", r), ("Documents", r), ("my Documents", r), ("Documents/Exam Notes", r + r"\Exam Notes"),
            ("Exam Notes", r + r"\Exam Notes"), (r"C:\Users\<username>\Documents", r),
            (r"C:\Users\User\Documents\Exam Notes", r + r"\Exam Notes"), (r"C:\Users\Ayush\Documents", r),
            (r"\Notes\todo.txt", r + r"\Notes\todo.txt"),
        ]:
            self.assertEqual(mcp_tools.fix_path(r, sent), want, sent)

    def test_outside_stays_outside(self):
        # (the server refuses these; they are not quietly moved into the folder)
        self.assertEqual(mcp_tools.fix_path(self.ROOT, r"C:\Users\Ayush\Desktop\x.txt"),
                         r"C:\Users\Ayush\Desktop\x.txt")
        self.assertEqual(mcp_tools.fix_path(self.ROOT, r"D:\secret"), r"D:\secret")

    def test_only_path_arguments_change(self):
        out = mcp_tools._fix_args(self.ROOT, {"path": "Documents", "pattern": "*.pdf",
                                              "paths": ["a.txt", "Documents/b.txt"]})
        self.assertEqual(out, {"path": self.ROOT, "pattern": "**/*.pdf",        # every subfolder
                               "paths": [self.ROOT + r"\a.txt", self.ROOT + r"\b.txt"]})

    def test_question_says_the_short_path(self):
        info = {"title": "", "tool": "create_directory"}
        self.assertEqual(mcp_tools._action(info, {"path": self.ROOT + r"\Exam Notes"}, self.ROOT),
                         "create directory, with path Exam Notes")


class Brain(unittest.TestCase):
    def test_mcp_results_are_read_by_the_model(self):
        import llm_brain
        fake = mock.Mock(_final_flags=[], _asked_llm=False)
        self.assertTrue(llm_brain.Brain._needs_synthesis(fake, ["mcp_fake_echo"]))
        self.assertFalse(llm_brain.Brain._needs_synthesis(fake, ["open_app"]))


if __name__ == "__main__":
    unittest.main()
