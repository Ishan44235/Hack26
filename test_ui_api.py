"""Exercise every endpoint the new frontend calls."""
import sys
sys.path.insert(0, ".")

import threading
import pymupdf
from fastapi.testclient import TestClient
import scriptgen, tts
from config import UPLOAD_DIR
from models import Annotation, Depth, Segment, SlideScript, Concept, Lecture, Slide

def fake_script(lecture, i, profile, prior=None):
    return SlideScript(slide=i,
        concepts=[Concept(id=f"concept-{i}", label=f"Concept {i}")],
        segments=[
            Segment(id=f"s{i}_d2_1", slide=i, kind="explain", depth=Depth.NORMAL,
                    speech=f"Teaching slide {i}.", teaches="an analogy",
                    concept_id=f"concept-{i}",
                    annotations=[Annotation(text="key", x=.6, y=.8, at_ms=400)]),
            Segment(id=f"s{i}_d2_2", slide=i, kind="question", depth=Depth.NORMAL,
                    speech="What was that?", rubric=["names it"], concept_id=f"concept-{i}"),
        ])

scriptgen.generate_slide_script = fake_script
tts.prewarm = lambda *a, **k: None
import server
c = TestClient(server.app)

pdf = "./ui.pdf"
doc = pymupdf.open()
for n in range(3):
    doc.new_page().insert_text((72, 100), f"Slide {n}", fontsize=24)
doc.save(pdf); doc.close()

print("GET  /api/voices           ->", end=" ")
v = c.get("/api/voices").json()
assert len(v) == 8 and v[0]["name"] and v[0]["accent"] and v[0]["description"]
print(f"{len(v)} voices with name/accent/description")

print("POST /api/courses          ->", end=" ")
cid = c.post("/api/courses", data={"name": "AERSP 311"}).json()["id"]
print(cid)

print("PATCH name + colour        ->", end=" ")
r = c.patch(f"/api/courses/{cid}", json={"name": "AERSP 410", "color": 2}).json()
assert r["name"] == "AERSP 410" and r["color"] == 2
print(f'{r["name"]}, colour {r["color"]}')

print("PUT  profile (good voice)  ->", end=" ")
prof = {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "speech_rate": 1.1, "depth": 3,
        "comfort_level": "solid", "encouragement_style": "coach",
        "persona_note": "aerospace examples"}
assert c.put(f"/api/courses/{cid}/profile", json=prof).status_code == 200
print("accepted (George)")

print("PUT  profile (bad voice)   ->", end=" ")
bad = dict(prof, voice_id="not-a-real-id")
assert c.put(f"/api/courses/{cid}/profile", json=bad).status_code == 400
print("rejected with 400")

print("POST lecture               ->", end=" ")
with open(pdf, "rb") as f:
    lid = c.post(f"/api/courses/{cid}/lectures",
                 files={"file": ("ui.pdf", f, "application/pdf")},
                 data={"title": "Nozzles", "instructions": "focus on derivations"}
                 ).json()["lecture_id"]
print(lid)

print("GET  lecture status        ->", end=" ")
st = c.get(f"/api/courses/{cid}/lectures/{lid}/status").json()
assert st["ready"] and st["slides"] == 3
assert st["depths"]["2"]["questions"] == 3, st["depths"]
assert st["depths"]["2"]["annotations"] == 3, st["depths"]
assert len(st["concepts"]) == 3, st["concepts"]
assert st["instructions"] == "focus on derivations"
assert st["source"].endswith(".pdf")
print(f'{st["slides"]} slides, {st["depths"]["2"]["questions"]} questions, '
      f'{len(st["concepts"])} concepts, source {st["source"]}')

print("GET  slide image           ->", end=" ")
img = c.get(f"/api/courses/{cid}/lectures/{lid}/slide/0")
assert img.status_code == 200 and img.headers["content-type"] == "image/png"
print(f"{len(img.content)} bytes PNG")

print("DELETE lecture              ->", end=" ")
assert c.delete(f"/api/courses/{cid}/lectures/{lid}").status_code == 200
assert all(item["id"] != lid for item in c.get(f"/api/courses/{cid}/lectures").json())
assert c.get(f"/api/courses/{cid}/lectures/{lid}/status").status_code == 404
assert not (UPLOAD_DIR / f"{cid}_ui.pdf").exists()
print("removed from the course with its unused uploaded source")

print("CANCEL + DELETE generation ->", end=" ")
pending = Lecture(
    id="pending-lecture",
    title="Pending",
    source_file="pending.pdf",
    slides=[Slide(index=0, text="Pending slide")],
)
server.store.save_lecture(cid, pending)
key = (cid, pending.id)
cancel = threading.Event()
server._GENERATING.add(key)
server._GENERATION_CANCELLATIONS[key] = cancel
try:
    stopped = c.post(f"/api/courses/{cid}/lectures/{pending.id}/cancel")
    assert stopped.status_code == 200 and cancel.is_set(), stopped.text
    status = c.get(f"/api/courses/{cid}/lectures/{pending.id}/status").json()
    assert status["generating"] and status["cancel_requested"], status
    assert server.store.load_lecture(cid, pending.id).error == "Generation cancelled by user."
    assert c.delete(f"/api/courses/{cid}/lectures/{pending.id}").status_code == 200
    assert cancel.is_set()
    assert not (server.store._lecture_path(cid, pending.id).parent.exists())
    server._generate_scripts_job(*key)
    assert pending.id not in server.store.load_course(cid).lecture_ids
finally:
    server._GENERATING.discard(key)
    server._GENERATION_CANCELLATIONS.pop(key, None)
print("cancellation requested and lecture files removed")

print("GET  static index.html     ->", end=" ")
page = c.get("/")
assert page.status_code == 200 and "Prof. MKR" in page.text
assert "/app.js" in page.text and "/style.css" in page.text
print("served, links app.js + style.css")

for asset in ("/app.js", "/style.css"):
    r = c.get(asset)
    assert r.status_code == 200 and len(r.text) > 1000, asset
    print(f"GET  {asset:<21} -> {len(r.text)} chars")

print("DELETE course              ->", end=" ")
assert c.delete(f"/api/courses/{cid}").status_code == 200
assert all(x["id"] != cid for x in c.get("/api/courses").json())
print("gone from the list")

print("\nALL UI API CHECKS PASSED")
