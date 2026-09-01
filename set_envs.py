#!/usr/bin/env python3
"""Set required env vars on the voice-assistant app. Reads secrets from files (never inline)."""
import json, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"

# Read secrets from vault files
groq = open("/home/hermes/.hermes/home/.tokens/groq_api_key").read().strip()
# API_SERVER_KEY from .env
api_key = ""
for line in open("/home/hermes/.hermes/.env"):
    if line.startswith("API_SERVER_KEY="):
        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")

# Env vars. API_SERVER_HOST: use the docker service name 'hermes-agent' (coolify network).
envs = [
    {"key": "GROQ_API_KEY", "value": groq, "is_literal": True},
    {"key": "API_SERVER_KEY", "value": api_key, "is_literal": True},
    {"key": "API_SERVER_HOST", "value": "hermes-agent", "is_literal": True},
    {"key": "API_SERVER_PORT", "value": "8642", "is_literal": True},
    {"key": "BRAIN_MODEL", "value": "hermes-agent", "is_literal": True},
    {"key": "TTS_VOICE", "value": "en-US-AriaNeural", "is_literal": True},
    {"key": "PORT", "value": "8000", "is_literal": True},
]

st, d = call(f"/applications/{APP}/envs/bulk", "PATCH", {"data": envs})
print("status", st)
print(json.dumps(d, indent=2) if isinstance(d, (dict, list)) else d)

# verify
st2, d2 = call(f"/applications/{APP}/envs")
print("\n=== envs now (keys only) ===")
if isinstance(d2, list):
    for e in d2:
        print(f"  {e.get('key')} = {'<set>' if e.get('is_literal') or e.get('value') else '??'}")
