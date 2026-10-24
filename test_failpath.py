"""Script generation failing for every slide must surface, not poll forever."""
import sys
sys.path.insert(0, ".")

import pymupdf
from fastapi.testclient import TestClient
import scriptgen, tts

def boom(lecture, i, profile, prior=None):
    raise RuntimeError("GOOGLE_API_KEY is not set")

scriptgen.generate_slide_script = boom
tts.prewarm = lambda *a, **k: None

import server
client = TestClient(server.app)

pdf = "./fail.pdf"
doc = pymupdf.open()
for n in range(2):
    doc.new_page().insert_text((72, 100), f"Slide {n}", fontsize=24)
doc.save(pdf); doc.close()

cid = client.post("/api/courses", data={"name": "Fail Test"}).json()["id"]
with open(pdf, "rb") as f:
    lid = client.post(f"/api/courses/{cid}/lectures",
                      files={"file": ("x.pdf", f, "application/pdf")}).json()["lecture_id"]

st = client.get(f"/api/courses/{cid}/lectures/{lid}/status").json()
print("status:", st)
assert st["ready"] is False
assert st["failed"] is True, "should report failure rather than staying pending"
assert "GOOGLE_API_KEY" in st["error"], st["error"]
print("\nfailure is surfaced with the real reason, so the UI can stop polling")
