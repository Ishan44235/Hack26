# Prof. MKR

Upload a lecture. A per-course voice tutor teaches it out loud, writes on the slides
as it talks, asks you questions, and lets you cut it off mid-sentence to ask your own.

Gemini writes the lesson script and grades answers. ElevenLabs speaks. Everything
else is yours.

---

## Setup

```bash
cd tutor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add GOOGLE_API_KEY and ELEVENLABS_API_KEY
uvicorn server:app --reload --port 8000
```

Open http://localhost:8000. Create a course, set the tutor, upload a PDF.

Script generation is one Gemini call per slide and runs in the background — a 40-slide
deck takes a few minutes. The lecture list shows `n/m slides` until it's ready.
Cancel generation from a lecture card to stop after the current slide; completed slides
are kept so you can retry later. Delete a lecture from its card to remove it and its
uploaded source file (unless another lecture still uses it) and generated slide files
from the course.

Optional: `pip install mutagen` for exact audio durations. Without it, durations are
estimated from character count and annotation timing drifts on long clips.

PPTX needs LibreOffice on PATH to render slide images. Without it, PPTX falls back to
text-only — the tutor still works, the slide pane just shows no image.

---

## Architecture

```
upload ─> ingest.py ──> slide PNGs + extracted text
                          │
                          v
                    scriptgen.py ──> Gemini ──> lesson script JSON
                          │                      (3 depths, rubrics, annotations)
                          v
                      store.py (flat files)
                          │
   browser <══ WebSocket ═╪═ server.py
                          │     ├─ tts.py ──────> ElevenLabs ──> cached mp3
                          │     ├─ grading.py ──> Gemini (rubric check)
                          │     └─ session.py ──> playlist, interrupt stack, mastery
```

| File | Does |
|---|---|
| `models.py` | Pydantic schema. Doubles as Gemini's `response_schema`. |
| `ingest.py` | PDF/PPTX → slide PNGs + text. |
| `scriptgen.py` | Gemini → segments at depths 1/2/3, with rubrics and annotations. |
| `tts.py` | ElevenLabs synthesis, content-hash cache, signed agent URL. |
| `grading.py` | Rubric grading and interruption answering. |
| `session.py` | The state machine. No I/O — testable without keys. |
| `store.py` | Flat-file persistence. |
| `server.py` | FastAPI + WebSocket. |
| `static/` | The four-pane GUI. |

### Three decisions worth knowing

**All three depths are generated up front.** A mid-session depth change is a playlist
filter, not a regeneration round-trip. Costs more tokens at ingest, costs nothing live.

**One segment per request.** The browser asks for the next segment when the current
audio clip ends. The server never pushes ahead — two clips in flight means the second
overwrites the first mid-sentence. This bit me once already.

**Annotations are scheduled against `audio.currentTime`, not wall-clock.** Pause, seek
and interrupt all stay in sync for free.

### Handwriting

Not real stroke-by-stroke handwriting. The text renders in Caveat and a clip rectangle
animates left-to-right, so glyphs appear as if being written. Looks right, costs
nothing, never breaks. Real handwriting synthesis is a multi-day rabbit hole — don't.

### Interrupts

Typing or speaking while the tutor talks fires `interrupt`:

1. Audio stops, annotation **freezes mid-stroke** (erasing reads as a crash)
2. Segment + resume point pushed onto a stack
3. Gemini answers in context, spoken in the same voice
4. On resume, the **last sentence** of the interrupted segment replays

Never resume mid-word. One sentence back feels natural; mid-word feels broken.

---

## What you need to implement yourself

Roughly in the order it'll hurt if you skip it.

### 1. Voice IDs — 10 minutes, blocking
The default `voice_id` in `TutorProfile` is a placeholder. Pull real ones from your
ElevenLabs dashboard and put a picker in the setup screen instead of a text input.
Nothing speaks correctly until you do this.

### 2. Verify the model and voice IDs actually exist — 10 minutes, blocking
`ELEVEN_TTS_MODEL` defaults to `eleven_turbo_v2_5`; Gemini defaults to
`gemini-flash-latest`. Both were current when this was written. Hit each API once
before the event — a retired model id is a five-minute fix during setup and a dead
demo on stage.

