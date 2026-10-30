"""The browser's scoring (static_backend.js) must agree with the server's. Needs Node."""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from game import geolite, scoring

BACKEND = Path(__file__).resolve().parent.parent / "game" / "static" / "static_backend.js"
KEYS = ["c:France", "c:United States of America", "c:Indonesia", "c:Egypt+c:Syria", "c:Russia",
        "c:South Africa", "o:-8.2,-78.9,350", "o:25.72,32.61,120+o:29.7,31.2,120"]
RUNNER = """
const { scoreGuess } = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(cases.map((c) => scoreGuess(c.obj, c.polygons, c.lat, c.lon, c.year))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_and_server_score_the_same():
    rng = random.Random(7)
    cases = []
    for _ in range(600):
        key = rng.choice(KEYS)
        begin = rng.randint(-3000, 2000)
        cases.append({
            "key": key,
            "polygons": geolite.geometry(key),
            "obj": {"begin": begin, "end": begin + rng.randint(0, 1000)},
            "lat": round(rng.uniform(-60, 84), 4),
            "lon": round(rng.uniform(-180, 180), 4),
            "year": rng.choice(range(-3000, 1751, 125)),
        })
    payload = json.dumps([{k: v for k, v in c.items() if k != "key"} for c in cases])
    done = subprocess.run(["node", "-e", RUNNER, str(BACKEND)], input=payload, capture_output=True,
                          text=True, encoding="utf-8", check=True)
    results = json.loads(done.stdout)

    for case, js in zip(cases, results):
        km = geolite.distance_to_region_km(case["lat"], case["lon"], case["key"])
        block = scoring.normalize_block(case["year"])
        gap = scoring.year_gap(block, case["obj"]["begin"], case["obj"]["end"])
        assert js["km"] == pytest.approx(km, abs=0.06), case["key"]
        assert js["in_region"] == (km == 0)
        assert js["year_gap"] == gap
        assert js["guess"]["block_start"] == block
        assert js["place_score"] == scoring.score_place(km)
        assert js["time_score"] == scoring.score_time(gap)
