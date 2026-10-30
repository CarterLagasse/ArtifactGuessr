"""pool.py — build the Provenance object pool.

Usage:
    python -m game.pool                  select from the CSV, fetch new objects, write the pool
    python -m game.pool --no-fetch       rebuild from data/game_objects.jsonl only
    python -m game.pool --per-bucket 1200

Selection follows what's known of the real game: public-domain objects with a
photo, recognisable from a picture, and balanced across world regions because
the Met's raw distribution would otherwise be dominated by Europe, Egypt and
Attic vases. Highlights are always taken first.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from game import geolite, scoring

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV = DATA / "MetObjects.csv"
JSONL = DATA / "game_objects.jsonl"
OUT = DATA / "game_pool.parquet"

API = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}"
HEADERS = {"User-Agent": "provenance-practice (local game builder)"}

MAX_SPAN_YEARS = 1000       # wider ranges make the time guess meaningless
MIN_BEGIN = scoring.TIMELINE_MIN
MAX_END = scoring.TIMELINE_MAX
MAX_PHOTOS = 6
SEED = 20260730

# Paper, books and sherds: hard or impossible to place from a picture alone.
EXCLUDED_DEPARTMENTS = {
    "The Libraries", "Drawings and Prints", "Photographs",
    "Modern and Contemporary Art", "Robert Lehman Collection",
}
EXCLUDED_CLASSIFICATION = re.compile(
    r"print|photograph|ephemera|drawing|book|album|codices|manuscript|stucco|"
    r"ostraca|papyr|tablet|architectural|textiles-(?:laces|trimmings)", re.I)
NONDESCRIPT_TITLE = re.compile(
    r"\b(?:fragments?|sherds?|shards?|bead|beads|button|nails?|pin|tile|rim|handle|lid|"
    r"knob|hook|ring bezel|scarab|seal impression|cast of|copy of|facsimile|reproduction)\b", re.I)

# Daily games draw from these buckets, so each needs enough objects to last.
DEFAULT_PER_BUCKET = 900


# ---------------------------------------------------------------- CSV pass

def _text(value) -> str:
    return "" if pd.isna(value) else str(value)


def load_candidates() -> pd.DataFrame:
    cols = [
        "Object ID", "Is Highlight", "Is Public Domain", "Title", "Culture", "Classification",
        "Object Begin Date", "Object End Date", "Department", "Country", "Region",
    ]
    df = pd.read_csv(CSV, low_memory=False, encoding="utf-8-sig", usecols=cols)
    print(f"CSV objects: {len(df):,}")
    df = df[df["Is Public Domain"].astype(str).str.lower() == "true"].copy()
    print(f"public domain: {len(df):,}")

    for c in ("Object Begin Date", "Object End Date"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Object Begin Date", "Object End Date"])
    begin, end = df["Object Begin Date"], df["Object End Date"]
    df = df[(begin >= MIN_BEGIN) & (end <= MAX_END) & (begin <= end) & (end - begin <= MAX_SPAN_YEARS)]
    print(f"usable dates: {len(df):,}")

    df = df[~df["Department"].isin(EXCLUDED_DEPARTMENTS)]
    df = df[~df["Classification"].fillna("").str.contains(EXCLUDED_CLASSIFICATION)]
    df = df[~df["Title"].fillna("").str.contains(NONDESCRIPT_TITLE)]
    print(f"recognisable kinds of object: {len(df):,}")

    # Distinct strings are few, so resolve each combination once.
    keys = df[["Culture", "Country", "Region"]].fillna("").drop_duplicates()
    places = {
        (cu, co, re_): geolite.resolve_object(cu, co, re_)
        for cu, co, re_ in keys.itertuples(index=False, name=None)
    }
    resolved = [
        places[(_text(cu), _text(co), _text(re_))]
        for cu, co, re_ in df[["Culture", "Country", "Region"]].itertuples(index=False, name=None)
    ]
    df["place"] = resolved
    df = df[df["place"].notna()].copy()
    for field in ("key", "label", "hint", "bucket", "lat", "lon", "tier"):
        df[f"place_{field}"] = [getattr(p, field) for p in df["place"]]
    print(f"resolved to a region: {len(df):,}")
    return df.drop(columns=["place"])


def choose(df: pd.DataFrame, per_bucket: int) -> pd.DataFrame:
    """Highlights, then a sample that flattens over-represented places and kinds."""
    rng = np.random.default_rng(SEED)
    chosen = []
    for _, group in df.groupby("place_bucket"):
        highlight = group["Is Highlight"].astype(str).str.lower() == "true"
        picks = group[highlight].head(per_bucket)
        rest = group[~highlight]
        need = min(per_bucket - len(picks), len(rest))
        if need > 0:
            place_n = rest.groupby("place_key")["Object ID"].transform("size")
            kind_n = rest.groupby(rest["Classification"].fillna(""))["Object ID"].transform("size")
            weight = 1.0 / (place_n ** 0.3 * np.sqrt(kind_n))
            # Instruments split into many small classes, so they dodge the kind penalty.
            weight = weight.where(rest["Department"] != "Musical Instruments", weight * 0.2)
            idx = rng.choice(len(rest), size=need, replace=False, p=(weight / weight.sum()).to_numpy())
            picks = pd.concat([picks, rest.iloc[idx]])
        chosen.append(picks)
    return pd.concat(chosen)


# ---------------------------------------------------------------- API pass

def fetch_object(oid: int) -> dict | None:
    req = urllib.request.Request(API.format(oid=oid), headers=HEADERS)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 4:
                cool = 90 if e.code == 403 else 30        # burst-protection cooldown
                print(f"  {e.code} on {oid}; cooling {cool}s ...", flush=True)
                time.sleep(cool)
                continue
            print(f"  {type(e).__name__} {e} on {oid}", flush=True)
            return None
        except Exception:
            if attempt == 4:
                return None
            time.sleep(1 + attempt)
    return None


def load_cache() -> dict[int, dict]:
    cache: dict[int, dict] = {}
    if JSONL.exists():
        with JSONL.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    obj = json.loads(line)
                    cache[obj["objectID"]] = obj
    return cache


def fetch_missing(ids: list[int], cache: dict[int, dict]) -> None:
    miss = [i for i in ids if i not in cache]
    if not miss:
        return
    print(f"API fetch: {len(miss):,} objects ...", flush=True)
    t0 = time.time()
    with JSONL.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=4) as ex:
        # 4 workers + 403-aware cooldown keeps our burst footprint far under the 80 rps cap
        for done, j in enumerate(ex.map(fetch_object, miss), 1):
            if j is not None and j.get("objectID"):
                fh.write(json.dumps(j, ensure_ascii=False) + "\n")
                cache[j["objectID"]] = j
            if done % 100 == 0:
                fh.flush()
                print(f"  {done:,}/{len(miss):,} ({time.time() - t0:.0f}s)", flush=True)


def photo_urls(obj: dict) -> list[str]:
    url = obj.get("primaryImageSmall") or ""
    if not url.startswith("http"):
        return []
    urls = [url]
    for u in obj.get("additionalImages") or []:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u.replace("/original/", "/web-large/"))
    return list(dict.fromkeys(urls))[:MAX_PHOTOS]


# ---------------------------------------------------------------- main

def build(per_bucket: int, fetch: bool) -> pd.DataFrame:
    candidates = load_candidates()
    chosen = choose(candidates, per_bucket)
    print(f"selected: {len(chosen):,}")
    print(chosen["place_bucket"].value_counts().to_string())

    cache = load_cache()
    if fetch:
        fetch_missing([int(i) for i in chosen["Object ID"]], cache)

    # Cached objects that still qualify count too, even if this sample skipped them.
    usable = candidates[candidates["Object ID"].isin(cache)]
    rows = []
    for oid in usable["Object ID"]:
        obj = cache[int(oid)]
        urls = photo_urls(obj)
        if urls:
            rows.append({
                "objectID": obj["objectID"],
                "title": obj.get("title") or "",
                "objectName": obj.get("objectName") or "",
                "culture": obj.get("culture") or "",
                "period": obj.get("period") or "",
                "objectDate": obj.get("objectDate") or "",
                "medium": obj.get("medium") or "",
                "department": obj.get("department") or "",
                "objectURL": obj.get("objectURL") or "",
                "urls": urls,
            })
    pool = pd.DataFrame(rows)
    meta = usable[["Object ID", "Object Begin Date", "Object End Date", "place_key", "place_label",
                   "place_hint", "place_bucket", "place_lat", "place_lon", "place_tier"]]
    pool = pool.merge(meta, left_on="objectID", right_on="Object ID").drop(columns=["Object ID"])
    pool = pool.rename(columns={"Object Begin Date": "begin", "Object End Date": "end"})
    pool[["begin", "end"]] = pool[["begin", "end"]].astype(int)
    pool = pool.sort_values("objectID").reset_index(drop=True)
    pool.to_parquet(OUT, index=False)

    print(f"\npool written: {OUT} ({len(pool):,} objects)")
    print(pool["place_bucket"].value_counts().to_string())
    print(pool["place_tier"].value_counts().to_string())
    print(pool["place_label"].value_counts().head(20).to_string())
    return pool


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    ap.add_argument("--no-fetch", action="store_true", help="use only cached API objects")
    args = ap.parse_args()
    build(per_bucket=args.per_bucket, fetch=not args.no_fetch)
