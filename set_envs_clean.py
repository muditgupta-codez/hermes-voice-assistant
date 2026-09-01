#!/usr/bin/env python3
"""Write a clean, single set of env vars for the voice-assistant app.

Reads secrets from vault files/.env (never printed). Sets exactly one of each key.
"""
import sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"

def read(path, prefix=None, strip_quotes=True):
    val = open(path).read().strip()
    if prefix:
        val = val.split(prefix, 1)[1] if prefix in val else ""
    if strip_quotes:
        val = val.strip('"').strip("'")
    return val

groq = read("/home/hermes/.hermes/home/.tokens/groq_api_key")
api_key = ""
for line in open("/home/hermes/.hermes/.env"):
    if line.startswith("API_SERVER_KEY="):
        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")

envs = [
    {"key": "GROQ_API_KEY", "value": groq, "is_literal": True},
    {"key": "API_SERVER_KEY", "value": api_key, "is_literal": True},
    {"key": "API_SERVER_HOST", "value": "hermes-agent-kl7hbed36wlg9vcxudhm8jco", "is_literal": True},
    {"key": "API_SERVER_PORT", "value": "8642", "is_literal": True},
    {"key": "BRAIN_MODEL", "value": "hermes-agent", "is_literal": True},
    {"key": "TTS_VOICE", "value": "en-US-AriaNeural", "is_literal": True},
    {"key": "PORT", "value": "8000", "is_literal": True},
]

st, d = call(f"/applications/{APP}/envs/bulk", "PATCH", {"data": envs})
print("bulk status", st, "(ok)" if st in (200, 201) else "")

# verify: exactly one of each key, no dupes
st2, d2 = call(f"/applications/{APP}/envs")
if isinstance(d2, list):
    counts = {}
    for e in d2:
        counts[e.get("key")] = counts.get(e.get("key"), 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    print("env count:", len(d2))
    print("keys:", sorted(counts.keys()))
    print("DUPLICATES:", dupes if dupes else "none")
