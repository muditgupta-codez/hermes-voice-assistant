#!/usr/bin/env python3
"""
Hermes Web Voice Assistant — backend.

Proxies STT (Groq Whisper) and TTS (edge-tts), and calls the Hermes brain
(api_server, OpenAI-compatible) for the assistant reply.

The frontend (static/index.html) captures the user's mic with the browser's
getUserMedia + MediaRecorder (clean audio, echo cancellation built in), then:

    mic audio --POST /stt--> text --POST /brain--> reply --GET /tts--> mp3 -> play
"""
import os, io, asyncio, uuid, logging, tempfile
from pathlib import Path

import httpx
import edge_tts
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------- config ----------
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

BRAIN_KEY = os.environ.get("API_SERVER_KEY", "")
BRAIN_HOST = os.environ.get("API_SERVER_HOST", "127.0.0.1")
BRAIN_PORT = os.environ.get("API_SERVER_PORT", "8642")
BRAIN_URL = f"http://{BRAIN_HOST}:{BRAIN_PORT}/v1/chat/completions"
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "hermes-agent")

# Candidate hosts to try when reaching the brain on the docker 'coolify' network.
# Order: explicit env host first (if set to something other than default), then
# known compose/container aliases. The first that yields a 200 from /v1/models wins.
BRAIN_HOST_CANDIDATES = [
    h for h in [BRAIN_HOST,
                "hermes-agent",
                "hermes-webui",
                "hermes-agent-kl7hbed36wlg9vcxudhm8jco",
                "host.docker.internal",
                "172.18.0.1",
                "172.17.0.1",
                "127.0.0.1",
                "169.58.74.130",
                "10.0.3.1"]
    if h
]


async def _probe_brain(host: str, port: str, key: str, timeout: float = 2.0) -> bool:
    """Return True if the brain api_server answers with 200 at host:port."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"http://{host}:{port}/v1/models",
                            headers={"Authorization": f"Bearer {key}"})
            return r.status_code in (200, 401, 403)  # auth reachable = live
    except Exception:
        return False


_brain_host_cache: dict = {}


async def find_brain_host() -> tuple[str, str, int]:
    """Try each candidate in order; return the first working (host, port, index).

    Candidate order: configured BRAIN_HOST first (usually a compose/container
    name), then known aliases on the 'coolify' network. The first to answer on
    /v1/models wins; if none do, fall back to the configured host.

    The discovered host is cached after the first success, so subsequent calls
    skip the probe walk entirely (the walk costs ~2s per dead candidate and was
    the cause of multi-second 'thinking' stalls once the port is published).
    """
    if _brain_host_cache.get("host"):
        return _brain_host_cache["host"], _brain_host_cache["port"], _brain_host_cache["idx"]
    for i, h in enumerate(BRAIN_HOST_CANDIDATES):
        if await _probe_brain(h, BRAIN_PORT, BRAIN_KEY):
            _brain_host_cache.update(host=h, port=BRAIN_PORT, idx=i)
            return h, BRAIN_PORT, i
    return BRAIN_HOST, BRAIN_PORT, -1

TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")

# System prompt / persona for the assistant.
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are Mudit's personal Hermes voice assistant, speaking out loud. "
    "Keep replies short, conversational, and natural — 1-3 sentences. "
    "Do not use markdown, bullets, or emojis. Answer as if speaking. "
    "Always respond in English, even if the user speaks another language.",
)

DEFAULT_USER = os.environ.get("DEFAULT_USER", "there")

app = FastAPI(title="Hermes Web Voice Assistant")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceroom")

STATIC_DIR = Path(__file__).parent / "static"
TMP_DIR = Path(tempfile.gettempdir()) / "voice-room"
TMP_DIR.mkdir(exist_ok=True)
audio_files = TMP_DIR / "audio"
audio_files.mkdir(exist_ok=True)


# ---------- models ----------
class BrainRequest(BaseModel):
    text: str
    session_id: str | None = None


# ---------- STT ----------
@app.post("/stt")
async def stt(file: UploadFile = File(...)):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty audio")

    # webm/ogg/mp4 from MediaRecorder — Groq accepts these (language pinned to English).
    ext = Path(file.filename or "audio.webm").suffix or ".webm"
    async with httpx.AsyncClient(timeout=60) as client:
        files = {"file": (f"audio{ext}", io.BytesIO(data))}
        data_ = {"model": GROQ_MODEL, "temperature": "0", "language": "en"}
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files=files,
            data=data_,
        )
    if r.status_code != 200:
        log.error("groq stt failed %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"STT failed: {r.status_code}")
    text = r.json().get("text", "").strip()
    log.info("STT -> %r", text)
    return {"text": text}


# ---------- BRAIN ----------
# Uses the NATIVE Hermes agent session endpoint (/v1/runs), NOT the stateless
# /v1/chat/completions. This gives real conversation memory (same session_id
# loads prior turns) plus the full Hermes persona/tools — "Hermes native".
def _runs_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1/runs"


@app.post("/brain")
async def brain(req: BrainRequest):
    if not BRAIN_KEY:
        raise HTTPException(500, "API_SERVER_KEY not configured")
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty text")

    # session_id (client-generated, persisted in localStorage) provides memory.
    # Omitting it starts a fresh agent session each turn — that's the old bug.
    session_id = (req.session_id or "").strip() or None

    payload = {
        "input": text,
        "model": BRAIN_MODEL,
        # instructions guide tone/concision without replacing the native persona.
        "instructions": SYSTEM_PROMPT,
    }
    if session_id:
        payload["session_id"] = session_id

    async with httpx.AsyncClient(timeout=120) as client:
        host, port, _idx = await find_brain_host()
        url = _runs_url(host, port)

        # Start the run (returns run_id immediately).
        r = await client.post(
            url,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {BRAIN_KEY}"},
            json=payload,
        )
        if r.status_code != 200 and r.status_code != 202:
            log.error("brain start failed %s: %s", r.status_code, r.text[:300])
            raise HTTPException(502, f"Brain start failed: {r.status_code}")
        run_id = r.json().get("run_id")

        # Poll GET /v1/runs/{run_id} until the run settles.
        status_url = url + "/" + run_id
        for _ in range(60):
            await asyncio.sleep(2)
            pr = await client.get(status_url, headers={"Authorization": f"Bearer {BRAIN_KEY}"})
            if pr.status_code == 404:
                # transient: run not yet registered under a fresh host; retry
                continue
            if pr.status_code != 200:
                continue
            st = pr.json()
            status = st.get("status")
            if status in ("completed", "failed", "interrupted", "cancelled"):
                if status != "completed":
                    raise HTTPException(502, f"Brain run {status}: {st.get('error','')}")
                reply = (st.get("output") or "").strip()
                if not reply:
                    raise HTTPException(502, "Brain returned empty reply")
                log.info("BRAIN -> %r", reply)
                return {"reply": reply, "run_id": run_id, "session_id": session_id}
            if status == "queued" or status == "running":
                continue
        raise HTTPException(504, "Brain run timed out")


# ---------- TTS ----------
@app.post("/tts")
async def tts(req: BrainRequest):
    """req.text holds the reply text; returns audio/mp3."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty text")
    mp3_path = TMP_DIR / f"tts_{uuid.uuid4().hex}.mp3"

    async def _do():
        c = edge_tts.Communicate(text, voice=TTS_VOICE)
        await c.save(str(mp3_path))

    try:
        await _do()
    except Exception as e:
        log.error("tts failed: %s", e)
        raise HTTPException(502, f"TTS failed: {e}")
    data = mp3_path.read_bytes()
    mp3_path.unlink(missing_ok=True)
    return Response(content=data, media_type="audio/mpeg")


