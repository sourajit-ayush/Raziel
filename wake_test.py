"""
wake_test.py - find out WHY "Raziel" does not wake her up.

Close main.py first (only one program should own the microphone), then:

    py wake_test.py            guided test: you say the wake word ~15 times, it scores every take
    py wake_test.py live       free-running monitor: say things, watch the scores move
    py wake_test.py record     record YOUR voice (about 5 minutes) so Raziel can be retrained on it
    py wake_test.py record --negatives-only   only the "other words" part (about 3 minutes)
    py wake_test.py devices    list microphones (use --device N to pick one)

It uses exactly the same model, the same microphone settings (16 kHz, 80 ms chunks)
and the same openWakeWord call as wake_word.py, so what you see here is what she hears.
It also loads the stock "hey_jarvis" model next to Raziel as a control:

    Raziel low, Jarvis high  -> the microphone is fine, the Raziel MODEL doesn't recognise your voice
    both low                 -> microphone / level / device problem
    Raziel high              -> it works; the threshold or the way you said it in main.py is the problem

Every take is saved as a .wav in wake_takes\\ together with results.json, so the
recordings can be re-scored offline without asking you to say it again.
Nothing is sent anywhere. Say each phrase the way you would really say it to her.
"""

import argparse
import collections
import json
import os
import re
import sys
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SAMPLE_RATE = 16000
CHUNK = 1280                      # 80 ms - the same as config.FRAME_LENGTH
CHUNK_S = CHUNK / SAMPLE_RATE
TAKES_DIR = os.path.join(HERE, "wake_takes")

try:
    import config
    THRESHOLD = float(getattr(config, "WAKE_WORD_THRESHOLD", 0.5))
except Exception as exc:          # config problems must not stop a diagnostic
    print(f"(could not read config.py: {exc!r} - assuming threshold 0.5)")
    THRESHOLD = 0.5


# ----------------------------------------------------------------- helpers

def _import_pyaudio():
    try:
        import pyaudio
        return pyaudio
    except ImportError:
        sys.exit("pyaudio is not installed in this Python. Run this with the same Python as main.py "
                 "(the venv in this folder), e.g.  venv\\Scripts\\python.exe wake_test.py")


def load_models(control=True):
    """One openWakeWord Model holding Raziel and (if it loads) the stock hey_jarvis control."""
    from openwakeword.model import Model

    raziel = os.path.join(HERE, "Raziel.onnx")
    if not os.path.isfile(raziel):
        sys.exit(f"Raziel.onnx is not in {HERE}")
    if not control:
        return Model(wakeword_models=[raziel], inference_framework="onnx")
    try:
        model = Model(wakeword_models=[raziel, "hey_jarvis"], inference_framework="onnx")
    except Exception as exc:
        print(f"(stock hey_jarvis control not available: {exc!r}) - testing Raziel only")
        model = Model(wakeword_models=[raziel], inference_framework="onnx")
    return model


def _find_key(keys, wanted):
    wanted = wanted.lower()
    for k in keys:
        if os.path.splitext(os.path.basename(k))[0].lower().startswith(wanted):
            return k
    return None


def score_keys(model):
    """Which key in Model.predict() is Raziel / hey_jarvis (found with one silent predict)."""
    out = model.predict(np.zeros(CHUNK, dtype=np.int16))
    model.reset()
    keys = list(out)
    return _find_key(keys, "raziel"), _find_key(keys, "hey_jarvis"), keys


def open_mic(pa, pyaudio, device):
    kw = dict(rate=SAMPLE_RATE, channels=1, format=pyaudio.paInt16, input=True,
              frames_per_buffer=CHUNK)
    if device is not None:
        kw["input_device_index"] = device
    return pa.open(**kw)


def describe_device(pa, device):
    try:
        info = pa.get_device_info_by_index(device) if device is not None else pa.get_default_input_device_info()
        return f"[{info.get('index')}] {info.get('name')}"
    except Exception as exc:
        return f"(unknown: {exc!r})"


def rms(x):
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def save_wav(path, audio):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.astype(np.int16).tobytes())


