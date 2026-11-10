"""
Tests for talking like a person: the head start for Whisper (early_transcribe.py), the thinking sound and
what was cut off (speaker.py), and going on after an interruption (main.py).

Run: venv\\Scripts\\python.exe -m unittest test_conversation -v
No microphone, no models: the recorder gets a fake stream, the transcriber and the speaker's audio are stand-ins.
"""

import os
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import audio_recorder
import config
import early_transcribe
import llm_brain
import main
import speaker

LOUD = struct.pack("<h", 6000) * config.FRAME_LENGTH
QUIET = struct.pack("<h", 0) * config.FRAME_LENGTH
PER_SECOND = config.SAMPLE_RATE / config.FRAME_LENGTH


def frames(*runs):
    """frames(("loud", 1.0), ("quiet", 0.5)) -> a list of 80 ms frames."""
    out = []
    for kind, seconds in runs:
        out += [LOUD if kind == "loud" else QUIET] * int(round(seconds * PER_SECOND))
    return out


class FakeMic:
    def __init__(self, data):
        self.data = list(data)

    def read(self, n, exception_on_overflow=False):
        return self.data.pop(0) if self.data else QUIET

    def stop_stream(self):
        pass

    def close(self):
        pass


class FakeTranscriber:
    """Takes `seconds` per call; remembers the language like the real one."""

    def __init__(self, seconds=0.0):
        self.seconds = seconds
        self.calls = []
        self._prev_language, self.last_language = "en", "en"

    def transcribe(self, path):
        self.calls.append(os.path.basename(path))
        time.sleep(self.seconds)
        self._prev_language, self.last_language = self.last_language, "hi"
        return "text of " + os.path.basename(path)


