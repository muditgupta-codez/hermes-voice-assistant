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
@app.post("/brain")
async def brain(req: BrainRequest):
    if not BRAIN_KEY:
        raise HTTPException(500, "API_SERVER_KEY not configured")
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty text")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    payload = {
        "model": BRAIN_MODEL,
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.6,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        # Discover the brain's reachable hostname, then make the request.
        host, port, _idx = await find_brain_host()
        url = f"http://{host}:{port}/v1/chat/completions"
        r = await client.post(
            url,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {BRAIN_KEY}"},
            json=payload,
        )
    if r.status_code != 200:
        log.error("brain failed %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"Brain failed: {r.status_code}")
    reply = r.json()["choices"][0]["message"]["content"].strip()
    log.info("BRIAN -> %r", reply)
    return {"reply": reply}


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
