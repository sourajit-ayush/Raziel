# Raziel — Session Handoff #2 (post-avatar-rewrite, mid wake-word-training)

Paste this as the first message of a new chat to resume with full context.
Supersedes/extends the original HANDOFF.md — that doc's §1-4, §6-9 are still
accurate background; this covers everything since.

---

## 0. Two parallel threads running

1. **Voice Agent app** — `C:\Users\Ayush\Desktop\Voice Agent` — the assistant itself
2. **Wake word training** — `C:\Users\Ayush\Desktop\Raziel Training` — separate folder, separate venv, produces `Raziel.onnx` to drop into (1)

Do not confuse the two folders/venvs. Different Python installs, different
dependency sets, different purposes.

---

## 1. Ctrl+C fix — DELIVERED, integration APPLIED, not re-confirmed since

**File:** `shutdown.py` (new). Native `SetConsoleCtrlHandler` via ctypes,
because Windows only delivers `KeyboardInterrupt` between bytecode
instructions on the main thread, and `webview.start()` blocks that thread in
a native message loop.

**Critical ordering, both applied to main.py at time of writing:**
- `import shutdown` must be the **first** import in main.py, before numpy/
  torch/faster-whisper — sets `FOR_DISABLE_CONSOLE_CTRL_HANDLER=1` before
  Intel MKL's Fortran runtime loads (MKL installs its own Ctrl+C handler
  that calls `abort()`, producing `forrtl: error (200)`).
- `shutdown.install()` called **late** (right before `webview.start()`),
  registered **after** `shutdown.on_shutdown(lambda: webview.destroy())` —
  Windows calls console handlers in reverse registration order, so ours
  must be last-registered to run first.
- `run_voice_assistant`'s main loop changed to
  `while not shutdown.is_shutting_down():`

**Status:** applied to main.py in an earlier session turn. Never explicitly
re-tested after later changes (avatar rewrite, initiative wiring). Worth a
quick Ctrl+C test next time the app runs.

---

## 2. Avatar system — mostly working, transparency UNRESOLVED

**Files:** `avatar.html` (full rewrite, ~1360 lines), `avatar_server.py`
(WebSocket bridge), `avatar_window.py` (self-hosted file server + pywebview
window). All three replace the untested originals from HANDOFF.md §10.

### Working
- Full-body framing (fixed T-pose, camera auto-frames to model height)
- Idle life: breathing, blinking, weight shifts, continuous head/limb drift
- 4-state behaviour machine: idle / listening / thinking / speaking, each
  with distinct posture, gaze, blink rate
- 22-gesture library (head, torso, hand-speaking, hand-thinking/idle) with
  finger curl, spread, per-state weighted pools, ambient auto-firing
- Lip sync: attack/release smoothing, jaw-bone coupling, babble fallback if
  Python stops sending visemes mid-utterance
- `avatar_server.py` has extensive back-compat aliases (`send_emotion`,
  `send_state`, `send_speech`, `send_speech_stop`, etc.) PLUS a
  `__getattr__` fallback: any unknown call from speaker.py prints
  `[avatar] note: avatar_server.X() is not implemented` instead of crashing
  the voice thread. **Check console for these notes** — each one is a real
  gap worth wiring properly.

### Fixed this session (7-issues list)
- **Gestures shown "active" but invisible**: `ambientGesture()` only avoided
  repeating the *previous* pick, not names *currently playing*, so it
  frequently requested something already blocked by the no-self-stack rule
  and silently did nothing. Fixed: filters against truly-active names,
  concurrency cap 3→4, amplitude floor raised. Added one-time bone
  diagnostics at startup (console) — check for `MISSING CORE BONES` warning.
- **Mouth moves with no speech**: added `CONFIG.maxSpeakingMs = 25000`
  safety valve — if `speaking` state persists 25s with no real viseme,
  forces idle automatically. Doesn't fix the root cause in speaker.py
  (something sets state=speaking without ever clearing it), but prevents
  the symptom.
- **Hand clipping through clothes**: partial mitigation only — rest-pose
  arms nudged ~2-3° further from torso, `handToChest`/`armFidget` reach-in
  depth reduced. Real fix needs cloth colliders on the VRM (may not have
  any) or a different model. NOT fully solvable via pose code alone.

