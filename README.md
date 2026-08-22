# roadwatch

A hobby rig for pointing computer vision at public road cameras and asking
*"has anything stopped where it shouldn't have?"*

It runs out of the box with zero configuration against a synthetic road scene,
so you can watch the whole pipeline work before you go hunting for real feeds.

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
# open http://localhost:8000
```

About a minute in, a white vehicle pulls onto the hard shoulder of the demo feed
and stops. It should turn up boxed in red, with an event in the log.

---

## What it actually detects

Not police. This is the part worth being blunt about.

The original idea was "spot the patrol car." Public traffic cameras can't
support that — they're low resolution, they're pointed at traffic flow rather
than the verge, and a marked car at 80m is a few dozen pixels. Any classifier
you train on that will produce confident nonsense.

So the signal here is **dwell**: an object that arrives, stops, and holds
position while traffic keeps moving around it. That's a weaker claim, and it's
one the imagery can actually support. It also happens to catch the things worth
knowing about anyway — a vehicle on the shoulder is a speed trap, a breakdown,
a crash, or a recovery truck, and you want to slow down for all four.

Deciding *which* of those it is, is your job — hence the labelling buttons in
the event log. Label enough events and you'd have the dataset you'd need to
train a real classifier. That's the honest path to the original idea.

## One app, two deployments

Everything lives in `app/` — a single frontend with four tabs. The same files
deploy two ways:

| | Static host (GitHub Pages) | Served by the backend |
|---|---|---|
| Cost | free | ~$5/mo or Oracle free tier |
| Nearby (fixed cameras) | yes | yes |
| Map + traffic | yes | yes |
| Trip scoring | yes | yes |
| **Live camera detection** | — | yes |
| Jurisdiction policy | — | yes |

The **Live** tab hides itself unless a backend actually answers `api/policy`.
Not greyed out, not showing errors — absent, with the header reading `offline`
instead of `live + offline`. All URLs are relative for the same reason, so
`api/policy` and `trip.js` resolve correctly whether FastAPI is serving the page
or a static host is.

That split is deliberate: camera polling needs a machine running 24/7, and the
other three tabs don't need a server at all. Making them share a build means you
deploy free today and the Live tab lights up the day you have somewhere to host
the backend — no second app, no migration.

```bash
python tools/fetch_fixed_cameras.py MY ZA -o app/cameras.geojson

# free, no backend:      push app/ to GitHub Pages
# everything:            uvicorn backend.main:app --port 8000
```

## Getting it on your phone

**See `DEPLOY.md` for the runbook** — it splits every step into what Claude Code
can run, what Claude for Chrome can click, and the two things only you can do
(authenticating, and tapping Add to Home Screen). A GitHub Actions workflow in
`.github/workflows/pages.yml` handles the Pages deploy, because Pages can only
serve the repo root or `/docs` directly and the app lives in `app/`.

### The short version

The heavy end — OpenCV, background models, polling — cannot run on the handset,
so the phone is a thin client either way. That makes the fast path a PWA:

1. Deploy the backend somewhere with HTTPS (see below). Service workers and
   Add to Home Screen both require it — `http://` will not install.
2. Open the URL in **Safari** (not Chrome — on iOS only Safari can install to
   the home screen).
3. Share → **Add to Home Screen**.

You get an icon, a full-screen chromeless app, no Xcode, no App Store review,
and no seven-day re-signing dance with a free Apple Developer account. The
manifest, service worker, icons and safe-area handling for the Dynamic Island
are already in `frontend/`.

Notes on what's already handled:

- **Live data is never cached.** The service worker caches the shell only; every
  `/api/*` request goes to the network with no fallback. A cached dwell alert is
  worse than none — it points at a road where nothing is stopped.
- **Polling suspends when backgrounded** via the Page Visibility API and resumes
  with an immediate refresh. A 2.5s loop running in a backgrounded tab is a real
  battery drain and hits your server for data nobody is reading.
- **Bump `CACHE_VERSION` in `sw.js` on every deploy**, or iOS will serve stale
  HTML from the home-screen icon more or less forever.

### Hosting: do not use a sleeping free tier

Render's free web services spin down after ~15 minutes without traffic, and for
this app that's worse than the usual cold-start annoyance:

