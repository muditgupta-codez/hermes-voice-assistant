#!/usr/bin/env python3
"""Inspect the Hermes gateway service on Coolify to find the api_server public/internal routing."""
import json, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

# list all services
st, d = call("/services")
print("=== services (status", st, ") ===")
if isinstance(d, list):
    for s in d:
        print(f"  {s.get('uuid')}  {s.get('name')}  fqdn={s.get('fqdn')}  status={s.get('status')}")
