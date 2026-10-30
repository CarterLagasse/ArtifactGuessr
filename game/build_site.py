"""build_site.py — build the static site for GitHub Pages into docs/.

Usage: python -m game.build_site

Run it after anything changes in game/static/ or the pool, then commit docs/.
The site needs no server: static_backend.js scores in the browser from the JSON
written here, and photos load straight from the Met. That means answers are
readable in the page's data, which is fine for a casual game. Keep using
game/server.py for the agent, whose rounds must stay hidden.
"""
from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

from game import geolite, server

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SITE = ROOT / "docs"
DATA_DIR = "gamedata"   # not "data": .gitignore excludes every folder with that name
SCHEDULE_DAYS = 3653    # ten years of daily games; after that the schedule repeats


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def site_index() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # Pages serves from /<repo>/, so absolute paths would break.
    html = html.replace('/static/', '')
    script = '<script src="app.js"></script>'
    assert script in html
    return html.replace(script, f'<script src="static_backend.js"></script>\n{script}')


def build() -> None:
    pool = server.pool()
    keys = sorted(pool["place_key"].unique())
    region_index = {key: i for i, key in enumerate(keys)}

    objects = [{
        "id": int(row.objectID),
        "title": row.title,
        "object_name": row.objectName,
        "culture": row.culture,
        "period": row.period,
        "object_date": row.objectDate,
        "medium": row.medium,
        "department": row.department,
        "url": row.objectURL,
        "photos": [str(u) for u in row.urls],
        "begin": int(row.begin),
        "end": int(row.end),
        "place": row.place_label,
        "lat": float(row.place_lat),
        "lon": float(row.place_lon),
        "region": region_index[row.place_key],
    } for row in pool.itertuples(index=False)]

    daily = [[int(i) for i in server._picks_for(server.EPOCH + timedelta(days=d))] for d in range(SCHEDULE_DAYS)]

    shutil.rmtree(SITE, ignore_errors=True)
    (SITE / DATA_DIR).mkdir(parents=True, exist_ok=True)
    for name in ("app.js", "style.css", "static_backend.js", "countries.json"):
        shutil.copy(STATIC / name, SITE / name)
    (SITE / "index.html").write_text(site_index(), encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    write_json(SITE / DATA_DIR / "objects.json", objects)
    write_json(SITE / DATA_DIR / "regions.json", [list(geolite.geometry(k)) for k in keys])
    write_json(SITE / DATA_DIR / "daily.json", daily)

    total = sum(f.stat().st_size for f in SITE.rglob("*") if f.is_file())
    print(f"{SITE}: {len(objects):,} objects, {len(keys):,} regions, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    build()
