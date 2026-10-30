/* app.js — Provenance frontend.
   Screens: home → ten rounds (guess, reveal) → results. Daily games persist in
   localStorage so a reload resumes the same round. All scoring happens server-side. */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const fmt = (n) => Math.round(n).toLocaleString("en-US");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const TL_MIN = -3000, TL_MAX = 2025, BLOCK = 250, SNAP = 125, BLOCK_START_MAX = 1750;
const HINTS = 3, ROUNDS = 10;
const HINT_KINDS = [
  { kind: "time", label: "Time", gives: "Narrows the date to a 1,000-year span" },
  { kind: "place", label: "Place", gives: "Names the world region it comes from" },
];

const game = {
  kind: null,        // "daily" | "practice"
  day: null,
  rounds: [],
  index: 0,
  results: [],       // api responses, one per finished round
  hints: {},         // { roundIndex: { time: "text", place: "text" } }
};
const turn = { pin: null, block: null, photo: 0, revealing: false };

/* ---------- storage ---------- */
const store = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage full or disabled */ }
  },
};
const dayKey = (day) => `provenance:game:${day}`;
const HISTORY_KEY = "provenance:history";

function localDay() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function prettyDay(day) {
  const [y, m, d] = day.split("-").map(Number);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[m - 1]} ${d} ${y}`;
}
function saveGame() {
  if (game.kind === "daily") store.set(dayKey(game.day), game);
}

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 2400);
}

/* ---------- screens ---------- */
function show(id) {
  for (const s of ["home", "game", "results"]) $(s).hidden = s !== id;
  if (id === "game") { fitMap(); drawTimeline(); }
}

function renderHome() {
  const day = localDay();
  const saved = store.get(dayKey(day), null);
  const finished = saved && saved.results.length >= ROUNDS;
  const inProgress = saved && !finished && saved.results.length > 0;
  $("home-date").textContent = prettyDay(day);
  $("home-copy").textContent = finished
    ? "You've finished today's objects. New ones arrive tomorrow."
    : inProgress
      ? `Your game is in progress. Round ${saved.results.length + 1} of ${ROUNDS}. Pick up right where you left off.`
      : "Ten new objects every day, from across human history. Place each one on the map and the timeline.";
  $("btn-play").textContent = finished ? "See your results" : inProgress ? "Resume game" : "Play today's game";
  show("home");
}

async function startDaily() {
  const day = localDay();
  const saved = store.get(dayKey(day), null);
  if (saved && saved.rounds && saved.rounds.length) {
    Object.assign(game, saved);
  } else {
    const data = await api(`/api/daily?date=${day}`);
    Object.assign(game, { kind: "daily", day: data.day, rounds: data.rounds, index: 0, results: [], hints: {} });
    saveGame();
  }
  if (game.results.length >= ROUNDS) return renderResults();
  beginRound();
}

async function startPractice() {
  const data = await api("/api/practice");
  Object.assign(game, { kind: "practice", day: null, rounds: data.rounds, index: 0, results: [], hints: {} });
  beginRound();
}

/* ---------- rounds ---------- */
function beginRound() {
  game.index = game.results.length;
  Object.assign(turn, { pin: null, block: null, photo: 0, revealing: false });
  view.reset();
  $("reveal").hidden = true;
  $("reveal").replaceChildren();
  $("hints").hidden = false;
  show("game");
  renderStats();
  renderPhoto();
  renderHints();
  renderAction();
  drawMap();
  drawTimeline();
}

function renderStats() {
  $("stat-round").textContent = `${Math.min(game.index + 1, ROUNDS)} / ${ROUNDS}`;
  $("stat-score").textContent = fmt(game.results.reduce((sum, r) => sum + r.total, 0));
}

function renderAction() {
  const b = $("btn-action");
  if (turn.revealing) {
    b.disabled = false;
    b.textContent = game.index + 1 >= ROUNDS ? "See final score" : "Next round →";
  } else {
    const hasPin = !!turn.pin, hasBlock = turn.block !== null;
    b.disabled = !(hasPin && hasBlock);
    b.textContent = hasPin && hasBlock ? "Submit guess"
      : hasPin ? "Select a time"
      : hasBlock ? "Select a location" : "Drop a pin and pick a time";
  }
}

async function onAction() {
  if (turn.revealing) {
    if (game.results.length >= ROUNDS) renderResults();
    else beginRound();
    return;
  }
  if (!turn.pin || turn.block === null) return;
  const btn = $("btn-action");
  btn.disabled = true;
  btn.textContent = "Scoring…";
  try {
    const res = await api("/api/guess", {
      round_id: game.rounds[game.index].round_id,
      lat: +turn.pin.lat.toFixed(4),
      lon: +turn.pin.lon.toFixed(4),
      year: turn.block,
    });
    game.results.push(res);
    if (game.results.length >= ROUNDS) recordHistory();
    saveGame();
    turn.revealing = true;
    renderReveal(res);
    renderStats();
    renderAction();
    view.fitTo(res);
    drawMap();
    drawTimeline();
  } catch {
    btn.disabled = false;
    btn.textContent = "Connection hiccup — tap to retry";
  }
}

function recordHistory() {
  const total = game.results.reduce((sum, r) => sum + r.total, 0);
  const history = store.get(HISTORY_KEY, []);
  const label = game.kind === "daily" ? game.day : "practice";
  if (game.kind !== "daily" || !history.some((h) => h.label === label)) {
    history.push({ label, total, at: Date.now() });
    store.set(HISTORY_KEY, history.slice(-200));
  }
}

/* ---------- reveal ---------- */
function card(title, main, pts, ptsIsPlain) {
  const c = el("div", "card");
  c.append(el("div", "eyebrow", title), el("div", "card-main", main),
    ptsIsPlain ? el("div", "eyebrow", pts) : el("div", "card-pts", `+${fmt(pts)}`));
  return c;
}

function renderReveal(res) {
  const t = res.truth, box = $("reveal");
  box.replaceChildren();
  box.append(el("div", "reveal-culture", t.culture || t.place_label));
  box.append(el("div", "reveal-title", t.title || t.object_name));
  const cards = el("div", "cards");
  cards.append(
    card("Place", res.in_region ? "Inside region" : `${fmt(res.km)} km`, res.place_score),
    card("Time", res.year_gap === 0 ? "Inside range" : `${fmt(res.year_gap)} yrs`, res.time_score),
    card("Round", fmt(res.total), "of 10,000", true),
  );
  box.append(cards);
  const dl = el("dl");
  const row = (k, v) => { if (v) dl.append(el("dt", "eyebrow", k), el("dd", "", v)); };
  row("Region", t.place_label);
  row("Date", t.object_date || t.range_text);
  row("Range", t.object_date ? t.range_text : "");
  box.append(dl);
  if (t.url) {
    const a = el("a", "", "View on The Met →");
    a.href = t.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    box.append(a);
  }
  box.hidden = false;
  $("hints").hidden = true;
}

/* ---------- photos ---------- */
function photos() { return game.rounds[game.index]?.photos ?? []; }
function renderPhoto() {
  const list = photos();
  $("photo").src = list[turn.photo] ?? "";
  const multi = list.length > 1;
  $("ph-prev").hidden = $("ph-next").hidden = !multi;
  $("lb-prev").hidden = $("lb-next").hidden = !multi;
  $("ph-count").hidden = !multi;
  $("ph-count").textContent = `${turn.photo + 1} / ${list.length}`;
  $("credit").textContent = game.rounds[game.index]?.credit ?? "";
  const medium = game.rounds[game.index]?.medium ?? "";
  $("medium").textContent = medium;
  $("medium").hidden = !medium;
}
function stepPhoto(d) {
  const n = photos().length;
  if (n < 2) return;
  turn.photo = (turn.photo + d + n) % n;
  renderPhoto();
  if (!$("lightbox").hidden) showLightboxPhoto();
}

/* ---------- fullscreen photo: scroll to zoom, drag to move ---------- */
const LB_MAX_SCALE = 12;
const lb = { scale: 1, x: 0, y: 0, pointers: new Map(), drag: null, pinch: null, loading: 0 };

function applyLightbox() {
  const box = $("lightbox");
  const maxX = (lb.scale - 1) * box.clientWidth / 2, maxY = (lb.scale - 1) * box.clientHeight / 2;
  lb.x = Math.max(-maxX, Math.min(maxX, lb.x));
  lb.y = Math.max(-maxY, Math.min(maxY, lb.y));
  $("lightbox-img").style.transform = `translate(${lb.x}px, ${lb.y}px) scale(${lb.scale})`;
  box.classList.toggle("zoomed", lb.scale > 1);
}
function zoomLightbox(clientX, clientY, factor) {
  const box = $("lightbox");
  const next = Math.max(1, Math.min(LB_MAX_SCALE, lb.scale * factor));
  // Keep the point under the cursor in place.
  const cx = clientX - box.clientWidth / 2, cy = clientY - box.clientHeight / 2;
  lb.x = cx - (cx - lb.x) * (next / lb.scale);
  lb.y = cy - (cy - lb.y) * (next / lb.scale);
  lb.scale = next;
  applyLightbox();
}
function showLightboxPhoto() {
  const src = photos()[turn.photo] ?? "", img = $("lightbox-img");
  Object.assign(lb, { scale: 1, x: 0, y: 0 });
  applyLightbox();
  img.src = src;
  // Show the normal photo at once, then swap in the full-resolution one for zooming.
  const token = ++lb.loading, full = new Image();
  full.onload = () => { if (token === lb.loading) img.src = full.src; };
  full.src = `${src}?full=true`;
}
function openLightbox() {
  $("lightbox").hidden = false;
  showLightboxPhoto();
}
function closeLightbox() {
  lb.loading++;
  lb.pointers.clear();
  lb.drag = lb.pinch = null;
  $("lightbox").classList.remove("dragging");
  $("lightbox").hidden = true;
}

/* ---------- hints ---------- */
function hintsUsed() {
  return Object.values(game.hints).reduce((sum, h) => sum + Object.keys(h).length, 0);
}
function renderHints() {
  const left = HINTS - hintsUsed();
  const noun = game.kind === "daily" ? "today" : "this game";
  $("hints-left").textContent = `${left} of ${HINTS} left ${noun}`;
  const shown = $("hints-shown");
  shown.replaceChildren();
  const here = game.hints[game.index] ?? {};
  for (const h of HINT_KINDS) {
    if (here[h.kind]) shown.append(el("dt", "eyebrow", h.label), el("dd", "", here[h.kind]));
  }
}
function renderHintModal() {
  const left = HINTS - hintsUsed();
  const copy = $("hint-copy");
  copy.replaceChildren(`You have ${HINTS} hints to spend across all ten rounds, and they never cost you points. You have `,
    el("strong", "", String(left)), " left.");
  const opts = $("hint-options");
  opts.replaceChildren();
  const here = game.hints[game.index] ?? {};
  for (const h of HINT_KINDS) {
    const row = el("div", "hint-option");
    const text = el("div");
    text.append(el("b", "", h.label), el("div", "", here[h.kind] ?? h.gives));
    row.append(text);
    if (!here[h.kind]) {
      const b = el("button", "btn-outline", "Reveal");
      b.disabled = left <= 0;
      b.addEventListener("click", () => revealHint(h.kind));
      row.append(b);
    }
    opts.append(row);
  }
}
async function revealHint(kind) {
  if (hintsUsed() >= HINTS) return;
  try {
    const res = await api("/api/hint", { round_id: game.rounds[game.index].round_id, kind });
    (game.hints[game.index] ??= {})[kind] = res.text;
    saveGame();
    renderHints();
    renderHintModal();
  } catch {
    toast("Couldn't load that hint");
  }
}

/* ---------- map ---------- */
// Miller projection cropped to inhabited latitudes, which fills the map box far better than plate carree.
const miller = (lat) => 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * Math.PI / 180));
const WORLD_W = 1440, MAP_NORTH = 84, MAP_SOUTH = -58;
const PX_PER_RAD = WORLD_W / (2 * Math.PI);
const WORLD_H = (miller(MAP_NORTH) - miller(MAP_SOUTH)) * PX_PER_RAD;
const canv = $("map"), ctx = canv.getContext("2d");
const toWorld = (lon, lat) => [(lon + 180) / 360 * WORLD_W, (miller(MAP_NORTH) - miller(lat)) * PX_PER_RAD];
const fromWorld = (x, y) => [
  x / WORLD_W * 360 - 180,
  (2.5 * Math.atan(Math.exp(0.8 * (miller(MAP_NORTH) - y / PX_PER_RAD))) - 5 * Math.PI / 8) * 180 / Math.PI,
];
let landPath = null, labels = [];
let regionCache = { res: null, path: null };

const view = {
  zoom: 1, cx: WORLD_W / 2, cy: WORLD_H / 2,
  minZoom() { return Math.min(canv.width / WORLD_W, canv.height / WORLD_H); },
  reset() {
    // On tall screens the whole world is a thin strip, so start closer in, on the Old World.
    const tall = canv.width / canv.height < 1.2;
    this.zoom = this.minZoom() * (tall ? 1.9 : 1);
    this.cx = tall ? WORLD_W * 0.56 : WORLD_W / 2;
    this.cy = WORLD_H * (tall ? 0.42 : 0.5);
    this.clamp();
  },
  clamp() {
    this.zoom = Math.max(this.minZoom(), Math.min(this.minZoom() * 40, this.zoom));
    const hw = canv.width / this.zoom / 2, hh = canv.height / this.zoom / 2;
    this.cx = hw * 2 >= WORLD_W ? WORLD_W / 2 : Math.max(hw, Math.min(WORLD_W - hw, this.cx));
    this.cy = hh * 2 >= WORLD_H ? WORLD_H / 2 : Math.max(hh, Math.min(WORLD_H - hh, this.cy));
  },
  zoomAt(px, py, factor) {
    const wx = (px - canv.width / 2) / this.zoom + this.cx;
    const wy = (py - canv.height / 2) / this.zoom + this.cy;
    this.zoom *= factor;
    this.clamp();
    this.cx = wx - (px - canv.width / 2) / this.zoom;
    this.cy = wy - (py - canv.height / 2) / this.zoom;
    this.clamp();
  },
  fitTo(res) {   // frame the guess and the whole answer region
    const [gx, gy] = toWorld(res.guess.lon, res.guess.lat);
    let x0 = gx, x1 = gx, y0 = gy, y1 = gy;
    for (const poly of res.truth.region.coordinates) for (const [lon, lat] of poly[0]) {
      const [x, y] = toWorld(lon, lat);
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    const pad = 1.35;
    this.zoom = Math.min(canv.width / ((x1 - x0) * pad + 1), canv.height / ((y1 - y0) * pad + 1), this.minZoom() * 14);
    this.cx = (x0 + x1) / 2; this.cy = (y0 + y1) / 2;
    this.clamp();
  },
};

function geoPath(polygons) {
  const path = new Path2D();
  for (const poly of polygons) for (const ring of poly) {
    ring.forEach(([lon, lat], i) => {
      const [x, y] = toWorld(lon, lat);
      if (i) path.lineTo(x, y); else path.moveTo(x, y);
    });
    path.closePath();
  }
  return path;
}

function screenXY(lon, lat) {
  const [x, y] = toWorld(lon, lat);
  return [(x - view.cx) * view.zoom + canv.width / 2, (y - view.cy) * view.zoom + canv.height / 2];
}

function drawPin(x, y, fill) {
  const d = window.devicePixelRatio || 1, r = 8 * d;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.bezierCurveTo(x - r * 0.35, y - r * 0.9, x - r, y - r * 1.1, x - r, y - r * 1.9);
  ctx.arc(x, y - r * 1.9, r, Math.PI, 0);
  ctx.bezierCurveTo(x + r, y - r * 1.1, x + r * 0.35, y - r * 0.9, x, y);
  ctx.closePath();
  ctx.fillStyle = fill; ctx.fill();
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5 * d; ctx.stroke();
  ctx.beginPath(); ctx.arc(x, y - r * 1.9, r * 0.36, 0, 2 * Math.PI);
  ctx.fillStyle = "#fff"; ctx.fill();
}

function drawMap() {
  if (!canv.width) return;
  const d = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = css("--sea");
  ctx.fillRect(0, 0, canv.width, canv.height);

  const toScreen = () => ctx.setTransform(view.zoom, 0, 0, view.zoom,
    canv.width / 2 - view.cx * view.zoom, canv.height / 2 - view.cy * view.zoom);
  toScreen();
  if (landPath) {
    ctx.fillStyle = css("--land"); ctx.fill(landPath);
    ctx.strokeStyle = css("--border"); ctx.lineWidth = 0.7 * d / view.zoom; ctx.stroke(landPath);
  }
  const res = turn.revealing ? game.results[game.index] : null;
  if (res) {
    if (regionCache.res !== res) regionCache = { res, path: geoPath(res.truth.region.coordinates) };
    ctx.globalAlpha = 0.45; ctx.fillStyle = css("--region"); ctx.fill(regionCache.path);
    ctx.globalAlpha = 1; ctx.strokeStyle = css("--region"); ctx.lineWidth = 1.2 * d / view.zoom; ctx.stroke(regionCache.path);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);

  // Country names appear as you zoom in, major countries first.
  const scale = view.zoom / view.minZoom();
  ctx.font = `italic ${11 * d}px Spectral, Georgia, serif`;
  ctx.fillStyle = css("--label");
  ctx.textAlign = "center";
  const placed = [], pad = 3 * d, lineH = 12 * d;
  for (const l of labels) {   // sorted biggest first, so small countries give way
    if (l.rank > 1.5 + scale) continue;
    const [x, y] = screenXY(l.lon, l.lat);
    if (x < 0 || y < 0 || x > canv.width || y > canv.height) continue;
    const hw = ctx.measureText(l.name).width / 2 + pad;
    if (placed.some((b) => Math.abs(b.x - x) < b.hw + hw && Math.abs(b.y - y) < lineH)) continue;
    placed.push({ x, y, hw });
    ctx.fillText(l.name, x, y);
  }

  if (res) {
    const [tx, ty] = nearestRegionPoint(res);
    const [gx, gy] = screenXY(res.guess.lon, res.guess.lat);
    if (!res.in_region) {
      ctx.save();
      ctx.strokeStyle = css("--pin"); ctx.lineWidth = 2 * d; ctx.setLineDash([6 * d, 6 * d]);
      ctx.beginPath(); ctx.moveTo(gx, gy); ctx.lineTo(tx, ty); ctx.stroke();
      ctx.restore();
    }
    drawPin(gx, gy, css("--pin"));
  } else if (turn.pin) {
    const [gx, gy] = screenXY(turn.pin.lon, turn.pin.lat);
    drawPin(gx, gy, css("--pin"));
  }
}

function nearestRegionPoint(res) {
  // Same nearest-border search the server scored with, so the dashed line ends where the km were measured.
  const { lat, lon } = res.guess;
  let best = Infinity, out = [lon, lat];
  for (const poly of res.truth.region.coordinates) for (const ring of poly) {
    for (let i = 0; i < ring.length - 1; i++) {
      const [x1, y1] = ring[i], [x2, y2] = ring[i + 1];
      const dx = x2 - x1, dy = y2 - y1, seg = dx * dx + dy * dy;
      const t = seg ? Math.max(0, Math.min(1, ((lon - x1) * dx + (lat - y1) * dy) / seg)) : 0;
      const px = x1 + t * dx, py = y1 + t * dy;
      const dist = (px - lon) ** 2 * Math.cos(lat * Math.PI / 180) ** 2 + (py - lat) ** 2;
      if (dist < best) { best = dist; out = [px, py]; }
    }
  }
  return screenXY(out[0], out[1]);
}

function fitMap() {
  const rect = canv.getBoundingClientRect();
  if (!rect.width) return;
  const d = window.devicePixelRatio || 1;
  const first = !canv.width || canv.width === 300;
  canv.width = Math.round(rect.width * d);
  canv.height = Math.round(rect.height * d);
  if (first) view.reset(); else view.clamp();
  if (turn.revealing && game.results[game.index]) view.fitTo(game.results[game.index]);
  drawMap();
}
window.addEventListener("resize", () => { if (!$("game").hidden) { fitMap(); drawTimeline(); } });

function canvasPoint(e) {
  const rect = canv.getBoundingClientRect(), d = canv.width / rect.width;
  return [(e.clientX - rect.left) * d, (e.clientY - rect.top) * d];
}

canv.addEventListener("wheel", (e) => {
  e.preventDefault();
  const [x, y] = canvasPoint(e);
  view.zoomAt(x, y, e.deltaY < 0 ? 1.25 : 0.8);
  drawMap();
}, { passive: false });

// One pointer pans (or taps to drop a pin); two pointers pinch-zoom.
const pointers = new Map();
let pan = null, pinch = null;
canv.addEventListener("pointerdown", (e) => {
  canv.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, e);
  if (pointers.size === 1) {
    pan = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy, moved: false };
  } else if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinch = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    pan = null;
  }
});
canv.addEventListener("pointermove", (e) => {
  if (!pointers.has(e.pointerId)) return;
  pointers.set(e.pointerId, e);
  if (pointers.size === 2 && pinch) {
    const [a, b] = [...pointers.values()];
    const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    const [mx, my] = canvasPoint({ clientX: (a.clientX + b.clientX) / 2, clientY: (a.clientY + b.clientY) / 2 });
    view.zoomAt(mx, my, dist / pinch);
    pinch = dist;
    drawMap();
  } else if (pan) {
    const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
    if (Math.hypot(dx, dy) > 4) { pan.moved = true; canv.classList.add("dragging"); }
    if (pan.moved) {
      const d = canv.width / canv.getBoundingClientRect().width;
      view.cx = pan.cx - dx * d / view.zoom;
      view.cy = pan.cy - dy * d / view.zoom;
      view.clamp();
      drawMap();
    }
  }
});
function endPointer(e) {
  const wasPan = pan;
  pointers.delete(e.pointerId);
  canv.classList.remove("dragging");
  if (pointers.size < 2) pinch = null;
  if (e.type === "pointerup" && wasPan && !wasPan.moved && pointers.size === 0) dropPin(e);
  if (pointers.size === 0) pan = null;
}
canv.addEventListener("pointerup", endPointer);
canv.addEventListener("pointercancel", endPointer);

function dropPin(e) {
  if (turn.revealing || !game.rounds.length) return;
  const [px, py] = canvasPoint(e);
  const wx = (px - canv.width / 2) / view.zoom + view.cx;
  const wy = (py - canv.height / 2) / view.zoom + view.cy;
  if (wx < 0 || wx > WORLD_W || wy < 0 || wy > WORLD_H) return;
  const [lon, lat] = fromWorld(wx, wy);
  turn.pin = { lon, lat };
  renderAction();
  drawMap();
}

$("zoom-in").addEventListener("click", () => { view.zoomAt(canv.width / 2, canv.height / 2, 1.6); drawMap(); });
$("zoom-out").addEventListener("click", () => { view.zoomAt(canv.width / 2, canv.height / 2, 1 / 1.6); drawMap(); });
$("zoom-reset").addEventListener("click", () => { view.reset(); drawMap(); });

/* ---------- timeline ---------- */
const svg = $("timeline");
const SVG = "http://www.w3.org/2000/svg";
const TL_PAD = 36, TL_AXIS = 22;

function svgEl(tag, attrs, text) {
  const n = document.createElementNS(SVG, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (text !== undefined) n.textContent = text;
  return n;
}
const tlWidth = () => svg.getBoundingClientRect().width;
function yearX(year, w) {
  const y = Math.max(TL_MIN, Math.min(TL_MAX, year));
  return TL_PAD + (y - TL_MIN) / (TL_MAX - TL_MIN) * Math.max(0, w - 2 * TL_PAD);
}
function xYear(x, w) {
  const f = Math.max(0, Math.min(1, (x - TL_PAD) / Math.max(1, w - 2 * TL_PAD)));
  return TL_MIN + f * (TL_MAX - TL_MIN);
}
function snapBlock(year) {
  const s = Math.round((year - SNAP) / SNAP) * SNAP;
  return Math.max(TL_MIN, Math.min(BLOCK_START_MAX, s));
}
const yearLabel = (y) => (y === 0 ? "1 BCE" : y < 0 ? `${-y} BCE` : `${y} CE`);
function rangeLabel(a, b) {
  if (a === b) return yearLabel(a);
  if (a > 0 && b > 0) return `${a}–${b} CE`;
  if (a < 0 && b < 0) return `${-a}–${-b} BCE`;
  return `${yearLabel(a)}–${yearLabel(b)}`;
}

function drawTimeline() {
  const w = tlWidth();
  if (!w) return;
  svg.replaceChildren();
  svg.classList.toggle("locked", turn.revealing);
  const axis = css("--axis"), tick = css("--tick");
  svg.append(svgEl("rect", { x: TL_PAD, y: TL_AXIS, width: Math.max(0, w - 2 * TL_PAD), height: 1, fill: axis }));
  const narrow = w < 560;
  for (let y = -3000; y <= 2000; y += 500) {
    const major = y % 1000 === 0;
    if (!major && narrow) continue;
    const x = yearX(y, w);
    svg.append(svgEl("line", { x1: x, x2: x, y1: TL_AXIS + (major ? 8 : 12), y2: TL_AXIS + 18, stroke: css("--muted") }));
    svg.append(svgEl("text", {
      x, y: TL_AXIS + 32, fill: tick, "font-size": major ? 12 : 11, "text-anchor": "middle",
    }, y === 0 ? "1 CE" : yearLabel(y)));
  }

  const res = turn.revealing ? game.results[game.index] : null;
  if (res) {   // true range, widened enough to stay visible
    const a = yearX(res.truth.begin, w), b = yearX(res.truth.end, w);
    const width = Math.max(22, b - a);
    svg.append(svgEl("rect", {
      x: (a + b) / 2 - width / 2, y: TL_AXIS - 11, width, height: 22, rx: 2,
      fill: css("--region"), "fill-opacity": 0.45, stroke: css("--region"),
    }));
  }
  if (turn.block !== null) {
    const x1 = yearX(turn.block, w), x2 = yearX(turn.block + BLOCK, w);
    svg.append(svgEl("rect", {
      x: x1, y: TL_AXIS - 8, width: Math.max(4, x2 - x1), height: 16, rx: 2, fill: css("--pin"), "fill-opacity": 0.9,
    }));
  } else {
    svg.append(svgEl("text", {
      x: w / 2, y: TL_AXIS - 6, fill: tick, "font-size": 12, "font-style": "italic", "text-anchor": "middle",
    }, "Click the bar to guess"));
  }

  const out = $("tl-readout");
  out.replaceChildren();
  if (res) {
    out.append("Answer: ", res.truth.range_text);
    out.append(el("br"), el("em", "", `You: ${rangeLabel(turn.block, turn.block + BLOCK)}`));
  } else if (turn.block !== null) {
    out.append(rangeLabel(turn.block, turn.block + BLOCK));
  } else {
    out.append(el("em", "", "No time selected"));
  }
}

let tlDragging = false;
function setBlockFromEvent(e) {
  const rect = svg.getBoundingClientRect();
  const block = snapBlock(xYear(e.clientX - rect.left, rect.width));
  if (block !== turn.block) {
    turn.block = block;
    renderAction();
    drawTimeline();
  }
}
svg.addEventListener("pointerdown", (e) => {
  if (turn.revealing || !game.rounds.length) return;
  svg.setPointerCapture(e.pointerId);
  tlDragging = true;
  setBlockFromEvent(e);
});
svg.addEventListener("pointermove", (e) => { if (tlDragging) setBlockFromEvent(e); });
svg.addEventListener("pointerup", () => { tlDragging = false; });
svg.addEventListener("pointercancel", () => { tlDragging = false; });

/* ---------- results ---------- */
function shareSquare(total) { return total >= 9000 ? "🟦" : total >= 6000 ? "🟩" : total >= 3000 ? "🟨" : "🟥"; }
function squareColor(total) { return total >= 9000 ? "#4c8dff" : total >= 6000 ? "#2dd4bf" : total >= 3000 ? "#f5b14c" : "#ff6b5e"; }

function renderResults() {
  const total = game.results.reduce((sum, r) => sum + r.total, 0);
  $("res-date").textContent = game.kind === "daily" ? prettyDay(game.day) : "Practice game";
  $("res-total").textContent = fmt(total);
  const squares = $("res-squares");
  squares.replaceChildren(...game.results.map((r) => {
    const s = el("span");
    s.style.background = squareColor(r.total);
    return s;
  }));
  renderHistory(total);
  renderTable();
  show("results");
}

function renderHistory(total) {
  // The real game ranks you against that day's players; locally the only honest comparison is your own games.
  const box = $("res-history");
  box.replaceChildren();
  const history = store.get(HISTORY_KEY, []);
  if (history.length < 2) {
    box.append(el("div", "", "Play more games to see your scores compared here."));
    return;
  }
  const beaten = history.filter((h) => h.total < total).length;
  const pct = Math.round((beaten / (history.length - 1)) * 100);
  const best = Math.max(...history.map((h) => h.total));
  box.append(el("div", "", `Better than ${Math.min(pct, 100)}% of your ${history.length - 1} other games · your best: ${fmt(best)}`));
  const spark = el("div", "spark");
  const recent = history.slice(-40);
  const peak = Math.max(...recent.map((h) => h.total), 1);
  recent.forEach((h, i) => {
    const bar = el("span", i === recent.length - 1 ? "now" : "");
    bar.style.height = `${(h.total / peak) * 100}%`;
    bar.title = `${h.label}: ${fmt(h.total)}`;
    spark.append(bar);
  });
  box.append(spark);
}

function renderTable() {
  const table = $("res-table");
  table.replaceChildren();
  const head = el("tr");
  [["", ""], ["Artifact", ""], ["Off by", "num"], ["Place", "num gold"], ["Off by", "num"], ["Time", "num gold"], ["Round", "num"]]
    .forEach(([t, c]) => head.append(el("th", c, t)));
  const thead = el("thead"); thead.append(head);
  const tbody = el("tbody");
  game.results.forEach((r, i) => {
    const tr = el("tr");
    const imgTd = el("td", "wide");
    const img = el("img");
    img.src = game.rounds[i].photos[0]; img.alt = ""; img.loading = "lazy";
    imgTd.append(img);
    const nameTd = el("td");
    const a = el("a", "", r.truth.title || r.truth.object_name);
    a.href = r.truth.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    nameTd.append(a, el("div", "eyebrow", r.truth.place_label));
    tr.append(imgTd, nameTd,
      el("td", "num", r.in_region ? "—" : `${fmt(r.km)} km`),
      el("td", "num gold", fmt(r.place_score)),
      el("td", "num", r.year_gap === 0 ? "—" : `${fmt(r.year_gap)} yrs`),
      el("td", "num gold", fmt(r.time_score)),
      el("td", "num sum", fmt(r.total)));
    tbody.append(tr);
  });
  table.append(thead, tbody);
}

async function share() {
  const total = game.results.reduce((sum, r) => sum + r.total, 0);
  const title = game.kind === "daily" ? `Provenance · ${prettyDay(game.day)}` : "Provenance";
  const text = [title, game.results.map((r) => shareSquare(r.total)).join(""), fmt(total)].join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied results to clipboard");
  } catch {
    toast("Couldn't reach the clipboard");
  }
}

/* ---------- theme ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  store.set("provenance:theme", theme);
  drawMap();
  drawTimeline();
}
document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}));

/* ---------- wiring ---------- */
const reportFailure = (fn) => () => fn().catch(() => toast("Couldn't reach the game server"));
$("btn-play").addEventListener("click", reportFailure(startDaily));
$("btn-practice").addEventListener("click", reportFailure(startPractice));
$("btn-again").addEventListener("click", reportFailure(startPractice));
$("btn-action").addEventListener("click", onAction);
$("btn-share").addEventListener("click", share);
$("btn-home").addEventListener("click", renderHome);
$("btn-home2").addEventListener("click", renderHome);
$("btn-hint").addEventListener("click", () => { renderHintModal(); $("hint-modal").hidden = false; });
$("hint-close").addEventListener("click", () => { $("hint-modal").hidden = true; });
$("hint-modal").addEventListener("click", (e) => { if (e.target === $("hint-modal")) $("hint-modal").hidden = true; });

$("ph-prev").addEventListener("click", () => stepPhoto(-1));
$("ph-next").addEventListener("click", () => stepPhoto(1));
$("lb-prev").addEventListener("click", () => stepPhoto(-1));
$("lb-next").addEventListener("click", () => stepPhoto(1));
$("lb-close").addEventListener("click", closeLightbox);
$("photo").addEventListener("click", openLightbox);
const lbBox = $("lightbox");
lbBox.addEventListener("wheel", (e) => {
  e.preventDefault();
  zoomLightbox(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.002));
}, { passive: false });
lbBox.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button")) return;
  lbBox.setPointerCapture(e.pointerId);
  lb.pointers.set(e.pointerId, e);
  if (lb.pointers.size === 1) {
    lb.drag = { x: e.clientX, y: e.clientY, ox: lb.x, oy: lb.y, moved: false };
  } else if (lb.pointers.size === 2) {
    const [a, b] = [...lb.pointers.values()];
    lb.pinch = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    lb.drag = null;
  }
});
lbBox.addEventListener("pointermove", (e) => {
  if (!lb.pointers.has(e.pointerId)) return;
  lb.pointers.set(e.pointerId, e);
  if (lb.pointers.size === 2 && lb.pinch) {
    const [a, b] = [...lb.pointers.values()];
    const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    zoomLightbox((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2, dist / lb.pinch);
    lb.pinch = dist;
  } else if (lb.drag) {
    const dx = e.clientX - lb.drag.x, dy = e.clientY - lb.drag.y;
    if (Math.hypot(dx, dy) > 4) { lb.drag.moved = true; lbBox.classList.add("dragging"); }
    if (lb.drag.moved) {
      lb.x = lb.drag.ox + dx;
      lb.y = lb.drag.oy + dy;
      applyLightbox();
    }
  }
});
function endLightboxPointer(e) {
  const drag = lb.drag;
  lb.pointers.delete(e.pointerId);
  if (lb.pointers.size < 2) lb.pinch = null;
  if (lb.pointers.size === 0) {
    lb.drag = null;
    lbBox.classList.remove("dragging");
    if (e.type === "pointerup" && drag && !drag.moved) closeLightbox();   // a plain click
  }
}
lbBox.addEventListener("pointerup", endLightboxPointer);
lbBox.addEventListener("pointercancel", endLightboxPointer);
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeLightbox(); $("hint-modal").hidden = true; }
  if ($("lightbox").hidden) return;
  if (e.key === "ArrowLeft") stepPhoto(-1);
  if (e.key === "ArrowRight") stepPhoto(1);
});

/* ---------- boot ---------- */
(async () => {
  document.documentElement.dataset.theme = store.get("provenance:theme", "dark");
  renderHome();
  const world = await (await fetch("/static/countries.json")).json();
  const polys = [];
  labels = [];
  for (const f of world.features) {
    const g = f.geometry;
    if (!g || f.properties.ADMIN === "Antarctica") continue;
    polys.push(...(g.type === "Polygon" ? [g.coordinates] : g.coordinates));
    const p = f.properties;
    if (p.LABEL_X != null) labels.push({ name: p.NAME, lon: p.LABEL_X, lat: p.LABEL_Y, rank: p.LABELRANK });
  }
  labels.sort((a, b) => a.rank - b.rank);
  landPath = geoPath(polys);
  drawMap();
})();
