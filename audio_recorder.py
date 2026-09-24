"""
Records audio from the microphone after the wake word fires.
Stops on silence (so you don't have to wait the full max duration every time)
or after RECORD_MAX_SECONDS, whichever comes first.
"""

import logging
import time
import wave
from collections import deque

import numpy as np
import pyaudio

import config
import initiative
import shutdown

logger = logging.getLogger("voice_assistant")


def _mic_gated() -> bool:
    """True while Raziel is speaking unprompted (initiative.py) or just after.
    Her own voice must not count as the user starting to talk."""
    engine = initiative.engine
    return bool(engine is not None and engine.mic_should_ignore())


def _rms(chunk_bytes: bytes) -> float:
    """Root-mean-square volume of a chunk of 16-bit PCM audio."""
    samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples**2)))


def _calibrate_ambient_noise(stream) -> float:
    """
    Samples the current room's background noise for a short window and
    returns its average RMS. This lets speech detection adapt to whatever
    room you're actually in, instead of relying on one fixed threshold
    that's wrong as soon as the environment changes.
    """
    chunks_per_second = config.SAMPLE_RATE / config.FRAME_LENGTH
    n_chunks = max(1, int(config.AMBIENT_CALIBRATION_SECONDS * chunks_per_second))
    levels = []
    for _ in range(n_chunks):
        data = stream.read(config.FRAME_LENGTH, exception_on_overflow=False)
        levels.append(_rms(data))
    return float(np.mean(levels)) if levels else 0.0


def wait_for_voice(pa: pyaudio.PyAudio):
    """
    Blocks silently (no recording, no transcription) until incoming audio
    is CLEARLY and CONSISTENTLY louder than the current room's ambient
    noise - i.e. until you actually start talking, not just a passing
    background sound. Adapts automatically to noisy vs quiet rooms instead
    of using one fixed threshold everywhere.

    Returns (pre_roll_frames, ambient_level):
      - pre_roll_frames: a short buffer of audio leading up to that point,
        so record_until_silence can pick up cleanly without clipping your
        first word
      - ambient_level: the just-measured room noise level, passed on to
        record_until_silence so its own silence-detection uses the SAME
        live measurement instead of an unrelated static threshold

    Returns (None, None) if shutdown was requested while waiting - callers
    must check shutdown.is_shutting_down() before recording.
    """
    # If she is mid-sentence, wait it out BEFORE opening the mic: calibrating
    # "ambient noise" against her own voice would set the trigger threshold far
    # too high and make her deaf to you for the rest of the turn.
    while _mic_gated():
        if shutdown.is_shutting_down():
            return None, None
        time.sleep(0.1)

    stream = pa.open(
        rate=config.SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=config.FRAME_LENGTH,
    )

    chunks_per_second = config.SAMPLE_RATE / config.FRAME_LENGTH
    buffer_len = max(1, int(0.5 * chunks_per_second))
    pre_roll = deque(maxlen=buffer_len)

    try:
        ambient_level = _calibrate_ambient_noise(stream)
        # She may have started speaking during the ~0.4 s calibration itself.
        while _mic_gated():
            if shutdown.is_shutting_down():
                return None, None
            time.sleep(0.1)
            ambient_level = _calibrate_ambient_noise(stream)
        # A real voice needs to clearly stand out above whatever this room's
        # background noise currently is. This is additive, not a multiplier -
        # your speaking volume into the mic stays roughly constant regardless
        # of room noise, so "ambient + fixed gap" matches reality far better
        # than "ambient x N" (which becomes unrealistically high once ambient
        # noise itself is more than trivial).
        trigger_threshold = ambient_level + config.VOICE_TRIGGER_MARGIN
        logger.info(
            "Ambient noise level: %.0f, voice trigger threshold: %.0f",
            ambient_level, trigger_threshold,
        )

        sustained_chunks_needed = max(
            1, int(config.MIN_SUSTAINED_VOICE_MS / 1000 * chunks_per_second)
        )
        consecutive_loud = 0

        while not shutdown.is_shutting_down():
            data = stream.read(config.FRAME_LENGTH, exception_on_overflow=False)

            # Raziel started talking on her own initiative while we were
            # listening: keep draining the mic but ignore it, and forget any
            # audio captured so far (it would contain her voice).
            if _mic_gated():
                consecutive_loud = 0
                pre_roll.clear()
                continue

            pre_roll.append(data)

            if _rms(data) >= trigger_threshold:
                consecutive_loud += 1
            else:
                consecutive_loud = 0

            # Require the sound to stay loud for a short sustained window,
            # not just one instantaneous spike - filters out clicks, coughs,
            # a door closing, etc. that cross the threshold only briefly.
            if consecutive_loud >= sustained_chunks_needed:
                return list(pre_roll), ambient_level

        return None, None   # shutdown requested while waiting
    finally:
        stream.stop_stream()
        stream.close()


def record_until_silence(pa: pyaudio.PyAudio, pre_roll_frames=None, ambient_level=None) -> str:
    """
    Records audio starting immediately, stops after sustained silence
    or after config.RECORD_MAX_SECONDS. Returns the path to a saved wav file.

    pre_roll_frames: optional list of raw PCM byte chunks captured just
    before this call (e.g. from WakeWordDetector or wait_for_voice),
    prepended to the recording so the first word or two isn't clipped.

    ambient_level: if given (from wait_for_voice's live measurement of the
    current room), the silence-detection threshold is calibrated from this
    SAME measurement rather than a static config value unrelated to the
    room you're actually in right now. Falls back to the static
    SILENCE_RMS_THRESHOLD when not provided (e.g. the very first turn after
    a wake word, which doesn't go through wait_for_voice).
    """
    stream = pa.open(
        rate=config.SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=config.FRAME_LENGTH,
    )

    if ambient_level is not None:
        silence_threshold = ambient_level + config.SILENCE_STOP_MARGIN
    else:
        silence_threshold = config.SILENCE_RMS_THRESHOLD

    frames = list(pre_roll_frames) if pre_roll_frames else []
    silent_chunks = 0
    chunks_per_second = config.SAMPLE_RATE / config.FRAME_LENGTH
    silence_chunks_needed = int(config.SILENCE_DURATION * chunks_per_second)
    min_chunks_before_stop = int(config.MIN_RECORD_SECONDS * chunks_per_second)
    max_chunks = int(config.RECORD_MAX_SECONDS * chunks_per_second)

    logger.info("Recording started... (silence threshold: %.0f)", silence_threshold)

    for i in range(max_chunks):
        if shutdown.is_shutting_down():
            break
        data = stream.read(config.FRAME_LENGTH, exception_on_overflow=False)
        frames.append(data)

        volume = _rms(data)
        if volume < silence_threshold:
            silent_chunks += 1
        else:
            silent_chunks = 0

        if i >= min_chunks_before_stop and silent_chunks >= silence_chunks_needed:
            logger.info("Silence detected, stopping recording.")
            break
    else:
        logger.info("Hit max recording duration (%ss), stopping.", config.RECORD_MAX_SECONDS)

    stream.stop_stream()
    stream.close()

    with wave.open(config.TEMP_AUDIO_PATH, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(config.SAMPLE_RATE)
        wf.writeframes(b"".join(frames))

    logger.info("Recording saved to %s", config.TEMP_AUDIO_PATH)
    return config.TEMP_AUDIO_PATH
