"""
Tests for the Kokoro voice (kokoro_tts.py, and speaker.py choosing between Kokoro and Piper).

Run: venv\\Scripts\\python.exe -m unittest test_kokoro -v
The worker is a stand-in process object: no model, no venv-kokoro needed. (A real run is in the session's
notes: speaking needs the model files.)
"""

import io
import json
import unittest
from unittest import mock

import kokoro_tts


class FakeWorker:
    """Answers like kokoro_worker.py: a ready line, then a header + PCM per request."""

    def __init__(self, ready=True, reply=None, die_after=None):
        self.requests = []
        self.reply = reply
        self.die_after = die_after
        self.out = io.BytesIO()
        self.stdout = self
        self.stdin = self
        self._buf = b'{"ok": true, "ready": true}\n' if ready else b'{"ok": false, "error": "load: no model"}\n'
        self.killed = False

    # stdin
    def write(self, data):
        req = json.loads(data)
        self.requests.append(req)
        if self.die_after is not None and len(self.requests) > self.die_after:
            return
        if self.reply is not None:
            self._buf += self.reply
        else:
            pcm = b"\x01\x00" * 240
            self._buf += json.dumps({"ok": True, "rate": 24000, "bytes": len(pcm)}).encode() + b"\n" + pcm

    def flush(self):
        pass

    # stdout
    def readline(self):
        i = self._buf.find(b"\n")
        if i < 0:
            import time
            time.sleep(5)                      # nothing more: like a stuck worker
            return b""
        line, self._buf = self._buf[:i + 1], self._buf[i + 1:]
        return line

    def read(self, n):
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def poll(self):
        return 1 if self.killed else None

    def kill(self):
        self.killed = True


