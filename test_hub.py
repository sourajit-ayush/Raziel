"""
Tests for Raziel Hub: deliverables.py (what skills produce) and hub_server.py (the page's server and its locks).

Run: venv\\Scripts\\python.exe -m unittest test_hub -v
A temporary deliverables folder, token, vault and memory.db; the server runs on a spare local port.
"""

import http.client
import json
import os
import shutil
import socket
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import deliverables
import hub_server
import memory
import obsidian_memory


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.vault = os.path.join(self.dir, "vault")
        os.makedirs(self.vault)
        for name, value in (("DELIVERABLES_DIR", os.path.join(self.dir, "deliverables")),
                            ("MEMORY_DB_PATH", os.path.join(self.dir, "memory.db")),
                            ("OBSIDIAN_VAULT", self.vault), ("LOG_FILE", os.path.join(self.dir, "assistant.log"))):
            p = mock.patch.object(deliverables.config, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        memory.init_db()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Deliverables(_Temp):
    def test_publish_load_and_body(self):
        did = deliverables.publish("briefing", "Afternoon briefing", "29° in Dhupgari", being="time",
                                   body="# Briefing\n\nfull text", when=datetime(2026, 11, 8, 14, 34, 12))
        self.assertTrue(did.startswith("20261108-143412-briefing-"))
        [m] = deliverables.load()
        self.assertEqual((m["being"], m["kind"], m["title"]), ("time", "report", "Afternoon briefing"))
        self.assertEqual(deliverables.body(did), "# Briefing\n\nfull text")

    def test_unknown_being_and_kind_fall_back(self):
        deliverables.publish("x", "t", being="pluto", kind="poem")
        [m] = deliverables.load()
        self.assertEqual((m["being"], m["kind"]), ("nexus", "report"))

    def test_newest_first_filter_and_prune(self):
        with mock.patch.object(deliverables, "MAX_KEEP", 3):
            for i in range(5):
                deliverables.publish("s", f"item {i}", being="music" if i % 2 else "time",
                                     body="b", when=datetime(2026, 11, 8, 10, i))
        titles = [m["title"] for m in deliverables.load()]
        self.assertEqual(titles, ["item 4", "item 3", "item 2"])
        self.assertEqual([m["title"] for m in deliverables.load(being="music")], ["item 3"])
        self.assertEqual(len([n for n in os.listdir(deliverables.folder()) if n.endswith(".md")]), 3)

    def test_body_only_for_its_own_ids(self):
        for bad in ("../secret", "..\\x", "20261108-143412-a-zzzz", "", "x" * 300):
            self.assertIsNone(deliverables.body(bad), bad)

    def test_turns(self):
        deliverables.record_turn("what's the date", 4.341, 0.112, 0.75, when=datetime(2026, 11, 8, 14, 35))
        deliverables.record_turn("Resil", 19.5, 0.0, None)
        a, b = deliverables.turns()
        self.assertEqual((a["said"], a["total"]), ("what's the date", 5.2))
        self.assertIsNone(b["total"])

    def test_never_raises(self):
        with mock.patch.object(deliverables, "folder", side_effect=OSError("disk full")):
            self.assertIsNone(deliverables.publish("x", "t"))
            deliverables.record_turn("x", 1, 1, 1)


class Metrics(unittest.TestCase):
    def test_daily_transcribe_from_the_log(self):
        log = ("2026-11-07 23:35:16,425 [INFO] Transcribing...\n"
               "2026-11-07 23:35:42,830 [INFO] Transcript: What's Today's Date?\n"
               "2026-11-08 14:35:02,708 [INFO] Transcribing...\n"
               "2026-11-08 14:35:07,053 [INFO] Transcript: What is today's date?\n")
        self.assertEqual(hub_server.daily_transcribe(log),
                         [{"day": "2026-11-07", "median": 26.4, "turns": 1},
                          {"day": "2026-11-08", "median": 4.3, "turns": 1}])


class Server(_Temp):
    def setUp(self):
        super().setUp()
        self.port = _free_port()
        for target, name, value in ((hub_server, "token_path", lambda: os.path.join(self.dir, "hub.token")),
                                    (hub_server, "_token", None), (hub_server, "_server", None),
                                    (hub_server.config, "HUB_PORT", self.port)):
            p = mock.patch.object(target, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.asked = []

        def ask(text):
            self.asked.append(text)
            return "Sunday, November 8, 2026."
        self.assertTrue(hub_server.start(ask))
        self.addCleanup(hub_server.stop)
        self.token = hub_server.token()

    def req(self, method, path, body=None, headers=None, token=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Raziel-Token"] = self.token
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_the_page_has_a_strict_policy(self):
        r, data = self.req("GET", "/", token=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"Raziel Hub", data)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_the_universe_script_and_the_pulse(self):
        r, data = self.req("GET", "/universe.js", token=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"const Universe", data)
        self.assertIn("javascript", r.getheader("Content-Type"))
        r, _ = self.req("GET", "/api/pulse", token=False)
        self.assertEqual(r.status, 403)
        with mock.patch("universes.active", return_value="forge"):
            r, data = self.req("GET", "/api/pulse")
        p = json.loads(data)
        self.assertEqual(p["active"], "forge")
        self.assertIn(p["state"], ("idle", "listening", "thinking", "speaking", "unknown"))

    def test_the_page_uses_only_its_own_files(self):
        # the policy allows no inline script or style and nothing from another site: the page must not need any
        for name in ("index.html", "app.js", "universe.js", "app.css"):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub", name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("https://", text.replace("http://www.w3.org/", ""), name)
            self.assertNotIn("innerHTML", text, name)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub", "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertNotIn(" style=", page)
        self.assertNotIn("<style", page)

    def test_the_api_needs_the_token(self):
        r, _ = self.req("GET", "/api/overview", token=False)
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/api/overview", headers={"X-Raziel-Token": "wrong"}, token=False)
        self.assertEqual(r.status, 403)

    def test_other_websites_are_refused(self):
        r, _ = self.req("GET", "/api/overview", host="evil.example:80")          # DNS rebinding
        self.assertEqual(r.status, 421)
        r, _ = self.req("GET", "/api/overview", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/ask", body={"text": "hi"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)                                           # no form-post trick
        self.assertEqual(self.asked, [])

    def test_overview_items_and_metrics(self):
        deliverables.publish("briefing", "Afternoon briefing", "29°", being="time", body="full")
        r, data = self.req("GET", "/api/overview")
        self.assertEqual(r.status, 200)
        o = json.loads(data)
        self.assertEqual([b["key"] for b in o["beings"]][:2], ["nexus", "music"])
        self.assertIn("time", o["stats"])
        r, data = self.req("GET", "/api/items?being=time")
        [it] = json.loads(data)["items"]
        self.assertEqual(it["title"], "Afternoon briefing")
        r, data = self.req("GET", "/api/item?id=" + it["id"])
        self.assertEqual(json.loads(data)["body"], "full")
        r, _ = self.req("GET", "/api/item?id=..%2F..%2Fconfig")
        self.assertEqual(r.status, 404)
        r, data = self.req("GET", "/api/metrics")
        self.assertIn("days", json.loads(data))

    def test_ask_answers_and_is_kept(self):
        r, data = self.req("POST", "/api/ask", body={"text": "what's the date"})
        self.assertEqual(json.loads(data), {"ok": True, "reply": "Sunday, November 8, 2026."})
        self.assertEqual(self.asked, ["what's the date"])
        self.assertEqual(deliverables.load()[0]["kind"], "answer")
        r, _ = self.req("POST", "/api/ask", body={"text": "x" * 501})
        self.assertEqual(r.status, 400)

    def test_forget_a_fact(self):
        obsidian_memory.add_fact("User has an exam on Friday")
        obsidian_memory.add_fact("User likes lo-fi")
        r, data = self.req("POST", "/api/forget", body={"fact": "User has an exam on Friday"})
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(obsidian_memory.facts(), ["User likes lo-fi"])
        r, data = self.req("POST", "/api/forget", body={"fact": "lo-fi"})              # exact lines only
        self.assertFalse(json.loads(data)["ok"])


if __name__ == "__main__":
    unittest.main()
