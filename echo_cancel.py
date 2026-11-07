"""
echo_cancel.py - takes Raziel's own voice out of the microphone while she speaks.

On laptop speakers the microphone hears her, so barge-in ("talk over her to stop her") needed a high
level threshold and a grace period, and still cut her off now and then. This feeds what she plays
(the "far end") into WebRTC's echo canceller (AEC3, from the `livekit` package) and subtracts it from
what the microphone hears, so what is left is you.

How it is wired
---------------
  speaker._play_locked   after each chunk goes to the speakers: feed(chunk) - resampled to 16 kHz
                         mono, cut into 10 ms frames, each stamped with when it can start playing
  speaker's barge-in ear every microphone frame: clean(frame) - for each 10 ms of it, the far-end
                         frame of the same moment goes in first, then the microphone's

The stamps keep the two streams in step even when the microphone opens late (after the grace
period): each 10 ms of microphone is paired with the far-end frame playing at that moment, older ones
are skipped, so the echo always arrives after its reference, which is what the canceller needs. AEC3 finds the remaining delay (the sound card's
buffers, the room) by itself. One canceller lives for the whole run, so it stays adapted to this
room and these speakers.

config.ECHO_CANCELLATION = False turns it off; without `livekit` installed it is off too, and
everything works as before (raw levels, BARGE_IN_RMS_THRESHOLD).
"""

import atexit
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

import config

logger = logging.getLogger("voice_assistant")

RATE = 16000
FRAME = 160                # 10 ms at 16 kHz: the only frame size the canceller takes
_STEP = FRAME / RATE
_MAX_FAR = 3000            # frames (30 s) kept at most, if nothing reads the microphone meanwhile
_RESYNC = 0.06             # seconds off before either side is put back on the clock
_EPS = 1e-6                # stamps are sums of 10 ms steps


def _to_mono16k(pcm: bytes, rate: int, width: int, channels: int) -> Optional[np.ndarray]:
    if width != 2 or not pcm:
        return None
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(x):
        n_out = max(1, int(round(len(x) * RATE / float(rate))))
        x = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16)


class _WebRtcApm:
    """WebRTC's audio processing module (echo canceller + high-pass filter), via livekit."""

    def __init__(self):
        from livekit import rtc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
        atexit.register(self.close)     # before livekit's own exit hook, or it complains on the way out

    def reverse(self, frame: np.ndarray) -> None:
        self._apm.process_reverse_stream(self._rtc.AudioFrame(frame.tobytes(), RATE, 1, FRAME))

    def capture(self, frame: np.ndarray) -> np.ndarray:
        audio = self._rtc.AudioFrame(frame.tobytes(), RATE, 1, FRAME)
        self._apm.set_stream_delay_ms(0)        # AEC3 estimates the delay itself
        self._apm.process_stream(audio)
        return np.frombuffer(bytes(audio.data), dtype=np.int16)

    def close(self):
        self._apm = None


class EchoCanceller:
    """`apm` needs reverse(int16[160]) and capture(int16[160]) -> int16[160]; injectable for tests."""

    def __init__(self, apm, clock: Callable[[], float] = time.monotonic):
        self._apm = apm
        self._clock = clock
        self._lock = threading.Lock()
        self._far = deque()                 # (stamp, int16[FRAME]) in playing order
        self._carry = np.zeros(0, dtype=np.int16)
        self._audio_end = 0.0               # when everything fed so far has finished playing
        self._mic_t = None                  # when the next 10 ms of microphone was heard

    def playback_started(self) -> None:
        """A new sentence: nothing left over from the last one is joined onto it."""
        with self._lock:
            self._carry = np.zeros(0, dtype=np.int16)

    def feed(self, pcm: bytes, rate: int, width: int = 2, channels: int = 1) -> None:
        """What was just handed to the speakers (it plays from now on, right after what came before)."""
        samples = _to_mono16k(pcm, rate, width, channels)
        if samples is None:
            return
        now = self._clock()
        with self._lock:
            # Stamps follow the audio itself, 10 ms per frame: re-anchored to the clock only after a
            # gap (a new sentence), never nudged on every chunk (that would squeeze frames together).
            new_start = max(now, self._audio_end)
            start = new_start - len(self._carry) / RATE
            data = np.concatenate([self._carry, samples])
            n = len(data) // FRAME
            for k in range(n):
                self._far.append((start + k * _STEP, data[k * FRAME:(k + 1) * FRAME]))
            self._audio_end = new_start + len(samples) / RATE
            self._carry = data[n * FRAME:]
            while len(self._far) > _MAX_FAR:
                self._far.popleft()

    def _far_frame(self, t: float) -> np.ndarray:
        """The far-end frame playing at `t`, one after another; silence when nothing is. If the
        microphone has fallen behind (it opened late, after the grace period), older frames are skipped
        so it catches up to the frame playing now."""
        with self._lock:
            if self._far and self._far[0][0] < t - _RESYNC:
                while len(self._far) > 1 and self._far[1][0] <= t + _EPS:
                    self._far.popleft()
            if self._far and self._far[0][0] <= t + _EPS:
                return self._far.popleft()[1]
        return np.zeros(FRAME, dtype=np.int16)

    def clean(self, mic: bytes) -> bytes:
        """A microphone frame (16 kHz mono int16, just read) with her voice taken out."""
        x = np.frombuffer(mic, dtype=np.int16)
        n = len(x) // FRAME
        if n == 0:
            return mic
        heard = self._clock() - n * _STEP        # when the first 10 ms of it was heard
        # A steady 10 ms per frame, so the jitter of one read doesn't skip or repeat a far-end frame;
        # back on the clock after a gap (the microphone was closed between replies).
        if self._mic_t is None or abs(self._mic_t - heard) > _RESYNC:
            self._mic_t = heard
        out = []
        for j in range(n):
            t = self._mic_t + j * _STEP
            self._apm.reverse(self._far_frame(t))
            out.append(self._apm.capture(x[j * FRAME:(j + 1) * FRAME].copy()))
        self._mic_t += n * _STEP
        rest = x[n * FRAME:]
        return np.concatenate(out + [rest]).astype(np.int16).tobytes()


_instance: Optional[EchoCanceller] = None
_tried = False
_init_lock = threading.Lock()


def get() -> Optional[EchoCanceller]:
    """The canceller for this run, or None when it is off or `livekit` isn't installed."""
    global _instance, _tried
    if _tried:
        return _instance
    with _init_lock:
        if not _tried:
            if getattr(config, "ECHO_CANCELLATION", True) and int(getattr(config, "SAMPLE_RATE", RATE)) == RATE:
                try:
                    _instance = EchoCanceller(_WebRtcApm())
                    logger.info("Echo cancellation on (WebRTC AEC3): her own voice is taken out of the mic.")
                except Exception as e:  # noqa: BLE001
                    logger.info("Echo cancellation off (%s). pip install livekit to turn it on.", e)
            _tried = True
    return _instance
