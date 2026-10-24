"""Data model for courses, lectures, lesson scripts and sessions.

The pydantic models here do double duty: they describe on-disk JSON and they are
handed to Gemini as `response_schema` so generation comes back already shaped.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _default_voice_id() -> str:
    """Read the first configured voice ID at runtime so .env changes take effect."""
    try:
        import voices  # import here to avoid a circular import
        return voices.DEFAULT_VOICE_ID
    except Exception:
        return "21m00Tcm4TlvDq8ikWAM"  # Rachel fallback


# --------------------------------------------------------------------------
# Tutor personality, one per course
# --------------------------------------------------------------------------

class Depth(int, Enum):
    SKIM = 1      # headline only, assumes you know the prerequisites
    NORMAL = 2    # standard explanation
    DEEP = 3      # derivations, edge cases, why it matters


class TutorProfile(BaseModel):
    voice_id: str = Field(
        default_factory=_default_voice_id,
        description="ElevenLabs voice id. Configure via VOICE_1..VOICE_4 in .env.",
    )
    speech_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    depth: Depth = Depth.NORMAL
    comfort_level: Literal["new", "shaky", "solid", "confident"] = "shaky"
    encouragement_style: Literal["warm", "neutral", "coach"] = "warm"
    persona_note: str = Field(
        default="",
        description="Free text appended to the system prompt, e.g. 'use aerospace examples'.",
    )


# --------------------------------------------------------------------------
# Lesson script — the output of scriptgen.py
# --------------------------------------------------------------------------

class Annotation(BaseModel):
    """One handwritten mark to draw on the slide."""

    text: str = Field(description="Short. A term, a formula, an arrow label. Under 40 chars.")
    x: float = Field(ge=0, le=1, description="Fraction of slide width for the left edge.")
    y: float = Field(ge=0, le=1, description="Fraction of slide height for the baseline.")
    at_ms: int = Field(ge=0, description="Ms into this segment's audio when writing starts.")
    style: Literal["note", "formula", "underline", "circle"] = "note"


class Segment(BaseModel):
    id: str
    slide: int = Field(ge=0, description="0-based slide index this segment belongs to.")
    kind: Literal["explain", "question", "checkpoint"]
    depth: Depth
    speech: str = Field(description="Exactly what the tutor says. Spoken prose, no markdown.")
    annotations: list[Annotation] = Field(default_factory=list)
    concept_id: Optional[str] = Field(
        default=None, description="Concept this segment teaches or tests."
    )
    teaches: str = Field(
        default="",
        description=(
            "For kind='explain': the one thing this segment adds that is NOT printed "
            "on the slide — the analogy, the mechanism, the misconception, the worked "
            "number. A few words. If you cannot name one, the segment is only reading "
            "the slide back and must be rewritten."
        ),
    )
    rubric: list[str] = Field(
        default_factory=list,
        description="For kind='question': the things a correct spoken answer must contain.",
    )
    accept_if_missing: list[str] = Field(
        default_factory=list,
        description="Rubric points that are nice-to-have, not required for a pass.",
    )


class Concept(BaseModel):
    id: str
    label: str
    prerequisites: list[str] = Field(default_factory=list)


class SlideScript(BaseModel):
    """What Gemini returns for one slide. Three depths generated up front."""

    slide: int
    concepts: list[Concept] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)


class Slide(BaseModel):
    index: int
    text: str = ""
    image_path: str = ""
    width: int = 0
    height: int = 0


class Lecture(BaseModel):
    id: str
    title: str
    source_file: str
    slides: list[Slide] = Field(default_factory=list)
    scripts: list[SlideScript] = Field(default_factory=list)
    instructions: str = Field(
        default="", description="User-supplied instructions, e.g. 'focus on the derivations'."
    )
    error: str = Field(
        default="",
        description="Last script-generation failure, surfaced to the UI instead of polling forever.",
    )


class Course(BaseModel):
    id: str
    name: str
    profile: TutorProfile = Field(default_factory=TutorProfile)
    color: int = Field(default=0, description="Index into the UI's subject palette.")
    lecture_ids: list[str] = Field(default_factory=list)
    mastery: dict[str, float] = Field(
        default_factory=dict, description="concept_id -> 0.0..1.0 rolling score."
    )


# --------------------------------------------------------------------------
# Grading — the output of grading.py
# --------------------------------------------------------------------------

class Grade(BaseModel):
    correct: bool
    matched: list[str] = Field(
        default_factory=list, description="Rubric points the answer did hit."
    )
    missed: list[str] = Field(
        default_factory=list, description="Rubric points the answer did not hit."
    )
    misconception: str = Field(
        default="",
        description="If wrong, the specific wrong idea — not 'they were incorrect'.",
    )
    feedback_speech: str = Field(
        description="What the tutor says back. Name what they got right before the gap."
    )
    followup_question: str = Field(
        default="",
        description="If wrong: a simpler scaffolded question. Empty if correct.",
    )


# --------------------------------------------------------------------------
# Wire protocol — WebSocket messages
# --------------------------------------------------------------------------

class ClientEvent(BaseModel):
    """Browser -> server."""

    type: Literal[
        "start", "answer", "interrupt", "resume", "set_depth", "skip_question",
        "next", "prev", "continue", "practice_more", "ping",
    ]
    text: str = ""
    depth: Optional[Depth] = None
    slide: Optional[int] = None


class ServerEvent(BaseModel):
    """Server -> browser."""

    type: Literal[
        "segment", "audio", "transcript", "grade", "await_answer",
        "checkpoint", "slide", "state", "error", "done",
    ]
    segment: Optional[Segment] = None
    audio_url: str = ""
    duration_ms: int = 0
    role: Literal["tutor", "student", "system"] = "tutor"
    text: str = ""
    grade: Optional[Grade] = None
    slide: Optional[int] = None
    state: str = ""
