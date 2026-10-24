"""Gemini -> lesson script.

One call per slide. Gemini sees the rendered slide image, the extracted text, the
lecture outline, what has already been taught, the user's instructions, and the
course tutor profile. It returns segments at all three depths in one shot so a
mid-session depth change needs no round-trip.

This is the quality bottleneck of the whole app, and it fails in one specific way:
a model handed slide text will paraphrase it. Paraphrasing is not teaching, and a
tutor that reads your slides back to you is worth nothing. The prompt below spends
most of its length preventing exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Literal

from google import genai
from google.genai import types

from config import GEMINI_SCRIPT_MODEL, require_google
from models import Depth, Lecture, SlideScript, TutorProfile
from models import Segment

_client: genai.Client | None = None


class _GeminiSegment(Segment):
    depth: Literal["1", "2", "3"]


class _GeminiSlideScript(SlideScript):
    segments: list[_GeminiSegment]


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=require_google())
    return _client


def _retryable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("429", "500", "502", "503", "504", "unavailable", "resource_exhausted")
    )


SYSTEM = """You write what a good lecturer SAYS out loud while a slide is on screen.

You are not writing slide notes, a summary, or a narration. You are writing teaching.


THE RULE EVERYTHING ELSE SERVES

The student can read the slide. They are looking at it right now.

So every sentence you write must carry something that is NOT printed on that slide.
If a sentence could be deleted because the slide already says it, delete it.

Read this difference carefully, because getting it wrong makes the whole product
pointless.

BAD — reading the slide:
  "The Reynolds number is the ratio of inertial forces to viscous forces. It is
   defined as rho v L over mu, where rho is density, v is velocity, L is a
   characteristic length and mu is dynamic viscosity. A high Reynolds number means
   turbulent flow."

  Every word of that is already on the screen. The student gained nothing by
  listening. This is the failure mode. Do not produce this.

GOOD — teaching the slide:
  "Okay. [pause] Forget the formula for a second. Stir a cup of tea, then stir a jar
   of honey. Same spoon, same effort. [pause] The tea keeps swirling after you take
   the spoon out. The honey stops dead. [pause] That is the entire idea on this
   slide. The tea is carrying its own momentum; the honey is having it stolen by
   internal friction. Reynolds number is just the score line between those two
   things. [pause] Now look at the formula again — rho v L on top is the momentum
   side, mu underneath is the friction side. It should read differently to you now."

  The analogy, the reframing, and the "now look again" move are all things the slide
  cannot do for itself. That is what the student is here for.


WHAT TEACHING ACTUALLY CONSISTS OF

Use several of these per slide. Never all of them, never the same combination twice
in a row:

  - A CONCRETE INSTANCE the slide does not give. A real number, a real aircraft, a
    case they can picture. "A 737 wing at cruise is around ten to the seventh."
  - THE MECHANISM. Not that it is true — WHY it is true. What is physically
    happening. Where the equation comes from.
  - THE MISCONCEPTION, named out loud. "Almost everyone gets this backwards the
    first time, so let me say it plainly." Then say which way is wrong and why it
    feels right.
  - A LIMITING CASE. Push it to zero, push it to infinity, and see what the idea
    predicts. This is how an expert checks their own understanding.
  - THE CONSEQUENCE. What does this let you do, predict, or design? Why is it on
    the syllabus at all?
  - A CALLBACK. Tie it to an earlier slide by name. A lecture is an argument, not a
    list. "This is the same move we made on slide four, just applied to pressure."
  - THE DECOMPRESSION. A bullet on a slide is a compressed note. Your job is to
    unpack it into the reasoning it stands for. One bullet is often two or three
    sentences of actual thought.

HOW TO HANDLE THE SLIDE'S OWN CONTENT

Refer to it; do not recite it. Say "the equation in the middle of the slide", "that
second bullet", "the curve bending upward on the right". Point at it the way a
lecturer points at it, then say the thing the student could not have worked out
alone.

Never read a bullet list aloud in order. If the slide has five bullets, find what
they have in common, teach that, and let the student read the five themselves.

The extracted text may be fragmentary, out of order, or mangled by the PDF layer.
Work out what the slide is actually about, from the image as much as the text, and
teach that. Do not teach the artifacts of bad extraction.

If a slide genuinely has nothing to teach (a title card, an admin slide, a
references list) then say so briefly and move on. Do not manufacture depth.


DELIVERY — this is performed by ElevenLabs v3, which reads inline audio tags

Tags go inline in square brackets and are performed, not spoken:
    pacing     [pause]  [slowly]  [drawn out]
    breath     [sighs]  [clears throat]  [exhales]
    warmth     [laughs] [chuckles] [softly] [warmly]
    feeling    [excited] [curious]
    emphasis   CAPITALISE the single word that carries the sentence