def bar(v, width=30):
    n = int(round(max(0.0, min(1.0, v)) * width))
    return "#" * n + "." * (width - n)


# ------------------------------------------------------------------ guided

PLAN = [
    ("raziel", "RAZIEL", 8),
    ("hey_raziel", "HEY RAZIEL", 4),
    ("hey_jarvis", "HEY JARVIS", 2),     # control: the stock model, proves the mic path
    ("quiet", "(say nothing - stay quiet)", 1),
]


def run_take(model, k_raz, k_jar, pa, pyaudio, device, label, prompt, index, total,
             secs, countdown):
    print(f"\nTake {index}/{total}:  say  {prompt}")
    if countdown:
        for n in (3, 2, 1):
            print(f"   {n}...", end="", flush=True)
            time.sleep(0.7)
        print()

    stream = open_mic(pa, pyaudio, device)
    model.reset()
    n_chunks = int(round(secs / CHUNK_S))
    cue_chunk = int(round(1.0 / CHUNK_S))
    audio, raz, jar, proc = [], [], [], []
    print("   (quiet for a moment...)", flush=True)
    try:
        for i in range(n_chunks):
            if i == cue_chunk and label != "quiet":
                print(f"   >>> SAY  {prompt}  NOW <<<", flush=True)
            data = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(data, dtype=np.int16)
            t = time.perf_counter()
            pred = model.predict(chunk)
            proc.append(time.perf_counter() - t)
            audio.append(chunk)
            raz.append(float(pred.get(k_raz, 0.0)) if k_raz else 0.0)
            jar.append(float(pred.get(k_jar, 0.0)) if k_jar else 0.0)
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass

    pcm = np.concatenate(audio)
    chunk_rms = [rms(c) for c in audio]
    res = {
        "label": label,
        "prompt": prompt,
        "raziel_peak": max(raz),
        "raziel_peak_at_s": round(int(np.argmax(raz)) * CHUNK_S, 2),
        "jarvis_peak": max(jar),
        "mic_peak_abs": int(np.max(np.abs(pcm.astype(np.int32)))),
        "loudest_chunk_rms": round(max(chunk_rms), 1),
        "quietest_chunk_rms": round(min(chunk_rms), 1),
        "predict_ms_mean": round(1000 * float(np.mean(proc)), 1),
        "predict_ms_max": round(1000 * float(np.max(proc)), 1),
        "raziel_scores": [round(s, 4) for s in raz],
        "jarvis_scores": [round(s, 4) for s in jar],
    }
    os.makedirs(TAKES_DIR, exist_ok=True)
    res["wav"] = f"take_{index:02d}_{label}.wav"
    save_wav(os.path.join(TAKES_DIR, res["wav"]), pcm)
    return res


def verdict(score):
    if score >= THRESHOLD:
        return "WOKE"
    if score >= 0.10:
        return "near miss"
    return "missed"


def print_take(res):
    r, j = res["raziel_peak"], res["jarvis_peak"]
    print(f"   Raziel  {bar(r)} {r:0.3f}  {verdict(r):9s}(wakes at {THRESHOLD:g}; peak at {res['raziel_peak_at_s']} s)")
    if any(x > 0 for x in res["jarvis_scores"]) or res["label"] == "hey_jarvis":
        print(f"   Jarvis  {bar(j)} {j:0.3f}")
    print(f"   mic: loudest {res['loudest_chunk_rms']:.0f} rms (speech is usually 400-3000), "
          f"quiet {res['quietest_chunk_rms']:.1f}, peak {res['mic_peak_abs']}")


