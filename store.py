"""Flat-file persistence. Swap for Postgres when you outgrow it.

Layout:
  data/courses/<course_id>/course.json
  data/courses/<course_id>/lectures/<lecture_id>/lecture.json
  data/courses/<course_id>/lectures/<lecture_id>/slides/000.png
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from config import COURSES_DIR, UPLOAD_DIR
from models import Course, Lecture, TutorProfile


def _course_path(course_id: str) -> Path:
    return COURSES_DIR / course_id / "course.json"


def _lecture_path(course_id: str, lecture_id: str) -> Path:
    return COURSES_DIR / course_id / "lectures" / lecture_id / "lecture.json"


def _read_json(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252")


def create_course(name: str, profile: TutorProfile | None = None) -> Course:
    course = Course(
        id=uuid.uuid4().hex[:12],
        name=name,
        profile=profile or TutorProfile(),
    )
    save_course(course)
    return course


def save_course(course: Course) -> None:
    p = _course_path(course.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(course.model_dump_json(indent=2), encoding="utf-8")


def load_course(course_id: str) -> Course:
    return Course.model_validate_json(_read_json(_course_path(course_id)))


def list_courses() -> list[Course]:
    out = []
    for d in sorted(COURSES_DIR.iterdir()) if COURSES_DIR.exists() else []:
        f = d / "course.json"
        if f.exists():
            try:
                out.append(Course.model_validate_json(_read_json(f)))
            except Exception:  # noqa: BLE001
                continue
    return out


def save_lecture(course_id: str, lecture: Lecture) -> None:
    p = _lecture_path(course_id, lecture.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(lecture.model_dump_json(indent=2), encoding="utf-8")

    course = load_course(course_id)
    if lecture.id not in course.lecture_ids:
        course.lecture_ids.append(lecture.id)
        save_course(course)


def delete_course(course_id: str) -> None:
    """Remove a course and everything under it. The UI confirms first."""
    d = COURSES_DIR / course_id
    if d.exists():
        shutil.rmtree(d)


def load_lecture(course_id: str, lecture_id: str) -> Lecture:
    return Lecture.model_validate_json(_read_json(_lecture_path(course_id, lecture_id)))


def delete_lecture(course_id: str, lecture_id: str) -> None:
    """Remove a lecture, its generated files, and its unused uploaded source."""
    course = load_course(course_id)
    if lecture_id not in course.lecture_ids:
        raise FileNotFoundError(f"Lecture {lecture_id!r} was not found in course {course_id!r}.")

    lecture = load_lecture(course_id, lecture_id)
    lecture_dir = _lecture_path(course_id, lecture_id).parent
    if lecture_dir.exists():
        shutil.rmtree(lecture_dir)
    course.lecture_ids.remove(lecture_id)
    save_course(course)

    source = Path(lecture.source_file)
    if (
        source.parent.resolve() == UPLOAD_DIR.resolve()
        and source.name.startswith(f"{course_id}_")
    ):
        still_used = any(
            Path(load_lecture(course_id, other_id).source_file) == source
            for other_id in course.lecture_ids
        )
        if not still_used:
            source.unlink(missing_ok=True)


def _display_name(course_id: str, source_file: str) -> str:
    """Uploads are stored as '<course_id>_<original>'; show the original."""
    name = Path(source_file).name
    prefix = f"{course_id}_"
    return name[len(prefix):] if name.startswith(prefix) else name


def lecture_status(course_id: str, lecture_id: str) -> dict:
    """Has script generation finished? The UI polls this after upload."""
    try:
        lec = load_lecture(course_id, lecture_id)
    except FileNotFoundError:
        return {"ready": False, "slides": 0, "scripted": 0}
    scripted = sum(1 for s in lec.scripts if s.segments)
    done = len(lec.scripts) == len(lec.slides)

    # Per-depth counts and concept list, so the lecture preview can show what
    # it is about before you commit to sitting through it.
    depths = {}
    for d in (1, 2, 3):
        segs = [sg for sc in lec.scripts for sg in sc.segments if int(sg.depth) == d]
        depths[str(d)] = {
            "segments": len(segs),
            "questions": sum(1 for sg in segs if sg.kind == "question"),
            "annotations": sum(len(sg.annotations) for sg in segs),
        }
    concepts = sorted({c.id for sc in lec.scripts for c in sc.concepts})

    return {
        "instructions": lec.instructions,
        "source": _display_name(course_id, lec.source_file),
        "depths": depths,
        "concepts": concepts,
        "ready": scripted > 0 and done,
        "slides": len(lec.slides),
        "scripted": scripted,
        "partial_failure": done and bool(lec.error) and scripted > 0,
        "title": lec.title,
        # Set when generation finished but produced nothing usable, so the UI
        # can stop polling and show the reason.
        "failed": done and scripted == 0,
        "error": lec.error,
    }
