"""ElevenLabs synthesis with an on-disk cache.

Two paths:

  synth()          scripted segments -> mp3 on disk, cached by content hash.
                   Deterministic, cheap to replay, and the duration lets the
                   frontend schedule annotations against playback time.

  signed_agent_url()  the live conversational path. Returns a signed WebSocket
                   URL the browser connects to directly so the API key never
                   reaches the client.

Scripted segments should be pre-warmed (see prewarm) — synthesising mid-session
adds a visible stall before the tutor speaks.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from elevenlabs.client import ElevenLabs

from config import (
    AUDIO_CACHE_DIR,
    ELEVEN_TTS_MODEL,
    ELEVENLABS_AGENT_ID,
    require_eleven,
)

_client: ElevenLabs | None = None


def client() -> ElevenLabs:
    global _client
    if _client is None:
        _client = ElevenLabs(api_key=require_eleven())
    return _client


#: Models that perform inline audio tags rather than reading them out loud.
TAG_AWARE_MODELS = ("eleven_v3",)

_TAG = re.compile(r"\[[a-z' ]+\]", re.I)


def strip_tags(text: str) -> str:
    """Remove [pause] / [sighs] / etc.

    v3 performs these; every older model reads them out as words, which sounds
    worse than having no tags at all. scriptgen writes them unconditionally, so
    this is what keeps a turbo fallback usable.
    """
    return re.sub(r"\s+", " ", _TAG.sub(" ", text)).strip()


def for_synthesis(text: str) -> str:
    return text if ELEVEN_TTS_MODEL.startswith(TAG_AWARE_MODELS) else strip_tags(text)


def for_display(text: str) -> str:
    """The transcript never shows tags, whichever model is in use."""
    return strip_tags(text)


def _cache_key(text: str, voice_id: str, rate: float) -> str:
    h = hashlib.sha256(f"{ELEVEN_TTS_MODEL}|{voice_id}|{rate}|{text}".encode()).hexdigest()
    return h[:32]


def estimate_duration_ms(text: str, rate: float = 1.0) -> int:
    """Rough fallback when we can't measure the real mp3 duration.

    ~15 characters per second of speech. Only used if mutagen-style probing
    isn't available; replace with a real probe if annotation timing drifts.
    """
    return int((len(text) / 15.0) * 1000 / max(rate, 0.1))


def synth(text: str, voice_id: str, rate: float = 1.0) -> tuple[Path, int]:
    """Synthesise text, return (path_to_mp3, duration_ms). Cached."""
    key = _cache_key(text, voice_id, rate)
    out = AUDIO_CACHE_DIR / f"{key}.mp3"

    if out.exists() and out.stat().st_size == 0:
        out.unlink()

    if not out.exists():
        temp = out.with_name(f"{out.stem}.{uuid.uuid4().hex}.tmp")
        try:
            stream = client().text_to_speech.convert(
                voice_id=voice_id,
                text=for_synthesis(text),
                model_id=ELEVEN_TTS_MODEL,
                output_format="mp3_44100_128",
                voice_settings={
                    "stability": 0.45,
                    "similarity_boost": 0.75,
                    "speed": rate,
                },
            )
            with open(temp, "wb") as f:
                for chunk in stream:
                    if chunk:
                        f.write(chunk)
            if temp.stat().st_size == 0:
                raise RuntimeError("ElevenLabs returned an empty audio stream.")
            temp.replace(out)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

    return out, _duration_ms(out, text, rate)


def _duration_ms(path: Path, text: str, rate: float) -> int:
    """Read the real duration from the mp3 if we can, else estimate."""
    try:
        from mutagen.mp3 import MP3  # optional dependency

        return int(MP3(str(path)).info.length * 1000)
    except Exception:  # noqa: BLE001
        return estimate_duration_ms(text, rate)


def prewarm(segments, voice_id: str, rate: float = 1.0, lookahead: int = 3) -> None:
    """Synthesise the next few segments so playback never stalls."""
    for seg in segments[:lookahead]:
        try:
            synth(seg.speech, voice_id, rate)
        except Exception:  # noqa: BLE001
            pass


def signed_agent_url(agent_id: str = "") -> str:
    """Signed WS URL for the live ElevenLabs agent. Never expose the API key."""
    aid = agent_id or ELEVENLABS_AGENT_ID
    if not aid:
        raise RuntimeError("ELEVENLABS_AGENT_ID is not set; live agent path unavailable.")
    resp = client().conversational_ai.get_signed_url(agent_id=aid)
    return getattr(resp, "signed_url", None) or resp["signed_url"]