### UNRESOLVED — transparency
Chroma-key approach: page paints magenta (`?chroma=ff00ff`), Win32
`SetLayeredWindowAttributes` + `LWA_COLORKEY` applied to the host window.
Diagnosed that WebView2 renders into its own child HWND
(`Chrome_WidgetWin_0`), not the host — fixed to key the child too, and
matched host `background_color` to the chroma colour. Also added
`GPU_COMPOSITING = False` (disables WebView2 hardware compositing via
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`) since GPU-composited surfaces may
bypass classic window layering (DirectComposition) entirely.

**Last known state: still showing magenta, unconfirmed after the
GPU_COMPOSITING fix specifically** — conversation moved to other issues
before this was retested. If it's still magenta after `GPU_COMPOSITING =
False`, that's very likely a hard architectural limit (DirectComposition
presentation bypasses GDI-level colour keys) — fallback documented at the
bottom of `avatar_window.py`: give up on true transparency, set
`TRANSPARENCY = "none"`, pick a dark background colour instead, shrink the
window to fit her silhouette.

**To resume:** run `python avatar_window.py` standalone (or `python
main.py`), check console for `[avatar] keyed host hwnd=...` and `[avatar]
keyed child hwnd=... class=Chrome_WidgetWin_0` lines, report whether magenta
is actually gone.

---

## 3. Wake word training — IN PROGRESS, close

**Location:** `C:\Users\Ayush\Desktop\Raziel Training` (separate from Voice
Agent). **Why local instead of Colab:** Colab's free-tier RAM (12.67 GB) was
insufficient for the 17 GB negative feature set, and the VM gets wiped on
disconnect, costing full re-downloads repeatedly. User has 16GB RAM + RTX
40-series GPU locally.

**Orchestrator:** `raziel_train.py` — stages: `setup`, `data`, `clips`,
`features`, `train`, `reset`, or `all`. Run one at a time:
```
py -3.12 raziel_train.py <stage>
```
Self-relaunches under its own `.venv` automatically if invoked via system
Python (handles the common mistake of running outside the venv).

**Preset: `"quality"`** (edit `PRESET` at top of file to `"fast"` for a
quick end-to-end pipeline check instead — 2k clips vs 30k):
- 30,000 positive clips (train) + 5,000 (val)
- Full 5.6M-row negative set (not the RAM-friendly subset — 16GB is enough
  since openWakeWord memory-maps it rather than loading it whole)
- 50,000 training steps, 2 augmentation rounds, 4 hours background audio
- `target_phrase`: `["Raziel", "Rah zee el", "Razzy el"]` — 3 pronunciation
  variants since DeepPhonemizer guessed *ruh-ZEEL* by default
- 23 custom hard negatives (Rachel, Ariel, Gabriel, Israel, Daniel,
  Nathaniel, radio, razzle, reseal, resell, surreal, cereal, serial,
  material, Rosie, Rosalie, Raz, "as real", "are you real", etc.) — these
  train the model to specifically reject phonetically-similar words

### Dependency hell — RESOLVED via constraints.txt
Resurrecting a 2023-era ML pipeline against Sept-2026 package versions
surfaced a chain of real, previously-invisible version conflicts. All now
locked in a pip constraints file (`constraints.txt`, auto-written, applied
via `-c` to **every** install call so no later package can silently
override an earlier pin):

| Package | Pin | Why |
|---|---|---|
| numpy | `<2` | pyarrow 14.0.2's compiled ext needs the NumPy 1.x ABI |
| pyarrow | `==14.0.2` | datasets 2.14.6 subclasses old `pa.PyExtensionType` |
| setuptools | `<81` | v82 deleted `pkg_resources` outright; `pronouncing` needs it |
| scipy | `<1.17` | `acoustics==0.2.6` calls `scipy.special.sph_harm`, removed in 1.17 |
| datasets | `==2.14.6` | matches maintainer's own tested training notebook |
| torch / torchaudio | `2.5.1+cu124` | predates the `weights_only` flip (2.6) and the `torchaudio.info` removal (2.9) — this pin is *why* local training avoids both bugs that plagued Colab |

Also required (not in constraints.txt, exact-pinned in the install list):
`torchinfo==1.8.0`, `torchmetrics==1.2.0`, `speechbrain==0.5.14`,
`audiomentations==0.33.0`, `torch-audiomentations==0.11.0`,
`acoustics==0.2.6`, `pronouncing==0.2.0`, `deep-phonemizer==0.0.19`,
`webrtcvad-wheels` (plain `webrtcvad` has no Windows wheel — this fork does,
verified by direct install test), `piper-phonemize-fix` (plain
`piper-phonemize` has never shipped Windows wheels — this fork does,
**verified directly**: installs as module `piper_phonemize` with function
`phonemize_espeak`, zero numpy dependency).

**Explicitly NOT installed:** `tensorflow-cpu`/`tensorflow_probability`/
`onnx_tf` — only needed for optional `.tflite` export (not used; this
project wants `.onnx`), and the notebook's exact pin (2.8.1) predates
Python 3.12 support so would fail to install anyway.

`setup` stage ends with a verification block importing every pinned
library + confirming `sph_harm`, `phonemize_espeak`, `webrtcvad` — if this
prints clean with no traceback, the environment is genuinely sound.

### Current status at last message
Positive clips done (30k+5k, confirmed on disk). Negative clip generation
hit a **corrupted download** (not a version issue) —
`en_us_cmudict_forward.pt` from DeepPhonemizer's S3 bucket, cached at
`.venv\Lib\site-packages\openwakeword\resources\en_us_cmudict_forward.pt`,
was 52.5MB instead of the full 66.7MB (`PytorchStreamReader failed reading
zip archive: failed finding central directory` = truncated zip). Diagnosed
and fixed via `fix_phonemizer_cache.py` (searches known cache locations,
verifies each `.pt` via `zipfile.is_zipfile()`, deletes only invalid ones).
User ran `--fix` and was re-running `clips` — **outcome not yet confirmed**.

**Next steps once clips finish:**
```
py -3.12 raziel_train.py clips      # confirm this completes clean first
py -3.12 raziel_train.py features
py -3.12 raziel_train.py train
```
Then copy `Raziel.onnx` into Voice Agent folder, set in `config.py`:
```python
WAKE_WORD_MODEL = "Raziel.onnx"
WAKE_WORD_THRESHOLD = 0.5   # start here, tune: false-wakes→raise, ignored→lower
```

**If `target_phrase` is ever edited again:** run `python raziel_train.py
reset` first (prompts `CONFIRM = True` before deleting) — the trainer
silently reuses existing positive clips otherwise, wasting the whole
regeneration.

---

## 4. Seven voice-pipeline issues reported — fixes delivered, INTEGRATION PENDING

Never seen the actual source of `main.py`, `speaker.py`, `tools.py`,
`transcriber.py`, `audio_recorder.py` — only line-number fragments via
tracebacks across the whole conversation. Everything below is either a
complete, tested, standalone drop-in file, or best-effort guidance pending
those files. **Ask the user to paste those five files** to finish this
properly rather than continuing to guess line numbers.

| # | Issue | Status | File |
|---|---|---|---|
| 1 | Hallucinated transcript ("Hi, I am Arijit Singh") | **Fixed, not integrated** | `transcript_guard.py` |
| 2 | Gestures shown active, not visible | **Fixed** | `avatar.html` (done, see §2) |
| 3a | She never speaks first | **Built, not wired** | `initiative.py` (exists since earlier session, never plugged into main.py) |
| 3b | Mouth moves with no speech | **Fixed** | `avatar.html` (done, see §2) |
| 4 | Random false transcriptions | **Same fix as #1** | `transcript_guard.py` |
| 5 | App-launch delay | **Not fixed** — needs real `open_app()` source | — |
| 6 | Spotify "add to queue" just plays | **Fixed, not integrated** | `spotify_queue.py` |
| 7 | Hand clipping through clothes | **Partially mitigated** | `avatar.html` (done, see §2) |

### #1 / #4 — root cause identified with high confidence
`CUSTOM_VOCABULARY = ["Arijit Singh"]` biases Whisper's `initial_prompt`.
Documented Whisper behaviour: an initial_prompt doesn't just aid
recognition, it gives the decoder a strong fallback when fed silence/noise
— producing a hallucinated *sentence* anchored to the prompted name, not
just a misheard word. Exactly explains "Hi, I am Arijit Singh."

`transcript_guard.py` (tested, 6/6 cases pass): two independent gates —
`is_likely_silence(audio, sr)` (energy pre-check, run before Whisper at
all) and `check_segments(segments, custom_vocabulary=[...])` (post-check
using faster-whisper's own `no_speech_prob`/`avg_logprob`, a known-phrase
blocklist, and a specific self-introduction-pattern match against
CUSTOM_VOCABULARY). Integration snippet in the file's own docstring —
needs `transcriber.py` and `audio_recorder.py` to place the two calls.

### #3a — initiative.py wiring (instructions given, NOT confirmed applied)
`initiative.py` already implements rate-limited proactive speech (max
6/hour, 4-min global cooldown, quiet hours 1-8am, goes silent if ignored
twice) plus much-more-frequent non-verbal presence (glances/fidgets every
25-70s, works even when speech is in cooldown). Added a module-level
`initiative.engine` singleton slot this session specifically so
`wake_word.py`/`audio_recorder.py` can check `mic_should_ignore()` without
importing main.py.

**Wiring (best-effort, inferred from traceback fragments):**
```python
# top of main.py
import initiative

# inside run_voice_assistant, after Whisper/Piper/memory load, before
# speaker.speak("Systems online."):
engine = initiative.Initiative(speak=speaker.speak, memory=memory,
                               avatar=avatar_server, llm=llm_brain, use_llm=False)
initiative.engine = engine
engine.start()
shutdown.on_shutdown(engine.stop)

# where "Wake word detected!" logs, before recording starts:
engine.user_turn_started()

# right after transcription produces `text`:
engine.user_turn_ended(text)

# right after speaker.speak(reply) for the actual LLM reply:
engine.assistant_replied(reply)
```
**Not optional** — in `wake_word.py`/`audio_recorder.py`'s frame loop:
```python
import initiative
if initiative.engine and initiative.engine.mic_should_ignore():
    continue
```
Without this, her own proactive speech will wake herself up on speakers
(BARGE_IN_RMS_THRESHOLD=1200 assumes headphones per original handoff).

### #5 — app-launch delay, NOT FIXED
From original handoff: `open_app` tries full-path candidates →
`Get-StartApps` (PowerShell) → `.lnk` scan → PATH → `os.startfile`.
Suspicion: `Get-StartApps` re-invoked fresh per-call rather than cached
once at startup — PowerShell cold-start alone is 300ms-1s+. Needs the
actual function to fix precisely; general guidance given (cache at
startup in a background thread, reorder fast-methods-first, cache
negative lookups too) but not applied to real code.

### #6 — Spotify queue
Root cause: tool list (`play_music`, `play_playlist`, `pause_music`,
`resume_music`, `next_track`, `previous_track`) has **no queue tool at
all**, so "add X to queue" gets routed by the flaky LLM tool-picker to
`play_music`, which plays immediately. `spotify_queue.py` (tested, 7/7
cases correct including near-miss non-matches) adds `queue_song(sp,
query)` using `sp.add_to_queue()` + a deterministic regex matcher,
following the project's own established pattern (handoff §5: deterministic
matching in main.py before the LLM ever sees flaky-tool-pick territory).
Filters null search results per the already-documented Spotify API bug.
Integration snippet in the file's docstring — needs `tools.py` (to add the
function/reuse the existing `sp` client) and `main.py`'s
`match_quick_command()` (to add the matcher call).

---

## 5. Full file inventory this session

All delivered to `/mnt/user-data/outputs/` and presented to user:

| File | Purpose | Status |
|---|---|---|
| `shutdown.py` | Ctrl+C fix via native console handler | Applied to main.py |
| `avatar.html` | Full VRM avatar rewrite | Working except transparency |
| `avatar_server.py` | WebSocket bridge, back-compat aliases | Working |
| `avatar_window.py` | Self-hosted server + pywebview window + chroma-key attempt | Transparency unresolved |
| `initiative.py` | Proactive speech + non-verbal presence engine | Built, not wired |
| `transcript_guard.py` | Whisper hallucination filter | Built, tested, not integrated |
| `spotify_queue.py` | Queue tool + matcher | Built, tested, not integrated |
| `raziel_train.py` | 5-stage local training orchestrator | In use, training in progress |
| `fix_phonemizer_cache.py` | One-off corrupted-download fixer | Used successfully |
| `INTEGRATION.md` | Early integration guide (avatar-focused) | Partially superseded by later inline instructions |

---

## 6. Immediate next actions, in priority order

1. Confirm `raziel_train.py clips` completed clean after the phonemizer
   cache fix; proceed to `features` then `train`
2. **Get main.py, speaker.py, tools.py, transcriber.py, audio_recorder.py**
   — unblocks precise integration of transcript_guard, spotify_queue,
   initiative wiring, and the #5 latency fix, instead of continued
   line-number archaeology from tracebacks
3. Re-test Ctrl+C end to end (fix applied but not re-confirmed since)
4. Resolve or abandon avatar transparency (test `GPU_COMPOSITING=False`
   result; if still opaque, switch to the dark-background fallback rather
   than continue chasing a possible hard OS limit)
5. Once wake word model trained: drop into Voice Agent, set config, tune
   `WAKE_WORD_THRESHOLD`
