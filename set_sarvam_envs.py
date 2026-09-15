#!/usr/bin/env python3
"""Set the Sarvam TTS env rows on the voice-assistant app.

The key is read from the voxvaani source and written straight to Coolify — never printed.
Coolify env rows override code defaults, so the TTS_VOICE row that still says
en-US-AriaNeural has to be updated here or nothing changes in production.
"""
import re, pathlib, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"
key = re.search(r"SARVAM_API_KEY\s*\|\|\s*'([^']+)'",
                pathlib.Path("/opt/data/voxvaani-src/backend/src/lib/tts.ts").read_text()).group(1).strip()
assert key.startswith("sk_") and len(key) > 30, "bad sarvam key"
print("sarvam key loaded, len", len(key))

envs = [
    {"key": "SARVAM_API_KEY",    "value": key,             "is_literal": True},
    {"key": "SARVAM_TTS_MODEL",  "value": "bulbul:v3",     "is_literal": True},
    {"key": "SARVAM_SPEAKER",    "value": "ishita",        "is_literal": True},
    {"key": "SARVAM_SAMPLE_RATE","value": "22050",         "is_literal": True},
    {"key": "TTS_VOICE",         "value": "sarvam:ishita", "is_literal": True},
    {"key": "TTS_VOICE_HI",      "value": "sarvam:ishita", "is_literal": True},
]
st, d = call(f"/applications/{APP}/envs/bulk", "PATCH", {"data": envs})
print("bulk PATCH status:", st)

st2, rows = call(f"/applications/{APP}/envs")
if isinstance(rows, list):
    for r in rows:
        k = r.get("key", "")
        if k.startswith(("SARVAM", "TTS")):
            v = "***" if k == "SARVAM_API_KEY" else r.get("value")
            print(f"  {k} = {v}")
    print("total env rows:", len(rows))
    counts = {}
    for r in rows:
        counts[r.get("key")] = counts.get(r.get("key"), 0) + 1
    print("DUPLICATES:", {k: v for k, v in counts.items() if v > 1} or "none")