class Client(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(kokoro_tts.os.path, "exists", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def client(self, worker):
        self.started = []

        def popen(args, **kw):
            self.started.append(args)
            return worker
        return kokoro_tts.KokoroClient(popen=popen)

    def test_speaks_through_the_worker(self):
        w = FakeWorker()
        c = self.client(w)
        with mock.patch.object(kokoro_tts.config, "KOKORO_VOICE", "bf_emma", create=True):
            pcm, rate = c.synth("Sunday, November 8.", "en")
        self.assertEqual((len(pcm), rate), (480, 24000))
        self.assertEqual(w.requests[0]["voice"], "bf_emma")
        self.assertEqual(w.requests[0]["lang"], "en-gb")
        self.assertEqual(len(self.started), 1)
        c.synth("Again.", "en")
        self.assertEqual(len(self.started), 1)          # one worker for the whole run

    def test_hindi_uses_the_hindi_voice(self):
        w = FakeWorker()
        c = self.client(w)
        with mock.patch.object(kokoro_tts.config, "KOKORO_HINDI_VOICE", "hf_alpha", create=True):
            c.synth("नमस्ते", "hi")
        self.assertEqual((w.requests[0]["voice"], w.requests[0]["lang"]), ("hf_alpha", "hi"))

    def test_missing_files_mean_piper(self):
        c = self.client(FakeWorker())
        with mock.patch.object(kokoro_tts.os.path, "exists", return_value=False):
            self.assertIsNone(c.synth("Hello.", "en"))
        self.assertEqual(self.started, [])
        self.assertIn("missing", c.disabled_reason)

    def test_a_worker_that_cant_load_means_piper(self):
        c = self.client(FakeWorker(ready=False))
        self.assertIsNone(c.synth("Hello.", "en"))
        self.assertIn("no model", c.disabled_reason)

    def test_a_bad_sentence_falls_back_once(self):
        w = FakeWorker(reply=b'{"ok": false, "error": "phonemizer failed"}\n')
        c = self.client(w)
        self.assertIsNone(c.synth("???", "en"))
        self.assertFalse(w.killed)                        # the worker itself is fine

    def test_gives_up_after_repeated_failures(self):
        w = FakeWorker(reply=b'{"ok": false, "error": "broken"}\n')
        c = self.client(w)
        for _ in range(kokoro_tts.MAX_FAILURES + 2):
            self.assertIsNone(c.synth("Hello.", "en"))
        self.assertEqual(len(w.requests), kokoro_tts.MAX_FAILURES)   # then it stops asking

    def test_off_unless_chosen(self):
        with mock.patch.object(kokoro_tts.config, "TTS_ENGINE", "piper", create=True):
            self.assertIsNone(kokoro_tts.client())


class Phrases(unittest.TestCase):
    """With Kokoro, long sentences are spoken phrase by phrase so the first sound comes sooner."""

    def test_splits_at_pauses_without_fragments(self):
        import speaker
        self.assertEqual(speaker.phrases("Sunday, November 8, 2026."), ["Sunday, November 8, 2026."])
        parts = speaker.phrases("The Dijkstra algorithm finds the shortest path between nodes in a graph, "
                                "using a priority queue to always expand the closest unvisited node first.")
        self.assertEqual(parts, ["The Dijkstra algorithm finds the shortest path between nodes in a graph,",
                                 "using a priority queue to always expand the closest unvisited node first."])
        for p in speaker.phrases("Good afternoon. It's twenty-nine degrees in Dhupgari, with no rain expected. "
                                 "You have one reminder at six: revise the DBMS notes."):
            self.assertGreaterEqual(len(p), 24, p)

    def test_nothing_is_lost(self):
        import speaker
        text = "First, open the folder; then, after a moment, read the file - slowly: line by line."
        self.assertEqual(" ".join(speaker.phrases(text)).split(), text.split())

    def test_stream_keeps_a_yes_no_question_whole(self):
        import queue
        import speaker
        s = speaker.Speaker.__new__(speaker.Speaker)
        played = []
        s._synthesize = lambda text: (b"\x00\x00", 1, 2, 24000, [])
        s._play = lambda text, stop_event=None, prepared=None, skippable=True: played.append(text)
        question = "Send Mom this WhatsApp message: I'm late. Say yes to send, or no to cancel."
        answer = ("The Dijkstra algorithm finds the shortest path between nodes in a graph, "
                  "using a priority queue to always expand the closest unvisited node first.")
        mic = mock.Mock()
        mic.read.side_effect = lambda n, **kw: b"\x00\x00" * n
        with mock.patch.object(speaker, "_kokoro_on", return_value=True), \
             mock.patch.object(speaker, "_mic_pa", return_value=mock.Mock(open=mock.Mock(return_value=mic))), \
             mock.patch.object(speaker, "echo_cancel", mock.Mock(get=mock.Mock(return_value=None))):
            s.speak_stream_interruptible(iter([answer, question]), mock.Mock(),
                                         protect=lambda t: "say yes" in t.lower())
        self.assertEqual(played[-1], question)                 # whole
        self.assertEqual(played[:2], speaker.phrases(answer))  # phrase by phrase


class SpeakerChoosesTheEngine(unittest.TestCase):
    def speaker(self):
        import speaker
        s = speaker.Speaker.__new__(speaker.Speaker)
        s.voice = s.syn_config = s.hindi_voice = s.hindi_syn_config = None
        s._synth_raw = mock.Mock(return_value=(b"\x00\x00" * 10, 1, 2, 22050,
                                               [{"viseme": "aa", "start": 0.0, "duration": 0.05}]))
        return speaker, s

    def test_kokoro_for_english(self):
        speaker, s = self.speaker()
        fake = mock.Mock()
        fake.synth.return_value = (b"\x01\x00" * 20, 24000)
        with mock.patch("kokoro_tts.client", return_value=fake):
            pcm, ch, w, rate, tl = s._synth_run("en", "Hello there.")
        self.assertEqual((rate, tl), (24000, []))
        s._synth_raw.assert_not_called()

    def test_piper_when_kokoro_has_nothing(self):
        speaker, s = self.speaker()
        fake = mock.Mock()
        fake.synth.return_value = None
        with mock.patch("kokoro_tts.client", return_value=fake):
            self.assertEqual(s._synth_run("en", "Hello.")[3], 22050)
        s._synth_raw.assert_called_once()

    def test_hindi_stays_with_piper_by_default(self):
        speaker, s = self.speaker()
        fake = mock.Mock()
        with mock.patch("kokoro_tts.client", return_value=fake), \
             mock.patch.object(speaker.config, "KOKORO_HINDI", False, create=True):
            s._synth_run("hi", "नमस्ते")
        fake.synth.assert_not_called()

    def test_mixed_sentence_joins_both(self):
        speaker, s = self.speaker()
        fake = mock.Mock()
        fake.synth.return_value = (b"\x01\x00" * 24, 24000)
        with mock.patch("kokoro_tts.client", return_value=fake), \
             mock.patch.object(speaker.config, "KOKORO_HINDI", False, create=True):
            pcm, ch, w, rate, tl = s._synthesize_runs([("en", "Your message to "), ("hi", "फ़ज़ल")])
        self.assertEqual(rate, 24000)                     # Piper's part is resampled to Kokoro's rate
        self.assertGreater(len(pcm), 48)


if __name__ == "__main__":
    unittest.main()
