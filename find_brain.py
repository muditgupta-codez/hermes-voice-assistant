#!/usr/bin/env python3
"""Find the Hermes brain's reachable URL (api_server on this box or via sslip.io)."""
import json, sys, os
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

# The brain is api_server on this box. Test local + sslip candidates.
# Also list the Hermes project's apps to see how the api_server service is routed.
st, d = call("/projects/yclzutihpfh26e8n63q459in")
print("=== Hermes project (project uuid yclzutihpfh26e8n63q459in) ===")
print("status", st)
if isinstance(d, dict):
    for env in d.get("environments", []):
        for app in env.get("applications", []):
            print(f"  app {app.get('uuid')}  {app.get('name')}  fqdn={app.get('fqdn')}  status={app.get('status')}")
        for svc in env.get("services", []):
            print(f"  service {svc.get('uuid')}  {svc.get('name')}  fqdn={svc.get('fqdn')}")
