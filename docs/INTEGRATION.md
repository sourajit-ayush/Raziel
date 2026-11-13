# Wiring the three fixes into Raziel

Drop `shutdown.py`, `avatar_server.py` and `avatar.html` into
`C:\Users\Ayush\Desktop\Voice Agent`. `avatar_server.py` and `avatar.html`
replace the existing files; `shutdown.py` is new.

---

## 1. Ctrl+C

### main.py — at the very top of `main()`, before anything else starts

```python
import shutdown
shutdown.install()
```

### After creating the webview window

```python
import webview
window = avatar_window.create()          # your existing call
shutdown.on_shutdown(lambda: webview.destroy())
```

Registration order matters: callbacks run in reverse, so register the webview
last and it tears down first.

### The voice loop

Replace every `while True:` with a shutdown check, and every `time.sleep(x)`
with `shutdown.wait(x)`:

```python
def voice_loop():
    while not shutdown.is_shutting_down():
        if not wake_word.listen():        # returns False on shutdown, see below
            continue
        ...
    print("[voice] loop exited")
```

Make sure the thread is a daemon:

```python
threading.Thread(target=voice_loop, name="voice", daemon=True).start()
```

### wake_word.py / audio_recorder.py

The blocking `stream.read(...)` is what actually wedges the process. Either
close the stream on shutdown:

```python
import shutdown

stream = sd.InputStream(...)
stream.start()
shutdown.on_shutdown(stream.close)      # forces the blocked read to raise
```

…or read in small chunks and poll:

```python
while not shutdown.is_shutting_down():
    data, _ = stream.read(CHUNK)        # keep CHUNK small (512 frames ≈ 32ms)
    ...
```

### speaker.py

Piper playback should abort mid-utterance:

```python
for chunk in chunks:
    if shutdown.is_shutting_down():
        break
    play(chunk)
```

You already have barge-in interrupt machinery here — reuse the same flag check,
just OR it with `shutdown.is_shutting_down()`.

### llm_brain.py

Give the Ollama HTTP call a timeout so it can't hang the shutdown:

```python
ollama.chat(..., options={...}, keep_alive="30m")   # add timeout via the client
```

**What you'll see:** one Ctrl+C prints `[shutdown] Ctrl+C — stopping Raziel...`
and exits within ~1s. If something is stuck inside faster-whisper's C++ (which
cannot be interrupted), the watchdog force-exits after 4 seconds. A second
Ctrl+C kills instantly.

---

## 2 & 3. The avatar

### avatar_window.py — the window must be tall enough for a full body

```python
webview.create_window(
    "Raziel",
    "avatar.html",
    width=460, height=820,        # was probably square-ish; full body needs 1:1.8
    frameless=True,
    easy_drag=True,
    on_top=True,
    transparent=True,
    background_color="#00000000",
)
```

If a portrait window doesn't suit your desktop, leave it and send
`avatar_server.set_framing("upper")` instead — she'll frame to the waist.

### main.py — drive the state machine

This is the part that makes her feel present. Four calls, at four moments:

```python
import avatar_server

avatar_server.start()
avatar_server.set_framing("full")

# --- wake word fires ---------------------------------------------------
avatar_server.set_state("listening")
avatar_server.set_emotion("focused", 0.9)
speaker.say("Listening.")

# --- while recording the user's speech ---------------------------------
# stay in "listening" — she nods, holds eye contact, brow slightly furrowed

# --- transcript handed to the LLM --------------------------------------
avatar_server.set_state("thinking")     # gaze aversion up-and-away, slower blinks

# --- reply starts playing ----------------------------------------------
avatar_server.set_emotion(emotion.classify(reply_text), 1.0)
avatar_server.speech_start()
# ... viseme stream ...
avatar_server.speech_end("idle")

# --- sleep -------------------------------------------------------------
avatar_server.set_state("idle")
avatar_server.set_emotion("neutral")
```

The `thinking` state is the highest-value single line here — the pause between
you finishing a sentence and Raziel replying is currently dead air with a frozen
face. Gaze aversion fills it exactly the way a person does.

### speaker.py — visemes

Keep whatever `viseme_map.py` produces. Two options:

```python
# Per-phoneme, as you play audio:
avatar_server.send_viseme("aa", duration=0.09)

# Or schedule the whole utterance up front (smoother, less jitter):
avatar_server.send_viseme_sequence([
    {"v": "aa", "t": 0.00, "d": 0.08},
    {"v": "ih", "t": 0.08, "d": 0.06},
])
```

If you also have the audio RMS per chunk, `avatar_server.send_level(rms01)`
improves the fallback mouth. Not required.

**Fallback:** if visemes stop arriving for 400ms while `speaking` is set, the
avatar generates plausible mouth movement on its own. So even if the eSpeak
phoneme path is broken on first run, she will still appear to be talking — the
mouth is never frozen.

### emotion.py

Your keyword classifier needs to return one of:

`neutral` `happy` `amused` `sad` `concerned` `focused` `surprised` `angry` `confident`

`focused` and `concerned` are new and are the two that matter most for the
"active listening" feel — map "problem", "error", "stuck", "doubt", "help",
"why", "how do I" to `concerned` or `focused`, and let `confident` carry
Raziel's default answering tone.

---

## Test the avatar with zero Python running

Open `avatar.html` directly in a browser (or in the pywebview window with the
bridge down) and use the keyboard:

| Key | Does |
|---|---|
| `t` | Self-test — cycles idle → listening → thinking → speaking with fake visemes |
| `i` `l` `k` `s` | Force idle / listening / thinking / speaking |
| `e` | Random emotion |
| `1` `2` `3` | Framing: full body / upper / face |
| `d` | Debug overlay (fps, state, link, last message) |

Press `t` first. If she breathes, blinks, shifts weight, nods while listening,
looks away while thinking, and moves her mouth while speaking — the whole visual
layer is verified and anything still wrong is on the Python side.

---

## Tuning

Everything worth changing is in the `CONFIG` block at the top of `avatar.html`:

| Key | Effect |
|---|---|
| `cameraDrift` | 0 = tripod, 1 = handheld. Default 0.35 — a real video call is never perfectly still |
| `breathRate` | 0.24 Hz ≈ 14 breaths/min. Raise for a more anxious read |
| `nodMin` / `nodMax` | How often she nods while you're talking |
| `gestureMin` / `gestureMax` | Hand beats while speaking |
| `visemeAttack` | Higher = snappier mouth. Lower if lip sync looks twitchy |
| `babbleAfterMs` | How long before the fallback mouth kicks in |

If the idle motion reads as too busy, halve the `noise(...)` multipliers in
`Avatar.update` under "continuous head drift" — that layer is deliberately at
the edge of perceptible.

### If she's still in a T-pose

Your VRM's rest pose differs. Adjust `REST_POSE` at the top of the rig section —
`leftUpperArm: [0.06, -0.05, -1.24]` is the z value that swings the arm down.
Make it more negative to bring the left arm closer to the body, and mirror the
sign for the right.