def summarise(results):
    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    groups = {}
    for r in results:
        groups.setdefault(r["label"], []).append(r)

    for label, prompt, _ in PLAN:
        rs = groups.get(label)
        if not rs:
            continue
        peaks = sorted((r["raziel_peak"] for r in rs), reverse=True)
        woke = sum(p >= THRESHOLD for p in peaks)
        near = sum(0.10 <= p < THRESHOLD for p in peaks)
        line = "  ".join(f"{p:.2f}" for p in peaks)
        if label == "hey_jarvis":
            jp = "  ".join(f"{r['jarvis_peak']:.2f}" for r in rs)
            print(f"{prompt:28s} Jarvis model peaks: {jp}")
        elif label == "quiet":
            print(f"{'silence':28s} Raziel peak {peaks[0]:.2f}  (should stay low)")
        else:
            print(f"{prompt:28s} woke {woke}/{len(rs)}, near-miss {near}   Raziel peaks: {line}")

    raz = [r for r in results if r["label"] in ("raziel", "hey_raziel")]
    jar = [r for r in results if r["label"] == "hey_jarvis"]
    if not raz:
        return
    raz_peaks = [r["raziel_peak"] for r in raz]
    best = max(raz_peaks)
    woke = sum(p >= THRESHOLD for p in raz_peaks)
    loud = max(r["loudest_chunk_rms"] for r in results)
    slow = max(r["predict_ms_max"] for r in results)

    print("\nWhat this means")
    if loud < 250:
        print("  * The microphone is very quiet in this test (loudest chunk rms %.0f). Check the Windows "
              "input device / level before anything else." % loud)
    if slow > 80:
        print("  * A predict call took up to %.0f ms; the chunk is only 80 ms long. If that happens often "
              "she can drop audio while something else hogs the CPU." % slow)
    if woke >= 0.6 * len(raz):
        print(f"  * Raziel woke on {woke}/{len(raz)} takes here. The model works on your voice - if main.py "
              f"still ignores you, tell me and we look at main.py (something after the wake word).")
    elif best < 0.10:
        jarvis_ok = bool(jar) and max(r["jarvis_peak"] for r in jar) >= 0.3
        if jarvis_ok:
            print("  * The stock Jarvis model reacted, so the microphone path is fine, but Raziel scored ~0 "
                  "on every take: the trained model does not recognise your voice/pronunciation. "
                  "Fix = retrain including your own recordings (send me the wake_takes folder).")
        else:
            print("  * Raziel scored ~0 on everything and the Jarvis control did not react either (or was "
                  "skipped). Suspect the microphone / device / level - run  py wake_test.py devices.")
    else:
        print(f"  * Raziel reacts a little (best {best:.2f}) but rarely reaches {THRESHOLD:g}. Lowering "
              f"WAKE_WORD_THRESHOLD toward {max(0.1, round(best * 0.7, 2)):g} would catch more, at the price "
              f"of more false wakes. A retrain with your own voice would be the real fix.")


def guided(args):
    pyaudio = _import_pyaudio()
    print("Loading models...")
    model = load_models()
    k_raz, k_jar, keys = score_keys(model)
    if k_raz is None:
        sys.exit(f"Could not find the Raziel score in {keys}")
    pa = pyaudio.PyAudio()
    print(f"Microphone : {describe_device(pa, args.device)}")
    print(f"Threshold  : {THRESHOLD:g}  (config.WAKE_WORD_THRESHOLD)")
    print(f"Models     : Raziel {'+ hey_jarvis control' if k_jar else '(no control model)'}")

    extra = {}
    try:
        ans = input("\nHow do you say 'Raziel'?  1 = RAY-zee-el   2 = RAH-zee-el   3 = RAZ-zee-el   "
                    "or type your own (Enter to skip): ").strip()
        extra["pronunciation"] = ans
    except EOFError:
        pass

    plan = []
    for label, prompt, n in PLAN:
        n = args.takes if label == "raziel" else n
        plan += [(label, prompt)] * n
    total = len(plan)
    print(f"\n{total} short takes (~{int(total * 6.5)} s). Each take: 3-2-1, one second of quiet, "
          f"then the cue - say the phrase once, normally, at your normal distance from the mic.")
    print("Ctrl+C stops early and keeps what was recorded.")

    results = []
    try:
        for i, (label, prompt) in enumerate(plan, 1):
            res = run_take(model, k_raz, k_jar, pa, pyaudio, args.device, label, prompt, i, total,
                           secs=4.0, countdown=not args.no_countdown)
            results.append(res)
            print_take(res)
    except KeyboardInterrupt:
        print("\n(stopped early)")
    finally:
        pa.terminate()

    if results:
        os.makedirs(TAKES_DIR, exist_ok=True)
        out = {
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "threshold": THRESHOLD,
            "device": args.device,
            "score_keys": keys,
            **extra,
            "takes": results,
        }
        with open(os.path.join(TAKES_DIR, "results.json"), "w", encoding="utf-8") as f:
            json.dump(out, f)
        summarise(results)
        print(f"\nSaved {len(results)} takes + results.json in: {TAKES_DIR}")
        print("Tell me when it's done - I can read that folder and analyse the recordings.")


