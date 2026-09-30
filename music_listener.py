"""
music_listener.py - lets the orb avatar dance to whatever music the PC is playing.

It listens to the SPEAKERS, not the microphone: Windows can hand any program a copy of what is
being played ("WASAPI loopback"), so Spotify, YouTube, a game or a music file all work, the room
stays out of it, and nothing is recorded or saved - each moment is turned into 48 numbers and
thrown away.

Needs one optional package (Raziel runs fine without it, the orb just won't dance):

    venv\\Scripts\\pip install PyAudioWPatch

What it sends (avatar_server.send_music, ~30 times a second, only while music is playing and the
orb window is connected): 48 log-spaced frequency bands from 30 Hz to 14 kHz as bytes 0-255,
computed exactly like a web browser's audio analyser (Blackman window, 2048-point FFT, -100..-30 dB),
so avatar_orb.html's tuning for calm / medium / hard music holds. The page decides which shape
the song gets; this file only decides WHETHER something that sounds like music is playing:

  * loud enough, most of the time, for a couple of seconds (a notification ping never counts)
  * few deep dips between frames - speech is full of pauses between words, songs are not
  * a steady loudness - speech jumps up and down syllable by syllable, a song much less
  * her own voice is in the speakers' mix too, so while she talks (and just after) nothing starts

Say "stop dancing" / "dance" to switch it off / on (avatar_forms.py -> set_enabled).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

BANDS = 48
FFT_SIZE = 2048
SEND_HZ = 30
MIN_DB, MAX_DB = -100.0, -30.0          # a browser analyser's default range
SMOOTHING = 0.30                        # the browser's 0.55 per 60 fps frame, at 30 fps
WAVE_POINTS = 32

LOUD_DB = -58.0                         # frame RMS (dBFS) that counts as "something is playing"
DIP_DB = 12.0                           # a frame this far under the recent median is a pause
MAX_DIP_FRACTION = 0.16                 # speech has many pauses; music hardly any
MAX_LEVEL_SPREAD_DB = 8.0               # speech's loudness jumps syllable to syllable; a song's is steadier
MIN_LOUD_FRACTION = 0.85
ON_SECONDS = 1.0                        # music-like (over the last 1.5 s) for this long -> she dances
OFF_SECONDS = 2.5                       # quiet for this long -> she stops
SPEECH_TAIL = 1.2                       # her own voice: ignored while she talks and this long after
REOPEN_QUIET_SECONDS = 30.0             # quiet this long -> reopen, in case the default speakers changed;
REOPEN_MAX_SECONDS = 600.0              # ...then less and less often (x2 each time) while nothing plays


# ------------------------------------------------------------------ analysis (pure, testable)

def fft_size_for(rate: int) -> int:
    """2048 at 44.1/48 kHz like the browser; longer at 96/192 kHz so a bin stays ~23 Hz wide."""
    size = FFT_SIZE
    while rate / size > 30.0 and size < 16384:
        size *= 2
    return size


def band_edges(rate: int, fft_size: int = FFT_SIZE, bands: int = BANDS) -> List[int]:
    """FFT bin where each band starts - the same formula as avatar_orb.html / the preview."""
    nyq, bins = rate / 2.0, fft_size // 2
    return [min(bins - 1, max(1, int(round(30.0 * (14000.0 / 30.0) ** (k / bands) / nyq * bins))))
            for k in range(bands + 1)]


def blackman(n: int) -> np.ndarray:
    """The browser analyser's window (alpha 0.16, periodic)."""
    a = 0.16
    i = np.arange(n)
    return (1 - a) / 2 - 0.5 * np.cos(2 * np.pi * i / n) + a / 2 * np.cos(4 * np.pi * i / n)


