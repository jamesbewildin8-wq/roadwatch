/* Trip scoring.
 *
 * Turns a stream of GPS fixes into a driving score. Kept in its own file with
 * no DOM access so it can be run headlessly against synthetic traces - the
 * thresholds below are only defensible if they've been checked against drives
 * where you know the right answer in advance.
 *
 * WHAT IS SCORED, AND WHAT DELIBERATELY IS NOT
 *
 * Scored: smoothness. Harsh braking, harsh acceleration, hard cornering, and
 * how steady your speed is. These are measurable from GPS alone, they
 * correlate with crash risk, and none of them require knowing the speed limit.
 *
 * NOT scored: whether you sped near a camera. That was the obvious thing to
 * build given this app already knows where cameras are, and it is a bad idea:
 * scoring camera-zone compliance rewards slowing down for cameras rather than
 * driving well, which is the exact behaviour that makes camera-dense roads
 * more dangerous, not less. If the score can be gamed by lifting off for 200m
 * and flooring it after, it is measuring the wrong thing.
 *
 * NOT scored: absolute speed. We have no road speed-limit data - the app's
 * dataset holds camera positions, not per-road limits - so any "speeding"
 * number would be invented. Max speed is reported as a fact, never scored.
 */

(function (root) {
"use strict";

const HARSH = {
  // m/s^2. Ranges consistent with telematics practice: ~3.0 is a firm stop you
  // would notice, ~4.5 is an emergency one. Deliberately not tighter - GPS
  // derived acceleration is noisy and a jumpy threshold produces a score that
  // punishes bad signal rather than bad driving.
  brake:      -3.0,
  brakeSevere:-4.5,
  accel:       3.0,
  accelSevere: 4.5,
  // Lateral, v^2/r approximated via heading change rate * speed.
  corner:      3.5,
  cornerSevere:5.0,
};

const MIN_EVENT_GAP_MS = 3000;   // one long brake is one event, not fifteen
const MAX_ACCURACY_M   = 30;     // discard fixes worse than this
const MIN_SPEED_MS     = 1.5;    // below walking pace, cornering maths is noise
const SMOOTH_N         = 3;      // moving average window on speed

// Heading must be measured over a long baseline, never between consecutive
// fixes. At 100km/h fixes are ~28m apart, so 15m of position noise - completely
// ordinary in a city - swings the derived bearing by tens of degrees. Testing a
// dead-straight, perfectly smooth drive with 15m noise produced 175 phantom
// cornering events and cost 25 points before this was fixed.
//
// The baseline is a TIME window, not a distance, and the reason is worth
// writing down. Bearing error from position noise is ~sigma/(v*T) radians, and
// lateral acceleration is turnRate*v, so the speed cancels:
//
//     lateral error  ~=  (sigma / (v*T)) / T * v  =  sigma / T^2
//
// The false-corner rate therefore depends only on how long the window is, not
// how fast you are going. At sigma=7.5m and T=3s that is ~0.8 m/s^2 of noise
// against a 3.5 m/s^2 threshold - comfortable margin at any speed. A distance
// baseline gets this wrong in both directions: too noisy when crawling, and so
// long at speed that it averages real corners away entirely (a 120m baseline
// made every synthetic corner vanish).
const HEADING_BASELINE_S = 3.0;
const HEADING_MIN_DIST_M = 20;   // guard against jitter while barely moving
const TURN_SMOOTH_N      = 2;
// No car corners at 12 m/s^2 (1.2g). Anything above is a GPS artefact.
const MAX_PLAUSIBLE_LAT  = 12.0;

function distM(a, b){
  const R = 6371000, rad = Math.PI/180;
  const x = (b.lon-a.lon)*rad*Math.cos((a.lat+b.lat)*rad/2);
  const y = (b.lat-a.lat)*rad;
  return Math.sqrt(x*x + y*y) * R;
}

function bearing(a, b){
  const rad = Math.PI/180;
  const y = Math.sin((b.lon-a.lon)*rad) * Math.cos(b.lat*rad);
  const x = Math.cos(a.lat*rad)*Math.sin(b.lat*rad) -
            Math.sin(a.lat*rad)*Math.cos(b.lat*rad)*Math.cos((b.lon-a.lon)*rad);
  return Math.atan2(y, x) / rad;
}

function angleDiff(a, b){
  let d = (b - a + 540) % 360 - 180;
  return d;
}

class TripRecorder {
  constructor(){ this.reset(); }

  reset(){
    this.samples = [];      // cleaned fixes
    this.events = [];       // {type, severity, at, value}
    this.startedAt = null;
    this.endedAt = null;
    this.distance = 0;
    this._speedBuf = [];
    this._turnBuf = [];
    this._headAnchor = null;   // sample the current heading is measured from
    this._heading = null;
    this._lastEventAt = {};
    this._rejected = 0;
    this._noiseRejected = 0;
  }

  /** Feed one GeolocationPosition-shaped object. Returns true if kept. */
  add(pos){
    const c = pos.coords || pos;
    const t = pos.timestamp ?? Date.now();

    // A bad fix is worse than no fix: a 100m jump reads as a 40 m/s^2 spike and
    // would dominate the whole score.
    if (c.accuracy != null && c.accuracy > MAX_ACCURACY_M){ this._rejected++; return false; }

    const s = { lat: c.latitude, lon: c.longitude, t,
                acc: c.accuracy ?? 0,
                rawSpeed: (c.speed != null && c.speed >= 0) ? c.speed : null,
                rawHeading: (c.heading != null && !Number.isNaN(c.heading)) ? c.heading : null };

    const prev = this.samples[this.samples.length - 1];
    if (prev){
      const dt = (t - prev.t) / 1000;
      if (dt <= 0) return false;
      // A long gap means the app was backgrounded or signal was lost. Bridging
      // it would fabricate a huge acceleration, so start a fresh segment.
      const gap = dt > 10;
      const d = distM(prev, s);
      if (!gap) this.distance += d;

      // Prefer the receiver's own speed - it uses Doppler and is far better
      // than differencing two noisy positions.
      s.speed = s.rawSpeed != null ? s.rawSpeed : (gap ? 0 : d / dt);
      this._speedBuf.push(s.speed);
      if (this._speedBuf.length > SMOOTH_N) this._speedBuf.shift();
      s.smooth = this._speedBuf.reduce((a,b)=>a+b,0) / this._speedBuf.length;

      if (gap){ this._headAnchor = null; this._heading = null; this._turnBuf = []; }

      if (!gap && prev.smooth != null){
        s.accel = (s.smooth - prev.smooth) / dt;
        this._updateHeading(s);
        s.heading = this._heading;
        this._detect(s);
      }
    } else {
      s.speed = s.rawSpeed ?? 0;
      s.smooth = s.speed;
      this._speedBuf.push(s.speed);
      this.startedAt = t;
    }

    this.samples.push(s);
    this.endedAt = t;
    return true;
  }

  /** Heading over a long baseline, or the receiver's own heading if it has one.
   *  GPS heading is Doppler-derived and far more stable than anything we can
   *  compute from two positions, so it wins whenever it's available. */
  _updateHeading(s){
    let newHeading = null, dtHead = null;

    if (s.rawHeading != null && s.smooth > MIN_SPEED_MS){
      newHeading = s.rawHeading;
      dtHead = this._headAnchor ? (s.t - this._headAnchor.t)/1000 : null;
      this._headAnchor = s;
    } else {
      if (!this._headAnchor){ this._headAnchor = s; return; }
      const elapsed = (s.t - this._headAnchor.t) / 1000;
      const d = distM(this._headAnchor, s);
      if (elapsed < HEADING_BASELINE_S || d < HEADING_MIN_DIST_M) return;
      newHeading = bearing(this._headAnchor, s);
      dtHead = (s.t - this._headAnchor.t)/1000;
      this._headAnchor = s;
    }

    if (this._heading != null && dtHead > 0 && s.smooth > MIN_SPEED_MS){
      const turnRate = angleDiff(this._heading, newHeading) / dtHead;  // deg/s
      const lat = Math.abs(turnRate * Math.PI/180 * s.smooth);         // m/s^2
      if (lat > MAX_PLAUSIBLE_LAT){
        this._noiseRejected++;
      } else {
        this._turnBuf.push(lat);
        if (this._turnBuf.length > TURN_SMOOTH_N) this._turnBuf.shift();
        s.lateral = this._turnBuf.reduce((a,b)=>a+b,0) / this._turnBuf.length;
      }
    }
    this._heading = newHeading;
  }

  _fire(type, severity, at, value){
    if (at - (this._lastEventAt[type] || 0) < MIN_EVENT_GAP_MS) return;
    this._lastEventAt[type] = at;
    this.events.push({ type, severity, at, value: Math.round(value*100)/100 });
  }

  _detect(s){
    if (s.accel != null){
      if (s.accel <= HARSH.brakeSevere)      this._fire("brake","severe",s.t,s.accel);
      else if (s.accel <= HARSH.brake)       this._fire("brake","harsh", s.t,s.accel);
      else if (s.accel >= HARSH.accelSevere) this._fire("accel","severe",s.t,s.accel);
      else if (s.accel >= HARSH.accel)       this._fire("accel","harsh", s.t,s.accel);
    }
    if (s.lateral != null){
      if (s.lateral >= HARSH.cornerSevere)   this._fire("corner","severe",s.t,s.lateral);
      else if (s.lateral >= HARSH.corner)    this._fire("corner","harsh", s.t,s.lateral);
    }
  }

  summary(){
    const moving = this.samples.filter(s => (s.smooth ?? 0) > MIN_SPEED_MS);
    const durationS = this.startedAt && this.endedAt
      ? (this.endedAt - this.startedAt)/1000 : 0;
    const speeds = moving.map(s => s.smooth);
    const avg = speeds.length ? speeds.reduce((a,b)=>a+b,0)/speeds.length : 0;
    const max = speeds.length ? Math.max(...speeds) : 0;

    // Coefficient of variation: steadiness independent of how fast you went, so
    // a steady 100km/h motorway run isn't penalised against a steady 40 in town.
    const variance = speeds.length
      ? speeds.reduce((a,v)=>a+(v-avg)**2,0)/speeds.length : 0;
    const cv = avg > 0 ? Math.sqrt(variance)/avg : 0;

    // Moving vs stopped, from sample spacing rather than sample count, so a
    // dropped-signal gap doesn't get counted as time spent stationary.
    let movingS = 0, stoppedS = 0;
    for (let i = 1; i < this.samples.length; i++){
      const dt = (this.samples[i].t - this.samples[i-1].t)/1000;
      if (dt <= 0 || dt > 10) continue;
      if ((this.samples[i].smooth ?? 0) > MIN_SPEED_MS) movingS += dt;
      else stoppedS += dt;
    }

    const counts = { brake:0, accel:0, corner:0, severe:0 };
    for (const e of this.events){
      counts[e.type] = (counts[e.type] || 0) + 1;
      if (e.severity === "severe") counts.severe++;
    }

    const peakBrake = Math.min(0, ...this.samples.map(s => s.accel ?? 0));
    const peakAccel = Math.max(0, ...this.samples.map(s => s.accel ?? 0));
    const peakLat   = Math.max(0, ...this.samples.map(s => s.lateral ?? 0));

    return {
      startedAt: this.startedAt, endedAt: this.endedAt,
      durationS: Math.round(durationS),
      distanceM: Math.round(this.distance),
      avgSpeedKmh: +(avg*3.6).toFixed(1),
      maxSpeedKmh: +(max*3.6).toFixed(1),
      samples: this.samples.length,
      rejected: this._rejected,
      noiseRejected: this._noiseRejected,
      events: this.events.slice(),
      cv: +cv.toFixed(3),
      movingS: Math.round(movingS),
      stoppedS: Math.round(stoppedS),
      counts,
      peakBrakeMs2: +peakBrake.toFixed(2),
      peakAccelMs2: +peakAccel.toFixed(2),
      peakLateralMs2: +peakLat.toFixed(2),
    };
  }
}

/** Score a summary 0-100, with a transparent per-component breakdown. */
function scoreTrip(sum){
  const km = Math.max(sum.distanceM / 1000, 0.1);

  // Too short to judge. Returning 100 for a 200m trip would let anyone farm a
  // perfect average, and returning 0 would be just as wrong.
  if (sum.distanceM < 500 || sum.durationS < 60){
    return { score: null, tooShort: true, km,
             reason: "Trip too short to score (needs 500m and 1 minute)." };
  }

  const per = (type) => {
    const e = sum.events.filter(x => x.type === type);
    const weighted = e.reduce((a,x) => a + (x.severity === "severe" ? 2 : 1), 0);
    return weighted / km;   // per kilometre, so long trips aren't punished
  };

  const brake  = per("brake");
  const accel  = per("accel");
  const corner = per("corner");

  // Each component: 100 down to 0. The divisor is the rate at which the
  // component is fully lost - e.g. 1.5 harsh brakes per km scores 0 there.
  const comp = (rate, full) => Math.max(0, Math.min(100, 100 * (1 - rate/full)));
  const cBrake  = comp(brake,  1.5);
  const cAccel  = comp(accel,  1.5);
  const cCorner = comp(corner, 1.2);
  // CV above ~0.55 is genuinely erratic; stop-go city traffic sits near 0.4 and
  // shouldn't be scored as bad driving.
  const cSteady = comp(Math.max(0, sum.cv - 0.25), 0.35);

  const score = Math.round(
    0.35*cBrake + 0.25*cAccel + 0.25*cCorner + 0.15*cSteady
  );

  return {
    score, tooShort: false, km,
    components: {
      braking:     Math.round(cBrake),
      acceleration:Math.round(cAccel),
      cornering:   Math.round(cCorner),
      steadiness:  Math.round(cSteady),
    },
    rates: {
      brakePerKm:  +brake.toFixed(2),
      accelPerKm:  +accel.toFixed(2),
      cornerPerKm: +corner.toFixed(2),
    },
  };
}

// Everything above is private to this IIFE, and that is deliberate rather than
// stylistic. This file previously leaked its helpers into global scope, where
// its two-argument distM(a, b) collided with the page's own four-argument
// distM(lat1, lon1, lat2, lon2). The page's script tag loads second, so it
// silently replaced this one and every distance inside the recorder became NaN.
// Node never saw it, because there the module is properly scoped - which is
// exactly the class of bug a headless test suite cannot catch for you.
const api = { TripRecorder, scoreTrip, HARSH,
              _internals: { distM, bearing, angleDiff } };

if (typeof module !== "undefined" && module.exports) module.exports = api;
root.RoadwatchTrip = api;

})(typeof globalThis !== "undefined" ? globalThis : this);
