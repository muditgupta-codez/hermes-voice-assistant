# Hermes Web Voice Assistant

A live, two-way voice UI for talking to the Hermes agent in the browser.

- **STT** — Groq Whisper (`whisper-large-v3-turbo`)
- **Brain** — Hermes `api_server` (OpenAI-compatible, port 8642)
- **TTS** — edge-tts (free MS voices)

The browser captures your mic with `getUserMedia` (echo cancellation +
noise suppression built in), streams it to `/stt`, sends the text to `/brain`,
and plays `/tts` audio back — a clean, truly-live loop that avoids the
loopback problems of a WhatsApp call bridge.

## Endpoints

| Method | Path    | Purpose                                        |
|--------|---------|------------------------------------------------|
| POST   | `/stt`  | multipart `file=` audio → transcript text      |
| POST   | `/brain`| `{"text": "..."}` → assistant reply            |
| POST   | `/tts`  | `{"text": "..."}` → `audio/mpeg`               |
| GET    | `/health`| readiness + which keys are configured          |
| GET    | `/`     | the interactive UI                             |

## Config (env)

| Var             | Default                 | Note                        |
|-----------------|-------------------------|-----------------------------|
| `GROQ_API_KEY`  | (required)              | from `~/.tokens/groq_api_key`|
| `API_SERVER_KEY`| (required)              | Hermes brain Bearer key     |
| `API_SERVER_HOST`| `127.0.0.1`             |                           |
| `API_SERVER_PORT`| `8642`                  |                           |
| `BRAIN_MODEL`   | `hermes-agent`          |                           |
| `TTS_VOICE`     | `en-US-AriaNeural`      | any edge-tts voice         |
| `SYSTEM_PROMPT` | (default persona)      |                           |
| `PORT`          | `8000`                  |                           |

## Run locally

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
GROQ_API_KEY=... API_SERVER_KEY=... .venv/bin/python app.py
# open http://localhost:8000  (secure context → mic works)
```

## Deploy (Coolify)

Build pack = Dockerfile. Set these env vars on the app, then deploy.
The Hermes brain must be reachable from the app container (set `API_SERVER_HOST`
to the brain's reachable host/URL and `API_SERVER_PORT` accordingly).
