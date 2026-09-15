#!/usr/bin/env python3
"""
Hermes Web Voice Assistant — backend.

Proxies STT (Groq Whisper) and TTS (edge-tts), and calls the Hermes brain
(api_server, OpenAI-compatible) for the assistant reply.

The frontend (static/index.html) captures the user's mic with the browser's
getUserMedia + MediaRecorder (clean audio, echo cancellation built in), then:

    mic audio --POST /stt--> text --POST /brain--> reply --GET /tts--> mp3 -> play
"""
import os, io, re, json, time, base64, asyncio, uuid, logging, tempfile
from pathlib import Path

import httpx
import edge_tts
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import Response, FileResponse, StreamingResponse
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
# How long /brain will hold the HTTP wait open for a run to settle. Tool runs that
# build, browse, or dispatch real work routinely outlive two minutes; the old cap was
# 60 polls x 2s = 120s, so a long run surfaced in the panel as "ERROR: Brain 504"
# while the agent was still working. The panel no longer depends on this wait (the
# run.settled event carries the reply, and /brain/{run_id} is pollable), so this is
# only a backstop for a genuinely stuck run.
BRAIN_RUN_TIMEOUT_S = float(os.environ.get("BRAIN_RUN_TIMEOUT_S", "1800"))
BRAIN_POLL_S = float(os.environ.get("BRAIN_POLL_S", "1.5"))

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

# Sarvam AI (bulbul:v3) is the natural Indian voice. edge-tts has exactly two Hindi
# voices (Swara, Madhur) and narrates them flat; worse, Mudit's replies are Hinglish —
# Devanagari and Latin words inside one sentence — and a single-edge-voice read of that
# is exactly what sounds robotic. Sarvam reads code-mixed text properly.
# Voice naming: "sarvam:<speaker>" routes to Sarvam, anything else stays on edge-tts.
SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")
SARVAM_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_SPEAKER = os.environ.get("SARVAM_SPEAKER", "ishita")
# 22050 Hz — NOT 8000. Telephone-rate audio is itself a large part of "sounds synthetic".
SARVAM_RATE = int(os.environ.get("SARVAM_SAMPLE_RATE", "22050"))

TTS_VOICE = (os.environ.get("TTS_VOICE")
             or (f"sarvam:{SARVAM_SPEAKER}" if SARVAM_KEY else "en-US-AriaNeural"))
TTS_VOICE_HI = os.environ.get("TTS_VOICE_HI") or TTS_VOICE
# Used only when a Sarvam call fails and we fall back to edge-tts mid-reply.
EDGE_FALLBACK = "hi-IN-SwaraNeural"
EDGE_FALLBACK_EN = "en-US-AriaNeural"

# Whisper auto-detects the spoken language only when we DON'T pin one. Pinning
# "en" is what mangled Hindi into English-sounding nonsense, so the default is now
# auto-detect; STT_LANGUAGE (env) or a per-request `language` form field can pin it.
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def pick_tts_voice(text: str, asked: str | None = None) -> str:
    """Voice for this reply: explicit request > Devanagari in the text > English default."""
    if asked and asked.strip().lower() not in ("", "auto", "default"):
        return asked.strip()
    return TTS_VOICE_HI if _DEVANAGARI.search(text or "") else TTS_VOICE


def sarvam_speaker_of(voice: str) -> str | None:
    """"sarvam:ishita" -> "ishita". None when this is an edge-tts voice name."""
    if (voice or "").lower().startswith("sarvam:"):
        return voice.split(":", 1)[1].strip() or SARVAM_SPEAKER
    return None


def is_real_edge_voice(voice: str) -> bool:
    """edge-tts voices are '<lang>-<REGION>-<Name>Neural'; 'sarvam:x' is not one."""
    return bool(voice) and not sarvam_speaker_of(voice) and voice.endswith("Neural")


