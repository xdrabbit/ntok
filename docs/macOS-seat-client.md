# macOS Seat Client Setup for ntok (LAN)

This documents the working configuration and startup procedure for running ntok as a thin client (seat) on macOS, connecting to the server on blackbird.

Tested successfully on Scout (MacBook Air) — performed *better* than on the wired M4 Mac mini (Ada) once set up correctly.

## Key Points for macOS

- Mic capture uses `ffmpeg -f avfoundation`
- Injection uses AppleScript (`System Events`)
- Control socket for `ntok client status` / `toggle` uses a runtime directory. On macOS this must be handled carefully or commands time out.
- The client-daemon must be started **detached** (nohup + redirect) or it gets suspended by the shell on tty output.

## Prerequisites

```bash
brew install ffmpeg
```

## Permissions (Required)

1. **System Settings → Privacy & Security → Microphone**  
   Add your Terminal (or iTerm/Warp/etc.) and enable it.

2. **System Settings → Privacy & Security → Accessibility**  
   Same app — needed for keystrokes.

Completely quit and reopen Terminal after granting.

## Config (`~/.config/ntok/config.toml`)

```toml
[audio]
sample_rate = 16000
source = ":0"                 # MacBook Air Microphone (confirm with ffmpeg list)
max_seconds = 300

[net]
server_host = "blackbird.local"   # or LAN IP
server_port = 6161
token = "the-exact-same-long-secret-as-on-blackbird"

[stream]
tick_ms = 100
min_silence_ms = 150
require_confirmation = false
model = "large-v3-turbo"      # or distil-large-v3
```

To list devices:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

## Clean Startup (The Working Method)

The root cause of "timed out" and suspended daemons is running `ntok client-daemon` without detaching its output.

**Recommended: use the provided start script**

```bash
cd ~/dev/ntok
./start-client.sh
```

This script:
- Activates the venv
- Sets a consistent `XDG_RUNTIME_DIR`
- Kills any stale daemons safely
- Starts with `nohup` + log redirection (prevents suspension)
- Shows status

### Manual equivalent

```bash
cd ~/dev/ntok
source .venv/bin/activate

export XDG_RUNTIME_DIR=/tmp/ntok-runtime
mkdir -p "$XDG_RUNTIME_DIR"

# Safe kill (avoids self-match issues)
ps aux | grep -v grep | grep 'ntok client-daemon' | awk '{print $2}' | xargs kill 2>/dev/null || true
sleep 0.5
rm -f "$XDG_RUNTIME_DIR/ntok-client.sock" 2>/dev/null || true

nohup ntok client-daemon > /tmp/ntok-client.log 2>&1 &
disown

sleep 2
ntok client status
```

**Add this to your ~/.zshrc** so the socket path is always consistent:

```bash
export XDG_RUNTIME_DIR=/tmp/ntok-runtime
mkdir -p "$XDG_RUNTIME_DIR"
```

Source it or open a new terminal.

## Daily Use

```bash
ntok client status
ntok client toggle      # start listening (bind this to a hotkey)
# speak naturally, pause ~150 ms
ntok client toggle      # stop + flush
```

Monitor what the client is doing:

```bash
tail -f /tmp/ntok-client.log
```

## Troubleshooting

- **"ntok: timed out" on status**  
  Client-daemon not listening on the expected socket.  
  Re-run the start script (it cleans up first).

- **"streaming" but no text appears**  
  - Microphone permission not granted (or granted to the wrong app).  
  - Test directly:  
    ```bash
    ffmpeg -f avfoundation -i ":0" -t 3 -ac 1 -ar 16000 -f wav /tmp/test.wav && afplay /tmp/test.wav
    ```
  - Check the log above.
  - Make sure blackbird's `ntok server` is running with the matching token.

- Daemon keeps getting suspended ("tty output")  
  You launched it without output redirection. Always use the nohup + redirect pattern (the start script does this).

## Why Scout Performed Better Than Ada

Once the client was launched with a stable detached process and consistent runtime directory, the lower overhead on the laptop + the tuned low-latency settings (100 ms tick, 150 ms silence, no confirmation, beam=1, large-v3-turbo) actually gave snappier results than the previous wired setup.

## Files of Interest

- `~/dev/ntok/start-client.sh` — the clean launcher
- `ntok/net/seat.py` — macOS mic + injection adapters
- `ntok/net/client_daemon.py` — the seat daemon (now has Darwin-friendly socket path)
- Server config on blackbird must have matching `token` and listen on the right port.

---

Documented: 2026-06-29 after final successful run on Scout (MacBook Air M-series).
This procedure finally gave reliable, low-latency dictation from the wireless seat.

## Fully Hands-Off Option (launchd)

For the other two machines, use the fully automatic launchd setup documented in
[fully-hands-off-macos-launchd.md](fully-hands-off-macos-launchd.md).

It provides a wrapper + plist so the client-daemon starts at login with no
manual intervention (after the initial one-time bootstrap and permission grants).