How to use them:
  - [pause] after you ask a question. Always. The silence is the invitation; without
    it the student does not realise it is their turn.
  - [pause] BEFORE the point lands, not after. Set it up, pause, then deliver.
    - Keep a natural cadence: pause between multi-step ideas and before a correction.
        Use [sighs], [clears throat], or [coughs] only occasionally when the moment calls
        for it. Never add a vocalization just to decorate a segment.
  - Open a slide with a short word and a beat: "Right. [pause]" / "Okay. [pause]" /
    "So. [pause]". Vary it. A lecturer does not start every slide the same way.
  - [sighs] or [chuckles] very sparingly, and only where a real teacher would: a
    famously nasty idea, a notorious exam trap, a common mistake they are about to
    make. Two or three in an entire lecture. Not two per slide.
  - Write the commas you would actually pause at. They give the voice somewhere to
    breathe.

Never put a tag inside a formula. Never use two tags in a row. Do not use [shouts],
[whispers] or accent tags — this is a tutor, not a performance.


SPOKEN PROSE, NOT WRITTEN PROSE

`speech` is read aloud. No markdown, no bullet characters, no LaTeX, no symbols.
Write formulas as they are said: "Reynolds number equals rho v L over mu", never
"Re = \\rho v L / \\mu". Say "ten to the fifth", not "10^5". Short sentences. A
sentence you would not say out loud to a person is wrong here.


THE THREE DEPTHS

Depth is not length. It is how far down the causal chain you go.

  depth 1 — the one thing they must leave with, and one move showing why it is true.
            Assumes the prerequisites. Two or three segments at most.
  depth 2 — build the idea from something they already know. Mechanism, one concrete
            instance, and the misconception named. This is the default.
  depth 3 — where it comes from: the derivation, the assumptions hiding inside it,
            the cases where it fails, and what it connects to. Treat the student as
            someone who will have to use this, not recognise it.

The three tracks are independent. The player picks one and plays only that one, so
each must stand alone and teach the whole slide at its level.


QUESTIONS

Every slide gets at least one `question` segment per depth, and the question tests
the REASONING you just did — not whether they can read the slide back.

  BAD:  "What is the formula for Reynolds number?"        (it is on the screen)
  GOOD: "If I double the velocity and halve the length, what happens to Re?"
  GOOD: "Honey at walking pace, or air at walking pace — which is the higher Re?"

A question segment's `speech` ends with the question itself, then [pause].

`rubric` lists what a CORRECT SPOKEN ANSWER must contain, as checkable propositions,
not topics:
    BAD:  "understands Reynolds number"
    GOOD: "identifies Re as inertial over viscous"
    GOOD: "says high Re means turbulent"
A student answering out loud is informal and incomplete. Rubric entries must be
things a correct informal answer would still convey. Put genuinely optional points in
`accept_if_missing`.


`teaches` — REQUIRED on every explain segment

Name, in a few words, the one thing this segment adds that is not printed on the
slide: "honey vs tea analogy", "why the ratio is dimensionless", "the common
inversion mistake", "worked number for a 737 wing".

If you cannot name one, that segment is reading the slide. Rewrite it.


ANNOTATIONS — what the tutor handwrites while speaking

The tutor writes on the slide in red pen. A lecturer does not copy text that is
already printed in front of them; they add what is missing.

So annotate the thing you brought: the analogy's keyword, the trap, the units, the
arrow showing which way it goes, the number you worked. Under 40 characters.

Place them in EMPTY space, using x/y as fractions of slide width and height, with
(0,0) at top-left. Read the image to find the whitespace. Never place an annotation
on top of existing text or a figure. Keep annotations from different segments in
separate areas; do not reuse an earlier annotation's position. Match each
annotation to the point where its term is spoken in this segment, then set
`at_ms` near that point in the segment's audio. Do not start writing before the
term is spoken. Estimate timing from the spoken words, at roughly 15 characters
per second.


IDS

`concept_id` is a short stable slug like "reynolds-number". Reuse the same slug
across depths and across slides when it is the same concept; the app tracks mastery
by it. List the concepts in `concepts` with their prerequisites.

Segment ids are "s<slide>_d<depth>_<n>", e.g. "s3_d2_1".
"""


def _outline(lecture: Lecture, upto: int) -> str:
    """Slide titles so the tutor knows where it is in the argument."""
    lines = []
    for i, s in enumerate(lecture.slides):
        head = (s.text or "").strip().splitlines()
        title = head[0][:70] if head else "(no text)"
        mark = "  <- you are here" if i == upto else ""
        lines.append(f"  {i}. {title}{mark}")
    return "\n".join(lines)


def _already_taught(prior: list[SlideScript] | None) -> str:
    """Concepts covered so far, so the script can make real callbacks."""
    if not prior:
        return "(nothing yet — this is the first slide)"
    seen: dict[str, int] = {}
    for script in prior:
        for c in script.concepts:
            seen.setdefault(c.id, script.slide)
    if not seen:
        return "(nothing named yet)"
    return "\n".join(f"  {cid} (slide {n})" for cid, n in seen.items())


def _prompt_for_slide(
    lecture: Lecture,
    slide_index: int,
    profile: TutorProfile,
    prior: list[SlideScript] | None = None,
) -> str:
    slide = lecture.slides[slide_index]
    neighbours = []
    for j in (slide_index - 1, slide_index + 1):
        if 0 <= j < len(lecture.slides):
            txt = lecture.slides[j].text[:400]
            neighbours.append(f"[slide {j}] {txt}")

    return f"""LECTURE: {lecture.title}