class _Recorder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, value in (("TEMP_AUDIO_PATH", os.path.join(self.dir, "rec.wav")), ("EARLY_TRANSCRIBE", True),
                            ("EARLY_TRANSCRIBE_AFTER", 0.4), ("SILENCE_DURATION", 1.2), ("MIN_RECORD_SECONDS", 0.6)):
            p = mock.patch.object(config, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def record(self, data, early):
        with mock.patch.object(audio_recorder, "_open_mic", return_value=FakeMic(data)):
            return audio_recorder.record_until_silence(mock.Mock(), ambient_level=100, early=early)


class HeadStart(_Recorder):
    def test_whisper_starts_at_the_pause(self):
        t = FakeTranscriber()
        early = early_transcribe.EarlyTranscriber(t)
        wav = self.record(frames(("loud", 1.0), ("quiet", 2.0)), early)
        self.assertEqual(early.result(wav), "text of rec.wav.early.wav")     # the head start's words
        self.assertEqual(t.calls, ["rec.wav.early.wav"])                      # and no second pass

    def test_a_pause_in_the_middle_is_thrown_away(self):
        t = FakeTranscriber()
        early = early_transcribe.EarlyTranscriber(t)
        # "open the..." (pause) "...downloads folder": the first pause starts a pass on half a sentence
        wav = self.record(frames(("loud", 1.0), ("quiet", 0.6), ("loud", 1.0), ("quiet", 0.2), ("loud", 0.5)), early)
        t.last_language = "hi"                                # (what the thrown-away pass left behind)
        early._job["thread"].join()
        self.assertEqual(early.result(wav), "text of rec.wav")               # the whole recording
        self.assertEqual(t.calls, ["rec.wav.early.wav", "rec.wav"])

    def test_the_language_of_a_dropped_pass_is_put_back(self):
        t = FakeTranscriber()
        early = early_transcribe.EarlyTranscriber(t)
        early.pause(frames(("loud", 0.5)))
        early._job["thread"].join()
        self.assertEqual(t.last_language, "hi")               # the pass ran...
        early.resume()
        early.cancel()
        self.assertEqual((t._prev_language, t.last_language), ("en", "en"))   # ...and left no trace

    def test_only_one_pass_at_a_time(self):
        t = FakeTranscriber(seconds=0.4)
        early = early_transcribe.EarlyTranscriber(t)
        early.pause(frames(("loud", 0.5)))
        early.resume()
        early.pause(frames(("loud", 1.0)))                    # the first is still running: no second one
        self.assertEqual(early.result("whole.wav"), "text of whole.wav")
        self.assertEqual(t.calls, ["rec.wav.early.wav", "whole.wav"])

    def test_a_failed_head_start_falls_back(self):
        t = FakeTranscriber()
        early = early_transcribe.EarlyTranscriber(t)
        with mock.patch.object(early_transcribe.wave, "open", side_effect=OSError("disk full")):
            early.pause(frames(("loud", 0.5)))
            early._job["thread"].join()
        self.assertEqual(early.result("whole.wav"), "text of whole.wav")

    def test_off_means_as_before(self):
        t = FakeTranscriber()
        early = early_transcribe.EarlyTranscriber(t)
        with mock.patch.object(config, "EARLY_TRANSCRIBE", False):
            wav = self.record(frames(("loud", 1.0), ("quiet", 2.0)), early)
            self.assertEqual(early.result(wav), "text of rec.wav")
        self.assertEqual(t.calls, ["rec.wav"])

    def test_the_recording_itself_is_unchanged(self):
        import wave
        data = frames(("loud", 1.0), ("quiet", 2.0))
        a = self.record(list(data), None)
        with wave.open(a) as w:
            plain = w.readframes(w.getnframes())
        b = self.record(list(data), early_transcribe.EarlyTranscriber(FakeTranscriber()))
        with wave.open(b) as w:
            self.assertEqual(w.readframes(w.getnframes()), plain)


def quiet_speaker(synth_seconds=0.0):
    """A Speaker with no voices and no sound card: _play records what it was asked to say."""
    s = speaker.Speaker.__new__(speaker.Speaker)
    s.played = []
    s.gate = threading.Event()             # set = playing goes on; cleared = the sentence "is playing"
    s.gate.set()

    def synth(text):
        time.sleep(synth_seconds)
        return (b"\x00\x00", 1, 2, 24000, [])

    def play(text, stop_event=None, prepared=None, skippable=True, count=True):
        s.played.append((text, count))
        while not s.gate.is_set() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(0.01)
    s._synthesize, s._play = synth, play
    return s


class _Speaking(unittest.TestCase):
    def setUp(self):
        self.mic = mock.Mock()
        self.mic.read.side_effect = lambda n, **kw: QUIET
        patches = [mock.patch.object(speaker, "_kokoro_on", return_value=False),
                   mock.patch.object(speaker, "_mic_pa", return_value=mock.Mock(open=mock.Mock(return_value=self.mic))),
                   mock.patch.object(speaker, "echo_cancel", mock.Mock(get=mock.Mock(return_value=None))),
                   mock.patch.object(speaker, "_barge_settings", return_value=(3000, 0.0)),
                   mock.patch.object(config, "BARGE_IN_CONSECUTIVE_FRAMES", 1, create=True),
                   mock.patch.object(config, "THINKING_FILLER", True, create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class ThinkingSound(_Speaking):
    def slow(self, seconds, sentences):
        time.sleep(seconds)
        yield from sentences

    def test_hmm_when_the_model_is_slow(self):
        s = quiet_speaker()
        s._fillers = {"en": [("Hmm.", (b"\x00\x00", 1, 2, 24000, []))]}
        _i, _pre, spoken = s.speak_stream_interruptible(self.slow(0.5, ["It is Sunday."]), mock.Mock(), filler_after=0.1)
        self.assertEqual(s.played, [("Hmm.", False), ("It is Sunday.", True)])    # not counted as her first sound
        self.assertEqual(spoken, "It is Sunday.")                                 # and not part of the reply

    def test_no_hmm_when_she_answers_at_once(self):
        s = quiet_speaker()
        s._fillers = {"en": [("Hmm.", (b"\x00\x00", 1, 2, 24000, []))]}
        s.speak_stream_interruptible(iter(["It is Sunday."]), mock.Mock(), filler_after=0.5)
        self.assertEqual([t for t, _ in s.played], ["It is Sunday."])

    def test_no_hmm_unless_asked_for_or_when_off(self):
        s = quiet_speaker()
        s._fillers = {"en": [("Hmm.", (b"\x00\x00", 1, 2, 24000, []))]}
        s.speak_stream_interruptible(self.slow(0.3, ["Done."]), mock.Mock())
        with mock.patch.object(config, "THINKING_FILLER", False):
            s.speak_stream_interruptible(self.slow(0.3, ["Done."]), mock.Mock(), filler_after=0.05)
        self.assertEqual([t for t, _ in s.played], ["Done.", "Done."])

    def test_never_the_same_one_twice_running(self):
        s = quiet_speaker()
        s._fillers = {"en": [("Hmm.", 1), ("Let me see.", 2)]}
        picks = [s._pick_filler()[0] for _ in range(6)]
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])), picks)

    def test_made_in_the_background(self):
        s = quiet_speaker()
        with mock.patch.object(config, "THINKING_FILLERS", {"en": ("Hmm.", "One sec.")}, create=True):
            s.prepare_fillers()
            for _ in range(100):
                if len(s._fillers["en"]) == 2:
                    break
                time.sleep(0.01)
        self.assertEqual([t for t, _ in s._fillers["en"]], ["Hmm.", "One sec."])


