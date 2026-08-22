/* Synthetic drive tests for trip.js.
 *
 * Each generator produces a GPS trace whose correct verdict is known in
 * advance, so the thresholds can be checked rather than asserted. Run:
 *     node tools/test_trip.js
 */
const { TripRecorder, scoreTrip } = require("../app/trip.js");

const HZ = 1;                    // GPS fixes per second
const R = 6371000, rad = Math.PI/180;

/** Walk a bearing at a speed profile, emitting fixes. */
function drive({ seconds, speedAt, headingAt, accuracy = 8, noiseM = 0, seed = 1 }){
  let lat = 3.10, lon = 101.62, t = Date.now();
  let rnd = seed;
  const rand = () => (rnd = (rnd*1103515245 + 12345) % 2147483648) / 2147483648 - 0.5;
  const out = [];
  for (let i = 0; i < seconds*HZ; i++){
    const s = Math.max(0, speedAt(i/HZ));
    const h = headingAt ? headingAt(i/HZ) : 90;
    const d = s / HZ;
    lat += (d * Math.cos(h*rad)) / R / rad;
    lon += (d * Math.sin(h*rad)) / (R * Math.cos(lat*rad)) / rad;
    out.push({
      coords: { latitude: lat + rand()*noiseM/111000,
                longitude: lon + rand()*noiseM/111000,
                speed: s, accuracy },
      timestamp: t + (i * 1000 / HZ),
    });
  }
  return out;
}

function run(name, fixes){
  const r = new TripRecorder();
  fixes.forEach(f => r.add(f));
  const sum = r.summary();
  const sc = scoreTrip(sum);
  const ev = {};
  sum.events.forEach(e => { ev[e.type] = (ev[e.type]||0) + 1; });
  console.log(
    `  ${name.padEnd(26)} score=${String(sc.score ?? "n/a").padStart(4)}` +
    `  ${(sum.distanceM/1000).toFixed(1)}km` +
    `  cv=${sum.cv.toFixed(2)}` +
    `  events=${JSON.stringify(ev)}` +
    (sc.components ? `  ${JSON.stringify(sc.components)}` : ` ${sc.reason||""}`)
  );
  return { sum, sc };
}

console.log("\nSYNTHETIC DRIVES\n");

// 1. Smooth motorway cruise. Should score near perfect.
const smooth = run("smooth cruise 100km/h",
  drive({ seconds: 600, speedAt: () => 27.8 }));

// 2. Same distance but braking hard every ~40s.
const braky = run("harsh braking every 40s",
  drive({ seconds: 600, speedAt: (s) => {
    const p = s % 40;
    return p < 34 ? 27.8 : 27.8 - (p-34)*5.5;   // ~-5.5 m/s^2
  }}));

// 3. Aggressive launches.
const accely = run("hard acceleration",
  drive({ seconds: 600, speedAt: (s) => {
    const p = s % 40;
    return p < 8 ? p*5.0 : 40;
  }}));

// 4. Hard cornering: tight repeated turns at speed.
const cornery = run("hard cornering",
  drive({ seconds: 600, speedAt: () => 18,
          headingAt: (s) => 90 + Math.sin(s/3.2)*60 }));

// A long motorway sweep. Real curvature, but nothing a passenger would notice.
const sweep = run("gentle motorway sweep",
  drive({ seconds: 600, speedAt: () => 27.8,
          headingAt: (s) => 90 + Math.sin(s/40)*25 }));

// 5. Stop-go city traffic. Should NOT be scored as terrible driving.
const city = run("stop-go city traffic",
  drive({ seconds: 600, speedAt: (s) => {
    const p = s % 60;
    if (p < 20) return 11;
    if (p < 26) return 11 - (p-20)*1.8;   // gentle, ~-1.8 m/s^2
    if (p < 40) return 0;
    return Math.min(11, (p-40)*1.5);      // gentle pull away
  }}));

// 6. Noisy GPS on an otherwise smooth drive - must not invent events.
const noisy = run("smooth + 15m GPS noise",
  drive({ seconds: 600, speedAt: () => 27.8, noiseM: 15, seed: 77 }));

// 7. Bad-accuracy fixes must be rejected outright.
const bad = run("poor accuracy (80m)",
  drive({ seconds: 600, speedAt: () => 27.8, accuracy: 80 }));

// 8. Too short to score.
const tiny = run("30s crawl",
  drive({ seconds: 30, speedAt: () => 5 }));

console.log("\nASSERTIONS\n");
let fail = 0;
const check = (name, cond) => {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) fail++;
};

check("smooth cruise scores >= 95", smooth.sc.score >= 95);
check("harsh braking scores well below smooth", braky.sc.score < smooth.sc.score - 25);
check("braking hit is on the braking component", braky.sc.components.braking < 60);
check("hard acceleration penalised", accely.sc.components.acceleration < 70);
check("hard cornering penalised", cornery.sc.components.cornering < 70);
check("gentle sweep not penalised", sweep.sc.components.cornering >= 90);
check("city traffic still scores >= 70", city.sc.score >= 70);
check("GPS noise does not fabricate events",
      noisy.sum.events.length <= 2 && noisy.sc.score >= 85);
check("poor-accuracy fixes rejected", bad.sum.rejected > 0 && bad.sum.samples === 0);
check("short trip unscored", tiny.sc.score === null && tiny.sc.tooShort);

console.log(`\n${fail === 0 ? "all assertions passed" : fail + " FAILED"}\n`);
process.exit(fail ? 1 : 0);