# ------------------------------------------------------------------ record
#
# Teaches Raziel YOUR voice. The model was trained on synthetic (text-to-speech) voices; on
# those it wakes ~95 % of the time, but a real voice in a real room can be far enough from
# them that it hardly reacts. Recording your own "Raziel" and retraining on it is the fix.
#
# Everything is saved under my_voice\ (positive\ = you saying the wake word, negative\ = you
# saying other things, so she learns YOUR voice is not always "Raziel"). raziel_train.py voice
# (in the Raziel Training folder) reads that folder.

VOICE_DIR = os.path.join(HERE, "my_voice")

ONSET_MIN = 100        # rms an 80 ms chunk must exceed to count as "started talking"
OFFSET_MIN = 70        # ... and fall below to count as "stopped"
HANG_CHUNKS = 6        # ~0.5 s of quiet ends an utterance
PRE_CHUNKS = 2         # ~160 ms kept from before the onset
TAIL_CHUNKS = 2        # ~160 ms kept after the last loud chunk

POSITIVE_PLAN = [
    # style, phrase, count, how to say it
    ("normal", "RAZIEL", 14, "the way you would really call her, from where you normally sit"),
    ("quiet", "RAZIEL", 6, "quietly, like it is late at night"),
    ("far", "RAZIEL", 6, "from further away - lean back or step back a little"),
    ("fast", "RAZIEL", 4, "quickly and casually"),
    ("hey", "HEY RAZIEL", 8, "with 'Hey' in front, as if getting her attention"),
]

NEGATIVE_WORDS = [
    "Rachel", "Ariel", "Gabriel", "Raphael", "Israel", "Daniel", "surreal", "cereal",
    "Rosie", "are you real", "radio", "what time is it", "play some music",
    "open the browser", "hey Jarvis", "okay, thank you", "good morning", "I am going to bed now",
]
FREE_TALK_TALKS = 3
FREE_TALK_SECS = 12.0
ROOM_SECS = 8.0


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:24] or "x"


class StartedTooEarly(Exception):
    """The person was already talking when the microphone opened, so the start of the word is lost."""


def _thresholds(ambient_rms):
    amb = min(float(ambient_rms), 60.0)               # a noisy measurement must not deafen us
    return max(ONSET_MIN, 4.0 * amb + 50.0), max(OFFSET_MIN, 2.5 * amb + 30.0)


