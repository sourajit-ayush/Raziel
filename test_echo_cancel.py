"""
Tests for echo cancellation (echo_cancel.py) and the turn timing's first-sound lookup (speaker.py).

Run: venv\\Scripts\\python.exe -m unittest test_echo_cancel -v
No microphone or speakers: playback and the microphone are simulated on one clock, with the
sound card's buffers and the room's echo in between. The WebRTC test needs `livekit`; it is
skipped without it.
"""

import unittest
from unittest import mock

import numpy as np

import echo_cancel
from echo_cancel import FRAME, RATE, EchoCanceller

PIPER_RATE = 22050


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class _RecordingApm:
    """Remembers which far-end frame was paired with each microphone frame; returns the mic as is."""

    def __init__(self):
        self.far = []

    def reverse(self, frame):
        self.far.append(int(frame[0]))

    def capture(self, frame):
        return frame


def _rms(x):
    x = np.asarray(x, dtype=np.float32)
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0


class Alignment(unittest.TestCase):
    def _canceller(self):
        clock, apm = _Clock(), _RecordingApm()
        return EchoCanceller(apm, clock=clock), clock, apm

    def _feed_frames(self, ec, values):
        # One 16 kHz frame per value, each frame filled with that value.
        pcm = np.concatenate([np.full(FRAME, v, dtype=np.int16) for v in values]).tobytes()
        ec.feed(pcm, RATE)

    def test_far_frames_pair_with_the_mic_frames_heard_after_them(self):
        ec, clock, apm = self._canceller()
        self._feed_frames(ec, [1, 2, 3, 4])            # play from t=100.00, 10 ms each
        clock.now = 100.04                             # a 40 ms mic read covering 100.00-100.04
        ec.clean(np.zeros(4 * FRAME, dtype=np.int16).tobytes())
        self.assertEqual(apm.far, [1, 2, 3, 4])

    def test_mic_heard_before_she_spoke_gets_silence(self):
        ec, clock, apm = self._canceller()
        clock.now = 100.0
        self._feed_frames(ec, [7, 8])
        clock.now = 100.0                              # this read covers 99.98-100.00: before her
        ec.clean(np.zeros(2 * FRAME, dtype=np.int16).tobytes())
        self.assertEqual(apm.far, [0, 0])

    def test_audio_from_before_a_late_mic_is_dropped(self):
        # The mic opens 0.6 s after she started (the grace period): what she said in between is
        # stale and must not be paired with what the mic hears now.
        ec, clock, apm = self._canceller()
        self._feed_frames(ec, list(range(1, 81)))      # 0.8 s of her
        clock.now = 100.64                             # a read covering 100.60-100.64
        ec.clean(np.zeros(4 * FRAME, dtype=np.int16).tobytes())
        self.assertEqual(apm.far, [61, 62, 63, 64])

    def test_chunks_continue_where_the_last_one_ended(self):
        ec, clock, apm = self._canceller()
        ec.feed(np.full(FRAME + 80, 5, dtype=np.int16).tobytes(), RATE)     # 1.5 frames
        ec.feed(np.full(80, 6, dtype=np.int16).tobytes(), RATE)             # the other half
        clock.now = 100.02
        ec.clean(np.zeros(2 * FRAME, dtype=np.int16).tobytes())
        self.assertEqual(apm.far, [5, 5])                                  # second frame: 80 x 5 + 80 x 6

    def test_odd_sized_mic_frames_keep_their_tail(self):
        ec, clock, _apm = self._canceller()
        mic = np.arange(FRAME + 7, dtype=np.int16)
        self.assertEqual(np.frombuffer(ec.clean(mic.tobytes()), dtype=np.int16).tolist(), mic.tolist())

    def test_resamples_piper_audio_to_16k(self):
        out = echo_cancel._to_mono16k(np.zeros(PIPER_RATE, dtype=np.int16).tobytes(), PIPER_RATE, 2, 1)
        self.assertEqual(len(out), RATE)
        stereo = np.array([100, 300] * 160, dtype=np.int16).tobytes()
        self.assertEqual(echo_cancel._to_mono16k(stereo, RATE, 2, 2).tolist(), [200] * 160)