# Hindi first-person verbs are gendered — "मैं सुन रहा हूँ" vs "मैं सुन रही हूँ" — and the
# assistant must agree with the voice it is speaking through. A female voice reading a
# masculine line is audible and wrong.
#
# These tables are MEASURED, not inferred from names: every bulbul:v3 speaker was
# synthesized on three different lines and its median F0 taken with two independent
# estimators (autocorrelation + harmonic product spectrum). Adult female speech centres
# ~180-270 Hz, adult male ~90-165 Hz. The two speakers landing in the 165-180 Hz overlap
# (neha, rohan) were resolved from Sarvam's published gender list instead of the number.
# Note `dev` measures female (≈205 Hz) despite the name — the audio is the authority.
SARVAM_FEMALE = {"ishita", "priya", "suhani", "neha", "roopa", "ritu", "pooja",
                 "kavya", "shreya", "shruti", "simran", "tanya", "kavitha", "rupali", "dev"}
SARVAM_MALE = {"shubh", "ratan", "ashutosh", "rehan", "rohan", "mani", "varun",
               "aditya", "rahul", "amit", "manan", "sumit", "kabir", "aayan", "advait",
               "anand", "tarun", "sunny", "gokul", "vijay", "mohit", "soham"}
# edge-tts publishes Gender per voice; read from list_voices(), not from the name.
EDGE_FEMALE = {"en-US-AriaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural",
               "en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural", "hi-IN-SwaraNeural"}
EDGE_MALE = {"en-US-GuyNeural", "en-GB-RyanNeural", "en-IN-PrabhatNeural",
             "hi-IN-MadhurNeural"}

# Lookup is done on the lowercased voice name (gender_of lowercases first), so both sets
# have to be lowercased too — edge names are mixed case ('en-US-AriaNeural').
FEMALE_VOICES = {f"sarvam:{s}" for s in SARVAM_FEMALE} | {v.lower() for v in EDGE_FEMALE}
MALE_VOICES = {f"sarvam:{s}" for s in SARVAM_MALE} | {v.lower() for v in EDGE_MALE}

FEMININE_CLAUSE = (
    " You are speaking aloud through a FEMALE voice, so use FEMININE first-person Hindi "
    "grammar: सुन रही हूँ, बताती हूँ, कर सकती हूँ, मैं आ गई। Never masculine forms like "
    "सुन रहा हूँ / कर सकता हूँ — the voice is female and the mismatch is audible. "
    "English needs no change."
)
MASCULINE_CLAUSE = (
    " You are speaking aloud through a MALE voice, so use MASCULINE first-person Hindi "
    "grammar: सुन रहा हूँ, बताता हूँ, कर सकता हूँ। Never feminine forms like सुन रही हूँ / "
    "कर सकती हूँ. English needs no change."
)


def gender_of(voice: str | None) -> str:
    """'f' | 'm' | '' — '' when the voice is unrecognised, so we add no clause at all."""
    v = (voice or "").strip().lower()
    if v in FEMALE_VOICES:
        return "f"
    if v in MALE_VOICES:
        return "m"
    return ""

# System prompt / persona for the assistant.
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are Mudit's personal Hermes voice assistant, speaking out loud. "
    "Keep replies short, conversational, and natural — 1-3 sentences. "
    "Do not use markdown, bullets, or emojis. Answer as if speaking. "
    "Reply in the language the user spoke to you in: Hindi in, Hindi out; English in, "
    "English out. Match their mix if they mix the two, and write Hindi in Devanagari.",
)

DEFAULT_USER = os.environ.get("DEFAULT_USER", "there")


def persona_for(voice: str | None) -> str:
    """The system prompt plus the grammatical-gender clause for the voice speaking it."""
    g = gender_of(voice)
    if g == "f":
        return SYSTEM_PROMPT + FEMININE_CLAUSE
    if g == "m":
        return SYSTEM_PROMPT + MASCULINE_CLAUSE
    return SYSTEM_PROMPT

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
    voice: str | None = None      # /tts only: empty/"auto" = pick by the reply's script


