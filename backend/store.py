"""SQLite event log.

Events are append-only. Confirmed dwell events are what you'd eventually push
to clients, and having them on disk means you can go back later and check how
often the detector was actually right - which is the only way to tune the
thresholds honestly.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   TEXT    NOT NULL,
    track_id    INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    label       TEXT,
    box         TEXT,
    still_frames INTEGER,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    confirmed   INTEGER DEFAULT 0,
    verdict     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_cam ON events(camera_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_track
    ON events(camera_id, track_id, started_at);
"""


class Store:
    def __init__(self, path: str | Path = "data/events.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def open_event(self, camera_id: str, track) -> int | None:
        try:
            cur = self.db.execute(
                "INSERT INTO events "
                "(camera_id, track_id, kind, label, box, still_frames, started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (camera_id, track.id, "dwell", track.label,
                 json.dumps(track.box), track.still_frames, track.first_seen),
            )
            self.db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def touch_event(self, event_id: int, track):
        self.db.execute(
            "UPDATE events SET still_frames=?, ended_at=?, box=? WHERE id=?",
            (track.still_frames, time.time(), json.dumps(track.box), event_id),
        )
        self.db.commit()

    def confirm(self, event_id: int):
        self.db.execute(
            "UPDATE events SET confirmed=1 WHERE id=?", (event_id,)
        )
        self.db.commit()

    def set_verdict(self, event_id: int, verdict: str):
        """Human labelling. `verdict` is free text - 'police', 'breakdown',
        'false-positive', 'roadworks'. This is your training set later."""
        self.db.execute(
            "UPDATE events SET verdict=? WHERE id=?", (verdict, event_id)
        )
        self.db.commit()

    def recent(self, limit: int = 60, camera_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: list = []
        if camera_id:
            sql += " WHERE camera_id=?"
            args.append(camera_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        rows = self.db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["box"] = json.loads(d["box"]) if d["box"] else None
            out.append(d)
        return out

    def stats(self, camera_ids: list[str] | None = None) -> dict:
        """Counts, optionally restricted to a set of cameras.

        The caller passes the cameras belonging to the active jurisdiction. A
        global count would leak the existence of activity in countries the
        current policy says you may not report on - a smaller leak than
        coordinates, but the same kind.
        """
        sql = ("SELECT COUNT(*) n, "
               "SUM(confirmed) confirmed, "
               "SUM(CASE WHEN verdict='false-positive' THEN 1 ELSE 0 END) fp, "
               "SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) labelled "
               "FROM events")
        args: list = []
        if camera_ids is not None:
            if not camera_ids:
                return {"n": 0, "confirmed": 0, "fp": 0, "labelled": 0}
            sql += f" WHERE camera_id IN ({','.join('?' * len(camera_ids))})"
            args = list(camera_ids)
        row = self.db.execute(sql, args).fetchone()
        return {k: (row[k] or 0) for k in ("n", "confirmed", "fp", "labelled")}
