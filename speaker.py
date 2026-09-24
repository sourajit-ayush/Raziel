"""
Speaks text out loud using Piper (local neural TTS, free, offline).

Unlike the old pyttsx3/SAPI approach, Piper is stateless - synthesizing
speech doesn't mutate any shared internal state, so the voice model is
loaded once here and safely reused for every utterance. No subprocess
isolation needed (pyttsx3 required this because it silently corrupted its
own internal state after being interrupted - Piper has no such state).

Playback happens by streaming the synthesized audio through a pyaudio
output stream in small chunks, checking a stop flag between chunks - this
is what makes barge-in able to cut speech off instantly, mid-word, rather
than waiting for a whole sentence/utterance to finish.

HINDI: a second Piper voice (config.HINDI_VOICE_NAME) speaks Devanagari text. It downloads
itself in the background on first start (about 60 MB) and is used as soon as it is ready.
A sentence that mixes both scripts ("आपके मैसेज में Fazal का नाम है") is split into runs and
each run is spoken by the voice that can pronounce it, then joined into one audio clip.
Until the Hindi voice is ready (or if it can't be loaded) Hindi text is spoken by the
English voice in romanised letters, which is understandable but not pretty.
"""

import io
import logging
import os
import queue
import threading
import time
import urllib.request
import wave
from collections import deque

import numpy as np
import pyaudio
from piper import PiperVoice, SynthesisConfig

import avatar_server
import config
import lang
from emotion import classify as classify_emotion
from viseme_map import phoneme_to_viseme

logger = logging.getLogger("voice_assistant")


def _rms(chunk_bytes: bytes) -> float:
    samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples**2)))


