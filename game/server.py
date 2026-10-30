"""server.py — ArtifactGuessr.

Run:  python -m game.server    → http://localhost:8321

Leak discipline: until a guess is submitted, a round is an opaque id plus proxied
image paths and the medium. Object ids and Met image URLs both give the answer away
(the URL path names the curatorial department), so neither is sent.
"""
from __future__ import annotations

import hashlib
import random
import secrets
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from game import geolite, scoring

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
POOL_PATH = ROOT / "data" / "game_pool.parquet"
SALT_PATH = ROOT / "data" / "round_salt.txt"
IMAGE_CACHE = ROOT / "data" / "image_cache"

# Daily games start here; each day's picks follow from the days before it.
EPOCH = date(2026, 1, 1)
# Share of daily slots per region. Europe is held near a third, as in the real game.
BUCKET_WEIGHTS = {
    "Europe": 0.30, "Near East": 0.20, "East Asia": 0.13, "Americas": 0.14,
    "South & Central Asia": 0.08, "Southeast Asia": 0.05,
    "Sub-Saharan Africa": 0.07, "Oceania": 0.03,
}

app = FastAPI(title="ArtifactGuessr", version="0.2")

_pool: pd.DataFrame | None = None
_rid_to_index: dict[str, int] = {}
_daily_picks: list[list[int]] = []
_daily_state: dict = {}
_daily_lock = threading.Lock()


def pool() -> pd.DataFrame:
    global _pool
    if _pool is None:
        if not POOL_PATH.exists():
            raise RuntimeError("game_pool.parquet missing — run `python -m game.pool` first")
        _pool = pd.read_parquet(POOL_PATH).sort_values("objectID").reset_index(drop=True)
        salt = _salt()
        _rid_to_index.update({_round_id(salt, oid): i for i, oid in enumerate(_pool["objectID"])})
    return _pool


def _salt() -> str:
    """Random per-install, so a round id can't be mapped back to an object id."""
    if not SALT_PATH.exists():
        SALT_PATH.write_text(secrets.token_hex(16), encoding="utf-8")
    return SALT_PATH.read_text(encoding="utf-8").strip()


def _round_id(salt: str, object_id: int) -> str:
    return hashlib.sha256(f"{salt}:{object_id}".encode()).hexdigest()[:16]


def _row(round_id: str) -> pd.Series:
    pool()
    if round_id not in _rid_to_index:
        raise HTTPException(404, "unknown round")
    return pool().iloc[_rid_to_index[round_id]]


def _round_payload(index: int) -> dict:
    row = pool().iloc[index]
    rid = _round_id(_salt(), int(row["objectID"]))
    return {
        "round_id": rid,
        "photos": [f"/api/image/{rid}/{i}" for i in range(len(row["urls"]))],
        "medium": str(row["medium"]),
        "credit": "The Metropolitan Museum of Art, CC0",
    }


# ---------------------------------------------------------------- daily picks

def _picks_for(day: date) -> list[int]:
    """Ten pool indices for a date. An object can't repeat until its region is exhausted."""
    p = pool()
    with _daily_lock:
        if not _daily_state:
            rng = random.Random("artifactguessr-daily")
            orders = {}
            for bucket in sorted(BUCKET_WEIGHTS):
                members = p.index[p["place_bucket"] == bucket].tolist()
                if members:
                    rng.shuffle(members)
                    orders[bucket] = members
            _daily_state.update(rng=rng, orders=orders, cursors=dict.fromkeys(orders, 0))

        orders, cursors, rng = _daily_state["orders"], _daily_state["cursors"], _daily_state["rng"]
        names = sorted(orders)
        weights = [BUCKET_WEIGHTS[n] for n in names]
        target = (day - EPOCH).days
        while len(_daily_picks) <= target:
            picks: list[int] = []
            while len(picks) < scoring.ROUNDS_PER_GAME:
                bucket = rng.choices(names, weights)[0]
                members = orders[bucket]
                pick = members[cursors[bucket] % len(members)]
                cursors[bucket] += 1
                if pick not in picks:
                    picks.append(pick)
            rng.shuffle(picks)
            _daily_picks.append(picks)
        return _daily_picks[target]