- polling stops, so the app is blind exactly when you aren't watching it
- **restart resets every background model** — anything already parked when the
  service wakes becomes permanently invisible to that camera, per the
  arrive-vs-present limitation above

`Dockerfile` and `render.yaml` are included, with a `starter` plan and a mounted
disk at `/app/data` (without the disk, every deploy wipes your labelled events —
the one thing here you cannot regenerate).

### Where to actually run it

| Option | Cost | Always on | Notes |
|---|---|---|---|
| **Oracle Always Free** | **free** | yes | Genuinely free indefinitely. `deploy/oracle-setup.sh` does the whole install including HTTPS. Card required at signup; take the AMD shape, ARM is capacity-blocked. |
| **Fly.io** | ~$5–6/mo | yes | `fly.toml` included. The only provider with regions in **both** your markets — `sin` (Singapore) and `jnb` (Johannesburg). |
| **Your own laptop** | free | no | `caffeinate -i uvicorn ...` keeps macOS awake. Polls from a residential IP, which looks far more like ordinary viewing than a datacentre IP on a timer. |
| **Oracle Cloud Always Free** | free | yes | An Ampere ARM VM, genuinely free indefinitely, with Singapore and Johannesburg regions — good for both your markets. Caveats: card required at signup, ARM capacity is often unavailable in popular regions, and idle instances can be reclaimed. |
| **Small VPS** (Vultr / DigitalOcean / Hetzner) | ~$5–6/mo | yes | Boring and reliable. Pick a region near the cameras you poll — Singapore for MY, Cape Town or Johannesburg for ZA. |
| **Render starter** | $7/mo | yes | Simplest deploy; `render.yaml` is set up for it. Most expensive per unit of compute, least fuss. |

There is no free, always-on, no-hardware option without a real asterisk. Oracle's
free tier is the closest thing, and its ARM capacity is genuinely hard to get in
popular regions. Everything else is about $5–6/month.

### Deploying to Fly

```bash
fly launch --no-deploy --copy-config
fly volumes create roadwatch_data --size 1 --region sin   # jnb for ZA
fly deploy
```

`fly.toml` sets `auto_stop_machines = false` and `min_machines_running = 1`,
which is the whole point of the file. Fly's defaults suspend idle machines, and
that default is actively wrong here: suspending stops polling, and **waking
resets every background model**, so anything parked during the sleep becomes
permanently invisible to that camera. The same trap applies to Render's free
tier, Cloud Run's scale-to-zero, and Railway's sleep settings — whatever you
pick, make sure it does not sleep.

### Frame width is the memory knob

Frames are downscaled to `max_frame_width` (default 640) before detection.
Measured on a synthetic 1080p feed:

| Cap | Peak RSS added | Per tick |
|---|---|---|
| 1920 (uncapped) | +292 MB | 192 ms |
| 960 | +139 MB | 119 ms |
| 640 | +109 MB | 115 ms |

Eight uncapped 1080p cameras would want ~2.2GB and OOM the 1GB machine — and the
OOM killer takes the whole process, not one camera. Dwell detection asks whether
a vehicle-sized blob is holding position, and 640px answers that as well as 1920
does. Raise the cap only if you have a specific reason.

### When you'd actually need a native app

A PWA cannot do background geolocation. If you want the thing to alert you
*while driving without the app open* — which is the only version of this that's
genuinely useful in a car — you need a native shell (Expo fits your stack) with
background location permission and local notifications triggered by geofences
around confirmed dwell events. That's a real project, not a wrapper. Build the
PWA first and find out whether the detections are good enough to be worth
alerting on at all.

## Jurisdiction gating

The app is scoped to one country at a time, chosen from the selector in the top
bar. `data/jurisdictions.json` holds a policy per country with four flags:

| Flag | Controls |
|---|---|
| `camera_ingest` | whether feeds in that country are polled **at all** |
| `live_alerts` | whether live stopped-vehicle detections reach the user |
| `police_labeling` | whether an event may be labelled police specifically |
| `fixed_cameras` | whether published fixed enforcement points are shown |

Plus `precision`: `exact` for point coordinates, `zone` to snap positions to a
~1km grid so an alert names a stretch of road rather than a spot. That's the
pattern jurisdictions like France require — a coarse warning is permitted where
a pin on a patrol car is not.

### Personal vs published