class Speaker:
    def __init__(self):
        logger.info("Loading Piper voice '%s'...", config.PIPER_VOICE_NAME)
        self.voice = PiperVoice.load(config.PIPER_MODEL_PATH, config.PIPER_CONFIG_PATH)
        self.syn_config = SynthesisConfig(
            length_scale=config.PIPER_LENGTH_SCALE,
            volume=config.TTS_VOLUME,
        )
        logger.info("Piper voice loaded.")
        # initiative.py speaks from its own thread while the voice loop speaks
        # from another. Without this, two utterances overlap in the speakers,
        # and whichever finishes first sends speech_end to the avatar while the
        # other is still talking (mouth freezes mid-sentence).
        self._play_lock = threading.Lock()

        # Hindi voice (optional, loaded in the background - see the module docstring)
        self.hindi_voice = None
        self.hindi_syn_config = None
        self.hindi_state = "off"          # off | loading | ready | failed
        self._hindi_warned = False
        if getattr(config, "HINDI_ENABLED", True):
            self.start_hindi_voice_loader()

    # ------------------------------------------------------------------ Hindi voice

    @staticmethod
    def _piper_voice_urls(name: str):
        """'hi_IN-priyamvada-medium' -> (.onnx URL, .onnx.json URL) in the rhasspy/piper-voices repo."""
        lang_region, voice, quality = name.split("-", 2)
        family = lang_region.split("_")[0]
        base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{family}/{lang_region}/{voice}/{quality}/{name}"
        return base + ".onnx", base + ".onnx.json"

    @staticmethod
    def _download_file(url: str, dest: str):
        """Downloads url to dest through a .part file, so a cut-off download never leaves a broken model."""
        req = urllib.request.Request(url, headers={"User-Agent": "Raziel/1.0"})
        part = dest + ".part"
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                out.write(block)
        os.replace(part, dest)

    def _load_hindi_voice(self):
        name = getattr(config, "HINDI_VOICE_NAME", "hi_IN-priyamvada-medium")
        script_dir = os.path.dirname(os.path.abspath(config.PIPER_MODEL_PATH))
        model_path = getattr(config, "HINDI_MODEL_PATH", os.path.join(script_dir, f"{name}.onnx"))
        config_path = getattr(config, "HINDI_CONFIG_PATH", os.path.join(script_dir, f"{name}.onnx.json"))
        if not (os.path.exists(model_path) and os.path.exists(config_path)):
            model_url, config_url = self._piper_voice_urls(name)
            logger.info("Downloading the Hindi voice '%s' (about 60 MB, one time)...", name)
            self._download_file(config_url, config_path)
            self._download_file(model_url, model_path)
            logger.info("Hindi voice downloaded.")
        self.hindi_voice = PiperVoice.load(model_path, config_path)
        self.hindi_syn_config = SynthesisConfig(
            length_scale=float(getattr(config, "HINDI_LENGTH_SCALE", 1.05)),
            volume=config.TTS_VOLUME,
        )

    def start_hindi_voice_loader(self):
        """Loads (downloading first if needed) the Hindi voice on a background thread."""
        if self.hindi_state in ("loading", "ready"):
            return
        self.hindi_state = "loading"

        def work():
            try:
                self._load_hindi_voice()
                self.hindi_state = "ready"
                logger.info("Hindi voice ready.")
            except Exception as e:
                self.hindi_state = "failed"
                logger.warning("Couldn't load the Hindi voice (%s). Hindi replies will be spoken by "
                               "the English voice in romanised letters. Check your internet "
                               "connection and restart, or set HINDI_ENABLED = False.", e)

        threading.Thread(target=work, name="hindi-voice-loader", daemon=True).start()

    def _voice_runs(self, text: str):
        """[(voice_key, text)] - which voice speaks which part of `text` ("en" | "hi")."""
        if not (getattr(config, "HINDI_ENABLED", True) and lang.has_devanagari(text)):
            return [("en", text)]
        runs = []
        for run_lang, chunk in lang.split_runs(text):
            if not any(ch.isalnum() or "\u0900" <= ch <= "\u097F" for ch in chunk):
                if runs:
                    runs[-1] = (runs[-1][0], runs[-1][1] + chunk)     # punctuation stays with its neighbour
                    continue
            key = run_lang
            if key == "hi" and self.hindi_voice is None:
                if not self._hindi_warned:
                    self._hindi_warned = True
                    logger.warning("Hindi voice not ready (%s) - speaking Hindi in romanised letters "
                                   "with the English voice.", self.hindi_state)
                chunk, key = lang.to_latin(chunk), "en"
            if runs and runs[-1][0] == key:
                runs[-1] = (key, runs[-1][1] + chunk)
            else:
                runs.append((key, chunk))
        return runs or [("en", text)]

    @staticmethod
    def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
        if src_rate == dst_rate or not pcm:
            return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n_out = max(1, int(round(len(x) * dst_rate / float(src_rate))))
        y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()

    def _synthesize(self, text: str):
        """Returns (pcm_bytes, n_channels, sample_width, frame_rate, viseme_timeline)."""
        runs = self._voice_runs(text)
        try:
            return self._synthesize_runs(runs)
        except Exception:
            if self.hindi_voice is None or not any(key == "hi" for key, _ in runs):
                raise
            # The Hindi voice loaded but cannot speak (corrupt download, ...): never go silent - give
            # Hindi to the English voice, romanised, for the rest of the run.
            logger.exception("The Hindi voice failed while speaking - using the English voice for Hindi from now on")
            self.hindi_voice, self.hindi_state = None, "failed"
            return self._synthesize_runs(self._voice_runs(text))

    def _synthesize_runs(self, runs):
        if len(runs) == 1:
            key, chunk = runs[0]
            return self._synth_raw(*(self._voice_for(key)), chunk)
        # Mixed Hindi / English: synthesize each run with its own voice and join them.
        pcm_all, timeline, offset = b"", [], 0.0
        channels = width = rate = None
        for key, chunk in runs:
            pcm, ch, w, r, tl = self._synth_raw(*(self._voice_for(key)), chunk)
            if rate is None:
                channels, width, rate = ch, w, r
            elif r != rate:
                pcm = self._resample(pcm, r, rate)
            for item in tl:
                timeline.append({**item, "start": item["start"] + offset})
            pcm_all += pcm
            offset += len(pcm) / float(rate * width * channels)
        return pcm_all, channels, width, rate, timeline

    def _voice_for(self, key: str):
        if key == "hi" and self.hindi_voice is not None:
            return self.hindi_voice, self.hindi_syn_config
        return self.voice, self.syn_config

    def _synth_raw(self, voice, syn_config, text: str):
        """One voice, one piece of text -> (pcm_bytes, n_channels, sample_width, frame_rate, timeline)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_writer:
            alignments = voice.synthesize_wav(
                text, wav_writer, syn_config=syn_config,
                include_alignments=config.AVATAR_ENABLED,
            )
        buf.seek(0)
        with wave.open(buf, "rb") as wav_reader:
            n_channels = wav_reader.getnchannels()
            sample_width = wav_reader.getsampwidth()
            frame_rate = wav_reader.getframerate()
            pcm_data = wav_reader.readframes(wav_reader.getnframes())

        viseme_timeline = []
        if alignments:
            t = 0.0
            for a in alignments:
                duration = a.num_samples / frame_rate
                viseme_timeline.append({
                    "viseme": phoneme_to_viseme(a.phoneme),
                    "start": t,
                    "duration": duration,
                })
                t += duration

        return pcm_data, n_channels, sample_width, frame_rate, viseme_timeline

    def _play(self, text: str, stop_event=None, prepared=None):
        """Synthesizes and plays text, checking stop_event between chunks for interruptibility.
        `prepared` is an already-synthesized _synthesize() result (streaming replies
        synthesize the NEXT sentence while the current one is still playing)."""
        with self._play_lock:
            self._play_locked(text, stop_event, prepared)

    def _play_locked(self, text: str, stop_event=None, prepared=None):
        pcm_data, n_channels, sample_width, frame_rate, viseme_timeline = (
            prepared if prepared is not None else self._synthesize(text)
        )

        out_pa = None
        speech_started = False
        try:
            out_pa = pyaudio.PyAudio()
            stream = out_pa.open(
                format=out_pa.get_format_from_width(sample_width),
                channels=n_channels,
                rate=frame_rate,
                output=True,
            )

            # Tell the avatar to start talking only NOW, with the audio device
            # open and the first chunk about to play. Previously this was sent
            # before PyAudio() and open() ran, so the mouth started moving
            # 100-500 ms ahead of any sound - and if opening the device failed,
            # it was left "speaking" with no audio at all.
            if config.AVATAR_ENABLED:
                avatar_server.send_emotion(classify_emotion(text))
                avatar_server.send_speech(viseme_timeline)
                speech_started = True

            chunk_size = 2048
            for i in range(0, len(pcm_data), chunk_size):
                if stop_event is not None and stop_event.is_set():
                    break
                stream.write(pcm_data[i:i + chunk_size])
            stream.stop_stream()
            stream.close()
        finally:
            if out_pa is not None:
                out_pa.terminate()
            # Paired with send_speech above: sent whenever (and only when) a
            # speech_start went out, on every exit path including exceptions.
            if config.AVATAR_ENABLED and speech_started:
                avatar_server.send_speech_stop()

    def speak(self, text: str):
        """Simple blocking speak, no interruption support."""
        if not text:
            text = "Input not registered."
        logger.info("Speaking: %s", text)
        self._play(text)

    def speak_interruptible(self, text: str, pa: pyaudio.PyAudio):
        """
        Speaks text, but stops early if you start talking over it.

        Returns (interrupted, pre_roll_frames):
          - interrupted: True if you cut it off mid-sentence
          - pre_roll_frames: audio captured right as the interruption
            happened, so the next recording doesn't clip your first word

        Note: this listens through the same mic that hears your speaker
        output, so on laptop speakers it may occasionally false-trigger on
        the assistant's own voice. Works best with headphones.
        """
        if not text:
            text = "Acknowledged."

        logger.info("Speaking (interruptible): %s", text)
        stop_event = threading.Event()
        interrupted = {"value": False}

        play_thread = threading.Thread(
            target=self._play, args=(text, stop_event), daemon=True
        )
        play_thread.start()

        # Brief grace period before listening, so it doesn't instantly
        # trip on the first syllable of its own speech through the speakers.
        time.sleep(config.BARGE_IN_GRACE_PERIOD)

        chunks_per_second = config.SAMPLE_RATE / config.FRAME_LENGTH
        pre_roll = deque(maxlen=max(1, int(0.5 * chunks_per_second)))

        stream = None
        peak = 0.0                       # loudest mic frame while she spoke (echo level, for tuning)
        consecutive = 0                  # frames in a row at/above threshold (debounce)
        need = max(1, int(getattr(config, "BARGE_IN_CONSECUTIVE_FRAMES", 1)))
        try:
            stream = pa.open(
                rate=config.SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=config.FRAME_LENGTH,
            )
            while play_thread.is_alive():
                data = stream.read(config.FRAME_LENGTH, exception_on_overflow=False)
                pre_roll.append(data)
                level = _rms(data)
                peak = max(peak, level)
                if level >= config.BARGE_IN_RMS_THRESHOLD:
                    consecutive += 1
                    if consecutive >= need:
                        logger.info("Barge-in detected (mic level %.0f >= %s, %d frames), stopping speech.",
                                    level, config.BARGE_IN_RMS_THRESHOLD, consecutive)
                        stop_event.set()
                        interrupted["value"] = True
                        break
                else:
                    consecutive = 0
        except Exception as e:
            logger.error("Barge-in monitoring failed: %s", e)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()

        play_thread.join(timeout=3)
        if not interrupted["value"]:
            logger.info("Mic level while speaking: peak %.0f (barge-in threshold %s)",
                        peak, config.BARGE_IN_RMS_THRESHOLD)
        return interrupted["value"], (list(pre_roll) if interrupted["value"] else None)

    def speak_stream_interruptible(self, pieces, pa: pyaudio.PyAudio, protect=None):
        """
        Speaks a reply that is still being WRITTEN: `pieces` is a generator of
        sentences (Brain.stream_turn()). Three stages run concurrently, so the
        first sentence is heard as soon as the model has produced it rather than
        after the whole reply:

            producer   pulls sentences out of the LLM generator
            synthesizer  turns the next sentence into audio while the current one plays
            player     plays them back to back

        Barge-in works exactly as in speak_interruptible(): talking over her
        stops playback AND closes the generator (which stops the LLM stream).

        `protect(sentence) -> bool` (optional) marks sentences that must be heard IN
        FULL - a question that needs a yes/no. While such a sentence plays, talking
        (or her own voice coming back through laptop speakers) does not cut her off.

        Returns (interrupted, pre_roll_frames, spoken_text).
        """
        protected = threading.Event()   # set from a protected sentence's start until the next one starts
        stop_event = threading.Event()
        interrupted = {"value": False}
        spoken = []
        first_audio_at = []        # set when the first sentence starts playing
        DONE = object()
        text_q = queue.Queue()     # sentences waiting to be synthesized
        audio_q = queue.Queue()    # (sentence, synthesized audio) waiting to be played

        def producer():
            produced = 0
            try:
                for piece in pieces:
                    if stop_event.is_set():
                        break
                    piece = (piece or "").strip()
                    if piece:
                        produced += 1
                        text_q.put(piece)
            except Exception:
                logger.exception("Generating the reply failed")
                if not produced:
                    text_q.put("That request failed.")
            finally:
                close = getattr(pieces, "close", None)
                if callable(close):
                    try:
                        close()          # lets Brain.stream_turn() record what was said
                    except Exception:
                        logger.exception("Closing the reply stream failed")
                text_q.put(DONE)

        def synthesizer():
            while True:
                item = text_q.get()
                if item is DONE:
                    audio_q.put(DONE)
                    return
                if stop_event.is_set():
                    continue             # keep draining until DONE
                try:
                    audio_q.put((item, self._synthesize(item)))
                except Exception:
                    logger.exception("Speech synthesis failed for: %s", item)

        def player():
            while True:
                item = audio_q.get()
                if item is DONE:
                    return
                if stop_event.is_set():
                    continue
                text, prepared = item
                logger.info("Speaking: %s", text)
                spoken.append(text)
                if not first_audio_at:
                    first_audio_at.append(time.time())
                must_hear = False
                if protect is not None:
                    try:
                        must_hear = bool(protect(text))
                    except Exception:
                        logger.exception("protect() failed (non-fatal)")
                # Stays set after the question ends, until the NEXT sentence starts (or the
                # player finishes): clearing it at the end of the sentence leaves a gap, before
                # the player thread exits, in which the tail of her own voice would still count.
                if must_hear:
                    protected.set()
                else:
                    protected.clear()
                try:
                    self._play(text, stop_event, prepared)
                except Exception:
                    logger.exception("Playback failed")

        threads = [
            threading.Thread(target=producer, name="reply-producer", daemon=True),
            threading.Thread(target=synthesizer, name="reply-synth", daemon=True),
            threading.Thread(target=player, name="reply-player", daemon=True),
        ]
        for t in threads:
            t.start()
        play_thread = threads[2]

        chunks_per_second = config.SAMPLE_RATE / config.FRAME_LENGTH
        pre_roll = deque(maxlen=max(1, int(0.5 * chunks_per_second)))

        monitor_failed = False
        stream = None
        peak = 0.0                       # loudest mic frame while she spoke (echo level, for tuning)
        consecutive = 0                  # frames in a row at/above threshold (debounce)
        need = max(1, int(getattr(config, "BARGE_IN_CONSECUTIVE_FRAMES", 1)))
        try:
            stream = pa.open(
                rate=config.SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=config.FRAME_LENGTH,
            )
            while play_thread.is_alive():
                data = stream.read(config.FRAME_LENGTH, exception_on_overflow=False)
                pre_roll.append(data)
                # Barge-in only counts once she is actually TALKING (plus the
                # usual grace period so her first syllable through the speakers
                # doesn't trip it). Until then she is still thinking or running a
                # tool - and a tool can be loud itself (music starting), which
                # must not read as "the user interrupted".
                if not first_audio_at or time.time() - first_audio_at[0] < config.BARGE_IN_GRACE_PERIOD:
                    continue
                level = _rms(data)
                peak = max(peak, level)
                if protected.is_set():
                    consecutive = 0
                    continue                     # a question she must finish asking
                if level >= config.BARGE_IN_RMS_THRESHOLD:
                    consecutive += 1
                    if consecutive >= need:
                        logger.info("Barge-in detected (mic level %.0f >= %s, %d frames), stopping speech.",
                                    level, config.BARGE_IN_RMS_THRESHOLD, consecutive)
                        stop_event.set()
                        interrupted["value"] = True
                        break
                else:
                    consecutive = 0
        except Exception as e:
            logger.error("Barge-in monitoring failed: %s", e)
            monitor_failed = True
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()

        if monitor_failed and not interrupted["value"]:
            play_thread.join(timeout=60)      # nothing can interrupt her now: let her finish
        else:
            play_thread.join(timeout=3)
        if not interrupted["value"] and first_audio_at:
            logger.info("Mic level while speaking: peak %.0f (barge-in threshold %s)%s",
                        peak, config.BARGE_IN_RMS_THRESHOLD,
                        " - a yes/no question was protected" if protected.is_set() else "")
        # Make sure the LLM generator has been closed (and its history
        # bookkeeping done) BEFORE the next turn starts using the same Brain.
        threads[0].join(timeout=6)

        return (
            interrupted["value"],
            (list(pre_roll) if interrupted["value"] else None),
            " ".join(spoken),
        )
