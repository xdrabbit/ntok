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

  Measured with the final winning local settings (100 ms tick, large-v3-turbo, beam=1, no confirmation):
  feels instantaneous on phrase boundaries. Halluc stripping + silence trim keep output clean.

The acceptance test stubs only the mic and ydotool — the Whisper engine is real.
What it *can't* test is the felt experience: do the manual mic smoke test below.

## Known limitations

- **End-of-utterance hallucination.** Handled well by the combination of short silence threshold, aggressive trim, and per-segment halluc stripping. Rare on normal speech.
- **Latency tracks GPU load.** First-commit lag is ~1 s on a free GPU but
  degrades when the card is saturated by other jobs.
- **macOS seats** are working (tested on Scout MacBook Air against blackbird).
  See `docs/macOS-seat-client.md` and `docs/fully-hands-off-macos-launchd.md` for
  the clean launch (nohup or launchd), permissions, XDG setup, and avfoundation source.

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
   port = 6161
   token = "PASTE-A-SECRET"   # e.g. output of: openssl rand -hex 32
   ```
2. Run it: `ntok server` (or install `systemd/ntok-server.service`; for an
   always-on headless box, `loginctl enable-linger $USER`).

### Seat (each machine — Linux or Mac)

Put the **same** token and the server's address in that machine's config:
```toml
[net]
server_host = "blackbird.local"   # or its LAN IP
server_port = 6161
token = "PASTE-THE-SAME-SECRET"
```
Run the seat daemon and bind a hotkey to the toggle:
```bash
./install-client.sh      # Linux seat: installs ntok-client.service + ydotoold
# macOS seat: see docs/macOS-seat-client.md (start-client.sh or launchd)
ntok client toggle       # bind your OS hotkey to this
ntok client status|stop|cancel
```

- **Linux seat** — uses ydotool injection and parec mic capture. Use
  `install-client.sh` for thin LAN clients; use `install.sh` only on a machine
  that should run a local/server Whisper model.
- **macOS seat** — captures via ffmpeg/avfoundation and injects via AppleScript
  `System Events`. `brew install ffmpeg`; grant Terminal **Microphone** +
  **Accessibility** perms in System Settings; use `[audio] source = ":0"` (or
  list with `ffmpeg -f avfoundation -list_devices true -i ""`). Use the provided
  `start-client.sh` (or launchd plist) for reliable detached daemon. See
  `docs/macOS-seat-client.md` (and fully-hands-off doc). Tested successfully on
  wireless Scout seat → blackbird (often snappier than wired setups with the
  low-latency tunings).
- **Mac local fallback** — the supported default is still thin-client mode using
  blackbird's 3090. A native Apple Metal fallback should be a separate backend
  (for example MLX/Core ML) so normal Mac installs do not pull CUDA or server
  wheels.

The network path itself (auth, framing, streaming, accuracy) is verified
end-to-end on blackbird via `tests/test_net_acceptance.py` (real model over a
loopback socket).

### Current LAN endpoint

Blackbird runs the central transcription server on TCP `6161`. Seat machines
should point `[net].server_host` at `blackbird.local`, the LAN IP, or the
tailnet name if LAN mDNS is not available, and `[net].server_port` at `6161`.

Python packaging is split for this topology:

- `pip install -e .` installs only the thin-client core.
- `pip install -e '.[server]'` installs Faster Whisper plus NVIDIA CUDA wheels
  for the blackbird server.

## Status (as of 2026-06-24)

**Local turbo is now "perfect" for real-time dictation on this hardware.**

Winning config (what finally delivered acceptable pace):
```toml
[stream]
tick_ms = 100
min_silence_ms = 150
require_confirmation = false
model = "large-v3-turbo"

[transcribe]
beam_size = 1
```

- Extremely responsive phrase-by-phrase typing.
- ydotoold backend working cleanly for fast injection.
- Local only (no API latency or cost).

All tests pass: 53 Tier 1 (no-GPU) + 8 Tier 2 acceptance. Live dictation runs
`large-v3-turbo` (lowest latency); the Tier 2 accuracy tests pin the
`distil-large-v3` baseline (WER ≤ 0.15) so they measure accuracy independently
of the seat's latency tuning.

**To use:**
```bash
# ~/.config/ntok/config.toml
# (then)
systemctl --user restart ntokd
ntok toggle
```

## Roadmap

**Done:** Phase 1 (local streaming dictation) and the Phase 2 client/server split
with shared-secret auth and serialized multi-seat GPU access.

**Next:** a local-agreement sliding window for finer-grained commits (also the
structural fix for the end-of-utterance hallucination); a request queue /
fairness across many simultaneous seats; and voice commands.

macOS seats verified on real hardware (Scout + blackbird LAN).

Open items for polish: push-to-talk hotkey adapter, better final hallucination filter, GUI indicator.
