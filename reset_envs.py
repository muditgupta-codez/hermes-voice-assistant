#!/usr/bin/env python3
"""Reset envs: delete all existing, then create exactly one of each via single POST."""
import sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"

# 1) delete all
st, d = call(f"/applications/{APP}/envs")
if isinstance(d, list):
    for e in d:
        call(f"/applications/{APP}/envs/{e.get('uuid')}", "DELETE")
print("deleted all")

# 2) read secrets
def read(path, lineprefix=None):
    v = open(path).read().strip()
    if lineprefix:
        v = [l.split("=",1)[1] for l in open(path).read().splitlines() if l.startswith(lineprefix)][0]
    return v.strip('"').strip("'")

groq = read("/home/hermes/.hermes/home/.tokens/groq_api_key")
api_key = read("/home/hermes/.hermes/.env", "API_SERVER_KEY=")

envs = [
    {"key": "GROQ_API_KEY", "value": groq},
    {"key": "API_SERVER_KEY", "value": api_key},
    {"key": "API_SERVER_HOST", "value": "hermes-agent-kl7hbed36wlg9vcxudhm8jco"},
    {"key": "API_SERVER_PORT", "value": "8642"},
    {"key": "BRAIN_MODEL", "value": "hermes-agent"},
    {"key": "TTS_VOICE", "value": "en-US-AriaNeural"},
    {"key": "PORT", "value": "8000"},
]

for e in envs:
    st, d = call(f"/applications/{APP}/envs", "POST", {"key": e["key"], "value": e["value"], "is_literal": True})
    print(f"  set {e['key']} -> {st}")

# 3) verify single set
st2, d2 = call(f"/applications/{APP}/envs")
if isinstance(d2, list):
    counts = {}
    for e in d2:
        counts[e.get("key")] = counts.get(e.get("key"), 0) + 1
    print("env count:", len(d2), "| duplicates:", {k:v for k,v in counts.items() if v>1} or "none")
