"""
Listens continuously on the microphone for the configured wake word,
using openWakeWord - fully open-source, offline, no account/API key needed.

Blocks until the wake word is heard, then returns so the caller can start
recording.

Custom models (e.g. the "Raziel" one from Raziel Training)
----------------------------------------------------------
Set config.WAKE_WORD_MODEL = "Raziel.onnx". A bare filename is resolved against
the project folder. Two things had to change for that to actually work, and
neither was in the old version of this file:

  * openWakeWord names each score by the model's file NAME WITHOUT EXTENSION
    ("Raziel"), never by the string you passed in ("Raziel.onnx"). The old
    `predictions.get(config.WAKE_WORD_MODEL)` therefore returned 0.0 forever -
    a perfectly trained model that could never wake her. The score key is now
    worked out from what openWakeWord actually reports.
  * If the .onnx isn't there (training still running), fall back to the
    pretrained model instead of crashing at startup.

Two things added after the first real-voice tests:

  * NEAR-MISS LOGGING. Only successful detections used to reach assistant.log, so
    "I said it twice and she ignored me once" left no trace. A score that rises to
    0.10+ but never reaches the threshold is now logged ("Wake word near-miss: best
    score 0.31 (needs 0.35)"), which is what lets the threshold be tuned from real data.
  * MICROPHONE RECOVERY. A PortAudio "Unanticipated host error" / "Stream closed" from
    stream.read() (another program grabbed the mic, a device was switched, Windows
    reset the audio service) used to bubble up to main.py, which retried 5 times in
    5 seconds against the same dead stream and then shut the whole assistant down.
    Now the stream is closed and reopened on a fresh PyAudio instance (several
    attempts over ~10 s) and listening simply resumes.
"""

import logging
import os
import time

import numpy as np
import pyaudio
from openwakeword.model import Model

import config
import initiative
import shutdown

logger = logging.getLogger("voice_assistant")

NEAR_MISS_FLOOR = 0.10          # scores from here up (but below the threshold) are logged as near misses
NEAR_MISS_QUIET_CHUNKS = 6      # ...once the score has stayed under the floor for ~0.5 s
NEAR_MISS_LOG_GAP_S = 1.0       # never more than one near-miss line per second
RECOVER_ATTEMPTS = 6


def _resolve_model(spec: str) -> str:
    """Bare .onnx/.tflite filenames -> absolute path in the project folder."""
    if spec.lower().endswith((".onnx", ".tflite")) and not os.path.isabs(spec):
        return os.path.join(config._SCRIPT_DIR, spec)
    return spec


def _is_file_spec(spec: str) -> bool:
    return spec.lower().endswith((".onnx", ".tflite"))


def _load_model(model_arg: str) -> Model:
    # onnx explicitly: tflite isn't installed here (the old log line "Tried to
    # import the tflite runtime, but it was not found" was openWakeWord falling
    # back to onnx on its own). Older openWakeWord builds without the keyword
    # get the plain call.
    try:
        return Model(wakeword_models=[model_arg], inference_framework="onnx")
    except TypeError:
        return Model(wakeword_models=[model_arg])


def _mic_gated() -> bool:
    """True while Raziel is speaking unprompted (initiative.py) or just after."""
    engine = initiative.engine
    return bool(engine is not None and engine.mic_should_ignore())


