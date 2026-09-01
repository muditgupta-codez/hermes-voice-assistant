#!/usr/bin/env python3
"""Fetch + parse deployment logs (handles char-array and plain-string forms)."""
import json, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

DEP = sys.argv[1] if len(sys.argv) > 1 else "v0d1khgufvqammybt0sqtbny"

st, d = call(f"/deployments/{DEP}")
print("status", st)
logs = d.get("logs") if isinstance(d, dict) else None
print("logs type:", type(logs).__name__)

if isinstance(logs, str):
    try: logs = json.loads(logs)
    except Exception: pass
elif isinstance(logs, list) and logs and isinstance(logs[0], str):
    try: logs = json.loads("".join(logs))
    except Exception: pass

total = len(logs) if isinstance(logs, list) else 0
print("log entries:", total)

if isinstance(logs, list):
    entries = [e for e in logs if isinstance(e, dict)]
    # print non-hidden (visible) first, then stderr
    for e in entries:
        if not e.get("hidden"):
            print(f"[{e.get('type')}] {e.get('output','')[:300]}".strip())
    print("=== stderr/hidden tail ===")
    for e in entries[-12:]:
        if e.get("type") == "stderr":
            print(f"  (stderr) {e.get('output','')[:400]}")