def _parse_day(raw: str | None) -> date:
    today = datetime.now(timezone.utc).date()
    if raw is None:
        return today
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    # Browsers send their local date, which can be a day ahead of UTC.
    if not EPOCH <= day <= today + timedelta(days=1):
        raise HTTPException(400, f"date must be between {EPOCH} and tomorrow")
    return day


# ---------------------------------------------------------------- API

class Guess(BaseModel):
    round_id: str
    lat: float
    lon: float
    year: int   # start of the 250-year block


@app.get("/api/daily")
def api_daily(date: str | None = None):
    day = _parse_day(date)
    return {
        "kind": "daily",
        "day": day.isoformat(),
        "rounds": [_round_payload(i) for i in _picks_for(day)],
    }


@app.get("/api/practice")
def api_practice():
    """Ten random rounds, one per place where possible. Doesn't touch the daily sequence."""
    p = pool()
    indices = p.sample(frac=1).drop_duplicates("place_key").index[:scoring.ROUNDS_PER_GAME].tolist()
    return {"kind": "practice", "day": None, "rounds": [_round_payload(i) for i in indices]}


@app.get("/api/scoring")
def api_scoring():
    return scoring.constants()


def _fmt_year(y: int) -> str:
    return f"{-y:,} BCE" if y < 0 else f"{y} CE"


@app.post("/api/guess")
def api_guess(g: Guess):
    row = _row(g.round_id)
    if not (-90 <= g.lat <= 90 and -360 <= g.lon <= 360):
        raise HTTPException(400, "lat/lon out of range")
    block = scoring.normalize_block(g.year)
    begin, end = int(row["begin"]), int(row["end"])
    key = str(row["place_key"])

    km = geolite.distance_to_region_km(g.lat, g.lon, key)
    gap = scoring.year_gap(block, begin, end)
    place_score = scoring.score_place(km)
    time_score = scoring.score_time(gap)

    return {
        "place_score": place_score,
        "time_score": time_score,
        "total": place_score + time_score,
        "max": scoring.PLACE_MAX + scoring.TIME_MAX,
        "km": round(km, 1),
        "in_region": km == 0,
        "year_gap": gap,
        "guess": {"lat": g.lat, "lon": g.lon, "block_start": block, "block_end": block + scoring.BLOCK_YEARS},
        "truth": {
            "object_id": int(row["objectID"]),
            "title": str(row["title"]),
            "object_name": str(row["objectName"]),
            "culture": str(row["culture"]),
            "period": str(row["period"]),
            "object_date": str(row["objectDate"]),
            "medium": str(row["medium"]),
            "department": str(row["department"]),
            "url": str(row["objectURL"]),
            "begin": begin,
            "end": end,
            "range_text": _fmt_year(begin) if begin == end else f"{_fmt_year(begin)} – {_fmt_year(end)}",
            "place_label": str(row["place_label"]),
            "lat": float(row["place_lat"]),
            "lon": float(row["place_lon"]),
            "region": {"type": "MultiPolygon", "coordinates": list(geolite.geometry(key))},
        },
    }


@app.get("/api/image/{round_id}/{index}")
def api_image(round_id: str, index: int, full: bool = False):
    """Web-size photo, or with ?full=true the original, fetched only when a player zooms."""
    urls = _row(round_id)["urls"]
    if not 0 <= index < len(urls):
        raise HTTPException(404, "no such photo")
    url = str(urls[index])
    if full:
        url = url.replace("/web-large/", "/original/")
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    cached = IMAGE_CACHE / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".jpg")
    if not cached.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "artifactguessr (local game)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except OSError as e:
            raise HTTPException(502, f"image fetch failed: {type(e).__name__}")
        tmp = cached.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        tmp.replace(cached)
    return FileResponse(cached, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------------------------------------------- static

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("game.server:app", host="127.0.0.1", port=8321, log_level="info")
