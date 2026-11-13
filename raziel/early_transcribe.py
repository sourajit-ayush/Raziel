"""
A head start for Whisper: transcription begins at your first pause, not when the recording ends.

The recorder ends a recording after SILENCE_DURATION (1.2 s) of silence, and only then did Whisper start.
Now the recorder tells this module as soon as you have been quiet for EARLY_TRANSCRIBE_AFTER (0.4 s):
Whisper starts on what was said so far, in the background, while the recorder keeps listening.

  * You stay quiet: the recording ends, and the transcript is already under way (or done). The wait
    after you stop talking is shorter by up to SILENCE_DURATION - EARLY_TRANSCRIBE_AFTER (0.8 s).
  * You go on talking (it was a pause, not the end): that transcript is thrown away, and its trace on
    the transcriber (the language of the "last" sentence) is put back. The whole recording is
    transcribed at the end, as before.

Only one head start runs at a time (two Whisper passes at once only slow each other down), so a second
pause while a thrown-away one is still running gets none.

The words are the same either way: the early pass hears everything up to the pause, the normal pass
hears that plus the silence after it.
"""

import logging
import threading
import time
import wave

import config

logger = logging.getLogger("voice_assistant")


def enabled() -> bool:
    return bool(getattr(config, "EARLY_TRANSCRIBE", True))


def pause_seconds() -> float:
    return float(getattr(config, "EARLY_TRANSCRIBE_AFTER", 0.4))


def _path() -> str:
    return str(config.TEMP_AUDIO_PATH) + ".early.wav"


class EarlyTranscriber:
    def __init__(self, transcriber):
        self.transcriber = transcriber
        self._job = None

    # -- called by the recorder (audio_recorder.record_until_silence)

    def pause(self, frames):
        """You went quiet: start transcribing `frames` (everything recorded so far)."""
        if not enabled():
            return
        job = self._job
        if job is not None and job["thread"].is_alive():
            return                                   # an earlier head start is still running
        job = {"valid": True, "text": None, "error": None, "started": time.monotonic(), "took": None,
               "language": (getattr(self.transcriber, "_prev_language", None),
                            getattr(self.transcriber, "last_language", None))}
        audio = b"".join(frames)

        def work():
            try:
                with wave.open(_path(), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(config.SAMPLE_RATE)
                    wf.writeframes(audio)
                job["text"] = self.transcriber.transcribe(_path())
            except Exception as e:  # noqa: BLE001 - the normal pass at the end still runs
                job["error"] = e
                logger.warning("Early transcription failed (%s) - transcribing at the end as usual.", e)
            job["took"] = time.monotonic() - job["started"]

        job["thread"] = threading.Thread(target=work, name="early-transcribe", daemon=True)
        self._job = job
        job["thread"].start()

    def resume(self):
        """You went on talking: what was started is no longer the whole sentence."""
        job = self._job
        if job is not None and job["valid"]:
            job["valid"] = False
            logger.info("You went on talking - the early transcript is dropped.")

    # -- called by main.py when the recording is over

    def result(self, wav_path: str) -> str:
        """The transcript of this recording: the head start's if it still stands, a normal pass otherwise."""
        job, self._job = self._job, None
        if job is None:
            return self.transcriber.transcribe(wav_path)
        waited = time.monotonic()
        job["thread"].join()
        if job["valid"] and job["error"] is None:
            logger.info("Early transcription: started %.1f s before the recording ended, took %.1f s "
                        "(waited %.1f s more).", waited - job["started"], job["took"] or 0.0,
                        time.monotonic() - waited)
            return job["text"] or ""
        self._restore(job)
        return self.transcriber.transcribe(wav_path)

    def cancel(self):
        """The recording is not going to be used (shutdown): forget the head start."""
        job, self._job = self._job, None
        if job is not None:
            job["valid"] = False
            job["thread"].join(timeout=30)
            self._restore(job)

    def _restore(self, job):
        prev, last = job["language"]
        if last is not None:
            try:
                self.transcriber._prev_language, self.transcriber.last_language = prev, last
            except Exception:  # noqa: BLE001
                pass