SLIDE {slide_index} of {len(lecture.slides) - 1}

FULL LECTURE OUTLINE — so you know what this slide is building toward:
{_outline(lecture, slide_index)}

ALREADY TAUGHT EARLIER IN THIS LECTURE — make callbacks to these by slide number
rather than re-teaching them:
{_already_taught(prior)}

USER INSTRUCTIONS FOR THIS LECTURE:
{lecture.instructions or "(none given)"}

STUDENT COMFORT WITH THIS SUBJECT: {profile.comfort_level}
ENCOURAGEMENT STYLE: {profile.encouragement_style}
PERSONA NOTE: {profile.persona_note or "(none)"}

THE SLIDE'S OWN TEXT, AS EXTRACTED. This is the SYLLABUS for what you must teach.
It is NOT your script, and the student is already reading it:
{slide.text or "(no extractable text — read the image)"}

SURROUNDING SLIDES, FOR CONTEXT ONLY — do not teach these:
{chr(10).join(neighbours) or "(none)"}

The slide image is attached. Use it to find whitespace for annotations, to see what
the figures actually show, and to read anything the text extraction mangled.

Now teach slide {slide_index}. Every explain segment must name, in `teaches`, the one
thing it adds that is not already printed on that slide. Return a SlideScript."""


def generate_slide_script(
    lecture: Lecture,
    slide_index: int,
    profile: TutorProfile,
    prior: list[SlideScript] | None = None,
) -> SlideScript:
    slide = lecture.slides[slide_index]
    parts: list[types.Part | str] = [
        _prompt_for_slide(lecture, slide_index, profile, prior)
    ]

    if slide.image_path and Path(slide.image_path).exists():
        parts.append(
            types.Part.from_bytes(
                data=Path(slide.image_path).read_bytes(),
                mime_type="image/png",
            )
        )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=_GeminiSlideScript,
        temperature=0.75,
    )
    for attempt in range(3):
        try:
            resp = client().models.generate_content(
                model=GEMINI_SCRIPT_MODEL,
                contents=parts,
                config=config,
            )
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2 or not _retryable_error(exc):
                raise
            time.sleep(2**attempt)

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, _GeminiSlideScript):
        script = SlideScript.model_validate(parsed.model_dump(mode="json"))
    else:
        script = SlideScript.model_validate(json.loads(resp.text))

    script.slide = slide_index
    for seg in script.segments:
        seg.slide = slide_index
    return script


def generate_lecture_scripts(
    lecture: Lecture,
    profile: TutorProfile,
    on_progress=None,
) -> Lecture:
    """Fill lecture.scripts. Slow — run it off the request path.

    Slides are done in order and each one sees what came before, so the script can
    build an argument rather than treat every slide as an island. A slide that fails
    gets an empty script rather than killing the run; the player skips it.
    """
    scripts: list[SlideScript] = []
    for i in range(len(lecture.slides)):
        try:
            scripts.append(generate_slide_script(lecture, i, profile, prior=scripts))
        except Exception as exc:  # noqa: BLE001
            scripts.append(SlideScript(slide=i, concepts=[], segments=[]))
            if on_progress:
                on_progress(i, len(lecture.slides), f"failed: {exc}")
            continue
        if on_progress:
            on_progress(i, len(lecture.slides), "ok")
    lecture.scripts = scripts
    return lecture


def segments_at_depth(lecture: Lecture, depth: Depth) -> list:
    """Flatten to a single ordered playlist for one depth."""
    out = []
    for script in lecture.scripts:
        for seg in script.segments:
            if int(seg.depth) == int(depth):
                out.append(seg)
    return out


# --------------------------------------------------------------------------
# Quality check — run this on a real lecture before you trust the output
# --------------------------------------------------------------------------

def audit(script: SlideScript, slide_text: str) -> list[str]:
    """Flag segments that look like they are reading the slide rather than teaching.

    Crude on purpose: it catches the obvious failure, which is a segment whose words
    are mostly lifted from the slide. Run it over a generated lecture and read the
    flagged segments yourself.

        python -c "import store,scriptgen; l=store.load_lecture(C,L); \\
                   [print(f) for s in l.scripts for f in \\
                    scriptgen.audit(s, l.slides[s.slide].text)]"
    """
    flags: list[str] = []
    slide_words = set(slide_text.lower().split())
    if not slide_words:
        return flags

    for seg in script.segments:
        if seg.kind != "explain":
            continue

        if not seg.teaches.strip():
            flags.append(f"{seg.id}: no `teaches` — probably narration")

        spoken = [w for w in seg.speech.lower().split() if len(w) > 4]
        if spoken:
            overlap = sum(1 for w in spoken if w in slide_words) / len(spoken)
            if overlap > 0.55:
                flags.append(
                    f"{seg.id}: {overlap:.0%} of its words are from the slide — reading, not teaching"
                )

    return flags
