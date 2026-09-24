# Raziel - HANDOFF v3 (supersedes HANDOFF_v2 section 6)

State as of 2026-09-19. All source files in this project are the CURRENT versions
written to `C:\Users\Ayush\Desktop\Voice Agent` (originals backed up in
`_backup_before_handoff_v2\`). config.py here has the Spotify client id/secret redacted.

## What was wrong / what changed

1. **App could not start.** main.py referenced `llm_brain` (NameError) when building the
   initiative engine, inside a thread pywebview swallows - so the voice loop died at
   startup with nothing in assistant.log. Fixed (`llm=brain`) and run_voice_assistant now
   logs any crash with a traceback, survives up to 5 consecutive per-turn failures, and
   requests shutdown on a fatal one.
2. **transcript_guard.py (new, rebuilt from the HANDOFF_v2 description)** - two gates:
   audio gate before Whisper (peak/voiced-time), text gate after (per-segment
   no_speech/logprob/compression, repetition loops, caption phrases, "hi I'm <vocab name>"
   self-intro anchoring, soft fillers only if confident). Sleep phrases are protected.
   All thresholds are `GUARD_*` in config.py; `GUARD_ENABLED = False` bypasses it.
   Rejections log `Guard rejected ...` with metrics - tune from those lines.
3. **spotify_queue.py (new)** + `queue_music` tool + deterministic matcher
   ("add X to the queue", "queue X"). Does not launch Spotify; 404/403 handled.
4. **#5 app-launch latency.** Root cause was `Get-StartApps` (2-3 s PowerShell) being run
   per lookup, not the LLM alone. Now: cached, thread-safe index warmed at startup
   (`warm_up_app_index`), plus a deterministic `try_auto_open_app` matcher ahead of the LLM.
5. **Routing** in main.py is a lazy, priority-ordered tuple `DETERMINISTIC_MATCHERS`
   (favorite-fact, open-site-action, queue-music, quick-command, open-app) then the LLM.
6. **initiative.py wired**: engine created after "Systems online.", hooks on wake / turn
   start / turn end / assistant reply, mic gate in wake_word.py and audio_recorder.py.
   speaker.py serialises utterances (lock) and only signals the avatar once audio is open.
7. **Ctrl+C**: shutdown.py untouched and intact. Missing pieces fixed: every loop
   (main, wake_word, wait_for_voice, record_until_silence) now checks
   `shutdown.is_shutting_down()`; duplicate `webview.destroy` registration removed.
8. **Wake word**: wake_word.py resolves a bare `Raziel.onnx` against the app folder,
   resolves the score key correctly (openWakeWord keys by filename stem), and falls back
   to `WAKE_WORD_FALLBACK = "hey_jarvis"` with a warning if the file is missing.
9. **Avatar transparency (final call)**: `TRANSPARENCY = "auto"` in avatar_window.py.
   Colour-key first; then samples real screen pixels inside the window. Desktop visible ->
   keep and cache "colorkey". Magenta visible -> reload with `?bg=0d1017` (dark tile) and
   cache "none". Inconclusive -> nothing cached, retried next launch.
   Result lives in `avatar_transparency.json` (delete to re-test). Pin with
   `TRANSPARENCY = "colorkey" | "none" | "native"`.
10. Small extras: avatar listening/thinking states now driven from main.py;
    `memory.all_facts()`; initiative's memory line extracts the bare value;
    `.spotify_cache` path anchored to the app folder; "Morning. Systems nominal." ->
    "Systems nominal. Standing by."

## NOT verified on real hardware (tested only with stubs on Linux)

- Avatar transparency probe on Windows/WebView2 (GetPixel on the screen DC).
- wake_word.py against real openwakeword 0.6.0 (`Model(wakeword_models=[...], inference_framework="onnx")`).
- Piper alignment -> viseme timing after the speaker.py reorder.
- The `Get-StartApps` warm-up timing on the actual machine.

## Wake-word training status (task 1) - STILL OPEN

Clips stage finished. Features stage partly done: only 1 of 4 .npy files exists in
`Raziel Training`. No `Raziel.onnx` yet. To finish (in `C:\Users\Ayush\Desktop\Raziel Training`):

    py -3.12 raziel_train.py features
    py -3.12 raziel_train.py train

Then copy `Raziel.onnx` into the Voice Agent folder and in config.py set
`WAKE_WORD_MODEL = "Raziel.onnx"` and a threshold around 0.5. Watch the first run's log:
wake_word.py logs the score key it resolved.

## Next actions

1. Run `py main.py`; check assistant.log for `Guard rejected`, `Deterministic ... matched`,
   `Ambient noise level`, and the avatar probe line. Report anything odd.
2. Finish wake-word training (above).
3. Tune GUARD_* thresholds and the initiative cooldowns from real logs.
4. Optional: flip `use_llm=True` for initiative once Brain gets a `quick_completion`.
