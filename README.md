# ntok — streaming local dictation for Linux/Wayland

"Voice In, only better": real-time, hands-free dictation that types into any
window, runs entirely on your own GPU, and never sends your voice anywhere.

You bind a hotkey to `ntok toggle`, start talking, and text appears
**phrase-by-phrase as you speak**. Toggle again to stop. A warm daemon keeps
the Whisper model resident in VRAM so there's no per-dictation load cost.

## How it works

```
mic ──▶ Recorder.drain() ──▶ StreamingSession ──▶ CommitEngine ──▶ ydotool types
                                  │                     │
                          rolling buffer of      decides what is
                          un-committed audio     stable enough to commit
```

Every `tick_ms` the session drains new audio, transcribes the rolling buffer
with faster-whisper, and asks the commit engine what is stable. Committed text
is typed and its audio is dropped from the buffer, so we never re-transcribe
committed speech or approach Whisper's 30-second window.

### Commit-only streaming (the one honest constraint)

OS-level injection (`ydotool`/uinput) **cannot un-type**. Unlike a browser
extension that owns a text box and can repaint interim words, ntok must emit
only text it will never revise. So the engine is *commit-only*: output only ever
grows, never retracts. Three guards keep commits trustworthy:

1. **Never commit the in-progress tail** — the last segment is still growing, so
   it's held until trailing silence ends the phrase (or you stop).
2. **Confirmation-delayed commit** — a phrase is committed only after two
   consecutive transcriptions agree on it. Costs one tick of latency; buys the
   guarantee that we never type a word the next tick would have corrected.
3. **Seam de-duplication** — strips any word Whisper re-emits at the cut where
   committed audio was dropped.

This forgoes live revisable words, but delivers continuous flow and is robust.

## Install

```bash
./install.sh          # creates .venv, installs ntok + CUDA libs, sets up systemd
```

First run downloads the streaming model weights from Hugging Face
(distil-large-v3 ≈ 1.5 GB). Then bind a desktop keyboard shortcut to:

```bash
ntok toggle
```

## Commands

```
ntok toggle    # start, or stop+flush (bind this to a hotkey)
ntok start
ntok stop
ntok cancel    # discard the current dictation, type nothing
ntok status    # idle | loading | streaming N commits / N words | finalizing
ntok daemon    # run the daemon in the foreground (systemd uses this)
```

## Configuration

`~/.config/ntok/config.toml` is written on first run. Key streaming knobs:

```toml
[stream]
tick_ms = 500              # how often the buffer is re-transcribed
min_silence_ms = 500       # trailing silence that ends a phrase and commits it
require_confirmation = true # commit only after two ticks agree (recommended)
vad_filter = false         # keep segment timestamps stable for buffer-cut math
silence_rms = 0.01         # tail RMS below this counts as a phrase-ending pause
max_buffer_seconds = 28    # safety net below Whisper's 30 s window
model = "distil-large-v3"  # streaming model (fast, low VRAM); "" = use [model].name
```

`distil-large-v3` is the default streaming model: near-large-v3 accuracy, ~12×
faster, and it fits in ~2 GB of VRAM so it coexists with other GPU jobs. For
maximum accuracy on an idle GPU, set `model = "large-v3"` (needs ~4 GB free).

## Testing

Two tiers (see `tests/`):

- **Tier 1 — pure logic, no GPU, milliseconds.** The commit engine, text utils,
  session plumbing, and daemon state machine. This is the fast safety net.

  ```bash
  ./.venv/bin/pytest            # runs Tier 1 (acceptance is excluded by default)
  ```

- **Tier 2 — end-to-end acceptance, real model on the GPU.** Streams a
  public-domain speech clip through the real engine and asserts it streams
  (≥2 commits), stays monotonic (commit-only, no seam duplication), is accurate
  (WER ≤ 0.15), and commits with low lag.

  ```bash
  ./.venv/bin/pytest -m acceptance            # uses the [stream] model (distil-large-v3)
  NTOK_TEST_MODEL=small.en ./.venv/bin/pytest -m acceptance   # fast iteration
  ```

  Measured on an (uncontended) RTX 3090 with distil-large-v3: WER **0.089**,
  first-commit lag **0.88 s**, median tick compute **0.12 s**, 4 commits over a
  ~21 s clip. (Under a fully saturated GPU, tick compute rises to ~1.4 s and
  first-commit lag drifts toward the 3 s bound — a real shared-GPU caveat.)

The acceptance test stubs only the mic and ydotool — the Whisper engine is real.
What it *can't* test is the felt experience: do the manual mic smoke test below.

## Known limitations

- **End-of-utterance hallucination.** Whisper occasionally appends a stock
  closing ("Thank you.") to the final-flush buffer. The trailing-silence trim
  mitigates the common case (you pause before toggling off); the structural fix
  is the Phase 2 local-agreement upgrade, since the final flush is the one path
  that commits without two-tick confirmation. Watch for it in the smoke test.
- **Latency tracks GPU load.** First-commit lag is ~1 s on a free GPU but
  degrades when the card is saturated by other jobs.

### Manual smoke test

Bind `ntok toggle` to a hotkey, open a text editor, toggle on, and dictate a
paragraph — include deliberate mid-sentence pauses and one long run-on sentence.
Watch text land phrase-by-phrase; check the pause/run-on boundaries for dropped
or duplicated words. Toggle off to flush the tail.

## Roadmap

**Phase 1 (this):** streaming, commit-only dictation on a single machine.

**Phase 2:** serve every seat on the LAN from the GPU box — a thin per-seat
client streams mic audio to a transcription server on blackbird and injects
locally. The audio source and inject sink are already injected dependencies, so
this is additive. Also planned: shared-secret auth, a request queue for
concurrent seats, a local-agreement sliding window for finer-grained commits,
and voice commands.