Country policy has two layers. The top-level flags apply when
`config.deployment` is `personal`; an optional `published` block overrides them
when it isn't.

The risk profile of the two is genuinely different. Labelling your own private
event log "police" is not the same act as shipping an app to a country's drivers
that identifies police positions — the first is a personal note, the second is a
product with users. So personal use runs unrestricted, and the conservative
settings re-arm automatically on publishing rather than depending on anyone
remembering to re-arm them.

**Malaysia and South Africa are identical for personal use** — ingest, live
alerts, police labelling and exact precision all on. ZA carries a `published`
override that turns police labelling back off, because reg 292A NRTR 2000 is
close enough to the line that distributing camera-derived police alerts to South
African drivers deserves a local opinion first. Labelling your own log isn't
that, which is why it's unrestricted here.

### Two gates, not one

Country policy is a **ceiling**; per-feed authorisation is the **floor**. Both
must pass before a single frame is fetched.

This split exists because they are unrelated facts. "Malaysia does not prohibit
police alerts" and "this operator permits automated polling of their camera" have
nothing to do with each other, and computer-misuse law in both target markets
turns on the second one:

- **Malaysia** — Computer Crimes Act 1997 s.3 criminalises access without
  authorisation *or in excess of authorised access*, regardless of whether data
  is obtained or damage caused. Extraterritorial.
- **South Africa** — ECTA s.86(1) criminalises accessing or intercepting data
  without authority or permission; being superseded by the Cybercrimes Act 19 of
  2020 s.2.

The operative word in both is **authorised**, and authorisation is granted by
the operator, not by the page being publicly viewable. A person opening a CCTV
viewer in a browser is authorised. A bot hitting the underlying JPEG endpoint
every 30 seconds may not be — same bytes, different access.

So every camera in `cameras.json` carries an `authorization` basis:

| Basis | Polled? | Meaning |
|---|---|---|
| `written` | yes | explicit written permission from the operator on file |
| `personal-use` | personal deployment only | terms **not** read; polling anyway as accepted risk |
| `tos-permits` | yes | terms read, and they allow this use |
| `unreviewed` | **no** | nobody has read the terms yet — **the default** |
| `prohibited` | **no** | terms explicitly forbid automated access |

### `personal-use` and why it's tied to deployment

`personal-use` records a real and defensible position honestly: the terms have
not been read, and you're polling anyway because this is a private project whose
output goes nowhere. That's a **risk acceptance, not a permission**, and the
config file distinguishes the two on purpose — a file claiming `tos-permits`
where nobody read the terms is worse than useless, because you can no longer
tell the checked feeds from the unchecked ones.

The reasoning is specifically about *personal* use, and that's where it earns
its keep: POPIA s.6(1)(a) excludes purely personal or household activity, and
Malaysia's PDPA is scoped to commercial transactions. **Neither exclusion
survives publishing to other users.** So the basis is bound to
`config.deployment` — set it to `"published"` and every `personal-use` feed
fails closed until it gets a reviewed basis. The assumption is load-bearing, so
it's enforced in code rather than left in a comment nobody re-reads at launch.

It buys nothing against computer-misuse law. CCA 1997 s.3 and ECTA s.86(1) turn
on authorisation, and purpose is not an element of either offence. Realistically
the downside of polling a public endpoint is an IP ban rather than a
prosecution — but "low risk" and "permitted" are different claims and the file
should say which one you're standing on.

Non-demo feeds are also floored at a 20s polling interval regardless of
`interval_s`. Most public snapshot endpoints refresh slower than that anyway, so
polling harder buys duplicate frames and a block.

Unreviewed is the default and it blocks polling, so a feed you add without doing
the reading simply never fetches. `cameras.json` ships with a worked example
(`example-prohibited`) that is `enabled: true` in a country whose policy permits
ingest, and is still never polled — it logs zero ticks. Record what the terms
actually said in `authorization_note` so the next person can check your reading.

Data protection applies on top of all of this and is not covered by either gate
— PDPA 2010 in Malaysia, POPIA in South Africa — because you are processing
footage of identifiable vehicles and people.

**The gate runs server-side.** Hiding a control in the UI is not compliance; if
the API still hands out the coordinates, the feature is on. So:

