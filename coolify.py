#!/usr/bin/env python3
"""Drive Coolify API. Reads token from the token file (never inline)."""
import json, os, sys, urllib.request, urllib.error

TOK_PATH = "/home/hermes/.hermes/home/.tokens/coolify_token"
BASE = "http://169.58.74.130:8000/api/v1"

def call(path, method="GET", body=None):
    tok = open(TOK_PATH).read().strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"raw": str(e)}
    except Exception as e:
        return 0, str(e)

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "projects":
        st, d = call("/projects")
        print("status", st)
        for p in d if isinstance(d, list) else []:
            print(f"  {p.get('uuid')}  {p.get('name')}")
    elif cmd == "apps":
        st, d = call("/applications")
        print("status", st, "count", len(d) if isinstance(d, list) else d)
        for a in d if isinstance(d, list) else []:
            print(f"  {a.get('uuid')}  {a.get('name')}  {a.get('fqdn')}")
    elif cmd == "servers":
        st, d = call("/servers")
        print("status", st)
        for s in d if isinstance(d, list) else []:
            print(f"  {s.get('uuid')}  {s.get('name')}  {s.get('ip')}")
    elif cmd == "create-project":
        st, d = call("/projects", "POST", {"name": sys.argv[2], "description": sys.argv[3] if len(sys.argv)>3 else ""})
        print("status", st, json.dumps(d))
    else:
        print("usage: coolify.py projects|apps|servers|create-project <name>")
