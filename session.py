"""Session engine: the playlist, the interrupt stack, mastery, and the wrong-answer loop.

One TutorSession per open browser tab. It owns no I/O — it takes events in and
yields ServerEvents out, so it's testable without a socket or an API key.

The interrupt design matters more than it looks. When the student cuts in:
  - the current segment is pushed onto a stack, not discarded
  - the partially-drawn annotation stays on the slide (erasing reads as a crash)
  - on resume, we replay the LAST SENTENCE of the interrupted segment, not the
    whole thing and not from mid-word. Backing up one sentence feels natural;
    resuming mid-word feels broken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from models import (
    ClientEvent,
    Course,
    Depth,
    Grade,
    Lecture,
    Segment,
    ServerEvent,
)

MASTERY_UP = 0.35
MASTERY_DOWN = 0.20


def last_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[-1] if parts else text


@dataclass
class TutorSession:
    course: Course
    lecture: Lecture
    depth: Depth = Depth.NORMAL

    playlist: list[Segment] = field(default_factory=list)
    cursor: int = -1
    state: str = "idle"  # idle | playing | awaiting_answer | interrupted | checkpoint | done

    _stack: list[tuple[int, str]] = field(default_factory=list)  # (cursor, resume_text)
    _wrong_streak: int = 0
    _pending_followup: str = ""
    transcript: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- setup

    def build_playlist(self) -> None:
        self.playlist = [
            seg
            for script in self.lecture.scripts
            for seg in script.segments
            if int(seg.depth) == int(self.depth)
        ]

    def set_depth(self, depth: Depth) -> None:
        """Swap depth track, landing on the nearest segment of the same slide.

        Scripts carry all three depths, so this needs no regeneration.
        """
        current_slide = self.current.slide if self.current else 0
        self.depth = depth
        self.build_playlist()
        for i, seg in enumerate(self.playlist):
            if seg.slide >= current_slide:
                self.cursor = i - 1
                return
        self.cursor = len(self.playlist) - 1

    # -------------------------------------------------------------- helpers

    @property
    def current(self) -> Segment | None:
        if 0 <= self.cursor < len(self.playlist):
            return self.playlist[self.cursor]
        return None

    def slide_text(self) -> str:
        seg = self.current
        if not seg or seg.slide >= len(self.lecture.slides):
            return ""
        return self.lecture.slides[seg.slide].text

    def _say(self, text: str) -> ServerEvent:
        self.transcript.append(f"tutor: {text}")
        return ServerEvent(type="transcript", role="tutor", text=text)

    def _heard(self, text: str) -> ServerEvent:
        self.transcript.append(f"student: {text}")
        return ServerEvent(type="transcript", role="student", text=text)

    def _state(self) -> ServerEvent:
        return ServerEvent(type="state", state=self.state)

    def bump_mastery(self, concept_id: str | None, up: bool) -> None:
        if not concept_id:
            return
        cur = self.course.mastery.get(concept_id, 0.3)
        delta = MASTERY_UP if up else -MASTERY_DOWN
        self.course.mastery[concept_id] = max(0.0, min(1.0, cur + delta))

    # ------------------------------------------------------------ advancing

    def advance(self) -> Iterator[ServerEvent]:
        """Move to the next segment and emit it. Stops at questions."""
        if self._pending_followup:
            text, self._pending_followup = self._pending_followup, ""
            self.state = "awaiting_answer"
            yield self._say(text)
            yield ServerEvent(type="await_answer", text=text)
            yield self._state()
            return

        self.cursor += 1
        seg = self.current
        if seg is None:
            self.state = "done"
            yield ServerEvent(type="done", text="That's the end of the lecture.")
            yield self._state()
            return

        yield ServerEvent(type="slide", slide=seg.slide)
        yield ServerEvent(type="segment", segment=seg)
        yield self._say(seg.speech)

        if seg.kind == "question":
            self.state = "awaiting_answer"
            yield ServerEvent(type="await_answer", text=seg.speech)
        elif seg.kind == "checkpoint":
            self.state = "checkpoint"
            yield ServerEvent(
                type="checkpoint",
                text="Want to keep going, or practise this a bit more?",
            )
        else:
            self.state = "playing"

        yield self._state()

    # ------------------------------------------------------------- grading

    def submit_answer(self, answer: str, grade: Grade) -> Iterator[ServerEvent]:
        seg = self.current
        yield self._heard(answer)
        yield ServerEvent(type="grade", grade=grade)

        self.bump_mastery(seg.concept_id if seg else None, grade.correct)
        yield self._say(grade.feedback_speech)

        if grade.correct:
            self._wrong_streak = 0
            self.state = "playing"
            yield self._state()
            return

        self._wrong_streak += 1

        # Two wrong in a row: stop drilling, offer the choice explicitly.
        if self._wrong_streak >= 2 or not grade.followup_question:
            self._wrong_streak = 0
            self.state = "checkpoint"
            yield ServerEvent(
                type="checkpoint",
                text="Want to keep going, or go over this one more time?",
            )
            yield self._state()
            return

        self._pending_followup = grade.followup_question
        self.state = "playing"
        yield self._state()

    # ---------------------------------------------------------- interrupts

    def interrupt(self, question: str) -> Iterator[ServerEvent]:
        """Freeze playback, stash where we were, hand the question to the caller.

        The caller runs grading.answer_interruption() and then calls
        deliver_interrupt_answer(). Splitting it keeps this class I/O-free.
        """
        seg = self.current
        resume_text = last_sentence(seg.speech) if seg else ""
        self._stack.append((self.cursor, resume_text))
        self.state = "interrupted"
        yield self._heard(question)
        yield self._state()

    def deliver_interrupt_answer(self, answer_speech: str) -> Iterator[ServerEvent]:
        yield self._say(answer_speech)
        yield ServerEvent(
            type="segment",
            segment=Segment(
                id=f"interrupt_{len(self._stack)}",
                slide=self.current.slide if self.current else 0,
                kind="explain",
                depth=self.depth,
                speech=answer_speech,
                annotations=[],
            ),
        )
        yield self._state()

    def resume(self) -> Iterator[ServerEvent]:
        """Pop the stack and re-enter with the last sentence we were speaking."""
        if not self._stack:
            yield from self.advance()
            return

        cursor, resume_text = self._stack.pop()
        self.cursor = cursor
        seg = self.current

        if resume_text:
            bridge = f"So, where we were. {resume_text}"
            yield ServerEvent(
                type="segment",
                segment=Segment(
                    id=f"{seg.id}_resume" if seg else "resume",
                    slide=seg.slide if seg else 0,
                    kind="explain",
                    depth=self.depth,
                    speech=bridge,
                    annotations=[],
                ),
            )
            yield self._say(bridge)

        if seg and seg.kind == "question":
            self.state = "awaiting_answer"
            yield ServerEvent(type="await_answer", text=seg.speech)
        else:
            self.state = "playing"
        yield self._state()

    # ------------------------------------------------------------ dispatch

    def handle(self, event: ClientEvent) -> Iterator[ServerEvent]:
        """Everything except the two paths that need a Gemini call.

        `answer` and `interrupt` are handled by the server, which calls
        submit_answer() / interrupt() around the model call.
        """
        if event.type == "start":
            self.build_playlist()
            self.cursor = -1
            if not self.playlist:
                yield ServerEvent(
                    type="error",
                    text="No segments at this depth. Script generation may have failed.",
                )
                return
            yield from self.advance()

        elif event.type == "set_depth" and event.depth is not None:
            self.set_depth(event.depth)
            yield from self.advance()

        elif event.type == "next":
            self._pending_followup = ""
            yield from self.advance()

        elif event.type == "prev":
            self.cursor = max(-1, self.cursor - 2)
            yield from self.advance()

        elif event.type == "continue":
            self._pending_followup = ""
            yield from self.advance()

        elif event.type == "practice_more":
            # Re-ask the current question rather than moving on.
            self.cursor -= 1
            yield from self.advance()

        elif event.type == "skip_question" and self.state == "awaiting_answer":
            self._pending_followup = ""
            yield from self.advance()

        elif event.type == "resume":
            yield from self.resume()

        elif event.type == "ping":
            yield self._state()
