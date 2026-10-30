/* static_backend.js — stands in for game/server.py on static hosting (GitHub Pages).
   Answers the same API calls in the browser from the JSON made by game/build_site.py.
   The scoring must match game/scoring.py and game/geolite.py; tests/test_static.py checks it. */
(() => {
"use strict";

const PLACE_MAX = 5000, PLACE_SCALE_KM = 2000, TIME_MAX = 5000, TIME_SCALE_YEARS = 1000;
const BLOCK_YEARS = 250, BLOCK_SNAP = 125, TIMELINE_MIN = -3000, BLOCK_START_MAX = 1750;
const ROUNDS_PER_GAME = 10, EARTH_KM = 6371.0, EPOCH_UTC = Date.UTC(2026, 0, 1);

function haversineKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180, p1 = lat1 * rad, p2 = lat2 * rad;
  const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((lon2 - lon1) * rad / 2) ** 2;
  return 2 * EARTH_KM * Math.asin(Math.sqrt(a));
}

function inRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0; i < ring.length - 1; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[i + 1];
    if ((y1 > lat) !== (y2 > lat) && lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1) inside = !inside;
  }
  return inside;
}

function distanceToRegionKm(lat, lon, polygons) {
  lon = (((lon + 180) % 360) + 360) % 360 - 180;
  if (polygons.some((p) => inRing(lon, lat, p[0]) && !p.slice(1).some((hole) => inRing(lon, lat, hole)))) return 0;
  let best = Infinity;
  for (const polygon of polygons) for (const ring of polygon) {
    for (let i = 0; i < ring.length - 1; i++) {
      const [x1, y1] = ring[i], [x2, y2] = ring[i + 1];
      const dx = x2 - x1, dy = y2 - y1, seg = dx * dx + dy * dy;
      const t = seg === 0 ? 0 : Math.max(0, Math.min(1, ((lon - x1) * dx + (lat - y1) * dy) / seg));
      best = Math.min(best, haversineKm(lat, lon, y1 + t * dy, x1 + t * dx));
    }
  }
  return best;
}

function normalizeBlock(year) {
  return Math.min(BLOCK_START_MAX, Math.max(TIMELINE_MIN, Math.round(year / BLOCK_SNAP) * BLOCK_SNAP));
}

function yearGap(block, begin, end) {
  const blockEnd = block + BLOCK_YEARS;
  if (blockEnd >= begin && block <= end) return 0;
  return blockEnd < begin ? begin - blockEnd : block - end;
}

function fmtYear(y) {
  return y < 0 ? `${(-y).toLocaleString("en-US")} BCE` : `${y} CE`;
}

function scoreGuess(obj, polygons, lat, lon, year) {
  const block = normalizeBlock(year);
  const km = distanceToRegionKm(lat, lon, polygons);
  const gap = yearGap(block, obj.begin, obj.end);
  const placeScore = Math.round(PLACE_MAX * Math.exp(-km / PLACE_SCALE_KM));
  const timeScore = Math.round(TIME_MAX * Math.exp(-gap / TIME_SCALE_YEARS));
  return {
    place_score: placeScore,
    time_score: timeScore,
    total: placeScore + timeScore,
    max: PLACE_MAX + TIME_MAX,
    km: Math.round(km * 10) / 10,
    in_region: km === 0,
    year_gap: gap,
    guess: { lat, lon, block_start: block, block_end: block + BLOCK_YEARS },
  };
}

if (typeof module !== "undefined") {
  module.exports = { scoreGuess };
} else {
  let dataPromise = null;
  const loadData = () => (dataPromise ??= Promise.all(
    ["objects", "regions", "daily"].map((name) => fetch(`gamedata/${name}.json`).then((r) => {
      if (!r.ok) throw new Error(`gamedata/${name}.json: ${r.status}`);
      return r.json();
    })),
  ).then(([objects, regions, daily]) => ({ objects, regions, daily })).catch((e) => {
    dataPromise = null;   // let the next click retry
    throw e;
  }));

  const toRound = (obj, index) => ({
    round_id: String(index),
    photos: obj.photos,
    medium: obj.medium,
    credit: "The Metropolitan Museum of Art, CC0",
  });

  function practiceRounds({ objects }) {
    // Fisher-Yates, then one object per place, as the server does.
    const order = objects.map((_, i) => i);
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    const seen = new Set(), picks = [];
    for (const i of order) {
      if (seen.has(objects[i].region)) continue;
      seen.add(objects[i].region);
      picks.push(i);
      if (picks.length === ROUNDS_PER_GAME) break;
    }
    return { kind: "practice", day: null, rounds: picks.map((i) => toRound(objects[i], i)) };
  }

  function dailyRounds({ objects, daily }, day) {
    const [y, m, d] = day.split("-").map(Number);
    const offset = Math.round((Date.UTC(y, m - 1, d) - EPOCH_UTC) / 86400000);
    if (!(offset >= 0)) throw new Error(`no daily game for ${day}`);
    const picks = daily[offset % daily.length];   // the schedule repeats after it runs out
    return { kind: "daily", day, rounds: picks.map((i) => toRound(objects[i], i)) };
  }

  function guess({ objects, regions }, body) {
    const obj = objects[Number(body.round_id)];
    const polygons = regions[obj.region];
    return {
      ...scoreGuess(obj, polygons, body.lat, body.lon, body.year),
      truth: {
        object_id: obj.id,
        title: obj.title,
        object_name: obj.object_name,
        culture: obj.culture,
        period: obj.period,
        object_date: obj.object_date,
        medium: obj.medium,
        department: obj.department,
        url: obj.url,
        begin: obj.begin,
        end: obj.end,
        range_text: obj.begin === obj.end ? fmtYear(obj.begin) : `${fmtYear(obj.begin)} – ${fmtYear(obj.end)}`,
        place_label: obj.place,
        lat: obj.lat,
        lon: obj.lon,
        region: { type: "MultiPolygon", coordinates: polygons },
      },
    };
  }

  window.StaticBackend = {
    countriesUrl: "countries.json",
    fullPhoto: (src) => src.replace("/web-large/", "/original/"),
    async api(path, body) {
      const data = await loadData();
      if (path === "/api/practice") return practiceRounds(data);
      if (path.startsWith("/api/daily")) return dailyRounds(data, new URLSearchParams(path.split("?")[1]).get("date"));
      if (path === "/api/guess") return guess(data, body);
      throw new Error(`unknown path ${path}`);
    },
  };
}
})();