# ---------- STT ----------
# Upstream (Groq) status -> what the panel should do. The old code mapped EVERY non-200 to
# HTTPException(502) and the client rendered the status verbatim, so a throttle (429), an
# undecodable blob (400 — a truncated/odd container from the browser recorder) and a Groq
# hiccup (5xx) ALL surfaced as the same scary "ERROR: STT 502". Each now gets the treatment
# it deserves:
#   429 -> 429 + Retry-After    (throttle: client waits, re-sends the SAME audio)
#   400 -> 200 {"text": ""}     (nothing to decode = "didn't catch that", not an error)
#   5xx -> retry once, then 503 + Retry-After (client retries, then stays quiet)
# Outcome counters + the last non-routine error ride along in /health so a failure can be
# attributed from outside the container (Coolify only keeps the current container's log).
STT_DIAG = {"counts": {}, "last_error": None, "last_blob": None}


def _sniff_container(data):
    """Name the container from its magic bytes. A blob Groq refuses can then be identified
    from /health alone — Coolify only keeps the running container's log, so a failure that
    scrolled past, or happened before a deploy, is otherwise unattributable."""
    h = data[:16]
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "webm/matroska"
    if h[4:8] == b"ftyp":
        return "mp4/mov"
    if h[:4] == b"OggS":
        return "ogg"
    if h[:4] == b"RIFF":
        return "wav"
    if h[:3] == b"ID3":
        return "mp3"
    return "unknown"


def _stt_note(outcome, **extra):
    STT_DIAG["counts"][outcome] = STT_DIAG["counts"].get(outcome, 0) + 1
    if outcome not in ("ok", "throttled", "silent"):
        STT_DIAG["last_error"] = {"when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "outcome": outcome, **extra}


@app.post("/stt")
async def stt(file: UploadFile = File(...), language: str | None = Form(None)):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty audio")

    # What actually arrived, for every single utterance (not just failures).
    STT_DIAG["last_blob"] = {"when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "container": _sniff_container(data), "head": data[:12].hex(),
                             "bytes": len(data), "name": file.filename or ""}

    # webm/ogg/mp4 from MediaRecorder — Groq accepts these. Language is auto-detected
    # unless STT_LANGUAGE / the `language` form field pins it (auto = Hindi works too).
    form = {"model": GROQ_MODEL, "temperature": "0"}
    _lang = (language or STT_LANGUAGE or "").strip()
    if _lang:
        form["language"] = _lang
    ext = Path(file.filename or "audio.webm").suffix or ".webm"
    last = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                last = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    files={"file": (f"audio{ext}", io.BytesIO(data))},
                    data=form,
                )
        except Exception as e:                      # network hiccup / timeout
            last = None
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            log.error("groq stt unreachable: %s", str(e)[:200])
            _stt_note("network_error", detail=str(e)[:200], bytes=len(data))
            raise HTTPException(503, "STT upstream unreachable", headers={"Retry-After": "2"})

        if 200 <= last.status_code < 300:
            break
        if last.status_code == 429:
            ra = str(last.headers.get("retry-after") or "3")
            log.warning("groq stt rate limited (retry-after=%ss)", ra)
            _stt_note("throttled")
            raise HTTPException(429, "STT rate limited", headers={"Retry-After": ra})
        if last.status_code == 400:
            # Groq could not decode the blob at all — a truncated/odd container from the
            # recorder, not a user-facing failure. Behave exactly like silence.
            log.warning("groq stt undecodable (400): %s", last.text[:200])
            _stt_note("undecodable", detail=last.text[:200], bytes=len(data),
                      container=_sniff_container(data))
            return {"text": "", "reason": "undecodable", "bytes": len(data)}
        if attempt == 0:                            # transient upstream blip: one retry
            log.warning("groq stt %s — retrying once", last.status_code)
            await asyncio.sleep(0.4)
            continue
        break

    if last is None or not (200 <= last.status_code < 300):
        code = getattr(last, "status_code", "n/a")
        body = last.text[:300] if last is not None else "no response"
        log.error("groq stt failed %s: %s", code, body)
        _stt_note("upstream_%s" % code, detail=body, bytes=len(data))
        raise HTTPException(503, f"STT upstream error {code}", headers={"Retry-After": "2"})

    text = last.json().get("text", "").strip()
    log.info("STT -> %r", text)
    _stt_note("ok")
    return {"text": text}