def segment_speech(read_chunk, max_wait_s=10.0, max_speech_s=3.5, ambient_rms=None, calib_chunks=6):
    """
    Read 80 ms chunks until someone says something, then until they stop.

    read_chunk() -> int16 array of CHUNK samples.
    ambient_rms: the room's noise level if already known (measured once at the start of the
      session), so detection is live from the very first chunk. If None, the first
      `calib_chunks` chunks are used to measure it.
    Returns None if nothing was heard, else (clip, lead, info):
      clip = the utterance with ~160 ms of margin either side (this is what gets saved),
      lead = up to ~1.2 s of the room just before it (only used to score the clip realistically),
      info = dict(onset_threshold, ambient_rms)
    Raises StartedTooEarly if speech was already under way in the first two chunks (only checked
    when the room level is known, because then nothing hides it).
    """
    max_wait = int(max_wait_s / CHUNK_S)
    max_speech = int(max_speech_s / CHUNK_S)
    pre = collections.deque(maxlen=PRE_CHUNKS)
    lead = collections.deque(maxlen=15)
    calibrating = ambient_rms is None
    ambient = []
    onset = offset = None
    if not calibrating:
        onset, offset = _thresholds(ambient_rms)
    speech, quiet_run, started, seen = [], 0, False, 0

    while True:
        c = read_chunk()
        r = rms(c)
        seen += 1
        if not started:
            if calibrating:                                # first ~0.5 s: measure the room
                ambient.append(r)
                lead.append(c)
                pre.append(c)
                if len(ambient) >= calib_chunks:
                    calibrating = False
                    ambient_rms = float(np.median(ambient))
                    onset, offset = _thresholds(ambient_rms)
                continue
            if r > onset:
                if seen <= 2:
                    raise StartedTooEarly()
                started = True
                speech = list(pre) + [c]
                quiet_run = 0
            else:
                lead.append(c)
                pre.append(c)
                if seen >= max_wait:
                    return None
        else:
            speech.append(c)
            quiet_run = quiet_run + 1 if r < offset else 0
            if quiet_run >= HANG_CHUNKS or len(speech) >= max_speech:
                break

    if quiet_run > TAIL_CHUNKS:                           # drop the long quiet tail
        speech = speech[:len(speech) - (quiet_run - TAIL_CHUNKS)]
    lead_audio = np.concatenate(list(lead)) if lead else np.zeros(0, dtype=np.int16)
    return np.concatenate(speech), lead_audio, {"onset_threshold": onset, "ambient_rms": float(ambient_rms)}


def capture_fixed(read_chunk, secs):
    n = int(round(secs / CHUNK_S))
    return np.concatenate([read_chunk() for _ in range(n)])


