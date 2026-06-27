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
        # local: large-v3 (most accurate), large-v3-turbo, distil-large-v3 (fast streaming)
        # For API backend set backend="openai" (uses model.name e.g. "whisper-1")
        "name": "large-v3",
        "device": "cuda",          # "cuda" | "cpu"
        "compute_type": "float16", # float16 on GPU; use int8 on CPU
        "language": "en",          # "" for auto-detect (slower)
        "backend": "faster-whisper",  # "faster-whisper" (local GPU, recommended) | "openai" | "grok" (xAI)
    },
    "audio": {
        "sample_rate": 16000,
        "source": "",              # "" = default mic; else a PipeWire/Pulse source name
        "max_seconds": 300,        # hard cap on a single dictation
    },
    "transcribe": {
        "vad_filter": True,        # Silero VAD trims silence -> faster + cleaner (local only)
        "beam_size": 5,  # beam search for accuracy; GPU has headroom for real-time streaming
        "initial_prompt": "",      # bias vocabulary, e.g. proper nouns you use
        # VAD edge padding (only applied when a VAD filter is active). Raise
        # speech_pad_ms if first/last words get clipped; lower it if leading
        # silence/hallucination creeps in.
        "speech_pad_ms": 400,
        "vad_min_silence_ms": 2000,  # gap (ms) before VAD counts it as a pause
        # Drop near-silent / hallucinated tail segments. Library default 0.6; a
        # segment is suppressed when its no-speech prob exceeds this, so LOWER it
        # (toward 0.4) to drop more phantom trailing words, raise it to keep more.
        "no_speech_threshold": 0.6,
        "openai_model": "whisper-1",  # for backend=openai
        "grok_model": "grok-stt",  # for backend=grok; use grok-stt or latest
    },
    "stream": {
        "tick_ms": 100,            # very frequent checks for snappy phrase commits
        "min_silence_ms": 150,     # short pause after last word → write fast
        "require_confirmation": False,  # no confirmation delay
        "vad_filter": False,
        "silence_rms": 0.01,
        "max_buffer_seconds": 28,
        "model": "large-v3-turbo",  # excellent speed/quality for low-latency streaming
        "continuous_listen": False,
        "final_vad_filter": True,
        # Flow + spoken-punctuation dictation. When true, committed text is NOT
        # auto-capitalized/auto-terminated per fragment; words flow continuously
        # and the speaker dictates punctuation ("period", "comma", "new
        # paragraph", "cap <word>", …). Server-side only; needs no client change.
        # Default off so existing behavior + acceptance baselines are unchanged.
        "spoken_punctuation": False,
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
    "net": {
        # Server bind address. Default to 0.0.0.0 so it's accessible to all machines on the LAN.
        # Set to 127.0.0.1 if you only want local.
        "host": "0.0.0.0",
        "port": 6161,
        "token": "",               # shared secret; REQUIRED — server won't serve empty
        # Client (each seat) — where to reach the server (use blackbird.local or LAN IP).
        "server_host": "127.0.0.1",
        "server_port": 6161,
        "sample_rate": 16000,
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
name = "large-v3-turbo"  # local: large-v3-turbo (fast) | distil-large-v3 | small.en ; api: whisper-1
device = "cuda"          # cuda | cpu
compute_type = "float16" # float16 (gpu) | int8 (cpu) | int8_float16
language = "en"          # "" for auto-detect
backend = "faster-whisper"  # faster-whisper (local GPU, best for low latency on your machine) | openai | grok (xAI)
# If using grok or openai, set the key via env var or [net] section below

[audio]
sample_rate = 16000
source = ""              # "" = default mic. List sources: `pactl list short sources`
max_seconds = 300

[transcribe]
vad_filter = true
beam_size = 5            # beam search for accuracy (GPU has headroom)
initial_prompt = ""      # e.g. "Kubernetes, Postgres, ntok" to bias spelling
speech_pad_ms = 400      # ↑ if first/last words get clipped; ↓ if leading silence creeps in
vad_min_silence_ms = 2000  # gap (ms) before VAD counts it as a pause
no_speech_threshold = 0.6  # ↓ toward 0.4 to drop more phantom trailing words
openai_model = "whisper-1"
grok_model = "grok-stt"

[stream]
tick_ms = 100              # very frequent checks → snappy response
min_silence_ms = 150       # short pause after last word → commit & write fast
require_confirmation = false
vad_filter = false
silence_rms = 0.01
max_buffer_seconds = 28
model = "large-v3-turbo"   # great speed/quality balance for dictation
continuous_listen = false
final_vad_filter = true
spoken_punctuation = false  # flow mode: speak punctuation ("period", "new paragraph", "cap <word>")

[inject]
method = "type"          # type | paste
restore_clipboard = true
key_delay_ms = 4
trailing_space = true
capitalize_first = false

[feedback]
sound = true
notify = true

[net]
# Server. Use host = "0.0.0.0" to serve all machines on the LAN (default now).
# Set token to a shared secret (REQUIRED).
host = "0.0.0.0"
port = 6161
token = ""
# Client (each seat) — point to the server (use hostname or LAN IP) and your mic rate.
server_host = "127.0.0.1"
server_port = 6161
sample_rate = 16000

# API keys for cloud transcription backends (used when [model].backend = "openai" or "grok")
# Preferred way: set as environment variables instead:
#   export XAI_API_KEY=sk-...          # for backend=grok
#   export OPENAI_API_KEY=sk-...       # for backend=openai
xai_key = ""
grok_key = ""
openai_key = ""
"""
