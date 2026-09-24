"""
Transcribes a wav file to text using a local faster-whisper model.
The model is loaded once and reused across calls (loading it is the slow part).

Two gates from transcript_guard.py sit around the model call, because Whisper
will happily invent a sentence ("Hi, I'm Arijit Singh.") out of silence or
noise - and initial_prompt makes it worse by handing it a name to anchor on:

  1. BEFORE Whisper: if the clip has no real speech energy, skip inference
     entirely (also saves 1-3 s).
  2. AFTER Whisper:  vet the segments with Whisper's own confidence numbers
     plus text-level checks (repetition loops, caption residue, prompt echo).

Either gate returns "" - main.py already treats that as "no speech, keep
listening", so nothing downstream changes.

HINDI
-----
With WHISPER_LANGUAGE = "auto" (the default) every utterance is first checked for
its language, restricted to English vs Hindi (a short English clip is easily
mislabelled as Urdu or Nepali by an unrestricted detector). English goes through
the same path as before. Hindi is transcribed with language="hi" (Whisper then
writes Devanagari) by the bigger HINDI_WHISPER_MODEL, which loads in the
background at start-up and downloads itself on first use; until it is ready the
normal model is used. WHISPER_LANGUAGE = "en" skips the check (saves ~0.5 s per
turn); "hi" forces Hindi.
"""

import logging
import os
import threading
import time

from faster_whisper import WhisperModel

import config
import transcript_guard

logger = logging.getLogger("voice_assistant")


def _vocabulary_terms() -> list:
    """Names/terms Whisper tends to mishear (CUSTOM_VOCABULARY + CONTACTS)."""
    names = set(config.CUSTOM_VOCABULARY)
    names.update(name.title() for name in config.CONTACTS.keys())
    return sorted(names)


def _build_vocabulary_prompt() -> str:
    """
    Builds a short hint string of names/terms Whisper tends to mishear
    (e.g. "Arijit Singh" -> "Arjeet Singh", "Fazal" -> "Fuzzle"). Passed as
    initial_prompt, this measurably biases word recognition toward these
    specific words without needing a bigger/slower model.
    """
    names = _vocabulary_terms()
    if not names:
        return ""
    return ", ".join(names)


