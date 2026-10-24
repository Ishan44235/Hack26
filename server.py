"""FastAPI app: upload, script generation, and the live session WebSocket.

Run:  uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import traceback
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import grading
import scriptgen
import store
import tts
import voices
from config import AUDIO_CACHE_DIR, UPLOAD_DIR
from ingest import ingest
from models import ClientEvent, ServerEvent, TutorProfile
from session import TutorSession

app = FastAPI(title="Prof. MKR | Personal Tutor")

STATIC_DIR = Path(__file__).parent / "static"
_GENERATING: set[tuple[str, str]] = set()
_GENERATION_LOCK = threading.RLock()
_GENERATION_CANCELLATIONS: dict[tuple[str, str], threading.Event] = {}


def _lecture_status(course_id: str, lecture_id: str) -> dict:
    key = (course_id, lecture_id)
    with _GENERATION_LOCK:
        status = store.lecture_status(course_id, lecture_id)
        active = key in _GENERATING
        cancel_requested = bool(
            _GENERATION_CANCELLATIONS.get(key)
            and _GENERATION_CANCELLATIONS[key].is_set()
        )
    status["generating"] = active
    status["cancel_requested"] = cancel_requested
    status["retryable"] = not active and (
        bool(status.get("error")) or 0 < status["scripted"] < status["slides"]
    )
    return status


def _queue_generation(background: BackgroundTasks, key: tuple[str, str]) -> None:
    with _GENERATION_LOCK:
        if key in _GENERATING:
            raise HTTPException(409, "Script generation is already running for this lecture.")
        _GENERATING.add(key)
        _GENERATION_CANCELLATIONS[key] = threading.Event()
        background.add_task(_generate_scripts_job, *key)


# ---------------------------------------------------------------- courses

@app.get("/api/courses")
def api_list_courses():
    return [c.model_dump() for c in store.list_courses()]


@app.post("/api/courses")
def api_create_course(name: str = Form(...)):
    return store.create_course(name).model_dump()


@app.get("/api/voices")
def api_voices():
    """The ElevenLabs catalogue the settings panel renders."""
    return voices.as_json()


@app.get("/api/voices/{voice_id}/preview")
def api_voice_preview(voice_id: str):
    if not voices.is_allowed(voice_id):
        raise HTTPException(404, "unknown voice")
    try:
        path, _ = tts.synth(
            "Hello. I will guide you through this lesson, one idea at a time.",
            voice_id,
            1.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Voice preview unavailable: {exc}") from exc
    return FileResponse(path, media_type="audio/mpeg")


@app.patch("/api/courses/{course_id}")
def api_rename_course(course_id: str, body: dict):
    course = store.load_course(course_id)
    name = str(body.get("name", "")).strip()
    if name:
        course.name = name
    if "color" in body:
        course.color = int(body["color"])
    store.save_course(course)
    return course.model_dump()


@app.delete("/api/courses/{course_id}")
def api_delete_course(course_id: str):
    with _GENERATION_LOCK:
        course = store.load_course(course_id)  # 404s if it is already gone
        for lecture_id in course.lecture_ids:
            cancellation = _GENERATION_CANCELLATIONS.get((course_id, lecture_id))
            if cancellation is not None:
                cancellation.set()
        store.delete_course(course_id)
    return {"deleted": course_id}


@app.put("/api/courses/{course_id}/profile")
def api_update_profile(course_id: str, profile: TutorProfile):
    course = store.load_course(course_id)
    if not voices.is_allowed(profile.voice_id):
        raise HTTPException(400, f"unknown voice id {profile.voice_id!r}")
    course.profile = profile
    store.save_course(course)
    return course.model_dump()


@app.get("/api/courses/{course_id}/lectures")
def api_list_lectures(course_id: str):
    course = store.load_course(course_id)
    return [
        _lecture_status(course_id, lid) | {"id": lid}
        for lid in course.lecture_ids
    ]


# ----------------------------------------------------------------- upload

def _generate_scripts_job_impl(
    course_id: str, lecture_id: str, cancellation: threading.Event
) -> None:
    """Runs in a background thread. Saves after every slide so the UI can poll."""
    with _GENERATION_LOCK:
        if cancellation.is_set():
            return
        course = store.load_course(course_id)
        lecture = store.load_lecture(course_id, lecture_id)

    for i in range(len(lecture.slides)):
        if cancellation.is_set():
            return
        if i < len(lecture.scripts) and lecture.scripts[i].segments:
            continue
        try:
            # Pass what has been written so far, so the tutor can make real
            # callbacks instead of treating every slide as an island.
            script = scriptgen.generate_slide_script(
                lecture, i, course.profile, prior=lecture.scripts
            )
        except Exception as exc:  # noqa: BLE001
            from models import SlideScript

            # Record it. Swallowing this leaves the UI polling a lecture that will
            # never become ready, with nothing on screen explaining why.
            if not lecture.error:
                lecture.error = f"slide {i}: {type(exc).__name__}: {exc}"
            traceback.print_exc()
            script = SlideScript(slide=i, concepts=[], segments=[])
        if len(lecture.scripts) <= i:
            lecture.scripts.extend(
                SlideScript(slide=index, concepts=[], segments=[])
                for index in range(len(lecture.scripts), i)
            )
            lecture.scripts.append(script)
        else:
            lecture.scripts[i] = script
        with _GENERATION_LOCK:
            if cancellation.is_set():
                return
            store.save_lecture(course_id, lecture)

    if cancellation.is_set():
        return
    # Warm the first few audio clips so the session starts instantly.
    segs = scriptgen.segments_at_depth(lecture, course.profile.depth)
    try:
        tts.prewarm(segs, course.profile.voice_id, 1.0, lookahead=4)
    except Exception:  # noqa: BLE001
        pass


def _generate_scripts_job(course_id: str, lecture_id: str) -> None:
    key = (course_id, lecture_id)
    with _GENERATION_LOCK:
        cancellation = _GENERATION_CANCELLATIONS.get(key)
    if cancellation is None:
        return
    try:
        _generate_scripts_job_impl(course_id, lecture_id, cancellation)
    finally:
        with _GENERATION_LOCK:
            if _GENERATION_CANCELLATIONS.get(key) is cancellation:
                _GENERATION_CANCELLATIONS.pop(key, None)
                _GENERATING.discard(key)


@app.post("/api/courses/{course_id}/lectures")
async def api_upload_lecture(
    course_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    instructions: str = Form(""),
):
    store.load_course(course_id)  # 404s early if the course doesn't exist

    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in {".pdf", ".pptx"}:
        raise HTTPException(400, "Upload a PDF or PPTX lecture.")

    dest = UPLOAD_DIR / f"{course_id}_{filename}"
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)

        fallback = Path(filename).stem
        lecture = ingest(dest, course_id, title=title or fallback, instructions=instructions)
        if not lecture.slides:
            raise HTTPException(422, "No slides were found in that file.")
        store.save_lecture(course_id, lecture)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read uploaded lecture: {exc}") from exc

    _queue_generation(background, (course_id, lecture.id))

    return {
        "lecture_id": lecture.id,
        "slides": len(lecture.slides),
        "status": "generating",
    }


@app.get("/api/courses/{course_id}/lectures/{lecture_id}/status")
def api_lecture_status(course_id: str, lecture_id: str):
    with _GENERATION_LOCK:
        course = store.load_course(course_id)
        if lecture_id not in course.lecture_ids:
            raise HTTPException(404, "Lecture not found.")
        return _lecture_status(course_id, lecture_id)


@app.post("/api/courses/{course_id}/lectures/{lecture_id}/retry")
def api_retry_lecture(course_id: str, lecture_id: str, background: BackgroundTasks):
    with _GENERATION_LOCK:
        course = store.load_course(course_id)
        if (course_id, lecture_id) in _GENERATING:
            raise HTTPException(409, "Script generation is already running for this lecture.")
        if lecture_id not in course.lecture_ids:
            raise HTTPException(404, "Lecture not found.")
        lecture = store.load_lecture(course_id, lecture_id)
        if not lecture.slides:
            raise HTTPException(400, "This lecture has no slides to generate.")
        lecture.error = ""
        store.save_lecture(course_id, lecture)
        _queue_generation(background, (course_id, lecture_id))
    return {"lecture_id": lecture_id, "status": "generating"}


@app.post("/api/courses/{course_id}/lectures/{lecture_id}/cancel")
def api_cancel_lecture_generation(course_id: str, lecture_id: str):
    key = (course_id, lecture_id)
    with _GENERATION_LOCK:
        course = store.load_course(course_id)
        if lecture_id not in course.lecture_ids:
            raise HTTPException(404, "Lecture not found.")
        status = store.lecture_status(course_id, lecture_id)
        if status["ready"] or status["failed"]:
            raise HTTPException(409, "Lecture generation has already finished.")
        cancellation = _GENERATION_CANCELLATIONS.get(key)
        if cancellation is None or key not in _GENERATING:
            raise HTTPException(409, "Lecture generation is not running.")
        cancellation.set()
        lecture = store.load_lecture(course_id, lecture_id)
        lecture.error = "Generation cancelled by user."
        store.save_lecture(course_id, lecture)
    return {"lecture_id": lecture_id, "status": "cancelling"}


@app.delete("/api/courses/{course_id}/lectures/{lecture_id}")
def api_delete_lecture(course_id: str, lecture_id: str):
    key = (course_id, lecture_id)
    with _GENERATION_LOCK:
        course = store.load_course(course_id)
        if lecture_id not in course.lecture_ids:
            raise HTTPException(404, "Lecture not found.")
        cancellation = _GENERATION_CANCELLATIONS.get(key)
        if cancellation is not None:
            cancellation.set()
        store.delete_lecture(course_id, lecture_id)
    return {"deleted": lecture_id}


@app.get("/api/courses/{course_id}/lectures/{lecture_id}/slide/{index}")
def api_slide_image(course_id: str, lecture_id: str, index: int):
    lecture = store.load_lecture(course_id, lecture_id)
    if index < 0 or index >= len(lecture.slides):
        raise HTTPException(404, "no such slide")
    slide = lecture.slides[index]
    if not slide.image_path or not Path(slide.image_path).exists():
        raise HTTPException(404, "slide has no rendered image")
    return FileResponse(slide.image_path, media_type="image/png")


@app.get("/api/audio/{key}")
def api_audio(key: str):
    path = AUDIO_CACHE_DIR / f"{key}.mp3"
    if not path.exists():
        raise HTTPException(404, "not synthesised")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/agent-url")
def api_agent_url():
    """Signed WS URL for the live ElevenLabs agent path."""
    try:
        return {"signed_url": tts.signed_agent_url()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=503)


# -------------------------------------------------------------- websocket

async def _send(ws: WebSocket, ev: ServerEvent) -> None:
    await ws.send_text(ev.model_dump_json())


async def _speak(ws: WebSocket, text: str, voice_id: str, rate: float) -> None:
    """Synthesise off the event loop and push the audio URL to the browser."""
    if not text.strip():
        return
    try:
        path, dur = await asyncio.to_thread(tts.synth, text, voice_id, rate)
    except Exception as exc:  # noqa: BLE001
        await _send(ws, ServerEvent(type="error", text=f"TTS failed: {exc}"))
        return
    await _send(
        ws,
        ServerEvent(
            type="audio",
            audio_url=f"/api/audio/{path.stem}",
            duration_ms=dur,
            text=text,
        ),
    )


@app.websocket("/ws/{course_id}/{lecture_id}")
async def ws_session(ws: WebSocket, course_id: str, lecture_id: str):
    await ws.accept()

    try:
        course = store.load_course(course_id)
        lecture = store.load_lecture(course_id, lecture_id)
    except Exception as exc:  # noqa: BLE001
        await _send(ws, ServerEvent(type="error", text=f"load failed: {exc}"))
        await ws.close()
        return

    sess = TutorSession(course=course, lecture=lecture, depth=course.profile.depth)
    voice = course.profile.voice_id

    async def drain(events) -> None:
        """Emit server events, synthesising audio for anything the tutor says.

        Audio tags go to the synthesiser; the transcript never shows them.
        """
        for ev in events:
            if ev.type == "transcript" and ev.role == "tutor":
                spoken = ev.text
                ev = ev.model_copy(update={"text": tts.for_display(ev.text)})
                await _send(ws, ev)
                await _speak(ws, spoken, voice, 1.0)
            else:
                await _send(ws, ev)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = ClientEvent.model_validate_json(raw)
            except Exception:  # noqa: BLE001
                await _send(ws, ServerEvent(type="error", text="malformed event"))
                continue

            if event.type == "answer":
                seg = sess.current
                if seg is None or sess.state != "awaiting_answer":
                    await _send(ws, ServerEvent(type="error", text="not awaiting an answer"))
                    continue
                try:
                    grade = await asyncio.to_thread(
                        grading.grade_answer,
                        seg,
                        event.text,
                        course.profile,
                        sess.slide_text(),
                    )
                except Exception as exc:  # noqa: BLE001
                    await _send(ws, ServerEvent(type="error", text=f"Answer grading failed: {exc}"))
                    continue
                await drain(sess.submit_answer(event.text, grade))
                store.save_course(course)  # persist mastery
                # No auto-advance: the browser sends 'next' when the feedback
                # clip finishes playing.

            elif event.type == "interrupt":
                await drain(sess.interrupt(event.text))
                try:
                    answer = await asyncio.to_thread(
                        grading.answer_interruption,
                        event.text,
                        sess.current,
                        sess.slide_text(),
                        course.profile,
                        sess.transcript,
                    )
                except Exception as exc:  # noqa: BLE001
                    await _send(
                        ws,
                        ServerEvent(type="error", text=f"Could not answer that interruption: {exc}"),
                    )
                    await drain(sess.resume())
                    continue
                await drain(sess.deliver_interrupt_answer(answer))

            else:
                # Exactly one segment per request. The browser drives the next
                # one from the audio 'ended' event, which keeps speech, ink and
                # the transcript on a single clock. Do NOT auto-advance here —
                # a second clip pushed early overwrites the first mid-sentence.
                await drain(sess.handle(event))

    except WebSocketDisconnect:
        store.save_course(course)
    except Exception as exc:  # noqa: BLE001
        try:
            await _send(ws, ServerEvent(type="error", text=str(exc)))
        except Exception:  # noqa: BLE001
            pass


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
