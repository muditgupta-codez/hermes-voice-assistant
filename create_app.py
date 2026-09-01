#!/usr/bin/env python3
"""Create the voice-assistant app on Coolify from the GitHub repo."""
import json, sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

PROJECT = "iwe5xmat9pilwje6hngmuggd"
SERVER = "g41uf4obj3rhvtkypxkka5le"
REPO = "https://github.com/muditgupta-codez/hermes-voice-assistant"

payload = {
    "project_uuid": PROJECT,
    "server_uuid": SERVER,
    "environment_name": "production",
    "git_repository": REPO,
    "git_branch": "main",
    "build_pack": "dockerfile",
    "ports_exposes": "8000",
    "name": "voice-assistant",
    "is_auto_deploy_enabled": True,
}

st, d = call("/applications/public", "POST", payload)
print("status", st)
print(json.dumps(d, indent=2) if isinstance(d, (dict, list)) else d)
