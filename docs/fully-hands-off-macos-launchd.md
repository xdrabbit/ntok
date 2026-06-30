# Fully Hands-Off macOS ntok Client using launchd

This sets up the ntok client-daemon to start automatically at login and stay running. No manual scripts needed after initial setup. Perfect for the two additional machines.

## 1. Prepare the files

The repo already contains:
- macos/bin/ntok-client-launchd.sh   (the wrapper)
- macos/launchd/com.user.ntok.client-daemon.plist  (the agent template)

Copy them:

mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cp ~/dev/ntok/macos/bin/ntok-client-launchd.sh ~/dev/ntok/macos/bin/ || true
chmod +x ~/dev/ntok/macos/bin/ntok-client-launchd.sh
cp ~/dev/ntok/macos/launchd/com.user.ntok.client-daemon.plist ~/Library/LaunchAgents/

## 2. Load the agent

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ntok.client-daemon.plist

Check:
launchctl list | grep ntok

## 3. Permissions

Grant Microphone and Accessibility to your terminal / the Python that runs the wrapper.

First run may pop the permission dialogs.

## 4. Shell helper (for CLI commands like ntok client toggle)

Add to ~/.zshrc:

export XDG_RUNTIME_DIR="${TMPDIR%/}/ntok-runtime"
mkdir -p "$XDG_RUNTIME_DIR"

Then you can still use the normal client commands even though the daemon is managed by launchd.

## 5. Logs

~/Library/Logs/ntok-client.log

## Reloading

If you edit the plist:

launchctl bootout gui/$(id -u)/com.user.ntok.client-daemon 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ntok.client-daemon.plist

## For the other two machines

- Put ntok at the same ~/dev/ntok location (or update the plist and wrapper paths).
- Copy the two files.
- Bootstrap.
- Grant the two privacy permissions on that machine.
- Make sure the token in the seat config matches blackbird exactly.

This gives you set-and-forget behavior on all macOS seats.

See also: docs/macOS-seat-client.md for the manual / testing version.
