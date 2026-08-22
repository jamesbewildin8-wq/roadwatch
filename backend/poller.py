"""The polling loop.

One worker per camera, each with its own detector state and tracker, because a
background model learned on one camera is meaningless on another.

Each tick: grab a frame -> detect -> track -> promote long dwells to events ->
cache an annotated JPEG for the UI.
"""

from __future__ import annotations

import asyncio
import base64
import time
import traceback

import cv2

from .detector import build_detector, warmup_for_interval
from .policy import source_authorized
from .sources import Camera, build_source
from .store import Store
from .tracker import DwellTracker

# Drawn in BGR.
COL_TRACK = (180, 180, 175)
COL_DWELL = (46, 46, 200)
COL_TEXT = (245, 245, 245)


class CameraWorker:
    def __init__(self, cam: Camera, store: Store, cfg: dict):
        self.cam = cam
        self.store = store
        self.cfg = cfg
        self.source = build_source(cam)
        # Warm-up is scaled to this camera's effective poll rate, not the
        # configured one - a real feed floored at 20s must not inherit a
        # warm-up sized for the 2s demo cadence.
        effective_interval = (cfg.get("interval_s", 2.0) if cam.kind == "demo"
                              else max(cfg.get("interval_s", 2.0),
                                       Poller.MIN_REAL_INTERVAL_S))
        self.detector = build_detector(
            cfg.get("detector", "auto"),
            warmup_frames=warmup_for_interval(effective_interval),
        )
        self.effective_interval = effective_interval
        self.tracker = DwellTracker(
            max_distance=cfg.get("max_distance", 70.0),
            still_radius=cfg.get("still_radius", 12.0),
            still_threshold=cfg.get("still_threshold", 4),
        )
        self.open_events: dict[int, int] = {}   # track_id -> event row id
        self.last_frame_b64: str | None = None
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.ticks = 0
        self.active: list[dict] = []

    @property
    def confirm_frames(self) -> int:
        return self.cfg.get("confirm_frames", 8)

    def _annotate(self, frame, tracks, show_dwell: bool = True):
        """Draw overlays.

        `show_dwell=False` is a real gate, not cosmetics. Stripping the dwell
        count from the JSON while still shipping a JPEG with a red box burned
        into the pixels leaks exactly the claim the policy forbids - the frame
        *is* the alert. When alerts are suppressed, nothing is drawn at all.
        """
        out = frame.copy()
        thr = self.tracker.still_threshold
        for t in (tracks if show_dwell else []):
            x, y, w, h = t.box
            dwelling = t.still_frames >= thr
            col = COL_DWELL if dwelling else COL_TRACK
            cv2.rectangle(out, (x, y), (x + w, y + h), col, 2 if dwelling else 1)
            if dwelling:
                # Frames, not seconds. Wall-clock age counts from when the
                # track first appeared, which for a vehicle that drove in and
                # then stopped is not the same as how long it has been still.
                cv2.rectangle(out, (x, y - 16), (x + 76, y), col, -1)
                cv2.putText(out, f"STILL {t.still_frames}f", (x + 3, y - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.36, COL_TEXT, 1,
                            cv2.LINE_AA)
        stamp = time.strftime("%H:%M:%S")
        cv2.putText(out, f"{self.cam.id}  {stamp}  [{self.detector.name}]",
                    (8, out.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    COL_TEXT, 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 72])
        return base64.b64encode(buf).decode() if ok else None

    def _downscale(self, frame):
        """Cap frame width before anything touches it.

        Dwell detection asks "is a vehicle-sized blob holding position", and
        that question is answered just as well at 640px wide as at 1920. The
        cost of not downscaling is steep and compounding: a 1080p frame is 9x
        the pixels of 640x360, so it is ~9x the memory for the background
        model (measured ~24MB per camera at 640x360, so ~200MB at 1080p) and
        roughly 9x the MOG2 work per tick. Five 1080p cameras will OOM a 1GB
        machine, and the OOM killer takes the whole process rather than one
        camera.
        """
        max_w = self.cfg.get("max_frame_width", 640)
        h, w = frame.shape[:2]
        if w <= max_w:
            return frame
        scale = max_w / w
        return cv2.resize(frame, (max_w, int(round(h * scale))),
                          interpolation=cv2.INTER_AREA)

    def tick(self, show_dwell: bool = True):
        """One synchronous poll cycle. Runs in a thread - OpenCV and requests
        both block."""
        frame = self.source.grab()
        if frame is not None:
            frame = self._downscale(frame)
        if frame is None:
            self.last_error = "no frame"
            return
        self.ticks += 1
        self.last_ok = time.time()
        self.last_error = None

        detections = self.detector.detect(frame)
        tracks = self.tracker.update(detections)

        thr = self.tracker.still_threshold
        for t in tracks:
            if t.still_frames >= thr:
                eid = self.open_events.get(t.id)
                if eid is None:
                    eid = self.store.open_event(self.cam.id, t)
                    if eid:
                        self.open_events[t.id] = eid
                else:
                    self.store.touch_event(eid, t)
                    # A dwell only becomes an "event worth telling someone
                    # about" once it's held for a good while. Short stops are
                    # traffic lights and queues.
                    if t.still_frames >= self.confirm_frames:
                        self.store.confirm(eid)
            elif t.id in self.open_events:
                del self.open_events[t.id]

        live_ids = {t.id for t in tracks}
        self.open_events = {k: v for k, v in self.open_events.items()
                            if k in live_ids}

        self.active = [t.as_dict(thr) for t in tracks]
        self.last_frame_b64 = self._annotate(frame, tracks, show_dwell)

    def status(self) -> dict:
        return {
            "id": self.cam.id,
            "country": self.cam.country,
            "authorization": self.cam.authorization,
            "authorization_note": self.cam.authorization_note,
            "authorized": source_authorized(
                self.cam, self.cfg.get("deployment", "personal"))[0],
            "authorization_reason": source_authorized(
                self.cam, self.cfg.get("deployment", "personal"))[1],
            "name": self.cam.name,
            "road": self.cam.road,
            "lat": self.cam.lat,
            "lon": self.cam.lon,
            "kind": self.cam.kind,
            "notes": self.cam.notes,
            "detector": self.detector.name,
            "warming": bool(getattr(self.detector, "warming", False)),
            "warmup_progress": list(getattr(self.detector, "warmup_progress", (0, 0))),
            "warmup_eta_s": max(0, int(
                (getattr(self.detector, "warmup_progress", (0, 0))[1]
                 - getattr(self.detector, "warmup_progress", (0, 0))[0])
                * self.effective_interval)),
            "interval_s": self.effective_interval,
            "ticks": self.ticks,
            "last_ok": self.last_ok,
            "error": self.last_error,
            "online": self.last_error is None and self.last_ok is not None,
            "tracks": len(self.active),
            "dwelling": sum(1 for t in self.active if t["dwelling"]),
            "active": self.active,
        }


class Poller:
    MIN_REAL_INTERVAL_S = 20.0

    def __init__(self, cameras: list[Camera], store: Store, cfg: dict,
                 book=None):
        self.cfg = cfg
        self.store = store
        # The policy book is consulted per tick, not once at startup, so
        # editing jurisdictions.json takes effect on the next poll without a
        # restart. Ingest is gated here rather than in the API layer because
        # "may we fetch this at all" is a different question from "may we show
        # what we found" - a country that forbids ingest should never have its
        # frames pulled in the first place.
        self.book = book
        self.workers = {c.id: CameraWorker(c, store, cfg) for c in cameras}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _run_one(self, worker: CameraWorker):
        interval = self.cfg.get("interval_s", 2.0)
        # Synthetic feeds cost nobody anything, so they run at whatever speed
        # is convenient. Real ones get a hard floor: the fastest way to turn a
        # "probably fine" hobby project into an actual complaint is to hit
        # somebody's server every two seconds. Most public snapshot endpoints
        # refresh far slower than this anyway, so polling harder buys you
        # duplicate frames and an IP ban.
        if worker.cam.kind != "demo":
            interval = max(interval, self.MIN_REAL_INTERVAL_S)
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                pol = (self.book.get(worker.cam.country,
                                     self.cfg.get("deployment", "personal"))
                       if self.book is not None else None)
                # Two independent gates, both of which must pass. Country
                # policy is a ceiling; per-source authorisation is the floor.
                # A feed in a permissive country whose operator forbids bots is
                # still off, and that is the common case, not the exotic one.
                ok_src, why = source_authorized(
                    worker.cam, self.cfg.get("deployment", "personal"))
                if pol is not None and not pol.camera_ingest:
                    worker.last_error = "ingest blocked by jurisdiction policy"
                    worker.last_frame_b64 = None
                elif not ok_src:
                    worker.last_error = f"not authorised: {why}"
                    worker.last_frame_b64 = None
                else:
                    show = pol.live_alerts if pol is not None else True
                    await asyncio.to_thread(worker.tick, show)
            except Exception:
                worker.last_error = traceback.format_exc(limit=1).strip()
            elapsed = time.perf_counter() - started
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.1, interval - elapsed)
                )
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self._stop.clear()
        self._task = asyncio.gather(
            *(self._run_one(w) for w in self.workers.values())
        )

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
