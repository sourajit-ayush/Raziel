"""
Tests for Raziel's memory in an Obsidian vault (obsidian_memory.py, and its use in llm_brain.py / tools.py).

Run: venv\\Scripts\\python.exe -m unittest test_obsidian_memory -v
Everything happens in a temporary vault and a temporary memory.db; the LLM and the embeddings are stand-ins.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "raziel"))

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import memory
import obsidian_memory as om


def _chat_returning(data):
    return lambda messages: {"message": {"content": json.dumps(data) if not isinstance(data, str) else data}}


class _Vault(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.vault = os.path.join(self.dir, "Raziel")
        os.makedirs(self.vault)
        self.db = os.path.join(self.dir, "memory.db")
        for name, value in (("OBSIDIAN_VAULT", self.vault), ("MEMORY_DB_PATH", self.db),
                            ("OBSIDIAN_MIN_USER_TURNS", 2), ("OBSIDIAN_MIN_SCORE", 0.45)):
            p = mock.patch.object(om.config, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        memory.init_db()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, *parts):
        with open(os.path.join(self.vault, *parts), encoding="utf-8") as f:
            return f.read()


class Facts(_Vault):
    def test_first_use_keeps_what_she_already_knew(self):
        memory.add_fact("User has an exam on Friday")
        self.assertEqual(om.facts(), ["User has an exam on Friday"])
        self.assertIn("- User has an exam on Friday", self.read("Facts.md"))

    def test_your_edits_in_obsidian_are_what_she_reads(self):
        om.facts()
        with open(os.path.join(self.vault, "Facts.md"), "a", encoding="utf-8") as f:
            f.write("- User's sister is called Riya\n- [ ] a task, not a fact\n\nsome prose\n")
        self.assertEqual(om.facts(), ["User's sister is called Riya"])

    def test_add_skips_duplicates_and_replaces_a_changed_favourite(self):
        om.add_fact("User's favorite band is Coldplay")
        om.add_fact("user's favorite band is coldplay")
        om.add_fact("User's favorite band is Linkin Park", replace_prefix="User's favorite band is")
        self.assertEqual(om.facts(), ["User's favorite band is Linkin Park"])

    def test_forget_removes_matching_lines(self):
        om.add_fact("User has an exam on Friday")
        om.add_fact("User likes lo-fi music")
        self.assertEqual(om.forget_matching("EXAM"), ["User has an exam on Friday"])
        self.assertEqual(om.facts(), ["User likes lo-fi music"])

    def test_off_without_a_vault(self):
        with mock.patch.object(om.config, "OBSIDIAN_VAULT", None):
            self.assertIsNone(om.facts())
            self.assertFalse(om.add_fact("x"))
        with mock.patch.object(om.config, "OBSIDIAN_VAULT", os.path.join(self.dir, "missing")):
            self.assertIsNone(om.facts())


class Summaries(unittest.TestCase):
    def test_reads_the_models_json(self):
        note = om.summarise("user: hi\n", _chat_returning(
            {"title": "Exam: plan / revision?", "summary": "The user planned DBMS revision.",
             "topics": ["DBMS", "exam prep", "DBMS"], "facts": ["User has a DBMS exam on the 14th", 5]}))
        self.assertEqual(note["title"], "Exam plan revision")
        self.assertEqual(note["topics"], ["dbms", "exam-prep"])
        self.assertEqual(note["facts"], ["User has a DBMS exam on the 14th"])

    def test_requests_and_moods_are_not_facts(self):
        # what qwen3:8b really returned for real conversations on 2026-11-08
        for f in ("User asked for today's date.", "User requested Spotify to play songs.",
                  "User told the assistant to sleep.", "User indicated they were tired.",
                  "The user asked Raziel to describe itself"):
            self.assertFalse(om.is_lasting_fact(f), f)
        for f in ("User has a DBMS exam on the 14th", "User's favorite band is Coldplay",
                  "User lives in Dhupgari", "User is learning Dijkstra's algorithm for an exam"):
            self.assertTrue(om.is_lasting_fact(f), f)

    def test_noise_she_couldnt_answer_is_left_out(self):
        raw = ("assistant: " + "B" * 500 + "\nuser: What is today's date?\nassistant: Sunday.\n"
               "user: Resil\nassistant: (none)\nuser: MMMMMMMM\nassistant: \nuser: Explain Dijkstra\n"
               "assistant: It finds shortest paths.\n")
        cleaned = om.clean_transcript(raw)
        self.assertNotIn("Resil", cleaned)
        self.assertNotIn("MMMM", cleaned)
        self.assertIn("user: Explain Dijkstra", cleaned)
        self.assertIn("B" * 240 + "...", cleaned)
        self.assertNotIn("B" * 241, cleaned)

    def test_nothing_usable_is_none(self):
        self.assertIsNone(om.summarise("user: hi\n", _chat_returning("sorry, I can't")))
        self.assertIsNone(om.summarise("user: hi\n", _chat_returning({"title": "x", "summary": ""})))

    def test_only_the_end_of_a_long_transcript_is_sent(self):
        sent = []
        om.summarise("user: " + "x" * 20000, lambda messages: sent.append(messages) or {"message": {}})
        self.assertLessEqual(len(sent[0][1]["content"]), om.MAX_TRANSCRIPT_CHARS)


class Notes(_Vault):
    NOTE = {"title": "Exam plan", "summary": "The user planned revision. They start Monday.",
            "topics": ["dbms"], "facts": ["User has a DBMS exam on the 14th"]}

    def test_note_and_daily_line(self):
        path = om.write_note(7, "2026-11-08T14:35:10", self.NOTE)
        self.assertEqual(os.path.basename(path), "2026-11-08 1435 Exam plan.md")
        note = self.read("Conversations", "2026-11-08 1435 Exam plan.md")
        self.assertIn("tags: [raziel/conversation, topic/dbms]", note)
        self.assertIn("- User has a DBMS exam on the 14th", note)
        daily = self.read("Daily", "2026-11-08.md")
        self.assertIn("[[Conversations/2026-11-08 1435 Exam plan|Exam plan]] - The user planned revision.", daily)

    def test_a_conversation_that_went_on_replaces_its_note(self):
        first = om.write_note(7, "2026-11-08T14:35:10", self.NOTE)
        om.write_note(7, "2026-11-08T14:35:10", {**self.NOTE, "title": "Exam and music"}, replaces=first)
        self.assertFalse(os.path.exists(first))
        daily = self.read("Daily", "2026-11-08.md")
        self.assertNotIn("Exam plan|", daily)
        self.assertEqual(daily.count("[[Conversations/"), 1)

    def test_recent_and_search(self):
        om.write_note(1, "2026-11-07T10:00:00", {**self.NOTE, "title": "Music", "summary": "Played lo-fi.",
                                                 "facts": []})
        om.write_note(2, "2026-11-08T10:00:00", self.NOTE)
        self.assertTrue(om.recent(1)[0].startswith("2026-11-08: Exam plan. The user planned revision."))

        def embed(texts, query=False):           # "exam" questions point at the exam note
            return [[1.0, 0.0] if ("exam" in t.lower() or "revision" in t.lower()) else [0.0, 1.0] for t in texts]
        hits = om.search("when is my exam", embed=embed)
        self.assertEqual([h["date"] for h in hits], ["2026-11-08"])

    def test_search_falls_back_to_words(self):
        om.write_note(2, "2026-11-08T10:00:00", self.NOTE)

        def broken(texts, query=False):
            raise RuntimeError("Ollama is not running")
        self.assertEqual(len(om.search("revision plan", embed=broken)), 1)
        self.assertEqual(om.search("football", embed=broken), [])


class Writer(_Vault):
    def _session(self, started, lines):
        conn = sqlite3.connect(self.db)
        cur = conn.execute("INSERT INTO sessions (started_at, transcript) VALUES (?, ?)",
                           (started, "".join(f"{r}: {t}\n" for r, t in lines)))
        conn.commit()
        sid = cur.lastrowid
        conn.close()
        return sid

    def _since(self, value):
        conn = om._db()
        conn.execute("INSERT OR REPLACE INTO obsidian_meta(key, value) VALUES('since', ?)", (value,))
        conn.commit()
        conn.close()

    def test_only_new_and_long_enough_conversations(self):
        old = self._session("2026-11-01T09:00:00", [("user", "a"), ("assistant", "b"), ("user", "c")])
        self._since("2026-11-08T00:00:00")
        short = self._session("2026-11-08T09:00:00", [("user", "what time is it"), ("assistant", "9")])
        talk = self._session("2026-11-08T10:00:00", [("user", "my exam is on the 14th"), ("assistant", "ok"),
                                                     ("user", "remind me to revise"), ("assistant", "done")])
        chat = _chat_returning({"title": "Exam", "summary": "Exam talk.", "topics": [],
                                "facts": ["User has an exam on the 14th"]})
        self.assertEqual(om.process_pending(chat), 1)
        notes = os.listdir(os.path.join(self.vault, "Conversations"))
        self.assertEqual(notes, ["2026-11-08 1000 Exam.md"])
        self.assertIn("User has an exam on the 14th", om.facts())
        self.assertEqual(om.process_pending(chat), 0)                    # nothing new
        self.assertNotIn(old, [r["id"] for r in om.pending()])
        self.assertNotIn(short, [r["id"] for r in om.pending()])
        self.assertNotIn(talk, [r["id"] for r in om.pending()])

    def test_a_conversation_that_goes_on_gets_its_note_again(self):
        self._since("2026-11-08T00:00:00")
        sid = self._session("2026-11-08T10:00:00", [("user", "a"), ("user", "b")])
        om.process_pending(_chat_returning({"title": "First", "summary": "One.", "topics": [], "facts": []}))
        for t in ("c", "d"):
            memory.append_to_session(sid, "user", t)
        self.assertEqual(om.process_pending(_chat_returning(
            {"title": "Second", "summary": "Two.", "topics": [], "facts": []})), 1)
        self.assertEqual(os.listdir(os.path.join(self.vault, "Conversations")), ["2026-11-08 1000 Second.md"])


class UsedByRaziel(_Vault):
    def test_her_prompt_has_facts_md_and_recent_summaries(self):
        import llm_brain
        om.add_fact("User's sister is called Riya")
        om.write_note(2, "2026-11-08T10:00:00", Notes.NOTE)
        brain = llm_brain.Brain.__new__(llm_brain.Brain)
        brain.reset()
        prompt = brain.messages[0]["content"]
        self.assertIn("- User's sister is called Riya", prompt)
        self.assertIn("=== RECENT CONVERSATIONS", prompt)
        self.assertIn("Exam plan", prompt)

    def test_remember_recall_forget_use_the_vault(self):
        import tools
        tools.remember("User's cat is called Mochi")
        self.assertIn("User's cat is called Mochi", om.facts())
        with mock.patch.object(om, "search", return_value=[]):
            self.assertIn("Mochi", tools.recall("cat"))
        tools.forget("Mochi")
        self.assertNotIn("User's cat is called Mochi", om.facts())


if __name__ == "__main__":
    unittest.main()