class Transcriber:
    def __init__(self):
        logger.info(
            "Loading Whisper model '%s' (device=%s, compute_type=%s)... "
            "first run will download the model, may take a minute.",
            config.WHISPER_MODEL_SIZE,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        threads = int(getattr(config, "WHISPER_CPU_THREADS", 0) or 0)
        if threads <= 0:
            threads = min(8, os.cpu_count() or 4)
        logger.info("Whisper using %d CPU threads.", threads)
        try:
            self.model = WhisperModel(
                config.WHISPER_MODEL_SIZE,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
                cpu_threads=threads,
            )
        except TypeError:       # a faster-whisper without the cpu_threads argument
            self.model = WhisperModel(
                config.WHISPER_MODEL_SIZE,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
            )
        logger.info("Whisper model loaded.")

        self.last_language = "en"           # language of the previous utterance ("en" | "hi")
        self.hindi_model = None             # bigger model for Hindi, loaded in the background
        self._hindi_loading = False
        self._threads = threads
        if self._hindi_wanted() and self._language_mode() != "en":
            self._start_hindi_loader()

    # ------------------------------------------------------------------ Hindi support

    @staticmethod
    def _language_mode() -> str:
        return str(getattr(config, "WHISPER_LANGUAGE", "auto") or "auto").strip().lower()

    @staticmethod
    def _hindi_wanted() -> bool:
        return bool(getattr(config, "HINDI_ENABLED", True))

    def _start_hindi_loader(self):
        name = getattr(config, "HINDI_WHISPER_MODEL", "medium")
        if not name or name == config.WHISPER_MODEL_SIZE:
            self.hindi_model = self.model            # same model does both languages
            return
        if self._hindi_loading:
            return
        self._hindi_loading = True

        def work():
            try:
                logger.info("Loading the Hindi Whisper model '%s' in the background "
                            "(first run downloads it - Hindi uses the normal model until it is ready)...",
                            name)
                try:
                    model = WhisperModel(name, device=config.WHISPER_DEVICE,
                                         compute_type=config.WHISPER_COMPUTE_TYPE,
                                         cpu_threads=self._threads)
                except TypeError:
                    model = WhisperModel(name, device=config.WHISPER_DEVICE,
                                         compute_type=config.WHISPER_COMPUTE_TYPE)
                self.hindi_model = model
                logger.info("Hindi Whisper model '%s' ready.", name)
            except Exception as e:                   # no network, disk full ...
                logger.warning("Couldn't load the Hindi Whisper model '%s' (%s) - Hindi will use the "
                               "normal model, which is less accurate.", name, e)
            finally:
                self._hindi_loading = False

        threading.Thread(target=work, name="hindi-whisper-loader", daemon=True).start()

    def _language_probs(self, wav_path: str) -> dict:
        """Probabilities for the languages Whisper considers ({"en": 0.93, "hi": 0.02, ...})."""
        from faster_whisper import decode_audio          # lazy: the stub in tests has none
        audio = decode_audio(wav_path, sampling_rate=16000)
        result = self.model.detect_language(audio)
        probs = {}
        if isinstance(result, dict):
            probs = dict(result)
        elif isinstance(result, (tuple, list)):
            if len(result) >= 3 and result[2]:
                probs = dict(result[2])
            elif len(result) >= 2:
                probs = {result[0]: float(result[1])}
        return {str(k): float(v) for k, v in probs.items()}

    def _choose_language(self, wav_path: str, duration_s: float = 0.0) -> str:
        """'en' or 'hi' for this utterance (see the module docstring)."""
        mode = self._language_mode()
        if not self._hindi_wanted() or mode == "en":
            return "en"
        if mode == "hi":
            return "hi"
        try:
            probs = self._language_probs(wav_path)
        except Exception as e:
            logger.warning("Language detection failed (%s) - assuming %s.", e, self.last_language)
            return self.last_language
        en_p, hi_p = probs.get("en", 0.0), probs.get("hi", 0.0)
        need = float(getattr(config, "HINDI_MIN_PROBABILITY", 0.6))
        if self.last_language == "hi":
            # Stay in Hindi unless the sentence is clearly English (a mixed "Hinglish" sentence
            # scores in between and must not flip the conversation back and forth).
            chosen = "en" if (en_p >= need and en_p > hi_p) else "hi"
        else:
            chosen = "hi" if (hi_p >= need and hi_p > en_p) else "en"
        if duration_s and duration_s < 1.0:
            # A one-word answer ("yes", "haan") tells the detector almost nothing:
            # keep the language of the conversation unless it is very sure.
            other = "en" if self.last_language == "hi" else "hi"
            sure = (en_p if other == "en" else hi_p) >= 0.85
            chosen = other if sure else self.last_language
        logger.info("Language check: en=%.2f hi=%.2f -> %s", en_p, hi_p, chosen)
        return chosen

    # ------------------------------------------------------------------ transcription

    def transcribe(self, wav_path: str) -> str:
        guard_on = getattr(config, "GUARD_ENABLED", True)

        # Gate 1: is there anything worth transcribing at all?
        stats = None
        if guard_on:
            try:
                samples, sample_rate = transcript_guard.load_wav(wav_path)
                has_speech, stats, reason = transcript_guard.check_audio(samples, sample_rate)
                if not has_speech:
                    logger.info("Guard skipped Whisper: %s", reason)
                    return ""
            except Exception as e:  # never let the guard itself break transcription
                logger.warning("Audio pre-check failed (%s) - transcribing anyway.", e)

        language = self._choose_language(wav_path, getattr(stats, "duration_s", 0.0) if stats else 0.0)
        model = self.model
        if language == "hi":
            model = self.hindi_model or self.model
        vocabulary_prompt = _build_vocabulary_prompt() if language == "en" else ""
        started = time.time()
        segments, _info = model.transcribe(
            wav_path,
            beam_size=4,
            language=language,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=vocabulary_prompt or None,
            # Whisper's own silence/loop defences, stated explicitly so they
            # can't silently change with a library upgrade.
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

        # `segments` is a lazy generator - consume it exactly once.
        segments = list(segments)
        if language == "hi":
            logger.info("Hindi transcription took %.1fs.", time.time() - started)
        self.last_language = language

        # Gate 2: is what came back a real utterance?
        if not guard_on:
            return " ".join(s.text.strip() for s in segments).strip()

        result = transcript_guard.check_segments(
            segments,
            custom_vocabulary=_vocabulary_terms(),
            stats=stats,
            protected_phrases=config.SLEEP_PHRASES,
        )
        return result.text if result.ok else ""
