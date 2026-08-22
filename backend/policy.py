"""Jurisdiction gating.

The rule this module exists to enforce: **the gate runs on the server.**

Hiding a button in the frontend is not compliance. If the API will still hand
out live alert coordinates to anyone who calls it, the feature is on, whatever
the UI shows. So every response that could carry an enforcement-position claim
passes through `apply()` here before it leaves the process, and the poller asks
`may_ingest()` before it so much as fetches a frame.

Two design choices worth keeping:

  Fail closed. An unknown country resolves to `_default`, which has everything
  off. Adding a market is a deliberate act of reviewing it and writing an entry
  - never something that happens by default because a user picked a country
  from a dropdown.

  Degrade, don't just disable. Several jurisdictions permit coarse warnings but
  not exact positions, so `precision: "zone"` snaps coordinates to a grid rather
  than removing the alert. An alert that says "somewhere along this 1km stretch"
  is both useful to a driver and materially different from a pin on a patrol
  car.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

# Roughly 1km at the equator, shrinking with latitude for longitude. Good
# enough for a warning that names a stretch of road.
ZONE_GRID_DEG = 0.01

# Per-source authorisation bases, weakest to strongest. Only the last two let
# a feed be polled.
#
# This axis exists because country-level policy cannot express it. "Malaysia
# permits police alerts" and "this operator's terms permit automated polling" are
# unrelated facts, and computer-misuse statutes in both target markets turn on
# the second one. Malaysia's Computer Crimes Act 1997 s.3 criminalises access
# "without authorisation or in excess of authorised access" regardless of
# whether data is obtained; South Africa's ECTA s.86(1) (moving to the
# Cybercrimes Act 2020 s.2) criminalises accessing data "without authority or
# permission". In both, the operative question is what the operator authorised
# - not whether the page loads without a login.
AUTH_BASES = {
    "prohibited":   False,  # terms explicitly forbid automated access
    "unreviewed":   False,  # nobody has read the terms yet - the default
    "personal-use": None,   # see below - allowed only in personal deployment
    "tos-permits":  True,   # terms reviewed and they allow this use
    "written":      True,   # explicit written permission from the operator
}

# "personal-use" is an honest name for a real position: the terms have NOT been
# read, and polling is proceeding anyway on the view that a private,
# non-redistributed hobby project is low risk. That is a risk acceptance, not a
# permission, and the two must not be recorded as the same thing - a file that
# says "tos-permits" when nobody read the terms is worse than useless, because
# future-you cannot tell the checked feeds from the unchecked ones.
#
# The reasoning it rests on is specifically about *personal* use. Data
# protection law is where that actually buys something: POPIA s.6(1)(a) excludes
# purely personal or household activity, and Malaysia's PDPA is scoped to
# commercial transactions. Neither exclusion survives publishing the output to
# other people.
#
# So the basis is tied to DEPLOYMENT_MODE. Flip the deployment to "published"
# and every personal-use feed fails closed until it gets a real basis. This is
# the whole point: the assumption is load-bearing, so it is enforced in code
# rather than left as a comment nobody re-reads at launch.
#
# Note it buys nothing at all against computer-misuse law. Malaysia's Computer
# Crimes Act 1997 s.3 and South Africa's ECTA s.86(1) turn on authorisation,
# and purpose is not an element of either offence.
PERSONAL_ONLY_BASES = {"personal-use"}


@dataclass(frozen=True)
class Policy:
    code: str
    name: str
    status: str
    camera_ingest: bool
    live_alerts: bool
    police_labeling: bool
    fixed_cameras: bool
    precision: str
    summary: str
    cautions: tuple[str, ...]
    sources: tuple[str, ...]
    last_reviewed: str | None
    published_note: str | None = None
    tightens_on_publish: bool = False

    @property
    def reviewed(self) -> bool:
        return self.last_reviewed is not None

    def as_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "status": self.status,
            "camera_ingest": self.camera_ingest, "live_alerts": self.live_alerts,
            "police_labeling": self.police_labeling,
            "fixed_cameras": self.fixed_cameras, "precision": self.precision,
            "summary": self.summary, "cautions": list(self.cautions),
            "sources": list(self.sources), "last_reviewed": self.last_reviewed,
            "reviewed": self.reviewed,
            "published_note": self.published_note,
            "tightens_on_publish": self.tightens_on_publish,
        }


class PolicyBook:
    def __init__(self, path: str | Path):
        raw = json.loads(Path(path).read_text())
        self._raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        self._default = raw["_default"]
        self._cache: dict[str, Policy] = {}

    def _mk(self, code: str, d: dict) -> Policy:
        return Policy(
            code=code,
            name=d.get("name", code),
            status=d.get("status", "unreviewed"),
            camera_ingest=bool(d.get("camera_ingest", False)),
            live_alerts=bool(d.get("live_alerts", False)),
            police_labeling=bool(d.get("police_labeling", False)),
            fixed_cameras=bool(d.get("fixed_cameras", False)),
            precision=d.get("precision", "zone"),
            summary=d.get("summary", ""),
            cautions=tuple(d.get("cautions", [])),
            sources=tuple(d.get("sources", [])),
            last_reviewed=d.get("last_reviewed"),
            published_note=(d.get("published") or {}).get("note"),
            tightens_on_publish=any(
                k != "note" for k in (d.get("published") or {})
            ),
        )

    def get(self, code: str | None, deployment: str = "personal") -> Policy:
        """Resolve a country's policy for the current deployment mode.

        The top-level flags are the personal-use position. A `published` block,
        if present, overrides them when deployment is anything other than
        personal - so the conservative settings re-arm on publishing without
        anyone having to remember to re-arm them.
        """
        code = (code or "").upper()
        key = (code, deployment)
        if key not in self._cache:
            # Unknown country keeps the unknown code but inherits the locked
            # -down default, so the UI can say which country it refused for.
            base = dict(self._raw.get(code, self._default))
            if deployment != "personal":
                over = {k: v for k, v in (base.get("published") or {}).items()
                        if k != "note"}
                base.update(over)
            self._cache[key] = self._mk(code, base)
        return self._cache[key]

    def countries(self, deployment: str = "personal") -> list[dict]:
        return sorted(
            (self.get(c, deployment).as_dict() for c in self._raw),
            key=lambda p: p["name"],
        )


def source_authorized(cam, deployment: str = "personal") -> tuple[bool, str]:
    """May we poll this specific feed? Returns (allowed, human reason).

    Synthetic feeds are self-generated, so there is no third party to authorise
    anything and they are always allowed. Everything else must carry an
    explicit basis, and anything unrecognised fails closed.
    """
    if getattr(cam, "kind", None) == "demo":
        return True, "synthetic feed - no operator to authorise"
    basis = (getattr(cam, "authorization", "") or "unreviewed").lower()
    if basis not in AUTH_BASES:
        return False, f"unrecognised authorisation basis {basis!r}"

    if basis in PERSONAL_ONLY_BASES:
        if deployment != "personal":
            return False, (
                "basis is personal-use but deployment is "
                f"{deployment!r} - needs a reviewed basis before publishing"
            )
        return True, "personal use, terms unreviewed - accepted risk, not permission"

    if not AUTH_BASES[basis]:
        return False, {
            "prohibited": "operator terms prohibit automated access",
            "unreviewed": "operator terms not yet reviewed",
        }[basis]
    return True, {
        "tos-permits": "operator terms reviewed and permit this use",
        "written": "written permission from operator on file",
    }[basis]


def snap_to_zone(lat: float, lon: float) -> tuple[float, float]:
    """Round a position to the centre of its grid cell."""
    g = ZONE_GRID_DEG
    return (
        round(math.floor(lat / g) * g + g / 2, 5),
        round(math.floor(lon / g) * g + g / 2, 5),
    )


def gate_camera(status: dict, policy: Policy) -> dict:
    """Strip live-alert content from a camera status if the policy forbids it.

    Note what stays: the camera is still listed, still shows online/offline,
    still shows a frame. What goes is the *claim* - the dwell count and the
    per-track stillness that together amount to "something is stopped there".
    """
    out = dict(status)
    if not policy.live_alerts:
        out["active"] = []
        out["dwelling"] = 0
        out["alerts_suppressed"] = True
    if policy.precision == "zone":
        out["lat"], out["lon"] = snap_to_zone(out["lat"], out["lon"])
        out["zone_snapped"] = True
    return out


def gate_event(event: dict, policy: Policy) -> dict | None:
    """Return a gated event, or None if it must not be surfaced at all."""
    if not policy.live_alerts:
        return None
    out = dict(event)
    if not policy.police_labeling:
        # Don't leak the classification through the verdict field either.
        if out.get("verdict") == "police":
            out["verdict"] = "unknown"
            out["verdict_suppressed"] = True
    if policy.precision == "zone":
        # A pixel box within a known camera's frame is a precise position by
        # another name. Drop it.
        out["box"] = None
        out["zone_snapped"] = True
    return out


def allowed_verdicts(policy: Policy) -> list[str]:
    base = ["breakdown", "roadworks", "queue", "false-positive", "unknown"]
    return (["police"] + base) if policy.police_labeling else base