def score_clip(model, key, clip, lead):
    """Peak Raziel score if this clip were heard live: ~1.2 s of room, the clip, then a moment of quiet."""
    want = int(1.2 * SAMPLE_RATE)
    lead = lead[-want:] if len(lead) else lead
    pad = np.zeros(max(0, want - len(lead)), dtype=np.int16)
    audio = np.concatenate([pad, lead.astype(np.int16), clip.astype(np.int16),
                            np.zeros(int(0.8 * SAMPLE_RATE), dtype=np.int16)])
    audio = audio[: (len(audio) // CHUNK) * CHUNK]
    model.reset()
    peak = 0.0
    for i in range(0, len(audio), CHUNK):
        peak = max(peak, float(model.predict(audio[i:i + CHUNK]).get(key, 0.0)))
    model.reset()
    return peak


def _redo_requested(wait_s=1.3):
    """Windows: True if R is pressed within wait_s (lets you redo a take you fumbled)."""
    try:
        import msvcrt
    except ImportError:
        return False
    end = time.time() + wait_s
    while time.time() < end:
        if msvcrt.kbhit() and msvcrt.getwch().lower() == "r":
            return True
        time.sleep(0.03)
    return False


def _next_index(folder, prefix):
    best = 0
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            m = re.match(rf"{prefix}_(\d+)", name)
            if m:
                best = max(best, int(m.group(1)))
    return best + 1


class _Session:
    """Holds the microphone plumbing shared by every recording step."""

    def __init__(self, pa, pyaudio, device, model, key):
        self.pa, self.pyaudio, self.device, self.model, self.key = pa, pyaudio, device, model, key
        self.ambient = None            # the room's noise level, measured once (see record())

    def listen(self, fn):
        """Open the mic, run fn(read_chunk), close it."""
        stream = open_mic(self.pa, self.pyaudio, self.device)
        try:
            return fn(lambda: np.frombuffer(stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16))
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass


def _record_utterance(sess, header, retries=3):
    """Prompt, capture, report. Returns (clip, lead) or None when the person gave up."""
    for attempt in range(retries + 1):
        print(header, flush=True)
        try:
            got = sess.listen(lambda rc: segment_speech(rc, ambient_rms=sess.ambient))
        except StartedTooEarly:
            print("   you started before the prompt finished - wait for it, then say it. Again:")
            continue
        if got is None:
            print("   ...didn't hear anything. Try again a little louder / closer.")
            continue
        clip, lead, info = got
        secs = len(clip) / SAMPLE_RATE
        if secs < 0.30:
            print(f"   too short ({secs:.2f} s) - again please.")
            continue
        return clip, lead, info
    print("   skipping this one.")
    return None


def _save_take(folder, name, clip, manifest, **meta):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    save_wav(path, clip)
    manifest.append(dict(file=os.path.relpath(path, VOICE_DIR).replace("\\", "/"),
                         secs=round(len(clip) / SAMPLE_RATE, 2),
                         rms=round(rms(clip), 0), peak=int(np.max(np.abs(clip.astype(np.int32)))), **meta))


def record(args):
    pyaudio = _import_pyaudio()
    print("Loading the current Raziel model (so each take can show how it reacts)...")
    model = load_models(control=False)
    k_raz, _, keys = score_keys(model)
    if k_raz is None:
        sys.exit(f"Could not find the Raziel score in {keys}")
    pa = pyaudio.PyAudio()
    sess = _Session(pa, pyaudio, args.device, model, k_raz)

    neg_only = bool(getattr(args, "negatives_only", False))
    plan = [] if neg_only else scale_plan(POSITIVE_PLAN, args.positives)
    n_pos = sum(n for _, _, n, _ in plan)

    print(f"\nMicrophone : {describe_device(pa, args.device)}")
    print(f"Saving to  : {VOICE_DIR}")
    if neg_only:
        print(f"""
NEGATIVES ONLY: no wake-word takes this time (the ones you already recorded are kept). About 3 minutes:
  1. {ROOM_SECS:.0f} s of silence (the sound of your room)
  2. you say {len(NEGATIVE_WORDS)} other words/phrases (so she learns what is NOT her name)
  3. you talk normally for {FREE_TALK_TALKS} x {FREE_TALK_SECS:.0f} s
Do NOT say "Raziel" in any of these. Speak at your normal volume when the prompt appears.
Please let it run to the end: the words and the talking are the part that matters here.""")
    else:
        print(f"""
This records YOU so the wake word can be retrained on your real voice. About 5 minutes:
  1. {ROOM_SECS:.0f} s of silence (the sound of your room)
  2. you say the wake word {n_pos} times, in a few different ways
  3. you say {len(NEGATIVE_WORDS)} other words/phrases (so she learns what is NOT her name)
  4. you talk normally for {FREE_TALK_TALKS} x {FREE_TALK_SECS:.0f} s
Speak when the prompt appears. After each take you have ~1 s to press R if you fumbled it.
Please let it run to the end - the words and the talking (steps 3 and 4) are needed too.
Do not click inside this window while it runs (Windows pauses a program while text is selected).
Ctrl+C stops early and keeps what was recorded.""")
    try:
        input("\nPress Enter when you are ready...")
    except EOFError:
        pass

    pos_dir = os.path.join(VOICE_DIR, "positive")
    neg_dir = os.path.join(VOICE_DIR, "negative")
    manifest_path = os.path.join(VOICE_DIR, "manifest.json")
    manifest = []
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f).get("clips", [])
        except Exception:
            manifest = []

    new_positive_peaks = []
    hot_takes = 0
    try:
        # 1 - the room
        print(f"\n[room] Stay quiet for {ROOM_SECS:.0f} seconds...")
        room = sess.listen(lambda rc: capture_fixed(rc, ROOM_SECS))
        _save_take(neg_dir, f"room_{_next_index(neg_dir, 'room'):02d}.wav", room, manifest,
                   kind="negative", style="room")
        chunk_levels = [rms(room[i:i + CHUNK]) for i in range(0, len(room) - CHUNK + 1, CHUNK)]
        sess.ambient = float(np.median(chunk_levels)) if chunk_levels else 0.0
        print(f"   ok (room noise level {sess.ambient:.1f} rms)")
        if int(np.max(np.abs(room.astype(np.int32)))) > 3000:
            print("   (there was speech or a loud noise in that - the noisy parts are left out of training automatically)")

        # 2 - the wake word
        for style, phrase, count, how in plan:
            print(f"\n--- {phrase}  ({how}) ---")
            done = 0
            while done < count:
                hdr = f"[{len(new_positive_peaks) + 1}/{n_pos}] say  {phrase}   ({style})"
                got = _record_utterance(sess, hdr)
                if got is None:
                    done += 1
                    continue
                clip, lead, info = got
                peak = score_clip(model, k_raz, clip, lead)
                loud = rms(clip)
                notes = []
                if int(np.max(np.abs(clip.astype(np.int32)))) >= 32000:
                    notes.append("clipping - lean back a little")
                    hot_takes += 1
                if style == "normal" and loud < 150:
                    notes.append("very quiet")
                print(f"   got it: {len(clip) / SAMPLE_RATE:.1f} s, level {loud:.0f} rms | current model {peak:.2f} "
                      f"({verdict(peak)})" + (f"  [{'; '.join(notes)}]" if notes else ""), flush=True)
                if allow_redo(args) and _redo_requested():
                    print("   (redo)")
                    continue
                _save_take(pos_dir, f"pos_{_next_index(pos_dir, 'pos'):03d}_{style}.wav", clip, manifest,
                           kind="positive", style=style, phrase=phrase, model_peak=round(peak, 3))
                new_positive_peaks.append(peak)
                done += 1

        if new_positive_peaks and hot_takes >= max(3, 0.4 * len(new_positive_peaks)):
            print(f"\n   NOTE: {hot_takes} of your {len(new_positive_peaks)} takes hit the top of the microphone's range. "
                  "Either you are speaking very loudly or the microphone level / automatic gain is set too high.\n"
                  "   (Windows Settings > System > Sound > Input > your mic: lower 'Input volume', turn off "
                  "'Enhance audio'.) Speak at the volume you will really use.")

        # 3 - other words
        print("\n--- now some OTHER words: say each one as it appears ---")
        for j, text in enumerate(NEGATIVE_WORDS, 1):
            while True:
                got = _record_utterance(sess, f"[{j}/{len(NEGATIVE_WORDS)}] say  \"{text}\"")
                if got is None:
                    break
                clip, lead, info = got
                peak = score_clip(model, k_raz, clip, lead)
                warn = "   <-- the current model false-wakes on this" if peak >= THRESHOLD else ""
                print(f"   got it: {len(clip) / SAMPLE_RATE:.1f} s | current model {peak:.2f}{warn}", flush=True)
                if allow_redo(args) and _redo_requested():
                    print("   (redo)")
                    continue
                _save_take(neg_dir, f"neg_{_next_index(neg_dir, 'neg'):03d}_{_slug(text)}.wav", clip, manifest,
                           kind="negative", style="word", phrase=text, model_peak=round(peak, 3))
                break

        # 4 - free talk
        for k in range(1, FREE_TALK_TALKS + 1):
            print(f"\n[talk {k}/{FREE_TALK_TALKS}] Talk normally for {FREE_TALK_SECS:.0f} seconds - anything: your "
                  f"day, what is on the screen, what you had for lunch. Starting in 3 seconds...", flush=True)
            time.sleep(3.0 if not args.no_countdown else 0)
            print("   GO", flush=True)
            talk = sess.listen(lambda rc: capture_fixed(rc, FREE_TALK_SECS))
            _save_take(neg_dir, f"talk_{_next_index(neg_dir, 'talk'):02d}.wav", talk, manifest,
                       kind="negative", style="talk")
            print(f"   ok (level {rms(talk):.0f} rms)")
    except KeyboardInterrupt:
        print("\n(stopped early)")
    finally:
        pa.terminate()
        os.makedirs(VOICE_DIR, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "threshold": THRESHOLD, "clips": manifest}, f, indent=1)

    if new_positive_peaks:
        woke = sum(p >= THRESHOLD for p in new_positive_peaks)
        near = sum(0.10 <= p < THRESHOLD for p in new_positive_peaks)
        print("\n" + "=" * 68)
        print(f"Saved {len(new_positive_peaks)} wake-word takes. The CURRENT model woke on {woke} of them "
              f"({near} more were near misses).")
        print("=" * 68)
    print(f"""
Recordings are in: {VOICE_DIR}

Next: retrain so she learns your voice. In the Raziel Training folder:
    py -3.12 raziel_train.py retrain
(about 30-40 minutes: it adds your recordings to the training data, then trains). Then copy the new
Raziel.onnx from Raziel Training\\my_custom_model into this folder, and run  py wake_test.py  again.""")


