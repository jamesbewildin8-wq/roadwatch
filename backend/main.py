"""FastAPI server.

    uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000

Every endpoint that could carry an enforcement-position claim is filtered
through backend/policy.py before it returns. See that module for why the gate
lives here rather than in the frontend.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .policy import (PolicyBook, allowed_verdicts, gate_camera, gate_event,
                     source_authorized)
from .poller import Poller
from .sources import Camera
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
CAMERAS_FILE = ROOT / "cameras.json"
FIXED_FILE = ROOT / "data" / "fixed_cameras.geojson"
JURIS_FILE = ROOT / "data" / "jurisdictions.json"
FRONTEND = ROOT / "app"

DEFAULT_CFG = {
    "interval_s": 2.0,       # demo feeds are fast; use 30-60 for real snapshots
    "detector": "auto",
    "still_threshold": 4,    # frames still before a track counts as dwelling
    "confirm_frames": 8,     # frames still before the event is confirmed
    "max_distance": 70.0,
    "still_radius": 12.0,
    "default_country": "MY",
    # "personal" | "published". Publishing invalidates the personal-use
    # authorisation basis and the data-protection exclusions it leans on.
    "deployment": "personal",
    # Frames are capped to this width before detection. Raising it costs memory
    # and CPU quadratically and buys almost nothing for dwell detection.
    "max_frame_width": 640,
}


def load_config() -> tuple[list[Camera], dict]:
    raw = json.loads(CAMERAS_FILE.read_text())
    cfg = {**DEFAULT_CFG, **raw.get("config", {})}
    fields = set(Camera.__dataclass_fields__)
    cams = [
        Camera(**{k: v for k, v in c.items() if k in fields})
        for c in raw["cameras"]
        if c.get("enabled", True)
    ]
    if not cams:
        raise RuntimeError("no enabled cameras in cameras.json")
    return cams, cfg


state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cams, cfg = load_config()
    book = PolicyBook(JURIS_FILE)
    store = Store(ROOT / "data" / "events.db")
    poller = Poller(cams, store, cfg, book=book)
    state.update(store=store, poller=poller, cfg=cfg, book=book,
                 country=cfg["default_country"])
    await poller.start()
    print(f"[roadwatch] polling {len(cams)} camera(s) every {cfg['interval_s']}s")
    print(f"[roadwatch] active jurisdiction: "
          f"{book.get(state['country'], cfg['deployment']).name} "
          f"(deployment={cfg['deployment']})")
    yield
    await poller.stop()


app = FastAPI(title="roadwatch", lifespan=lifespan)


def deployment() -> str:
    return state["cfg"].get("deployment", "personal")


def current_policy():
    return state["book"].get(state["country"], deployment())


# ---------------------------------------------------------------- jurisdiction

@app.get("/api/jurisdictions")
def jurisdictions():
    """Every reviewed country, plus which one is active."""
    return {"active": state["country"], "deployment": deployment(),
            "countries": state["book"].countries(deployment())}


@app.get("/api/policy")
def policy():
    p = current_policy()
    return {**p.as_dict(), "allowed_verdicts": allowed_verdicts(p)}


@app.post("/api/country")
def set_country(code: str):
    """Switch jurisdiction.

    Any country code is accepted, including one with no entry - it resolves to
    the locked-down default rather than being rejected. Refusing unknown codes
    would tempt a future caller into reading 'accepted' as 'permitted'.
    """
    state["country"] = code.upper()
    p = current_policy()
    return {"active": state["country"], "policy": p.as_dict(),
            "allowed_verdicts": allowed_verdicts(p)}


# --------------------------------------------------------------------- cameras

@app.get("/api/cameras")
def cameras():
    p = current_policy()
    country = state["country"]
    out = []
    for w in state["poller"].workers.values():
        # A camera in another country is not this jurisdiction's business.
        if w.cam.country and w.cam.country.upper() != country:
            continue
        out.append(gate_camera(w.status(), p))
    return {"config": state["cfg"], "policy": p.as_dict(), "cameras": out}


@app.get("/api/cameras/{camera_id}/frame")
def frame(camera_id: str):
    w = state["poller"].workers.get(camera_id)
    if not w:
        raise HTTPException(404, "unknown camera")
    if w.cam.country and w.cam.country.upper() != state["country"]:
        raise HTTPException(403, "camera is outside the active jurisdiction")
    if not current_policy().camera_ingest:
        raise HTTPException(403, "camera ingest is disabled for this jurisdiction")
    ok, why = source_authorized(w.cam, state["cfg"].get("deployment", "personal"))
    if not ok:
        raise HTTPException(403, f"feed not authorised for automated access: {why}")
    if not w.last_frame_b64:
        raise HTTPException(503, "no frame yet")
    return {"id": camera_id, "jpeg_b64": w.last_frame_b64, "at": w.last_ok}


# ---------------------------------------------------------------------- events

@app.get("/api/events")
def events(limit: int = 60, camera_id: str | None = None):
    p = current_policy()
    country = state["country"]
    cams = state["poller"].workers
    raw = state["store"].recent(limit, camera_id)

    out = []
    for e in raw:
        w = cams.get(e["camera_id"])
        if w and w.cam.country and w.cam.country.upper() != country:
            continue
        g = gate_event(e, p)
        if g is not None:
            out.append(g)

    local_ids = [cid for cid, w in cams.items()
                 if not w.cam.country or w.cam.country.upper() == country]
    # With alerts off, even a bare count is a claim that something was detected.
    # Report zeroes rather than a number the policy says we may not stand behind.
    stats = (state["store"].stats(local_ids) if p.live_alerts
             else {"n": 0, "confirmed": 0, "fp": 0, "labelled": 0})
    return {
        "events": out,
        "stats": stats,
        "suppressed": not p.live_alerts,
        "allowed_verdicts": allowed_verdicts(p),
    }


@app.post("/api/events/{event_id}/verdict")
def verdict(event_id: int, value: str):
    """Label an event. Everything you label here becomes the dataset you'd
    need to train an actual classifier later."""
    p = current_policy()
    if not p.live_alerts:
        raise HTTPException(
            403, f"labelling is disabled in {p.name} - live alerts are off")
    allowed = allowed_verdicts(p)
    if value not in allowed:
        raise HTTPException(
            400,
            f"verdict {value!r} not permitted in {p.name}. Allowed: {allowed}",
        )
    state["store"].set_verdict(event_id, value)
    return {"ok": True, "id": event_id, "verdict": value}


# ----------------------------------------------------------------------- fixed

@app.get("/api/fixed")
def fixed():
    """Static enforcement points - fixed speed cameras, red-light cameras.

    Needs no computer vision at all, and it's where most of the practical value
    of this app actually sits. Gated separately from live alerts because plenty
    of jurisdictions treat published camera locations and live police positions
    very differently.
    """
    p = current_policy()
    empty = {"type": "FeatureCollection", "features": []}
    if not p.fixed_cameras or not FIXED_FILE.exists():
        return JSONResponse(empty)

    gj = json.loads(FIXED_FILE.read_text())
    feats = [
        f for f in gj.get("features", [])
        if (f.get("properties", {}).get("country", state["country"]).upper()
            == state["country"])
    ]
    return JSONResponse({"type": "FeatureCollection", "features": feats})


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# Mounted at root so the same relative URLs ("api/policy", "trip.js",
# "cameras.geojson") resolve whether the app is served by this backend or
# dropped on static hosting with no backend at all. Absolute paths would work
# in one case and break in the other.
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="app")
