"""Central config. Everything reads from here, nothing reads os.environ directly."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")

# Four configurable voice slots. Format: NAME|VOICE_ID|ACCENT|DESCRIPTION|feminine
# Edit VOICE_1..VOICE_4 in .env to swap voices without touching code.
VOICE_SLOTS: list[str] = [
    v for v in [
        os.getenv("VOICE_1", ""),
        os.getenv("VOICE_2", ""),
        os.getenv("VOICE_3", ""),
        os.getenv("VOICE_4", ""),
    ]
    if v.strip()
]

GEMINI_SCRIPT_MODEL = os.getenv("GEMINI_SCRIPT_MODEL", "gemini-flash-latest")
GEMINI_GRADE_MODEL = os.getenv("GEMINI_GRADE_MODEL", "gemini-flash-latest")
GEMINI_INTERRUPT_MODEL = os.getenv("GEMINI_INTERRUPT_MODEL", "gemini-flash-latest")

# v3 performs the [pause] / [sighs] tags scriptgen writes; older models read
# them out loud, so tts.strip_tags() handles those. See .env.example.
ELEVEN_TTS_MODEL = os.getenv("ELEVEN_TTS_MODEL", "eleven_v3")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
COURSES_DIR = DATA_DIR / "courses"
AUDIO_CACHE_DIR = DATA_DIR / "audio"
UPLOAD_DIR = DATA_DIR / "uploads"

for _d in (DATA_DIR, COURSES_DIR, AUDIO_CACHE_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def require_google() -> str:
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set. Copy .env.example to .env.")
    return GOOGLE_API_KEY


def require_eleven() -> str:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set. Copy .env.example to .env.")
    return ELEVENLABS_API_KEY