class CutOff(_Speaking):
    def interrupt_during(self, s, sentence):
        """The microphone goes loud while `sentence` is playing."""
        def read(n, **kw):
            return LOUD if (s.played and s.played[-1][0] == sentence) else QUIET
        self.mic.read.side_effect = read
        s.gate.clear()
        real_play = s._play

        def play(text, stop_event=None, prepared=None, skippable=True, count=True):
            if text != sentence:
                s.played.append((text, count))          # earlier sentences finish at once
                return
            real_play(text, stop_event, prepared, skippable, count)
        s._play = play

    def test_remembers_what_she_did_not_say(self):
        s = quiet_speaker()
        self.interrupt_during(s, "Two.")
        interrupted, pre, spoken = s.speak_stream_interruptible(iter(["One.", "Two.", "Three.", "Four."]), mock.Mock())
        self.assertTrue(interrupted)
        self.assertEqual(s.last_interruption["rest"][0], "Two.")           # the sentence she was cut off in
        self.assertFalse("One." in s.last_interruption["rest"])

    def test_a_long_known_text_keeps_all_of_the_rest(self):
        s = quiet_speaker()
        self.interrupt_during(s, "Two.")
        with mock.patch.object(speaker, "split_long", return_value=["One.", "Two.", "Three.", "Four."]):
            interrupted, _pre = s.speak_interruptible("One. Two. Three. Four.", mock.Mock())
        self.assertTrue(interrupted)
        self.assertEqual(s.last_interruption, {"rest": ["Two.", "Three.", "Four."], "complete": True})

    def test_nothing_remembered_when_she_finishes(self):
        s = quiet_speaker()
        interrupted, _pre, _spoken = s.speak_stream_interruptible(iter(["One.", "Two."]), mock.Mock())
        self.assertFalse(interrupted)
        self.assertIsNone(s.last_interruption)


class GoingOn(unittest.TestCase):
    def test_what_counts_as_go_on(self):
        for said in ("Go on.", "sorry, continue", "Carry on please", "You were saying?", "What were you saying?",
                     "okay go ahead", "आगे बोलो"):
            self.assertTrue(main.is_resume_request(said), said)
        for said in ("continue the music", "go on a trip to Goa", "open notepad", "what is the time", ""):
            self.assertFalse(main.is_resume_request(said), said)

    def test_a_known_rest_is_spoken_as_it_was(self):
        cut = {"rest": ["it will rain at five.", "Take an umbrella."], "complete": True, "at": 0, "auto": False}
        self.assertEqual(main._resume_reply(cut, mock.Mock(), "go on"),
                         "As I was saying, it will rain at five. Take an umbrella.")

    def test_an_unfinished_answer_goes_back_to_the_model_without_tools(self):
        brain = mock.Mock()
        cut = {"rest": ["Dijkstra's algorithm finds"], "complete": False, "at": 0, "auto": False}
        with mock.patch.object(config, "LLM_STREAMING", True, create=True):
            main._resume_reply(cut, brain, "go on")
        said, prompt = brain.stream_turn.call_args.args
        self.assertEqual(said, "go on")
        self.assertIn("Dijkstra's algorithm finds", prompt)
        self.assertEqual(brain.stream_turn.call_args.kwargs, {"allow_tools": False})

    def test_cut_from_the_speaker(self):
        s = mock.Mock(last_interruption={"rest": ["Two.", " ", "Three."], "complete": True})
        cut = main._cut_from(s)
        self.assertEqual((cut["rest"], cut["complete"], cut["auto"]), (["Two.", "Three."], True, False))
        self.assertIsNone(main._cut_from(mock.Mock(last_interruption=None)))
        self.assertIsNone(main._cut_from(mock.Mock(last_interruption={"rest": [], "complete": True})))


class Questions(unittest.TestCase):
    def test_the_rule_is_in_the_prompt_and_can_be_switched_off(self):
        with mock.patch.object(config, "CONVERSATION_QUESTIONS", True, create=True):
            self.assertIn("ask ONE short question", llm_brain.system_prompt())
            self.assertTrue(llm_brain.system_prompt().startswith(llm_brain.BASE_SYSTEM_PROMPT))
        with mock.patch.object(config, "CONVERSATION_QUESTIONS", False, create=True):
            self.assertEqual(llm_brain.system_prompt(), llm_brain.BASE_SYSTEM_PROMPT)

    def test_her_question_keeps_the_microphone_hers(self):
        self.assertTrue(main._asks_back("Which folder?"))
        self.assertFalse(main._asks_back("Opening Downloads."))


if __name__ == "__main__":
    unittest.main()


class NothingToSay(unittest.TestCase):
    def test_none_in_brackets_is_nothing_to_say(self):
        # qwen3:8b writes "(none)" for background talk as often as "(nothing)": it was spoken aloud
        for text in ("(none)", "(None).", "(nothing)"):
            self.assertTrue(llm_brain.is_idle_reply(text), text)
        for text in ("None.", "Paris.", "You have none."):
            self.assertFalse(llm_brain.is_idle_reply(text), text)
