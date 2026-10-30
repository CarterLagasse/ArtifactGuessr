"""build_cities.py — make game/cities.json from a GeoNames cities15000 dump.

Usage: python -m game.build_cities path/to/cities15000.txt

Keeps only cities whose name appears in the Met's geography fields, so the file
stays small. City data: GeoNames (geonames.org), CC BY 4.0.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from game import geolite, pool


def met_place_names() -> set[str]:
    cols = ["Culture", "Subregion", "City", "County", "State", "Region"]
    df = pd.read_csv(pool.CSV, low_memory=False, encoding="utf-8-sig", usecols=cols)
    names: set[str] = set()
    for col in cols:
        for value in df[col].dropna().unique():
            for alt in geolite._alternatives(str(value)):
                names.update(geolite._fine_parts(alt))
    return {n for n in names if len(n) >= 4}


def build(dump: str) -> None:
    wanted = met_place_names()
    found: dict[str, list] = {}
    with open(dump, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            name, ascii_name, alternates = f[1], f[2], f[3]
            lat, lon, population = float(f[4]), float(f[5]), int(f[14])
            primary = {name.lower(), ascii_name.lower()}
            for n in primary | {a.lower() for a in alternates.split(",") if a}:
                if n in wanted:
                    found.setdefault(n, []).append([name, round(lat, 3), round(lon, 3), population, n in primary])
    # A city's own name outranks an alternate spelling of a bigger city.
    out = {n: [c[:3] for c in sorted(v, key=lambda c: (not c[4], -c[3]))] for n, v in sorted(found.items())}
    with open(geolite.CITIES_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out):,} place names matched of {len(wanted):,} -> {geolite.CITIES_PATH}")


if __name__ == "__main__":
    build(sys.argv[1])