class Analyser:
    """Samples in, 48 band bytes out - a Web Audio AnalyserNode.getByteFrequencyData() replica."""

    def __init__(self, rate: int, fft_size: Optional[int] = None, smoothing: float = SMOOTHING, gain_db: float = 0.0):
        self.rate, self.smoothing = int(rate), float(smoothing)
        self.fft_size = int(fft_size or fft_size_for(self.rate))
        self.window = blackman(self.fft_size)
        self.prev = np.zeros(self.fft_size // 2)
        edges = band_edges(self.rate, self.fft_size)
        self.ranges = [(edges[k], max(edges[k], edges[k + 1] - 1) + 1) for k in range(BANDS)]
        self.gain = 10.0 ** (float(gain_db) / 20.0)

    def bands(self, samples: np.ndarray) -> List[int]:
        x = np.asarray(samples, dtype=np.float64)[-self.fft_size:] * self.gain
        if len(x) < self.fft_size:
            x = np.concatenate([np.zeros(self.fft_size - len(x)), x])
        mag = np.abs(np.fft.rfft(x * self.window))[: self.fft_size // 2] / self.fft_size
        self.prev = self.smoothing * self.prev + (1.0 - self.smoothing) * mag
        db = 20.0 * np.log10(np.maximum(self.prev, 1e-12))
        byte = np.clip(np.floor((db - MIN_DB) * 255.0 / (MAX_DB - MIN_DB)), 0, 255)
        return [int(round(float(byte[a:b].mean()))) for a, b in self.ranges]


def band_energy(band_bytes) -> Tuple[float, float]:
    """(energy, bass) with the page's weighting, 0..~1.4."""
    k = np.arange(BANDS)
    v = np.power(np.asarray(band_bytes, dtype=np.float64) / 255.0, 1.4) * (1 + k / BANDS * 0.8)
    return float(v.mean()), float(v[:6].mean())


def rms_db(samples: np.ndarray) -> float:
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return -120.0
    return float(20.0 * np.log10(max(np.sqrt(np.mean(x * x)), 1e-6)))


def wave_points(samples: np.ndarray, n: int = WAVE_POINTS) -> List[int]:
    x = np.asarray(samples, dtype=np.float64)[-FFT_SIZE:]
    if x.size < n:
        return [0] * n
    idx = np.linspace(0, x.size - 1, n).astype(int)
    peak = max(1e-3, float(np.max(np.abs(x))))
    return [int(v) for v in np.clip(x[idx] / peak * 110.0, -127, 127)]


class MusicDetector:
    """Frame loudness in, "is music playing?" out, with hysteresis."""

    def __init__(self, hz: int = SEND_HZ):
        self.hz = hz
        self.hist: deque = deque(maxlen=int(hz * 1.5))
        self.on = False
        self.good_for = 0.0
        self.quiet_for = 0.0

    def musical(self) -> bool:
        if len(self.hist) < self.hz * 1.2:
            return False
        h = np.array(self.hist)
        med = float(np.median(h))
        if med < LOUD_DB:
            return False
        loud = float(np.mean(h > LOUD_DB))
        dips = float(np.mean(h < med - DIP_DB))
        spread = float(np.std(np.maximum(h, -100.0)))
        return loud >= MIN_LOUD_FRACTION and dips <= MAX_DIP_FRACTION and spread <= MAX_LEVEL_SPREAD_DB

    def update(self, level_db: float, dt: float, her_voice: bool = False) -> bool:
        if her_voice and not self.on:
            self.hist.clear()                    # her words must not count towards "music"
            self.good_for = 0.0
            return False
        self.hist.append(level_db)
        loud = level_db > LOUD_DB
        if self.on:
            self.quiet_for = 0.0 if loud else self.quiet_for + dt
            if self.quiet_for >= OFF_SECONDS:
                self.on, self.good_for = False, 0.0
        else:
            self.good_for = self.good_for + dt if (loud and self.musical()) else 0.0
            if self.good_for >= ON_SECONDS:
                self.on, self.quiet_for = True, 0.0
        return self.on


# ------------------------------------------------------------------ the capture thread

class _Loopback:
    """The default speakers' loopback stream (PyAudioWPatch). Collects mono samples in a ring."""

    def __init__(self, pa_mod):
        self.pa_mod = pa_mod
        self.pa = None
        self.stream = None
        self.rate = 48000
        self.channels = 2
        self.name = ""
        self.lock = threading.Lock()
        self.buf = np.zeros(16384, dtype=np.float32)
        self.last_data = 0.0

    def open(self):
        pa_mod = self.pa_mod
        self.pa = pa_mod.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pa_mod.paWASAPI)
        dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev.get("isLoopbackDevice"):
            for lb in self.pa.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
            else:
                raise RuntimeError(f"no loopback device for the speakers '{dev.get('name')}'")
        self.rate = int(dev["defaultSampleRate"])
        self.channels = max(1, int(dev["maxInputChannels"]))
        self.name = str(dev.get("name", ""))
        self.stream = self.pa.open(format=pa_mod.paInt16, channels=self.channels, rate=self.rate,
                                   input=True, input_device_index=dev["index"],
                                   frames_per_buffer=512, stream_callback=self._callback)
        self.stream.start_stream()

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            x = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            if self.channels > 1:
                x = x[: len(x) // self.channels * self.channels].reshape(-1, self.channels).mean(axis=1)
            with self.lock:
                n = min(len(x), len(self.buf))
                self.buf = np.roll(self.buf, -n)
                self.buf[-n:] = x[-n:]
                self.last_data = time.monotonic()
        except Exception:
            pass
        return (None, self.pa_mod.paContinue)

    def latest(self, n: int) -> np.ndarray:
        """The newest n samples; silence if the stream has gone quiet (Windows sends nothing then)."""
        with self.lock:
            if time.monotonic() - self.last_data > 0.3:
                return np.zeros(n, dtype=np.float32)
            return self.buf[-n:].copy()

    def close(self) -> bool:
        """Closes the stream on a helper thread: a loopback stream that never got any audio has been
        known to hang on stop, and that must not freeze the listener (or shutdown) with it."""
        stream, pa = self.stream, self.pa
        self.stream = self.pa = None

        def _close():
            for fn in (lambda: stream and stream.stop_stream(), lambda: stream and stream.close(),
                       lambda: pa and pa.terminate()):
                try:
                    fn()
                except Exception:
                    pass
        closer = threading.Thread(target=_close, name="music-listener-close", daemon=True)
        closer.start()
        closer.join(timeout=3.0)
        return not closer.is_alive()


class MusicListener:
    def __init__(self, send: Optional[Callable] = None, is_speaking: Optional[Callable] = None,
                 connected: Optional[Callable] = None, gain_db: float = 0.0, pa_mod=None):
        import avatar_server
        self.send = send or avatar_server.send_music
        self.is_speaking = is_speaking or (lambda: avatar_server.is_speaking(SPEECH_TAIL))
        self.connected = connected or avatar_server.is_connected
        self.gain_db = gain_db
        self.pa_mod = pa_mod
        self.enabled = True
        self.available: Optional[bool] = None    # None = not known yet
        self.detector = MusicDetector()
        self.playing = False                      # what the orb was last told
        self.source: Optional[_Loopback] = None
        self.reopen_after = REOPEN_QUIET_SECONDS
        self.can_reopen = True                    # False once a close has hung: never stack stuck streams
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="music-listener", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    # -- work
    def _import(self):
        if self.pa_mod is not None:
            return self.pa_mod
        try:
            import pyaudiowpatch
            return pyaudiowpatch
        except Exception:
            return None

    def _run(self):
        pa_mod = self._import()
        if pa_mod is None:
            self.available = False
            logger.info("Music reaction is off: PyAudioWPatch isn't installed "
                        "(venv\\Scripts\\pip install PyAudioWPatch lets the orb dance to music).")
            return
        failures = 0
        while not self._stop.is_set():
            src = _Loopback(pa_mod)
            try:
                src.open()
            except Exception as exc:
                src.close()
                failures += 1
                if failures >= 3 and not self.available:
                    self.available = False        # never managed to open it: say so if asked to dance
                if failures in (1, 3) or failures % 30 == 0:
                    logger.warning("Music listener: can't open the speakers' loopback (%s) - will retry.", exc)
                self._stop.wait(min(60.0, 5.0 * failures))
                continue
            failures = 0
            self.available = True
            self.source = src
            logger.info("Music listener: listening to '%s' (%d Hz) so the orb can dance.", src.name, src.rate)
            reopen = False
            try:
                reopen = bool(self._listen(src))
            except Exception:
                logger.exception("Music listener stopped unexpectedly - restarting it")
            self.music_off()
            self.source = None
            if not src.close():
                logger.warning("Music listener: the old stream didn't close; not reopening it again this run.")
                self.can_reopen = False
                return
            if reopen:
                self.reopen_after = min(REOPEN_MAX_SECONDS, self.reopen_after * 2)
            self._stop.wait(1.0)

    def music_off(self):
        """Tell the orb the music is over (if it thought otherwise) and start detection afresh."""
        self.detector.on = False
        self.detector.good_for = 0.0
        self.detector.hist.clear()
        if self.playing:
            self.playing = False
            try:
                self.send(False)
            except Exception:
                pass

    def step(self, samples: np.ndarray, level: float, analyser: "Analyser", dt: float) -> bool:
        """One 1/30 s frame: detect, and send the bands while music plays. Returns "playing"."""
        on = self.detector.update(level, dt, her_voice=bool(self.is_speaking()))
        band_bytes = analyser.bands(samples)          # every frame, so the smoothing stays continuous
        if on:
            self.send(True, band_bytes, wave_points(samples))
        if on != self.playing:
            self.playing = on
            if not on:
                self.send(False)
            logger.info("Music listener: %s", "music playing - the orb dances" if on else "music stopped")
        return on

    def _listen(self, src: "_Loopback") -> bool:
        """Runs until stop (-> False), or until it's time to reopen the stream (-> True): the default
        speakers may have changed (headphones plugged in), and a loopback stream stays on the old ones."""
        analyser = Analyser(src.rate, gain_db=self.gain_db)
        hop = max(1, src.rate // SEND_HZ)
        dt = 1.0 / SEND_HZ
        quiet_since = next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += dt
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_t = time.monotonic()        # fell behind (PC busy): don't try to catch up
            if not self.enabled or not self.connected():
                self.music_off()
                self._stop.wait(0.25)
                quiet_since = next_t = time.monotonic()
                continue
            samples = src.latest(analyser.fft_size)
            level = rms_db(samples[-hop:])
            on = self.step(samples, level, analyser, dt)
            if on:
                self.reopen_after = REOPEN_QUIET_SECONDS
            if level > LOUD_DB:
                quiet_since = time.monotonic()
            elif not on and self.can_reopen and time.monotonic() - quiet_since > self.reopen_after:
                return True                       # reopen: picks up new default speakers
        return False


# ------------------------------------------------------------------ module API (main.py, avatar_forms.py)

_listener: Optional[MusicListener] = None


def start() -> Optional[MusicListener]:
    """Start listening in the background. Never raises; never blocks the voice loop."""
    global _listener
    try:
        import config
        if _listener is None:
            _listener = MusicListener(gain_db=float(getattr(config, "AVATAR_MUSIC_GAIN_DB", 0.0) or 0.0))
            _listener.enabled = bool(getattr(config, "AVATAR_MUSIC_REACT", True))
        _listener.start()
        try:
            import shutdown
            shutdown.on_shutdown(stop)
        except Exception:
            pass
        import atexit
        atexit.register(stop)                   # also on a plain exit: close the stream before Python goes
    except Exception:
        logger.exception("Music listener couldn't start")
    return _listener


def stop():
    if _listener is not None:
        _listener.stop()


def set_enabled(on: bool):
    """ "dance" / "stop dancing". "dance" starts the listener if it wasn't running."""
    if _listener is None:
        if not on:
            return
        start()
    if _listener is not None:
        _listener.enabled = bool(on)
        if not on:
            _listener.music_off()


def available() -> bool:
    """False when we know the PC's sound can't be heard (package missing / no loopback device)."""
    if _listener is not None and _listener.available is not None:
        return bool(_listener.available)
    try:
        import importlib.util
        return importlib.util.find_spec("pyaudiowpatch") is not None
    except Exception:
        return False


def is_playing() -> bool:
    return bool(_listener is not None and _listener.playing)
