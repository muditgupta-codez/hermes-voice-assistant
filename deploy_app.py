#!/usr/bin/env python3
"""Trigger deploy for the voice-assistant app and poll until finished/failed."""
import json, sys, time
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"

st, d = call("/deploy", "POST", {"uuid": APP, "force": True})
print("deploy trigger status", st)
print(json.dumps(d, indent=2) if isinstance(d, (dict, list)) else d)

dep_uuid = None
if isinstance(d, dict) and d.get("deployments"):
    dep_uuid = d["deployments"][0].get("deployment_uuid")
print("deployment_uuid", dep_uuid)

if not dep_uuid:
    sys.exit(1)

for i in range(30):
    time.sleep(8)
    st, dd = call(f"/deployments/{dep_uuid}")
    status = dd.get("status") if isinstance(dd, dict) else None
    print(f"[{i}] status={status}")
    if status in ("finished", "failed"):
        print("FINAL:", status)
        break
    if status == "in_progress" and i % 4 == 3:
        pass  # keep polling
else:
    print("TIMEOUT — still not finished")
