"""Configuration loading for ntok.

Reads ~/.config/ntok/config.toml, falling back to sensible defaults tuned for
an NVIDIA GPU. A default config is written on first run so it's easy to tweak.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ntok"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS: dict[str, dict[str, Any]] = {
    "model": {
        # large-v3 is the most accurate; on a 3090 it loads in a few seconds and
        # transcribes a sentence in well under a second. Swap to "distil-large-v3"
        # or "small.en" for lower latency / VRAM.
        "name": "large-v3",
        "device": "cuda",          # "cuda" | "cpu"
        "compute_type": "float16", # float16 on GPU; use int8 on CPU
        "language": "en",          # "" for auto-detect (slower)
    },
    "audio": {
        "sample_rate": 16000,
        "source": "",              # "" = default mic; else a PipeWire/Pulse source name
        "max_seconds": 300,        # hard cap on a single dictation
    },
    "transcribe": {
        "vad_filter": True,        # Silero VAD trims silence -> faster + cleaner
        "beam_size": 5,
        "initial_prompt": "",      # bias vocabulary, e.g. proper nouns you use
    },
    "stream": {
        "tick_ms": 500,            # how often the buffer is re-transcribed
        "min_silence_ms": 500,     # trailing silence that ends a phrase -> commit
        "require_confirmation": True,  # commit a phrase only after 2 ticks agree
        "vad_filter": False,       # keep timestamps stable for buffer-cut math
        "max_buffer_seconds": 28,  # safety net below Whisper's 30 s window
        "model": "",               # optional model override; "" = use [model].name
    },
    "inject": {
        "method": "type",          # "type" (universal) | "paste" (fast, uses clipboard)
        "restore_clipboard": True, # only relevant for paste mode
        "key_delay_ms": 4,         # per-key delay for ydotool type
        "trailing_space": True,    # append a space so you can keep dictating
        "capitalize_first": False, # force-capitalize the first letter
    },
    "feedback": {
        "sound": True,
        "notify": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    user: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            user = tomllib.load(f)
    return _deep_merge(DEFAULTS, user)


def write_default_if_missing() -> bool:
    """Write a commented default config if none exists. Returns True if written."""
    if CONFIG_PATH.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_DEFAULT_TOML)
    return True


_DEFAULT_TOML = """\
# ntok configuration. Restart the daemon after changing model settings:
#   systemctl --user restart ntokd

[model]
name = "large-v3"        # large-v3 | distil-large-v3 | medium | small.en | base.en
device = "cuda"          # cuda | cpu
compute_type = "float16" # float16 (gpu) | int8 (cpu) | int8_float16
language = "en"          # "" for auto-detect

[audio]
sample_rate = 16000
source = ""              # "" = default mic. List sources: `pactl list short sources`
max_seconds = 300

[transcribe]
vad_filter = true
beam_size = 5
initial_prompt = ""      # e.g. "Kubernetes, Postgres, ntok" to bias spelling

[stream]
tick_ms = 500              # how often the rolling buffer is re-transcribed
min_silence_ms = 500       # trailing silence that ends a phrase and commits it
require_confirmation = true # commit a phrase only after two ticks agree (safer)
vad_filter = false         # keep segment timestamps stable for buffer-cut math
max_buffer_seconds = 28    # safety net below Whisper's 30 s attention window
model = ""                 # optional override, e.g. "large-v3-turbo"; "" = [model].name

[inject]
method = "type"          # type | paste
restore_clipboard = true
key_delay_ms = 4
trailing_space = true
capitalize_first = false

[feedback]
sound = true
notify = true
"""