def scale_plan(plan, total):
    """Resize the takes-per-style plan to exactly `total` takes (0 = leave as is), keeping the proportions."""
    have = sum(n for _, _, n, _ in plan)
    if not total or total == have:
        return list(plan)
    exact = [n * total / have for _, _, n, _ in plan]
    counts = [int(x) for x in exact]
    order = sorted(range(len(plan)), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return [(s, p, c, h) for (s, p, _, h), c in zip(plan, counts) if c > 0]


def allow_redo(args):
    return not args.no_redo




# -------------------------------------------------------------------- live

def live(args):
    pyaudio = _import_pyaudio()
    print("Loading models...")
    model = load_models()
    k_raz, k_jar, keys = score_keys(model)
    if k_raz is None:
        sys.exit(f"Could not find the Raziel score in {keys}")
    pa = pyaudio.PyAudio()
    print(f"Microphone : {describe_device(pa, args.device)}")
    print(f"Say 'Raziel' (and 'Hey Jarvis'). Every score >= 0.10 is printed. Wake threshold {THRESHOLD:g}. "
          f"Ctrl+C to stop.\n")
    stream = open_mic(pa, pyaudio, args.device)
    best_r = best_j = 0.0
    n = 0
    try:
        while True:
            chunk = np.frombuffer(stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16)
            pred = model.predict(chunk)
            r = float(pred.get(k_raz, 0.0))
            j = float(pred.get(k_jar, 0.0)) if k_jar else 0.0
            n += 1
            best_r, best_j = max(best_r, r), max(best_j, j)
            level = min(1.0, rms(chunk) / 3000.0)
            if r >= 0.10 or j >= 0.10:
                tag = "  <<< WOULD WAKE" if r >= THRESHOLD else ""
                print(f"\r{time.strftime('%H:%M:%S')}  Raziel {r:0.2f}  Jarvis {j:0.2f}{tag}".ljust(78))
            print(f"\rmic [{bar(level, 20)}]  Raziel now {r:0.2f} best {best_r:0.2f} | "
                  f"Jarvis now {j:0.2f} best {best_j:0.2f}   ", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\nBest scores: Raziel {best_r:0.2f}, Jarvis {best_j:0.2f} over {n * CHUNK_S:.0f} s")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()


def devices(args):
    pyaudio = _import_pyaudio()
    pa = pyaudio.PyAudio()
    try:
        default = pa.get_default_input_device_info().get("index")
    except Exception:
        default = None
    print("Input devices (the * one is what main.py uses):")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            print(f"  {'*' if i == default else ' '} {i:2d}  {info.get('name')}")
    pa.terminate()


def main():
    ap = argparse.ArgumentParser(description="Diagnose the Raziel wake word.")
    ap.add_argument("mode", nargs="?", default="guided", choices=["guided", "live", "record", "devices"])
    ap.add_argument("--device", type=int, default=None, help="input device index (see: devices)")
    ap.add_argument("--takes", type=int, default=8, help="number of plain 'Raziel' takes (default 8)")
    ap.add_argument("--no-countdown", action="store_true", help="skip the 3-2-1")
    ap.add_argument("--positives", type=int, default=0, help="record: number of wake-word takes (default 38)")
    ap.add_argument("--no-redo", action="store_true", help="record: do not offer the press-R-to-redo pause")
    ap.add_argument("--negatives-only", action="store_true",
                    help="record: skip the wake-word takes; record only the room, other words and free talk")
    args = ap.parse_args()
    {"guided": guided, "live": live, "record": record, "devices": devices}[args.mode](args)


if __name__ == "__main__":
    main()
