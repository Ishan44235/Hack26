"""Grade a spoken answer against a segment's rubric, and answer interruptions.

Two jobs, both Gemini:

  grade_answer()        rubric check + what the tutor says back
  answer_interruption()  mid-lecture question, answered in context

The grading prompt is deliberately strict about two things: accepting informal
spoken answers, and giving SPECIFIC encouragement. Generic praise ("great try!")
is worse than none — it tells the student nothing and reads as hollow.
"""

from __future__ import annotations

import json

from google import genai
from google.genai import types

from config import GEMINI_GRADE_MODEL, GEMINI_INTERRUPT_MODEL, require_google
from models import Grade, Segment, TutorProfile

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=require_google())
    return _client


GRADE_SYSTEM = """You grade a student's SPOKEN answer against a rubric, then write what the
tutor says back.

HOW TO GRADE

- The answer was spoken aloud and transcribed. It will be informal, rambling, and may have
  transcription errors. Grade the IDEAS, not the phrasing. "the sticky one over the
  smooshing one" hits a rubric point about viscous versus inertial forces.
- A rubric point counts as matched if the student conveyed it, however loosely.
- Points listed in accept_if_missing do NOT have to be matched to be correct.
- `correct` is true when every required rubric point is matched.
- If wrong, `misconception` names the specific wrong idea they hold. Not "they were
  incorrect" — say what they believe that isn't so. Empty string if they were simply
  incomplete rather than wrong.

HOW TO WRITE feedback_speech

- Spoken aloud by ElevenLabs v3, which performs inline audio tags. Use [pause] before
  the correction so it does not arrive on top of the praise. Use [sighs], [clears throat],
  or [coughs] only when the moment genuinely calls for it, never as decoration. One tag
  per response at most. No markdown, no formulas in symbols.
- Name what they got RIGHT first, specifically. Then the gap, specifically.
    GOOD: "You had the mechanism exactly right — it is a ratio of two forces. You just had
           it inverted; inertial goes on top."
    BAD:  "Great try! Not quite. Let me explain."
- Never generic praise. If there was nothing right, say so warmly and briefly, then
  re-explain from a DIFFERENT angle than the original explanation — a concrete example,
  an analogy, a limiting case. Do not repeat the same words.
- Keep it under 60 words.

followup_question
- Only if `correct` is false. A SIMPLER scaffolded question that isolates the thing they
  missed — not the same question again. Spoken, ends with the question.
- Empty string if correct.
"""


def grade_answer(
    segment: Segment,
    student_answer: str,
    profile: TutorProfile,
    slide_text: str = "",
) -> Grade:
    prompt = f"""THE QUESTION THE TUTOR ASKED (spoken):
{segment.speech}

REQUIRED RUBRIC POINTS:
{json.dumps(segment.rubric, indent=2)}

OPTIONAL POINTS (accept_if_missing):
{json.dumps(segment.accept_if_missing, indent=2)}

SLIDE CONTEXT:
{slide_text[:1500] or "(none)"}

STUDENT'S SPOKEN ANSWER (transcribed):
{student_answer}

STUDENT COMFORT WITH THIS SUBJECT: {profile.comfort_level}
ENCOURAGEMENT STYLE: {profile.encouragement_style}

Grade it."""

    resp = client().models.generate_content(
        model=GEMINI_GRADE_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=GRADE_SYSTEM,
            response_mime_type="application/json",
            response_schema=Grade,
            temperature=0.3,
        ),
    )

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, Grade):
        return parsed
    return Grade.model_validate(json.loads(resp.text))


INTERRUPT_SYSTEM = """A student interrupted a lecture to ask something. Answer it, out loud,
then hand control back.

- Spoken prose. No markdown. Read formulas aloud in words.
- Answer the question they actually asked. Do not restate the slide.
- Under 80 words unless the question genuinely needs more.
- If the question is about something a later slide covers, say so in one clause and give
  the short answer now anyway. Never defer entirely.
- Do not end with "shall we continue?" — the app handles resuming.
"""


def answer_interruption(
    question: str,
    current_segment: Segment | None,
    slide_text: str,
    profile: TutorProfile,
    recent_transcript: list[str] | None = None,
) -> str:
    """Return spoken text answering a mid-lecture question."""
    recent = "\n".join((recent_transcript or [])[-8:])
    prompt = f"""WHAT THE TUTOR WAS SAYING WHEN INTERRUPTED:
{current_segment.speech if current_segment else "(not mid-segment)"}

CURRENT SLIDE TEXT:
{slide_text[:2000] or "(none)"}

RECENT CONVERSATION:
{recent or "(none)"}

PERSONA NOTE: {profile.persona_note or "(none)"}

THE STUDENT'S QUESTION:
{question}

Answer it."""

    resp = client().models.generate_content(
        model=GEMINI_INTERRUPT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=INTERRUPT_SYSTEM,
            temperature=0.5,
        ),
    )
    return (resp.text or "").strip()