### 3. Tune the rubrics — this is the whole project
`scriptgen.SYSTEM` tells Gemini how to write rubrics and `grading.GRADE_SYSTEM` tells
it how to check them. Generate scripts from a real lecture of yours, read the rubrics,
and answer questions deliberately wrong in three ways: wrong, right-but-informal, and
right-but-incomplete. If informal-but-right gets marked wrong, loosen
`GRADE_SYSTEM`. If plainly wrong gets marked right, tighten the rubric instructions in
`scriptgen.SYSTEM`.

A tutor that accepts a wrong answer is the failure judges notice in the first minute.
Budget real hours here, not minutes.

### 4. Annotation placement — a few hours
Gemini places annotations by reading the slide image for whitespace. It will sometimes
put text over a figure. Options, cheapest first:
- Reserve a right-hand margin and force all annotations into it (`x` clamped to >0.78)
- Use PyMuPDF `page.get_text("blocks")` to find real empty rectangles and pass them to
  Gemini as allowed zones
- Collision-detect in `drawAnnotation()` and nudge down

Do the first one for the hackathon. It looks deliberate and takes ten minutes.

### 5. Barge-in detection — half a day, highest value
Right now an interrupt is a button press or typing. True barge-in means detecting that
you *started speaking over* the tutor. Add a WebAudio `AnalyserNode` on the mic stream,
threshold the RMS, and fire `submitInput()` when it stays above threshold for ~200ms
while `state.tutorSpeaking` is true.

This is the moment that wins the sponsor track. A judge **will** try to interrupt it.

### 6. Speech recognition quality — an hour
`app.js` uses the browser's `SpeechRecognition`. Free, zero setup, Chrome-only, and it
mangles technical vocabulary. For a demo on engineering content, swap to ElevenLabs
Scribe realtime and pass your lecture's key terms via the `keyterms` parameter — it
biases the model toward a term list, which is exactly the problem you have.

### 7. The live-agent path — half a day, optional
`tts.signed_agent_url()` and `GET /api/agent-url` are wired but unused. The scripted
TTS path is more reliable for a demo and easier to keep in sync with annotations. Build
the agent path only if you've got time after everything else works, and keep the
scripted path as the fallback you actually demo.

### 8. Session resume and export — an hour each
Mastery persists per course but the cursor doesn't. Add `last_segment_id` to `Course`.
Export is the end-of-session artifact: filter the transcript to wrong answers and emit
a study sheet of just those.

### 9. Auth, rate limits, cost caps — skip for a hackathon
No auth anywhere. Anyone who can reach the port can read every course. Fine for a
laptop on stage, not fine deployed. Also put a token ceiling on script generation
before you point it at a 200-slide deck.

---

## Build order

Steps 1–4 are a complete working tutor with no voice at all. That's your fallback demo
if ElevenLabs falls over at 2am.

1. Course folders, upload, slide extraction
2. Script generation, one depth
3. Slide viewer with annotation animation — silent, scripted playback
4. Transcript pane + typed input, text-only tutor
5. ElevenLabs voice on top
6. Interruption
7. Per-course profiles, depth switching, mastery

---

## Testing without burning API credits

`session.py` has no I/O, so the state machine tests with plain objects. For the HTTP
layer, monkeypatch `scriptgen.generate_slide_script` and `server._speak` — that's how
the plumbing here was verified without a key:

```python
scriptgen.generate_slide_script = lambda lecture, i, profile: SlideScript(...)
tts.prewarm = lambda *a, **k: None
```

Verified so far: the FSM (wrong→followup→correct, interrupt→resume to the same cursor,
depth switch staying on-slide), course and profile CRUD, upload → 3 slides → PNGs
served, and one segment per WebSocket request. **Not** verified: any real Gemini or
ElevenLabs call, or anything in the browser.

---

## One housekeeping note

HackPSU's rules say projects should be original work created on site — coming in with
an idea is fine, bringing an existing project is not. If you're building this before
the event, check with an organizer about what counts. A repo with commits predating the
hackathon is the kind of thing that gets noticed at judging.