# ---------- BRAIN ----------
# Uses the NATIVE Hermes agent session endpoint (/v1/runs), NOT the stateless
# /v1/chat/completions. This gives real conversation memory (same session_id
# loads prior turns) plus the full Hermes persona/tools — "Hermes native".
def _runs_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1/runs"


# ---------- LIVE STREAM (tool calls as they happen) ----------
# Tool calls used to reach the panel only when the turn finished, attached to the
# reply bubble as a collapsed chip. Now every tool event is pushed to the browser
# the moment it happens: the run's event stream is relayed to any connected
# browser over Server-Sent Events (GET /events), so the panel can paint
# "tool started" the instant the agent picks up a tool.
# One asyncio queue per connected browser; a slow/dead client is skipped by the
# bounded queue rather than ever blocking the agent run.
_STREAM_SUBS: "set[asyncio.Queue]" = set()


def publish(evt: dict) -> None:
    """Fan a live event out to every connected panel (never blocks, never raises)."""
    evt = dict(evt)
    evt.setdefault("ts", time.time())
    for q in list(_STREAM_SUBS):
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass


@app.get("/events")
async def events():
    """SSE feed of live agent activity: run.started, tool.started, tool.completed, run.settled."""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _STREAM_SUBS.add(q)

    async def gen():
        try:
            yield ": stream open\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"          # keep-alive through proxies
                    continue
                yield "data: " + json.dumps(evt) + "\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            _STREAM_SUBS.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- TOOL-CALL LOG ----------
# The panel shows which tools Hermes used for each turn. The api_server streams
# structured agent lifecycle events on GET /v1/runs/{run_id}/events
# (tool.started carries the tool name + an args preview, tool.completed carries
# duration/error). That stream only exists while the run lives — it is closed and
# dropped the moment the run settles — so we subscribe immediately after starting
# the run and buffer the rows while we poll for completion. Fetching events after
# the run finished returns nothing.
def _args_preview(raw: str) -> str:
    """Compress a tool's JSON arguments into one short human-readable line."""
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return (raw or "")[:160]
    if not isinstance(d, dict):
        return str(d)[:160]
    for k in ("command", "cmd", "query", "url", "path", "file_path", "prompt", "text", "name"):
        if k in d and isinstance(d[k], (str, int, float)):
            return f"{k}: {str(d[k])}"[:160]
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(d.items())[:4])[:160]


