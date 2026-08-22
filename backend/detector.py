"""Object detection backends.

Two interchangeable detectors, both returning the same shape:
    [{"box": (x, y, w, h), "score": float, "label": str}, ...]

`yolo`   - real object detection, needs `pip install ultralytics` (~2GB with torch).
           Knows what a car/truck/bus/person is.
`motion` - no model download, works on low-framerate stills. Learns the empty
           road as background and reports whatever stands out from it. Doesn't
           know *what* it found, only that something is there.

The motion backend is the honest default for public traffic cameras: they're
usually 320x240 to 720p stills refreshed every 10-60s, which is well below what
YOLO needs to reliably classify a vehicle at distance.
"""

from __future__ import annotations

import cv2
import numpy as np

# COCO classes worth keeping. Everything else on a road camera is noise.
VEHICLE_CLASSES = {
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    0: "person",
}


class MotionDetector:
    """Background-subtraction detector. One instance per camera.

    MOG2 builds a statistical model of each pixel over time. Anything that
    doesn't match the model is foreground. A car that parks and stays put will
    slowly be absorbed into the background, which is exactly why we pair this
    with the dwell tracker rather than relying on it alone.
    """

    name = "motion"

    def __init__(
        self,
        min_area: int = 700,
        min_w: int = 28,
        min_h: int = 18,
        history: int = 200,
        learning_rate: float = 0.002,
        warmup_frames: int = 40,
        border_margin: int = 6,
    ):
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=32, detectShadows=True
        )
        self.min_area = min_area
        # Absolute size floor as well as an area floor. Area alone lets through
        # long thin fragments that pass the aspect test but are really just
        # split pieces of one vehicle, and those fragments are what produced
        # most of the remaining false dwells.
        self.min_w = min_w
        self.min_h = min_h
        # The single most important knob here. MOG2 absorbs anything that stays
        # put into its background model, and a parked vehicle is exactly that -
        # so a fast learning rate makes the detector blind to the one thing you
        # most want it to see. Low values keep parked vehicles visible for
        # longer but adapt more slowly to real changes like cloud shadow,
        # headlights at dusk, or the camera being nudged by wind.
        self.learning_rate = learning_rate
        self.warmup_frames = warmup_frames
        self.border_margin = border_margin
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.frames_seen = 0

    @property
    def warming(self) -> bool:
        """True while the background model is still settling.

        This has to be visible all the way up to the UI. During warm-up the
        detector reports nothing, which is indistinguishable from "the road is
        clear" unless something says otherwise - and a warning app that looks
        healthy while it is blind is worse than one that is obviously down.
        """
        return self.frames_seen <= self.warmup_frames

    @property
    def warmup_progress(self) -> tuple[int, int]:
        return (min(self.frames_seen, self.warmup_frames), self.warmup_frames)

    def detect(self, frame: np.ndarray) -> list[dict]:
        self.frames_seen += 1
        # Learn fast while settling, then slow down. A model that starts slow
        # bakes whatever traffic happened to be in the first frames into the
        # background permanently, and you get "ghosts" - phantom detections
        # that sit at fixed spots forever and never move, which the dwell
        # tracker will happily report as parked vehicles.
        rate = 0.05 if self.frames_seen <= self.warmup_frames else self.learning_rate
        mask = self.bg.apply(frame, learningRate=rate)

        # 127 is MOG2's shadow value; drop it so shadows don't become "vehicles"
        mask[mask == 127] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel, iterations=2)

        # Nothing the model says during warm-up means anything yet.
        if self.frames_seen <= self.warmup_frames:
            return []

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        fh, fw = mask.shape[:2]
        m = self.border_margin
        out = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            # Drop anything touching the frame edge. A vehicle halfway out of
            # shot keeps shrinking while its centroid barely moves, which reads
            # as "stopped" to any centroid tracker. Edges were the single
            # biggest source of false dwells here.
            if x <= m or y <= m or x + w >= fw - m or y + h >= fh - m:
                continue
            if w < self.min_w or h < self.min_h:
                continue
            aspect = w / max(h, 1)
            # Vehicles from a roadside camera are wider than they are tall, or
            # roughly square head-on. Very tall thin blobs are usually poles,
            # rain streaks, or a person.
            if aspect < 0.3 or aspect > 6.0:
                continue
            out.append(
                {
                    "box": (int(x), int(y), int(w), int(h)),
                    "score": round(min(area / (self.min_area * 8), 1.0), 3),
                    "label": "object",
                }
            )
        return out


class YoloDetector:
    """Ultralytics YOLO wrapper. Optional - import is deferred.

    No warm-up: object detection is stateless per frame, which is a real
    advantage over background subtraction on slow feeds where settling a
    background model costs many minutes of wall-clock time.
    """

    name = "yolo"
    warming = False
    warmup_progress = (0, 0)

    def __init__(self, weights: str = "yolov8n.pt", conf: float = 0.35):
        from ultralytics import YOLO  # noqa: PLC0415

        self.model = YOLO(weights)
        self.conf = conf

    def detect(self, frame: np.ndarray) -> list[dict]:
        results = self.model.predict(frame, conf=self.conf, verbose=False)
        out = []
        for r in results:
            for b in r.boxes:
                cls = int(b.cls[0])
                if cls not in VEHICLE_CLASSES:
                    continue
                x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
                out.append(
                    {
                        "box": (int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
                        "score": round(float(b.conf[0]), 3),
                        "label": VEHICLE_CLASSES[cls],
                    }
                )
        return out


def warmup_for_interval(interval_s: float,
                        target_seconds: float = 240.0,
                        lo: int = 12, hi: int = 40) -> int:
    """How many frames of warm-up, given how slowly we poll.

    A fixed 40-frame warm-up is fine at 2s/frame (80 seconds) and awful at
    20s/frame (13 minutes of blindness every restart). Scale it to hit roughly
    `target_seconds` of wall clock instead, with a floor of 12 - below that
    MOG2 has not seen enough variance to separate a vehicle from sensor noise.

    The trade is real and not free: fewer warm-up frames means a less settled
    background and more false positives early on. 12 frames is the point where
    that started getting bad in testing, not an arbitrary minimum.
    """
    return int(max(lo, min(hi, round(target_seconds / max(interval_s, 0.1)))))


def build_detector(backend: str = "auto", **kwargs):
    """Pick a detector. `auto` prefers YOLO but never hard-fails on it."""
    if backend in ("auto", "yolo"):
        try:
            return YoloDetector(**{k: v for k, v in kwargs.items()
                                   if k in ("weights", "conf")})
        except Exception as exc:
            if backend == "yolo":
                raise
            print(f"[detector] yolo unavailable ({exc.__class__.__name__}), "
                  f"falling back to motion")
    return MotionDetector(**{k: v for k, v in kwargs.items()
                             if k in ("min_area", "history", "learning_rate",
                                            "min_w", "min_h", "warmup_frames")})