- the poller checks **both** `camera_ingest` and per-feed authorisation *before
  fetching a frame* — a blocked feed logs zero ticks, not "fetched then
  discarded"
- `/api/cameras`, `/api/events` and `/api/fixed` filter to the active country
  and strip dwell content when `live_alerts` is off
- requesting a frame from a camera outside the active jurisdiction returns 403
- POSTing a `police` verdict where `police_labeling` is off returns 400
- **frame annotations are gated too.** This one was a genuine bug during the
  build: the JSON was correctly stripped of dwell counts while the JPEG still
  had a red `STILL 17f` box burned into the pixels. The frame *is* the alert.
  Nothing is drawn when alerts are suppressed.
- even the event *count* is zeroed when alerts are off — a bare number is still
  a claim that something was detected

**Unknown countries fail closed.** Anything without an entry inherits
`_default`, which has every flag off. Adding a market is a deliberate act of
reviewing it and writing an entry — never something that happens because a user
picked a country from a dropdown.

### The policy file is yours, not mine

`jurisdictions.json` is a data file you maintain. The entries shipped are a
scaffold assembled from public reporting, with sources and `last_reviewed`
dates attached so you can check them. **It is not legal advice**, and two of
the five entries have `last_reviewed: null` — the UI says so out loud.

Where your two markets currently sit:

- **Malaysia** — all flags on. No statute makes reporting police presence an
  offence and Waze operates normally there, though KL police have publicly
  asked the public not to share roadblock locations. A request rather than a
  prohibition, but worth watching.
- **South Africa** — `police_labeling` off, everything else on. Regulation 292A
  of the National Road Traffic Regulations 2000 prohibits detectors and jammers
  of speed measuring equipment. That provision targets radar-receiving
  hardware, and this is a map database rather than a detector — but it is close
  enough to the line that the police label is off until someone qualified in SA
  reads it properly.

Both entries cover **alerting only**. Neither says anything about whether you
may access any given camera — that is decided per-feed, and both need a real
local opinion before you ship. Data-protection law applies
separately in each (PDPA in Malaysia, POPIA in South Africa) because you're
processing footage of identifiable vehicles.

### Known limitation: country is global

The active jurisdiction is server state, so every client of one server sees the
same country. Fine for a laptop; wrong for a real product, where it must be
per-user and derived from device location rather than a dropdown — otherwise a
user simply selects a permissive country to unlock alerts. The gating functions
in `policy.py` already take the policy as an argument, so the change is
threading a per-request policy through instead of reading global state.

## Architecture

```
cameras.json ──► sources.py ──► detector.py ──► tracker.py ──► store.py
                 (grab frame)   (find blobs)    (measure      (SQLite
                       ▲                         stillness)    events)
                       │               │
              camera_ingest?      poller.py ──► main.py ──► frontend/
                       │      (one worker per   (FastAPI)   (map + console)
                       └───── camera, async)        │
                                                    │
                       jurisdictions.json ──► policy.py
                                          (gates every response)
```

Each camera gets its own detector and tracker instance — a background model
learned on one camera means nothing on another.

## Five things that went wrong, and why they matter

These cost real debugging time and they'll bite you again on real feeds.

**1. Background subtraction can only see things that *arrive*.**
Anything present in the first frame *is* the background, permanently. Start the
poller while a patrol car is already parked and you will never see it. There is
no tuning fix for this — it's the definition of the method. Practical
consequence: restarts blind you to everything currently stationary.

**2. Frame-to-frame movement is the wrong way to measure stillness.**
A car creeping at 6px/frame never exceeds a 12px "is it moving" threshold on any
single frame, so slow traffic reads as parked. `tracker.py` measures cumulative
drift from a fixed anchor instead, which a moving vehicle escapes within a few
frames and a stopped one never does.

**3. Frame edges generate phantom stops.**
A vehicle halfway out of shot keeps shrinking while its centroid barely moves —
textbook fake dwell. Detections touching the border are dropped
(`border_margin`). This was the single largest source of false positives.

**5. Warm-up is invisible unless you make it visible.**
For the first N frames the detector reports nothing — which renders exactly like
"the road is clear". A warning app that looks healthy while it is blind is worse
than one that is obviously down, so warm-up is now surfaced everywhere: an amber
dot, a progress bar with an ETA, a top-bar counter, and explicit "not detecting
yet" text. Warm-up frames also scale with poll rate (`warmup_for_interval`),
because a fixed 40 frames is 80 seconds at the 2s demo cadence and 13 minutes at
the 20s floor for real feeds. Fewer warm-up frames means a less settled
background and more early false positives — 12 is where that started getting bad
in testing, hence the floor.

