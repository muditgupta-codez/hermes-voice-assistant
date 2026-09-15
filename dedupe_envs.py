#!/usr/bin/env python3
"""Collapse the voice-assistant app's env rows to exactly ONE per key.

The app had carried a full duplicate set of env rows since 2026-09-02 (12 keys x2), so
every deploy had two candidate values per key and "which one wins" was undefined — that
is how a stale TTS_VOICE=en-US-AriaNeural can silently override a code default. This
picks a winner per key (for TTS_VOICE: the Sarvam one) and deletes the losers.
"""
import sys
sys.path.insert(0, "/opt/data/voice-assistant")
from coolify import call

APP = "gfnyhjybqzsakfn3gowdfp5i"
WANT = {"TTS_VOICE": "sarvam:ishita", "TTS_VOICE_HI": "sarvam:ishita"}

st, rows = call(f"/applications/{APP}/envs")
if not isinstance(rows, list):
    sys.exit(f"cannot list envs: {st} {rows}")

groups = {}
for r in rows:
    groups.setdefault(r["key"], []).append(r)

losers, keepers = [], []
for key, rs in groups.items():
    if len(rs) == 1:
        keepers.append(rs[0]); continue
    want = WANT.get(key)
    def rank(r):
        # prefer the row holding the value we actually want; then the most recently touched
        return (1 if (want and r.get("value") == want) else 0,
                str(r.get("updated_at") or ""))
    rs_sorted = sorted(rs, key=rank, reverse=True)
    keepers.append(rs_sorted[0])
    losers.extend(rs_sorted[1:])

print(f"keys={len(groups)} rows={len(rows)} keep={len(keepers)} delete={len(losers)}")
for r in losers:
    ds, d = call(f"/applications/{APP}/envs/{r['uuid']}", "DELETE")
    print(f"  DEL {r['key']:18s} ({r['uuid']}) -> {ds}")

st2, rows2 = call(f"/applications/{APP}/envs")
counts = {}
for r in rows2:
    counts[r["key"]] = counts.get(r["key"], 0) + 1
print("after: rows =", len(rows2), "| duplicates =", {k: v for k, v in counts.items() if v > 1} or "none")
for r in sorted(rows2, key=lambda x: x["key"]):
    v = "<secret>" if "KEY" in r["key"] else r.get("value")
    print(f"  {r['key']:18s} = {v}")
