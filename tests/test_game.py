"""Rules, geography and leak guards for Provenance. Run: python -m pytest tests"""
import json
from datetime import date, timedelta

import pytest

from game import geolite, scoring, server


# ---------------------------------------------------------------- scoring

@pytest.mark.parametrize("km,points", [(0, 5000), (2000, 1839), (20000, 0)])
def test_place_score_matches_game_formula(km, points):
    assert scoring.score_place(km) == points


@pytest.mark.parametrize("gap,points", [(0, 5000), (1000, 1839), (5000, 34)])
def test_time_score_matches_game_formula(gap, points):
    assert scoring.score_time(gap) == points


@pytest.mark.parametrize("block,begin,end,gap", [
    (1000, 1100, 1100, 0),      # object inside the block
    (1000, 900, 1000, 0),       # ranges touch
    (1000, 1250, 1400, 0),      # block end touches range start
    (1000, 1300, 1400, 50),     # block ends 50 years before the range
    (1000, 500, 800, 200),      # block starts 200 years after the range
    (-500, 100, 200, 350),      # across the BCE/CE line
])
def test_year_gap_measures_block_to_range(block, begin, end, gap):
    assert scoring.year_gap(block, begin, end) == gap


@pytest.mark.parametrize("year,block", [(-9999, -3000), (9999, 1750), (1000, 1000), (1060, 1000), (1070, 1125), (-125, -125)])
def test_block_stays_on_grid_and_timeline(year, block):
    assert scoring.normalize_block(year) == block


# ---------------------------------------------------------------- geography

@pytest.mark.parametrize("culture,country,region,label", [
    ("Greek, Attic", "", "", "Attica, Greece"),
    ("French, Paris", "", "", "France"),
    ("possibly French", "", "", "France"),
    ("Italian", "France", "", "France"),       # an explicit country beats a demonym
    ("Moche", "Peru", "", "North coast of Peru"),
    ("", "present-day France", "", "France"),
    ("", "Byzantine Egypt", "", "Egypt"),
    ("", "United States|United States", "", "United States"),
    ("", "Egypt or Syria", "", "Egypt or Syria"),
    ("", "Trinidad and Tobago", "", "Trinidad and Tobago"),
])
def test_resolves(culture, country, region, label):
    assert geolite.resolve_object(culture, country, region).label == label


@pytest.mark.parametrize("culture,country", [
    ("", "United States|England"),     # alternatives on two continents
    ("American or European", ""),
    ("European", ""),
    ("", "Atlantis"),
])
def test_refuses_to_guess(culture, country):
    assert geolite.resolve_object(culture, country, "") is None


def test_no_substring_false_friends():
    assert "Antarctic" not in geolite.resolve_object("French", "", "").label
    assert geolite.resolve_object("", "Niger", "").label == "Niger"
    assert geolite.resolve_object("", "Nigeria", "").label == "Nigeria"


def test_benin_culture_is_not_benin_country():
    assert geolite.resolve_object("Benin", "", "").lon == 5.6
    assert geolite.resolve_object("", "Benin", "").label == "Benin"


@pytest.mark.parametrize("name,lat,lon", [("France", 46.6, 2.4), ("United States of America", 39, -98)])
def test_country_centre_is_on_the_mainland(name, lat, lon):
    centre = geolite._country_polygons(name)[1]
    assert geolite.haversine_km(centre[0], centre[1], lat, lon) < 500


def test_distance_is_zero_inside_and_positive_outside():
    assert geolite.distance_to_region_km(48.85, 2.35, "c:France") == 0        # Paris
    assert 100 < geolite.distance_to_region_km(51.5, -0.1, "c:France") < 400  # London
    # Overseas parts don't count: French Guiana is far from "France"
    assert geolite.distance_to_region_km(4, -53, "c:France") > 5000


def test_circle_region():
    key = "o:-8.2,-78.9,350"
    assert geolite.distance_to_region_km(-8.2, -78.9, key) == 0
    assert geolite.distance_to_region_km(-8.2, -70.0, key) == pytest.approx(630, abs=80)


def test_union_counts_both_parts():
    key = "c:Egypt+c:Syria"
    assert geolite.distance_to_region_km(30, 31, key) == 0
    assert geolite.distance_to_region_km(35, 38, key) == 0


def test_wraps_longitude():
    assert geolite.distance_to_region_km(48.85, 362.35, "c:France") == 0


# ---------------------------------------------------------------- game API

needs_pool = pytest.mark.skipif(not server.POOL_PATH.exists(), reason="pool not built")


@needs_pool
def test_daily_is_ten_distinct_and_deterministic():
    a = server.api_daily("2026-10-30")
    assert len(a["rounds"]) == 10
    assert len({r["round_id"] for r in a["rounds"]}) == 10
    assert a == server.api_daily("2026-10-30")
    assert a["rounds"] != server.api_daily("2026-10-29")["rounds"]


@needs_pool
def test_daily_does_not_repeat_objects_in_the_near_term():
    start = date(2026, 2, 1)
    seen = [r["round_id"] for d in range(14)
            for r in server.api_daily((start + timedelta(days=d)).isoformat())["rounds"]]
    # A repeat is only allowed once a region has shown every object it has.
    buckets = [server._row(rid)["place_bucket"] for rid in seen]
    for bucket in set(buckets):
        drawn = [rid for rid, b in zip(seen, buckets) if b == bucket]
        size = (server.pool()["place_bucket"] == bucket).sum()
        assert len(set(drawn)) == min(len(drawn), size)


@needs_pool
def test_pre_guess_payload_has_no_answer_in_it():
    for payload in (server.api_daily("2026-10-30"), server.api_practice()):
        # The medium is shown on purpose; everything else must stay hidden.
        hidden = [{k: v for k, v in r.items() if k != "medium"} for r in payload["rounds"]]
        text = json.dumps(hidden)
        assert "metmuseum.org" not in text and "objectID" not in text
        for r in payload["rounds"]:
            assert set(r) == {"round_id", "photos", "medium", "credit"}
            row = server._row(r["round_id"])
            if row["title"]:
                assert row["title"] not in text
            assert row["place_label"] not in text


@needs_pool
def test_guess_scores_and_reveals():
    rid = server.api_daily("2026-10-30")["rounds"][0]["round_id"]
    row = server._row(rid)
    res = server.api_guess(server.Guess(round_id=rid, lat=row["place_lat"], lon=row["place_lon"],
                                        year=scoring.normalize_block(row["begin"])))
    assert res["year_gap"] == 0 and res["time_score"] == 5000
    assert res["total"] == res["place_score"] + res["time_score"]
    assert res["truth"]["region"]["type"] == "MultiPolygon"


@needs_pool
def test_hints():
    rid = server.api_daily("2026-10-30")["rounds"][0]["round_id"]
    row = server._row(rid)
    assert server.api_hint(server.HintRequest(round_id=rid, kind="place"))["text"] == row["place_hint"]
    assert "–" in server.api_hint(server.HintRequest(round_id=rid, kind="time"))["text"]


def test_thousand_year_span():
    assert server._thousand_year_span(1500, 1600) == "1,000 – 2,000 CE"
    assert server._thousand_year_span(100, 200) == "1 – 1,000 CE"
    assert server._thousand_year_span(-1500, -1400) == "2,000 – 1,000 BCE"
    assert server._thousand_year_span(-500, -400) == "1,000 – 1 BCE"
