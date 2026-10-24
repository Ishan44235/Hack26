"""Exercise the session FSM with no API keys and no network."""

import sys

sys.path.insert(0, ".")

from models import (  # noqa: E402
    Annotation, Course, Depth, Grade, Lecture, Segment, Slide, SlideScript,
)
from session import TutorSession, last_sentence  # noqa: E402


def seg(sid, slide, kind, depth, speech, rubric=None, cid=None):
    return Segment(
        id=sid, slide=slide, kind=kind, depth=depth, speech=speech,
        rubric=rubric or [], concept_id=cid,
        annotations=[Annotation(text="Re = rho v L / mu", x=0.6, y=0.8, at_ms=1200)],
    )


lecture = Lecture(
    id="lec1", title="Viscous Flow", source_file="x.pdf",
    slides=[Slide(index=0, text="Reynolds number"), Slide(index=1, text="Transition")],
    scripts=[
        SlideScript(slide=0, segments=[
            seg("s0_d2_1", 0, "explain", Depth.NORMAL, "Reynolds number is a ratio. It compares two forces."),
            seg("s0_d2_2", 0, "question", Depth.NORMAL, "What two forces does it compare?",
                ["names inertial forces", "names viscous forces"], "reynolds-number"),
            seg("s0_d1_1", 0, "explain", Depth.SKIM, "Re is inertial over viscous."),
        ]),
        SlideScript(slide=1, segments=[
            seg("s1_d2_1", 1, "explain", Depth.NORMAL, "Transition happens around Re of five hundred thousand."),
        ]),
    ],
)

course = Course(id="c1", name="AERSP 311")
s = TutorSession(course=course, lecture=lecture, depth=Depth.NORMAL)


def run(events, label):
    print(f"\n--- {label}")
    for ev in events:
        bits = [ev.type]
        if ev.text:
            bits.append(repr(ev.text[:58]))
        if ev.state:
            bits.append(f"state={ev.state}")
        if ev.slide is not None:
            bits.append(f"slide={ev.slide}")
        print("   ", " ".join(bits))


from models import ClientEvent  # noqa: E402

run(s.handle(ClientEvent(type="start")), "start")
assert s.playlist and len(s.playlist) == 3, f"playlist={len(s.playlist)}"
print("    playlist size:", len(s.playlist), "(depth 2 only)")

run(s.advance(), "advance to the question")
assert s.state == "awaiting_answer", s.state

# Wrong answer -> followup queued
wrong = Grade(correct=False, matched=["names viscous forces"],
              missed=["names inertial forces"],
              misconception="thinks it is viscous over gravity",
              feedback_speech="You had viscous right. The other one is inertial.",
              followup_question="Which force dominates at high speed?")
run(s.submit_answer("viscous and gravity", wrong), "wrong answer")
assert s._pending_followup, "followup should be queued"
assert course.mastery["reynolds-number"] < 0.3, course.mastery

run(s.advance(), "followup asked")
assert s.state == "awaiting_answer", s.state

right = Grade(correct=True, matched=["names inertial forces", "names viscous forces"],
              feedback_speech="Exactly, inertial over viscous.")
run(s.submit_answer("inertial over viscous", right), "right answer")
assert course.mastery["reynolds-number"] > 0.1, course.mastery
print("    mastery:", course.mastery)

# Interrupt mid-explain
run(s.advance(), "next explain segment")
before_cursor = s.cursor
run(s.interrupt("wait, why five hundred thousand?"), "interrupt")
assert s.state == "interrupted"
assert len(s._stack) == 1
run(s.deliver_interrupt_answer("It is the flat-plate critical value."), "answer the interrupt")
run(s.resume(), "resume")
assert s.cursor == before_cursor, (s.cursor, before_cursor)
print("    resumed at same cursor:", s.cursor)

# Depth switch keeps us on the same slide
s2 = TutorSession(course=course, lecture=lecture, depth=Depth.NORMAL)
s2.build_playlist()
s2.cursor = 1  # on slide 0
s2.set_depth(Depth.SKIM)
run(s2.advance(), "switch to depth 1")
assert s2.current and s2.current.depth == Depth.SKIM, s2.current
print("    depth-1 segment:", s2.current.id)

skip = TutorSession(course=Course(id="c2", name="Skip test"), lecture=lecture)
list(skip.handle(ClientEvent(type="start")))
list(skip.handle(ClientEvent(type="next")))
assert skip.state == "awaiting_answer" and skip.current.kind == "question"
before_mastery = dict(skip.course.mastery)
list(skip.handle(ClientEvent(type="skip_question")))
assert skip.current and skip.current.id == "s1_d2_1"
assert skip.course.mastery == before_mastery
print("    skipped question without grading or mastery change")

assert last_sentence("One. Two three. Four five six.") == "Four five six."
print("\nALL CHECKS PASSED")