async def _collect_tool_events(host: str, port: str, run_id: str, out: list) -> None:
    """Stream a run's lifecycle events; append one row per tool call to `out`."""
    url = f"http://{host}:{port}/v1/runs/{run_id}/events"
    headers = {"Authorization": f"Bearer {BRAIN_KEY}"}
    open_rows: dict[str, list] = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    log.warning("tool-event stream HTTP %s for run %s", r.status_code, run_id)
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    kind = ev.get("event")
                    if kind == "tool.started":
                        n = len(out) + 1
                        row = {"id": f"{run_id}-{n}",
                               "tool": ev.get("tool") or "tool",
                               "args": (ev.get("preview") or "").strip()[:200],
                               "duration": None, "error": False}
                        out.append(row)
                        open_rows.setdefault(row["tool"], []).append(row)
                        # paint it in the panel NOW, not when the turn ends
                        publish({"type": "tool.started", "id": row["id"], "run_id": run_id,
                                 "tool": row["tool"], "args": row["args"]})
                    elif kind == "tool.completed":
                        name = ev.get("tool") or ""
                        pending = [x for x in open_rows.get(name, []) if x["duration"] is None]
                        if pending:
                            pending[0]["duration"] = ev.get("duration")
                            pending[0]["error"] = bool(ev.get("error"))
                            publish({"type": "tool.completed", "id": pending[0].get("id"),
                                     "run_id": run_id, "tool": name,
                                     "duration": ev.get("duration"),
                                     "error": bool(ev.get("error"))})
                    elif kind in ("run.completed", "run.failed", "run.cancelled"):
                        break
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as e:
        log.info("tool-event stream ended for run %s: %s", run_id, e)


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
        # The voice is resolved with the SAME function /tts uses, so the reply's Hindi
        # grammar agrees with the voice that will read it (सुन रही हूँ for a female voice).
        "instructions": persona_for(pick_tts_voice(text, req.voice)),
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
        publish({"type": "run.started", "run_id": run_id,
                 "session_id": session_id, "input": text[:120]})

        # Subscribe to the run's event stream NOW (it is dropped when the run
        # settles) so the panel can show which tools the agent used.
        tool_rows: list = []
        events_task = asyncio.create_task(
            _collect_tool_events(host, port, run_id, tool_rows)
        )

        # Poll GET /v1/runs/{run_id} until the run settles. The budget is the backstop
        # (BRAIN_RUN_TIMEOUT_S), NOT a 2-minute ceiling: long tool runs are normal.
        status_url = url + "/" + run_id
        deadline = time.monotonic() + BRAIN_RUN_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(BRAIN_POLL_S)
            pr = await client.get(status_url, headers={"Authorization": f"Bearer {BRAIN_KEY}"})
            if pr.status_code == 404:
                # transient: run not yet registered under a fresh host; retry
                continue
            if pr.status_code != 200:
                continue
            st = pr.json()
            status = st.get("status")
            if status in ("completed", "failed", "interrupted", "cancelled"):
                # brief grace so the last tool.completed frames land before we
                # cut the stream
                await asyncio.sleep(0.4)
                events_task.cancel()
                try:
                    await events_task
                except (asyncio.CancelledError, Exception):
                    pass
                if status != "completed":
                    publish({"type": "run.settled", "run_id": run_id, "status": status,
                             "session_id": session_id,
                             "error": (st.get("error") or "")[:300]})
                    raise HTTPException(502, f"Brain run {status}: {st.get('error','')}")
                reply = (st.get("output") or "").strip()
                if not reply:
                    raise HTTPException(502, "Brain returned empty reply")
                log.info("BRAIN -> %r (%d tool calls)", reply, len(tool_rows))
                # The panel may no longer be listening to THIS request (a long run can
                # outlive the browser's or a proxy's patience). Carry the answer on the
                # event stream too, so the turn finishes even when the wait dies.
                publish({"type": "run.settled", "run_id": run_id, "status": "completed",
                         "session_id": session_id, "reply": reply, "tools": tool_rows})
                return {"reply": reply, "run_id": run_id, "session_id": session_id,
                        "tools": tool_rows}
            if status == "queued" or status == "running":
                continue
        events_task.cancel()
        publish({"type": "run.settled", "run_id": run_id, "status": "timeout",
                 "session_id": session_id})
        raise HTTPException(504, f"Brain run still working after {int(BRAIN_RUN_TIMEOUT_S)}s")


@app.get("/brain/{run_id}")
async def brain_status(run_id: str):
    """Non-blocking view of a run the panel is still waiting on.

    /brain holds its HTTP response until the run settles; when that wait dies first
    (backend backstop, proxy, or browser idle limit) the panel polls this instead of
    losing the turn. It proxies one cheap GET to the brain and returns immediately.
    """
    if not BRAIN_KEY:
        raise HTTPException(500, "API_SERVER_KEY not configured")
    host, port, _idx = await find_brain_host()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"http://{host}:{port}/v1/runs/{run_id}",
                             headers={"Authorization": f"Bearer {BRAIN_KEY}"})
    if r.status_code != 200:
        raise HTTPException(502, f"run lookup failed: {r.status_code}")
    st = r.json()
    status = st.get("status")
    return {"run_id": run_id, "status": status,
            "reply": (st.get("output") or "").strip() if status == "completed" else "",
            "error": (st.get("error") or "")[:300]}