class TurnOff(unittest.TestCase):
    def test_off_in_config(self):
        with mock.patch.object(echo_cancel, "_tried", False), mock.patch.object(echo_cancel, "_instance", None), \
             mock.patch.object(echo_cancel.config, "ECHO_CANCELLATION", False, create=True):
            self.assertIsNone(echo_cancel.get())

    def test_barge_in_settings_follow_it(self):
        import speaker
        with mock.patch.object(speaker.config, "BARGE_IN_RMS_THRESHOLD", 4500), \
             mock.patch.object(speaker.config, "BARGE_IN_GRACE_PERIOD", 0.6), \
             mock.patch.object(speaker.config, "BARGE_IN_RMS_THRESHOLD_AEC", 2000, create=True), \
             mock.patch.object(speaker.config, "BARGE_IN_GRACE_PERIOD_AEC", 0.3, create=True):
            self.assertEqual(speaker._barge_settings(None), (4500, 0.6))
            self.assertEqual(speaker._barge_settings(object()), (2000.0, 0.3))

    def test_barge_in_keeps_the_old_settings_unless_lowered(self):
        import speaker
        with mock.patch.object(speaker.config, "BARGE_IN_RMS_THRESHOLD", 4500), \
             mock.patch.object(speaker.config, "BARGE_IN_GRACE_PERIOD", 0.6):
            for name in ("BARGE_IN_RMS_THRESHOLD_AEC", "BARGE_IN_GRACE_PERIOD_AEC"):
                if hasattr(speaker.config, name):
                    self.skipTest(f"config.py sets {name}")
            self.assertEqual(speaker._barge_settings(object()), (4500.0, 0.6))


def _webrtc_available():
    try:
        echo_cancel._WebRtcApm()
        return True
    except Exception:
        return False


@unittest.skipUnless(_webrtc_available(), "livekit is not installed")
class WebRtcOnASimulatedLaptop(unittest.TestCase):
    """Her voice goes out in Piper's 2048-byte chunks, plays 60 ms later, comes back through the
    room 10 ms after that at half volume, and the mic is read in 80 ms frames 50 ms late. You talk
    over her from 5 s to 6 s."""

    def test_her_voice_is_removed_and_yours_is_kept(self):
        rng = np.random.default_rng(1)
        seconds, out_latency, room, in_latency = 7.0, 0.06, 0.01, 0.05
        n22 = int(seconds * PIPER_RATE)
        # Speech-like far end: noise shaped by a slow envelope, loud enough to trip the old threshold.
        env = 0.6 + 0.4 * np.sin(np.arange(n22) * 2 * np.pi * 3 / PIPER_RATE)
        her = np.clip(rng.standard_normal(n22) * 9000 * env, -32768, 32767).astype(np.int16)
        her16 = echo_cancel._to_mono16k(her.tobytes(), PIPER_RATE, 2, 1).astype(np.float32)

        n16 = int(seconds * RATE)
        lag = int((out_latency + room) * RATE)
        echo = np.zeros(n16, dtype=np.float32)
        echo[lag:] = 0.5 * her16[: n16 - lag]
        echo = np.convolve(echo, [1.0, 0.35, 0.12])[:n16]          # a little room reverb
        you = np.zeros(n16, dtype=np.float32)
        you[5 * RATE:6 * RATE] = np.sin(np.arange(RATE) * 2 * np.pi * 220 / RATE) * 4000
        mic = np.clip(echo + you, -32768, 32767).astype(np.int16)

        clock = _Clock()
        start = clock.now
        ec = EchoCanceller(echo_cancel._WebRtcApm(), clock=clock)
        events = []
        chunk, chunk_s = 1024, 1024 / PIPER_RATE                    # 2048 bytes of 16-bit audio
        for k in range(0, n22 // chunk):
            events.append((start + k * chunk_s, "feed", k))
        mic_frame = 1280
        for m in range(n16 // mic_frame):
            events.append((start + (m + 1) * mic_frame / RATE + in_latency, "read", m))
        cleaned = np.zeros(n16, dtype=np.int16)
        for t, kind, i in sorted(events):
            clock.now = t
            if kind == "feed":
                ec.feed(her[i * chunk:(i + 1) * chunk].tobytes(), PIPER_RATE)
            else:
                raw = mic[i * mic_frame:(i + 1) * mic_frame]
                # The read happened in_latency after the frame was heard: the canceller is told the
                # read time, as in speaker.py.
                cleaned[i * mic_frame:(i + 1) * mic_frame] = np.frombuffer(ec.clean(raw.tobytes()), dtype=np.int16)

        def level(signal, a, b):
            return _rms(signal[int(a * RATE):int(b * RATE)])

        her_raw, her_left = level(mic, 3, 5), level(cleaned, 3, 5)
        self.assertGreater(her_raw, 2500)                            # she would trip a 2500 threshold raw
        self.assertLess(her_left, her_raw / 10)                     # >20 dB of her voice taken out
        self.assertLess(her_left, 800)
        self.assertGreater(level(cleaned, 5.2, 6), 2000)            # you talking over her still come through


class FirstSound(unittest.TestCase):
    def test_first_sound_after_a_moment(self):
        import speaker
        with mock.patch.object(speaker, "_play_starts", [10.0, 12.5, 13.0]):
            self.assertEqual(speaker.first_audio_after(11.0), 12.5)
            self.assertIsNone(speaker.first_audio_after(14.0))


if __name__ == "__main__":
    unittest.main()