# ---------- SESSION MANAGEMENT (proxy to the Hermes api_server) ----------
# The browser cannot reach the brain host directly (the auth key must stay on the
# server, and the brain may be on an internal docker network). So the frontend
# calls these same-origin routes and app.py proxies to the api_server's session
# REST API. Verified live against the brain: GET /api/sessions (list), GET
# /api/sessions/{id}/messages (history), DELETE /api/sessions/{id} (delete).
#
# We surface only source == 'api_server' sessions — those are the ones the web
# voice app itself creates via /v1/runs with a client-side session_id (v_<uuid>).
# Other sources (whatsapp/discord/cron) are the gateway's own conversations and
# don't belong in this app's picker.

async def _session_api_base() -> tuple[str, str]:
    """Return (api_base_url, key) for the api_server session REST API."""
    host, port, _idx = await find_brain_host()
    return f"http://{host}:{port}/api", BRAIN_KEY


@app.get("/api/sessions")
async def api_sessions(limit: int = 100, offset: int = 0):
    base, key = await _session_api_base()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{base}/sessions",
                params={"limit": 1000, "offset": 0},
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception as e:
        raise HTTPException(502, f"Brain unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:300])
    rows = [s for s in r.json().get("data", []) if s.get("source") == "api_server"]
    rows.sort(
        key=lambda s: s.get("last_active") or s.get("started_at") or "",
        reverse=True,
    )
    filtered = rows[offset : offset + limit]
    return {
        "object": "list",
        "data": filtered,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < len(rows),
    }


@app.get("/api/sessions/{sid}/messages")
async def api_session_messages(sid: str, limit: int = 300):
    base, key = await _session_api_base()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{base}/sessions/{sid}/messages",
                params={"limit": limit},
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception as e:
        raise HTTPException(502, f"Brain unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


@app.delete("/api/sessions/{sid}")
async def api_session_delete(sid: str):
    base, key = await _session_api_base()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.delete(
                f"{base}/sessions/{sid}",
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception as e:
        raise HTTPException(502, f"Brain unreachable: {e}")
    if r.status_code not in (200, 204, 404):
        raise HTTPException(r.status_code, r.text[:300])
    return {"deleted": True, "id": sid}


# ---------- health ----------
@app.get("/health")
async def health():
    probe = None
    brain_ok = False
    try:
        host, port, idx = await find_brain_host()
        async with httpx.AsyncClient(timeout=5) as c:
            rr = await c.get(f"http://{host}:{port}/v1/models",
                             headers={"Authorization": f"Bearer {BRAIN_KEY}"})
            brain_ok = rr.status_code == 200
        probe = {"host": host, "port": port, "code": rr.status_code, "idx": idx}
    except Exception as e:
        probe = {"host": BRAIN_HOST, "port": BRAIN_PORT, "err": str(e)[:120]}
    return {"ok": True, "stt": bool(GROQ_KEY), "brain": brain_ok,
            "tts": bool(TTS_VOICE), "brain_url": BRAIN_URL, "probe": probe}


# ---------- static ----------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