class WakeWordDetector:
    def __init__(self):
        wanted = config.WAKE_WORD_MODEL
        model_arg = _resolve_model(wanted)

        if _is_file_spec(model_arg) and not os.path.isfile(model_arg):
            fallback = getattr(config, "WAKE_WORD_FALLBACK", "hey_jarvis")
            logger.warning(
                "Wake word model file not found: %s - falling back to '%s'. "
                "(Finish training and copy the .onnx into the project folder.)",
                model_arg, fallback,
            )
            wanted, model_arg = fallback, _resolve_model(fallback)

        self.model_spec = model_arg
        self.label = os.path.splitext(os.path.basename(wanted))[0] or wanted
        logger.info(
            "Loading openWakeWord model '%s'... first run downloads the "
            "model files, may take a moment.",
            self.label,
        )
        self.model = _load_model(model_arg)
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self._score_key = None
        self._near_peak = 0.0
        self._near_quiet = 0
        self._near_last_log = 0.0
        logger.info("openWakeWord model loaded.")

    def _open_stream(self):
        self.stream = self.pa.open(
            rate=config.SAMPLE_RATE,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=config.FRAME_LENGTH,
        )

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _recover_stream(self, exc) -> bool:
        """
        The microphone stream died mid-read. Close it and reopen on a fresh PyAudio
        instance (PortAudio caches the device list, so after a host error only a new
        instance reliably sees the device again). True when listening can resume.
        """
        logger.warning("Microphone stream failed (%s: %s) - reopening it...", type(exc).__name__, exc)
        self._close_stream()
        for attempt in range(1, RECOVER_ATTEMPTS + 1):
            if shutdown.is_shutting_down():
                return False
            try:
                try:
                    self.pa.terminate()
                except Exception:
                    pass
                self.pa = pyaudio.PyAudio()
                self._open_stream()
                self.model.reset()
                logger.info("Microphone stream reopened (attempt %d).", attempt)
                return True
            except Exception as err:
                logger.warning("Reopening the microphone failed (%d/%d): %s", attempt, RECOVER_ATTEMPTS, err)
                self._close_stream()
                shutdown.wait(min(0.5 * attempt, 3.0))
        return False

    def _track_near_miss(self, score: float):
        """Log a burst of scores that got somewhere but never reached the threshold."""
        if score >= NEAR_MISS_FLOOR:
            self._near_peak = max(self._near_peak, score)
            self._near_quiet = 0
        elif self._near_peak > 0.0:
            self._near_quiet += 1
            if self._near_quiet >= NEAR_MISS_QUIET_CHUNKS:
                now = time.time()
                if (self._near_peak < config.WAKE_WORD_THRESHOLD
                        and now - self._near_last_log >= NEAR_MISS_LOG_GAP_S):
                    logger.info("Wake word near-miss: best score %.2f (needs %.2f)",
                                self._near_peak, config.WAKE_WORD_THRESHOLD)
                    self._near_last_log = now
                self._near_peak = 0.0
                self._near_quiet = 0

    def _score(self, predictions: dict) -> float:
        """
        This model's score, however openWakeWord happens to key it. Resolved once
        (the keys never change) and logged so a mismatch is visible in the log.
        """
        if not predictions:
            return 0.0

        if self._score_key is None:
            keys = list(predictions)
            stem = os.path.splitext(os.path.basename(self.model_spec))[0]
            chosen = None
            for candidate in (self.model_spec, stem):
                if candidate in predictions:
                    chosen = candidate
                    break
            if chosen is None:
                # e.g. asked for "hey_jarvis", openWakeWord reports "hey_jarvis_v0.1"
                prefixed = [k for k in keys if k.startswith(stem) or stem.startswith(k)]
                if len(prefixed) == 1:
                    chosen = prefixed[0]
            if chosen is None and len(keys) == 1:
                chosen = keys[0]       # only one model is loaded: its score is THE score
            self._score_key = chosen if chosen is not None else ""
            logger.info(
                "Wake word score key: %r (openWakeWord reports %s)",
                chosen if chosen is not None else "max of all", keys,
            )

        if self._score_key:
            return float(predictions.get(self._score_key, 0.0))
        return float(max(predictions.values()))

    def wait_for_wake_word(self):
        """Blocks until the wake word is detected (or shutdown is requested)."""
        if self.stream is None:
            self._open_stream()

        logger.info("Listening for wake word '%s'...", self.label)
        was_gated = False

        while not shutdown.is_shutting_down():
            try:
                pcm_bytes = self.stream.read(
                    config.FRAME_LENGTH, exception_on_overflow=False
                )
            except OSError as exc:
                if not self._recover_stream(exc):
                    raise
                continue

            # Raziel is speaking on her own initiative: don't let her wake
            # herself with her own voice (she is on speakers, not headphones).
            # The stream is still drained above so it never overflows.
            if _mic_gated():
                was_gated = True
                continue
            if was_gated:
                self.model.reset()      # drop features built from her own voice
                was_gated = False

            audio_chunk = np.frombuffer(pcm_bytes, dtype=np.int16)
            predictions = self.model.predict(audio_chunk)
            score = self._score(predictions)
            self._track_near_miss(score)

            if score >= config.WAKE_WORD_THRESHOLD:
                self._near_peak, self._near_quiet = 0.0, 0
                logger.info("Wake word detected! (score=%.2f)", score)
                # reset internal buffers so the next detection doesn't
                # immediately re-trigger on trailing audio
                self.model.reset()
                return

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
        self.pa.terminate()