# ---------- TTS ----------
async def sarvam_speak(text: str, speaker: str, lang: str) -> bytes:
    """Sarvam bulbul TTS -> mp3 bytes. Raises RuntimeError on anything but a clean 200."""
    payload = {
        "text": text,
        "target_language_code": lang,
        "speaker": speaker,
        "model": SARVAM_MODEL,
        "speech_sample_rate": SARVAM_RATE,
        "output_audio_codec": "mp3",
        "enable_preprocessing": True,   # normalises numbers/abbrevs/mixed script
    }
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post(SARVAM_URL, json=payload,
                         headers={"api-subscription-key": SARVAM_KEY,
                                  "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"sarvam {r.status_code}: {r.text[:200]}")
    audios = (r.json() or {}).get("audios") or []
    if not audios:
        raise RuntimeError("sarvam returned no audio")
    return base64.b64decode(audios[0])


async def edge_speak(text: str, voice: str) -> bytes:
    path = TMP_DIR / f"tts_{uuid.uuid4().hex}.mp3"
    try:
        await edge_tts.Communicate(text, voice=voice).save(str(path))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


@app.post("/tts")
async def tts(req: BrainRequest):
    """req.text holds the reply text; returns audio/mp3."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty text")
    voice = pick_tts_voice(text, req.voice)
    engine = "edge-tts"

    # Sarvam handles the code-mixed Hindi/English a voice reply actually contains; a
    # failure here must never cost the user their audio, so it degrades to edge-tts.
    speaker = sarvam_speaker_of(voice) if SARVAM_KEY else None
    if speaker:
        lang = "hi-IN" if _DEVANAGARI.search(text) else "en-IN"
        try:
            data = await sarvam_speak(text, speaker, lang)
            return Response(content=data, media_type="audio/mpeg",
                            headers={"X-TTS-Voice": voice, "X-TTS-Engine": "sarvam",
                                     "X-TTS-Lang": lang})
        except Exception as e:
            log.warning("sarvam tts failed (%s) — falling back to edge-tts", e)
            speaker = None

    if not is_real_edge_voice(voice):
        voice = EDGE_FALLBACK if _DEVANAGARI.search(text) else EDGE_FALLBACK_EN
    try:
        data = await edge_speak(text, voice)
    except Exception as e:
        log.error("tts failed: %s", e)
        raise HTTPException(502, f"TTS failed: {e}")
    return Response(content=data, media_type="audio/mpeg",
                    headers={"X-TTS-Voice": voice, "X-TTS-Engine": engine})


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
            "tts": bool(TTS_VOICE), "brain_url": BRAIN_URL, "probe": probe,
            # language plumbing, so the deployed panel can be checked without guessing
            "stt_language": STT_LANGUAGE or "auto", "tts_voice": TTS_VOICE,
            "tts_voice_hi": TTS_VOICE_HI,
            # TTS attribution: which engine the defaults actually resolve to (a stale
            # Coolify env row silently overrides every code default — this is the check)
            "tts_engine": "sarvam" if sarvam_speaker_of(TTS_VOICE) else "edge-tts",
            # which grammatical gender the assistant is told to speak in, and why:
            # it follows the voice, so a female voice never says "सुन रहा हूँ"
            "voice_gender": gender_of(TTS_VOICE) or "unset",
            "sarvam": {"key": bool(SARVAM_KEY), "model": SARVAM_MODEL,
                       "speaker": SARVAM_SPEAKER, "rate": SARVAM_RATE},
            # STT attribution: which upstream outcomes have happened this container, and the
            # last real (non-throttle, non-silence) failure with its Groq body.
            # STT attribution: outcome counts this container, the last real failure
            # with its Groq body, and the container/size of the last blob received.
            "stt_outcomes": STT_DIAG["counts"], "stt_last_error": STT_DIAG["last_error"],
            "stt_last_blob": STT_DIAG["last_blob"]}


# ---------- static ----------
@app.get("/")
async def index():
    # The client code must never go stale: a cached page from an older deploy is exactly how
    # a bug already fixed in the backend (the "STT 502" mapping) keeps showing up in the UI.
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
