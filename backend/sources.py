"""Camera source adapters.

Public road cameras come in roughly four flavours. Each adapter returns a single
BGR numpy frame from `grab()`, or None if the source is unreachable.

  snapshot : a JPEG/PNG URL that returns the current frame. Most government
             traffic cameras are this. Often needs a cache-buster query param.
  mjpeg    : multipart stream. Cheap to read, common on older municipal systems.
  hls      : .m3u8 stream. OpenCV can usually open these directly via ffmpeg.
  demo     : locally generated synthetic road scene. No network needed - use it
             to verify the pipeline works before you go hunting for real feeds.

On adding real sources: most operators publish feeds for public viewing but
their terms of service restrict automated polling and redistribution. Read them.
Poll politely (30-60s is plenty - the feed itself rarely updates faster), send a
real User-Agent, and don't rehost their imagery.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np
import requests

UA = "roadwatch-hobby/0.1 (personal research project)"


@dataclass
class Camera:
    id: str
    name: str
    lat: float
    lon: float
    kind: str = "demo"
    url: str | None = None
    road: str = ""
    notes: str = ""
    country: str = ""      # ISO-3166 alpha-2. Decides which policy gates it.
    # One of: written | tos-permits | unreviewed | prohibited.
    # Defaults to unreviewed, which blocks polling. Country permission is
    # necessary but never sufficient - this is the other half.
    authorization: str = "unreviewed"
    authorization_note: str = ""


class SnapshotSource:
    def __init__(self, cam: Camera, timeout: float = 8.0):
        self.cam = cam
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA

    def grab(self) -> np.ndarray | None:
        # Cache-busting: many snapshot endpoints sit behind a CDN that will
        # happily serve you the same frame for minutes otherwise.
        sep = "&" if "?" in (self.cam.url or "") else "?"
        url = f"{self.cam.url}{sep}_={int(time.time() * 1000)}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            buf = np.frombuffer(r.content, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            return frame
        except Exception:
            return None


class StreamSource:
    """MJPEG / HLS / RTSP via OpenCV's ffmpeg backend.

    Holds the capture open between grabs and reconnects on failure. Streams are
    the nicer case - you get real framerate, which makes dwell detection far
    more reliable than it is on 60-second snapshots.
    """

    def __init__(self, cam: Camera):
        self.cam = cam
        self.cap: cv2.VideoCapture | None = None

    def _open(self):
        self.cap = cv2.VideoCapture(self.cam.url)
        # Keep the buffer tiny or you read stale frames after any stall.
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def grab(self) -> np.ndarray | None:
        if self.cap is None or not self.cap.isOpened():
            self._open()
        if self.cap is None or not self.cap.isOpened():
            return None
        ok, frame = self.cap.read()
        if not ok:
            self.cap.release()
            self.cap = None
            return None
        return frame


class DemoSource:
    """Synthetic 640x360 road scene.

    Three lanes of traffic moving left to right at varying speeds, plus one
    vehicle parked on the shoulder that never moves. That parked vehicle is the
    thing the dwell tracker should find - if it doesn't light up within a minute
    of starting the server, something upstream is broken.
    """

    W, H = 640, 360

    def __init__(self, cam: Camera, seed: int | None = None):
        self.cam = cam
        self.rng = np.random.default_rng(
            seed if seed is not None else abs(hash(cam.id)) % (2**32)
        )
        self.t = 0
        self.cars = [
            {"x": self.rng.uniform(0, self.W), "lane": i % 3,
             "speed": self.rng.uniform(6, 16),
             "w": int(self.rng.uniform(40, 64)),
             "color": tuple(int(v) for v in self.rng.integers(120, 245, 3))}
            for i in range(7)
        ]
        # The plant. It has to *arrive* and then stop, not start stopped.
        # Background subtraction defines "background" as whatever was already
        # there, so a vehicle present in frame one is invisible to it forever.
        # Real cameras have the same property: restart your poller while a
        # patrol car is already parked and you will never see it.
        self.parked = {"x": -90.0, "lane": 3, "speed": 13.0, "w": 52,
                       "color": (238, 238, 240), "stop_at": 520.0}

    def _lane_y(self, lane: int) -> int:
        return [150, 196, 242, 292][lane]

    def grab(self) -> np.ndarray | None:
        self.t += 1
        f = np.zeros((self.H, self.W, 3), dtype=np.uint8)
        f[:, :] = (96, 122, 88)                     # verge
        cv2.rectangle(f, (0, 128), (self.W, 272), (58, 58, 62), -1)   # carriageway
        cv2.rectangle(f, (0, 272), (self.W, 312), (74, 74, 78), -1)   # hard shoulder

        # Road markings are painted on the road - a fixed camera sees them
        # static. Scrolling them would inject motion that no real feed has.
        for y in (174, 220):
            for x in range(0, self.W, 60):
                cv2.line(f, (x, y), (x + 30, y), (200, 200, 190), 2)
        cv2.line(f, (0, 270), (self.W, 270), (210, 210, 200), 2)

        for c in self.cars:
            c["x"] = (c["x"] + c["speed"]) % (self.W + 120)

        p = self.parked
        if p["speed"] > 0:
            p["x"] += p["speed"]
            if p["x"] >= p["stop_at"]:
                p["x"], p["speed"] = p["stop_at"], 0.0

        for c in self.cars + [self.parked]:
            y = self._lane_y(c["lane"])
            x, w = int(c["x"]) - 60, c["w"]
            body = c["color"]
            # Solid body, with a roof band that is darker than the paint but
            # still well clear of asphalt - otherwise the car's interior sits
            # inside MOG2's variance threshold and the blob breaks into
            # fragments too small to survive the area filter.
            cv2.rectangle(f, (x, y - 14), (x + w, y + 14), body, -1)
            roof = tuple(int(v * 0.62 + 40) for v in body)
            cv2.rectangle(f, (x + 10, y - 9), (x + w - 14, y + 9), roof, -1)

        # Sensor noise - keeps the background model honest.
        noise = self.rng.normal(0, 4, f.shape).astype(np.int16)
        return np.clip(f.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def build_source(cam: Camera):
    if cam.kind == "demo":
        return DemoSource(cam)
    if cam.kind == "snapshot":
        return SnapshotSource(cam)
    if cam.kind in ("mjpeg", "hls", "rtsp", "stream"):
        return StreamSource(cam)
    raise ValueError(f"unknown camera kind: {cam.kind!r}")
