# Getting Roadwatch onto your phone

Two deployments, same files. Start with the free one.

---

## What can be automated, and what can't

**Claude Code can do these**, because they're shell commands against your
already-authenticated CLIs:

- create the repo, commit, push
- fetch real camera data from OpenStreetMap
- enable Pages and trigger the deploy
- run the test suites
- deploy the backend to Fly, later

**Claude for Chrome can do these**, because they only exist as web UI:

- create a TomTom developer account and copy an API key
- restrict that key to your Pages domain in the TomTom dashboard
- flip repo settings that `gh` can't reach

**Only you can do these**, and no agent gets around them:

- **Authenticate.** `gh auth login`, `fly auth login`, TomTom signup. These are
  OAuth flows and credential prompts tied to your identity. An agent driving
  your browser is acting *as you* in an already-logged-in session — it never
  gets to create the account or approve the grant.
- **Add to Home Screen on the iPhone.** A physical tap on your device.
- **Pay**, if you take the backend route.

So: not fully unattended, but close. The realistic shape is you handle two
logins, and Claude Code does everything between them.

---

## Free path — Pages, no backend

Gets you Nearby, Map and Trips. The Live tab stays hidden because nothing is
answering `api/policy`.

Hand this whole block to Claude Code:

```bash
# 1. Real camera data (the shipped file is placeholder coordinates)
python tools/fetch_fixed_cameras.py MY ZA -o app/cameras.geojson
python -c "import json;print(len(json.load(open('app/cameras.geojson'))['features']),'points')"

# 2. Repo
git init -b main
git add -A
git commit -m "roadwatch: enforcement cameras, dwell detection, trip scoring"
gh repo create roadwatch --public --source=. --push

# 3. Pages via Actions (Pages can only serve / or /docs directly, and the app
#    lives in app/ — the workflow uploads it as an artifact instead)
gh api -X POST "repos/{owner}/roadwatch/pages" \
  -f 'build_type=workflow' 2>/dev/null || \
  echo "already enabled, or enable Settings > Pages > Source: GitHub Actions"

gh workflow run "Deploy to Pages"
gh run watch
```

Then on the iPhone: open the Pages URL **in Safari** (only Safari can install to
the home screen on iOS), Share → Add to Home Screen.

`gh auth login` first if you haven't. That one's yours.

### Traffic (optional, free)

Only needed for the traffic overlay; everything else works without it.

1. Sign up at developer.tomtom.com and copy an API key. Free tier is 50,000 tile
   requests a day, no card. **Claude for Chrome can do this signup.**
2. In the app: Map tab → Traffic setup → paste → Save.
3. **Restrict the key to your Pages domain in the TomTom dashboard.** A key used
   by a static site is readable by anyone who opens the page — the browser has
   to send it. Domain restriction is the only thing stopping a stranger
   spending your quota. Also worth having Chrome do while it's there.

The key is stored in your browser, never in the repo, so it won't get committed.

---

## Full path — backend, Live tab lights up

Adds camera polling, dwell detection and jurisdiction policy. Needs a machine
running 24/7.

### Free: Oracle Cloud Always Free

The only genuinely free always-on option — free indefinitely, not a trial.
Measured footprint is ~234MB with two feeds polling and ~24MB per additional
camera, against 1GB on the free AMD shape. Roughly 490MB spare after Ubuntu and
Docker, so it fits comfortably.

1. Create an Always Free account. Pick **Singapore** or **Johannesburg** for
   your markets.
2. Launch a `VM.Standard.E2.1.Micro` (AMD, 1GB) with Ubuntu 22.04. Take AMD, not
   the ARM Ampere shape — ARM is more powerful and free too, but it's almost
   always capacity-blocked in popular regions and you'll spend a week retrying.
3. Get a free subdomain at duckdns.org.

```bash
scp -r roadwatch/ ubuntu@<vm-ip>:~/
ssh ubuntu@<vm-ip>
sudo bash roadwatch/deploy/oracle-setup.sh yourname.duckdns.org <duckdns-token>
```

Two caveats worth knowing before you commit to it:

- **A credit card is required at signup** for identity verification. Always Free
  resources aren't charged, but the card check is unavoidable.
- **Oracle reclaims idle Always Free compute.** A continuously polling service
  isn't idle, so this shouldn't bite — but it's their policy, not a guarantee,
  and it's the reason this is "free with an asterisk" rather than simply free.

#### Why the script installs Caddy

The app is a PWA, and service workers plus Add to Home Screen both hard-require
HTTPS. `http://<ip>:8000` gives you a webpage you cannot install and that won't
work offline — the exact capability GitHub Pages was providing for free. Caddy
plus a DuckDNS hostname gets a Let's Encrypt certificate automatically and
renews it unattended.

#### The Oracle gotcha that wastes everyone's afternoon

Oracle's Ubuntu images ship **iptables rules that drop everything except SSH**,
and those are entirely separate from the VCN Security List in the web console.
Open only one of the two and the port stays shut while both screens look
correctly configured. The script handles iptables; you must still add ingress
for TCP 80 and 443 under Networking → VCN → Security Lists → Default. That part
is web-only — **a good job for Claude for Chrome.**

### Paid: Fly.io, ~$5/month

Less setup, no card-verification dance, regions in both markets.

```bash
fly auth login          # yours
fly launch --no-deploy --copy-config
fly volumes create roadwatch_data --size 1 --region sin   # jnb for ZA
fly deploy
```

Then re-point the phone at the Fly URL instead of the Pages one. Same app — the
Live tab appears on its own once `api/policy` answers. No migration, no second
install.

**Do not use a plan that sleeps.** `fly.toml` already sets
`auto_stop_machines = false` and `min_machines_running = 1`, and the reason is in
the file: waking resets every background model, so anything parked during the
sleep becomes permanently invisible to that camera.

---

## Keeping it current

The Pages workflow re-fetches OSM camera data on the 1st of each month and
refuses to deploy a dataset under 10 features — an Overpass timeout returning
zero would otherwise silently replace real data with nothing while the app
carried on looking healthy.

It also stamps the service worker cache with the commit SHA on every deploy.
Without that, the shell is cached permanently and the home-screen icon would
keep serving the old app forever.

Manual refresh:

```bash
gh workflow run "Deploy to Pages" -f refresh_cameras=true
```

---

## Before you trust it on a drive

```bash
node tools/test_trip.js     # 10 synthetic drives, scoring thresholds
```

And read the two warnings the app puts on its own face. Camera coverage is
volunteer-mapped, so silence means unmapped rather than clear. Trip scoring only
runs with the app open and unlocked — iOS suspends `watchPosition` when Safari is
backgrounded or the screen locks. Background alerts need the native shell, which
is a separate build.