**4. A slow learning rate creates ghosts; a fast one creates blindness.**
Start the background model slow and whatever traffic happened to be in frame
during startup gets baked in forever, producing phantom blobs that sit
motionless at fixed spots and look exactly like parked cars. Start it fast and
real parked vehicles get absorbed in seconds. `detector.py` learns fast for the
first 40 frames, then drops to a slow rate.

On the demo feed at 2s polling, absorption is visible within a couple of
minutes. At realistic polling (30–60s) the same learning rate buys you *hours*
of wall-clock time, so this matters far less in production than it looks here.

## Tuning

Everything lives in the `config` block of `cameras.json`.

| Key | Does | Notes |
|---|---|---|
| `interval_s` | seconds between polls | **30–60 for real feeds.** 2s is a demo-only speed |
| `detector` | `auto` / `yolo` / `motion` | `auto` tries YOLO, falls back silently |
| `still_threshold` | frames of stillness before flagging | lower = twitchier |
| `confirm_frames` | frames before the event is confirmed | filters traffic lights and queues |
| `max_distance` | px a centroid may jump between frames | scale to frame width ÷ poll rate |
| `still_radius` | px of drift still counted as stopped | must be well under a slow vehicle's per-frame travel |
| `default_country` | jurisdiction active at startup | must have an entry in `jurisdictions.json` |

Detector-level knobs (`min_area`, `min_w`, `min_h`, `learning_rate`,
`warmup_frames`, `border_margin`) are constructor arguments in `detector.py`.
Raising `min_area` to 700 with a 28×18px floor cut false dwells roughly tenfold
here with no loss of true detections — size filtering is the cheapest win
available.

## Adding real cameras

Set `enabled: true` on a camera in `cameras.json` and give it a URL.

Set `country` on every real camera — it decides which policy gates it. A camera
with no country is visible in every jurisdiction, which is fine for the
synthetic demo feeds and wrong for anything real.

- **`snapshot`** — a JPEG endpoint. Most government traffic cameras. Find the URL
  by opening an operator's public CCTV viewer and reading the browser network
  tab.
- **`mjpeg` / `hls` / `rtsp`** — opened through OpenCV's ffmpeg backend. Streams
  give real framerate, which makes dwell detection substantially more reliable
  than snapshot polling.

Before you enable anything: **read the operator's terms of service.** Feeds
published for public viewing very often prohibit automated polling and
redistribution, and that's a separate question from whether the page is public.
Poll gently, send a real User-Agent, and don't rehost their imagery. Running
detection over footage of identifiable vehicles also has data-protection
implications that vary a lot by country — worth understanding before this grows
past your own laptop.

## The fixed-camera layer

`/api/fixed` serves `data/fixed_cameras.geojson`. **The shipped file is
placeholder coordinates** — replace it. Every feature needs a
`properties.country` or it will only appear in whichever jurisdiction happens to
be active.

This layer needs no computer vision at all, and it's where most of the practical
value of the original idea actually sits. Fixed speed and red-light cameras are
published datasets in most countries, and OpenStreetMap carries them under
`highway=speed_camera`. An Overpass query gets you a whole region:

```
[out:json];
node["highway"="speed_camera"]({{bbox}});
out body;
```

Convert the result to GeoJSON and drop it in. That alone is a more useful app
than anything the CV pipeline produces.

## Where to take it

- **Label events for a few weeks**, then train a small classifier on the crops.
  Real supervised learning on data you actually collected.
- **Per-camera zones.** Mask off the carriageway so only the shoulder and laybys
  are scored. Kills most remaining false positives.
- **Swap in YOLO** (`pip install ultralytics`) on any feed with enough
  resolution. You get vehicle classes, which lets you ignore pedestrians and
  distinguish a stopped truck from a stopped car.
- **Dashcam sourcing.** If you want mobile-trap coverage that public cameras
  genuinely cannot give you, on-device detection on phone cameras is the
  architecture that works — the cameras are where the cars are.
