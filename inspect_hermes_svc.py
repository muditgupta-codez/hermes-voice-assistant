#!/usr/bin/env python3
"""Inspect the hermes-agent service detail: domains, ports, network, env for api_server."""
import json, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

st, d = call("/services/kl7hbed36wlg9vcxudhm8jco")
print("status", st)
print(json.dumps(d, indent=2)[:3000])
