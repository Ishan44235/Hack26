"""The ElevenLabs voices offered to students, driven entirely by .env.

Configure up to 4 voices via VOICE_1..VOICE_4 in .env.  Each value uses the
pipe-delimited format:

    NAME|VOICE_ID|ACCENT|DESCRIPTION|feminine(true/false)

Example:
    VOICE_1=Rachel|21m00Tcm4TlvDq8ikWAM|American|Calm and even.|true

Get voice IDs from https://elevenlabs.io/app/voice-library

Run before the event to confirm every id resolves on your account:
    python -c "import voices; voices.verify()"
"""

from __future__ import annotations

from dataclasses import dataclass

from config import VOICE_SLOTS


@dataclass(frozen=True)
class Voice:
    name: str
    voice_id: str
    accent: str
    description: str
    feminine: bool  # used to pick a sensible default per course


def _parse_slot(raw: str) -> Voice | None:
    """Parse a pipe-delimited voice slot string into a Voice, or None on error."""
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    name, voice_id, accent, description, feminine_str = (p.strip() for p in parts)
    if not name or not voice_id:
        return None
    return Voice(
        name=name,
        voice_id=voice_id,
        accent=accent,
        description=description,
        feminine=feminine_str.lower() in ("true", "1", "yes"),
    )


# Build catalog from .env slots; fall back to a safe built-in default so the
# app doesn't crash if VOICE_1..4 are all missing.
_FALLBACK = Voice(
    name="Rachel",
    voice_id="21m00Tcm4TlvDq8ikWAM",
    accent="American",
    description="Calm and even. The safe default.",
    feminine=True,
)

CATALOG: list[Voice] = [v for raw in VOICE_SLOTS if (v := _parse_slot(raw)) is not None]
if not CATALOG:
    CATALOG = [_FALLBACK]

DEFAULT_VOICE_ID = CATALOG[0].voice_id

_BY_ID = {v.voice_id: v for v in CATALOG}
_BY_NAME = {v.name.lower(): v for v in CATALOG}


def by_id(voice_id: str) -> Voice | None:
    return _BY_ID.get(voice_id)


def by_name(name: str) -> Voice | None:
    return _BY_NAME.get(name.lower())


def label(voice_id: str) -> str:
    """What to show in the UI for a stored id."""
    v = _BY_ID.get(voice_id)
    return v.name if v else "Unknown voice"


def is_allowed(voice_id: str) -> bool:
    """Reject anything not in the catalog, so a stale profile can't 500 a session."""
    return voice_id in _BY_ID


def as_json() -> list[dict]:
    """What GET /api/voices returns — the settings panel renders this directly."""
    return [
        {
            "name": v.name,
            "voice_id": v.voice_id,
            "accent": v.accent,
            "description": v.description,
        }
        for v in CATALOG
    ]


def verify() -> None:
    """Check every id resolves on your account. Run this before the event.

    A retired voice id does not fail until you try to synthesise with it, which
    in a demo means it fails on stage.
    """
    from elevenlabs.client import ElevenLabs

    from config import require_eleven

    client = ElevenLabs(api_key=require_eleven())
    available = {v.voice_id for v in client.voices.get_all().voices}

    missing = [v for v in CATALOG if v.voice_id not in available]
    for v in CATALOG:
        print(f"{'ok  ' if v.voice_id in available else 'MISS'}  {v.name:<10} {v.voice_id}")

    if missing:
        raise SystemExit(
            f"\n{len(missing)} voice id(s) not on this account: "
            + ", ".join(v.name for v in missing)
            + "\nCheck the voice IDs in your .env against the ElevenLabs voice library."
        )
    print(f"\nAll {len(CATALOG)} voices resolve.")


if __name__ == "__main__":
    verify()
