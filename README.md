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
- **macOS seat is unverified on hardware.** The protocol/server/client path is
  tested end-to-end on blackbird, but the macOS mic (ffmpeg/avfoundation) and
  injection (AppleScript) adapters have not been run on a real Mac yet.

### Manual smoke test

Bind `ntok toggle` to a hotkey, open a text editor, toggle on, and dictate a
paragraph — include deliberate mid-sentence pauses and one long run-on sentence.
Watch text land phrase-by-phrase; check the pause/run-on boundaries for dropped
or duplicated words. Toggle off to flush the tail.

## Networked seats (Phase 2)

Serve every seat on the LAN from blackbird's GPU. A thin client on each machine
captures the mic and streams it to a transcription server on blackbird; the
server runs the *same* CommitEngine per connection and streams committed text
back, which the client types locally. Injection is app-agnostic on both ends —
web (Claude in Chrome) and standalone (the Claude desktop app) both just work,
because keystrokes go to whatever window has focus.

```
seat (mic) ──audio──▶ ntok server (blackbird GPU) ──committed text──▶ seat (types locally)
```

### Server (on blackbird)

1. Set a shared secret in `~/.config/ntok/config.toml`:
   ```toml
   [net]
   host = "0.0.0.0"            # serve the LAN (use 127.0.0.1 for local-only)
   port = 8765
   token = "PASTE-A-SECRET"   # e.g. output of: openssl rand -hex 32
   ```
2. Run it: `ntok server` (or install `systemd/ntok-server.service`; for an
   always-on headless box, `loginctl enable-linger $USER`).

### Seat (each machine — Linux or Mac)

Put the **same** token and the server's address in that machine's config:
```toml
[net]
server_host = "blackbird.local"   # or its LAN IP
server_port = 8765
token = "PASTE-THE-SAME-SECRET"
```
Run the seat daemon and bind a hotkey to the toggle:
```bash
ntok client-daemon       # the thin seat (systemd: ntok-client.service on Linux)
ntok client toggle       # bind your OS hotkey to this
ntok client status|stop|cancel
```

- **Linux seat** — reuses ydotool injection and parec mic capture (run
  `install.sh` first for ydotool/uinput).
- **macOS seat** — captures via ffmpeg/avfoundation and injects via AppleScript
  `System Events`. You must: `brew install ffmpeg`; grant your terminal (or
  whatever runs `ntok client-daemon`) **Microphone** and **Accessibility**
  permissions; and set `[audio].source` to your mic's avfoundation index
  (list with `ffmpeg -f avfoundation -list_devices true -i ""`). Bind the hotkey
  to `ntok client toggle` via Shortcuts/Automator/Karabiner. *(These macOS
  adapters are written but not yet hardware-tested — see Known limitations.)*

The network path itself (auth, framing, streaming, accuracy) is verified
end-to-end on blackbird via `tests/test_net_acceptance.py` (real model over a
loopback socket).

## Roadmap

**Done:** Phase 1 (local streaming dictation) and the Phase 2 client/server split
with shared-secret auth and serialized multi-seat GPU access.

**Next:** verify the macOS seat on real hardware; a local-agreement sliding
window for finer-grained commits (also the structural fix for the end-of-
utterance hallucination); a request queue / fairness across many simultaneous
seats; and voice commands.
