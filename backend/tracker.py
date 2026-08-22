"""Dwell tracking.

The useful signal from a public traffic camera is almost never "that's a police
car" - the resolution isn't there. It's *"that vehicle has been in the same spot
for four minutes while everything around it moves."*

That pattern covers what you actually care about: a stationary vehicle on the
shoulder, at a junction mouth, or in a layby. It's also what a breakdown or a
crash looks like, which is a feature - those are worth knowing about too.

This module matches detections frame-to-frame by centroid distance and counts
how many consecutive frames each track has stayed within a small radius.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from itertools import count


@dataclass
class Track:
    id: int
    cx: float
    cy: float
    w: int
    h: int
    label: str
    first_seen: float
    last_seen: float
    frames: int = 1
    still_frames: int = 0
    missing: int = 0
    peak_still: int = 0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    path: list[tuple[float, float]] = field(default_factory=list)

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (
            int(self.cx - self.w / 2),
            int(self.cy - self.h / 2),
            self.w,
            self.h,
        )

    def as_dict(self, still_threshold: int) -> dict:
        return {
            "id": self.id,
            "box": self.box,
            "label": self.label,
            "frames": self.frames,
            "still_frames": self.still_frames,
            "dwelling": self.still_frames >= still_threshold,
            "age_s": round(self.last_seen - self.first_seen, 1),
        }


class DwellTracker:
    """Greedy nearest-centroid tracker with a stillness counter.

    Deliberately simple. A Kalman filter or a ByteTrack-style tracker would be
    better on dense traffic, but public camera feeds refresh too slowly for
    motion prediction to help - between frames a moving car has often left the
    scene entirely.

    Args:
        max_distance: px a centroid can jump between frames and still be the
            same object. Scale this to your frame width and refresh rate.
        still_radius: px of movement still counted as "not moving". Covers
            camera shake and detection jitter.
        still_threshold: consecutive still frames before a track is flagged.
        max_missing: frames a track survives without a matching detection.
    """

    def __init__(
        self,
        max_distance: float = 70.0,
        still_radius: float = 12.0,
        still_threshold: int = 4,
        max_missing: int = 3,
        max_missing_dwelling: int = 60,
    ):
        self.max_distance = max_distance
        self.still_radius = still_radius
        self.still_threshold = still_threshold
        self.max_missing = max_missing
        # A track that we watched come to a stop gets a much longer grace
        # period. Losing the detection does not mean the vehicle left - far
        # more often it means the background model finally absorbed it, which
        # is guaranteed to happen to anything that holds still long enough.
        # Departure produces fresh motion, so a genuine exit still shows up.
        self.max_missing_dwelling = max_missing_dwelling
        self.tracks: dict[int, Track] = {}
        self._ids = count(1)

    def update(self, detections: list[dict]) -> list[Track]:
        now = time.time()
        centroids = [
            (d["box"][0] + d["box"][2] / 2, d["box"][1] + d["box"][3] / 2)
            for d in detections
        ]

        unmatched_tracks = set(self.tracks)
        unmatched_dets = set(range(len(detections)))

        # Score every (track, detection) pair, then take them cheapest-first.
        pairs = []
        for tid in unmatched_tracks:
            t = self.tracks[tid]
            for i, (cx, cy) in enumerate(centroids):
                dist = math.hypot(t.cx - cx, t.cy - cy)
                if dist <= self.max_distance:
                    pairs.append((dist, tid, i))
        pairs.sort()

        for dist, tid, i in pairs:
            if tid not in unmatched_tracks or i not in unmatched_dets:
                continue
            unmatched_tracks.discard(tid)
            unmatched_dets.discard(i)

            t = self.tracks[tid]
            cx, cy = centroids[i]
            x, y, w, h = detections[i]["box"]

            # Stillness is drift from an anchor, NOT frame-to-frame movement.
            # Measuring per-frame movement looks correct and is badly wrong: a
            # car creeping at 6px/frame never exceeds a 12px threshold on any
            # single frame, so slow traffic reads as parked. Against a fixed
            # anchor that same car drifts out of the radius within a few frames
            # while a genuinely parked one never does.
            drift = math.hypot(t.anchor_x - cx, t.anchor_y - cy)
            if drift <= self.still_radius:
                t.still_frames += 1
                t.peak_still = max(t.peak_still, t.still_frames)
            else:
                t.still_frames = 0
                t.anchor_x, t.anchor_y = cx, cy

            # Ease the stored position toward the new one so a single noisy
            # detection doesn't reset a long dwell.
            t.cx += (cx - t.cx) * 0.5
            t.cy += (cy - t.cy) * 0.5
            t.w, t.h = w, h
            t.label = detections[i]["label"]
            t.frames += 1
            t.missing = 0
            t.last_seen = now
            t.path.append((round(t.cx, 1), round(t.cy, 1)))
            del t.path[:-40]

        for i in unmatched_dets:
            cx, cy = centroids[i]
            x, y, w, h = detections[i]["box"]
            tid = next(self._ids)
            self.tracks[tid] = Track(
                id=tid, cx=cx, cy=cy, w=w, h=h,
                label=detections[i]["label"],
                first_seen=now, last_seen=now,
                anchor_x=cx, anchor_y=cy,
                path=[(round(cx, 1), round(cy, 1))],
            )

        for tid in list(unmatched_tracks):
            t = self.tracks[tid]
            t.missing += 1
            limit = (self.max_missing_dwelling
                     if t.peak_still >= self.still_threshold
                     else self.max_missing)
            if t.missing > limit:
                del self.tracks[tid]

        return list(self.tracks.values())

    def dwelling(self) -> list[Track]:
        return [
            t for t in self.tracks.values()
            if t.still_frames >= self.still_threshold
        ]
