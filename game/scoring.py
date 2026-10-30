"""scoring.py — scoring rules, matched to the client code of the game this one is modelled on.

Each artifact is worth up to 10,000 points, split evenly between place and time:
    place = round(5000 * exp(-km / 2000))      km = distance to the region; 0 inside it
    time  = round(5000 * exp(-years / 1000))   years = gap from the 250-year block to the
                                               object's [begin, end] range; 0 if they overlap
A guess's year is the START of its block. Blocks snap to 125-year steps.
"""
from __future__ import annotations

import math

PLACE_MAX = 5000
PLACE_SCALE_KM = 2000
TIME_MAX = 5000
TIME_SCALE_YEARS = 1000

BLOCK_YEARS = 250
BLOCK_SNAP = 125
TIMELINE_MIN = -3000
TIMELINE_MAX = 2025
BLOCK_START_MAX = 1750

ROUNDS_PER_GAME = 10
HINTS_PER_GAME = 3


def normalize_block(block_start: float) -> int:
    """Put a block start on the 125-year grid and on the timeline."""
    snapped = round(block_start / BLOCK_SNAP) * BLOCK_SNAP
    return int(min(BLOCK_START_MAX, max(TIMELINE_MIN, snapped)))


def year_gap(block_start: int, begin: int, end: int) -> int:
    block_end = block_start + BLOCK_YEARS
    if block_end >= begin and block_start <= end:
        return 0
    return begin - block_end if block_end < begin else block_start - end


def score_place(km: float) -> int:
    return round(PLACE_MAX * math.exp(-km / PLACE_SCALE_KM))


def score_time(gap_years: float) -> int:
    return round(TIME_MAX * math.exp(-gap_years / TIME_SCALE_YEARS))


def constants() -> dict:
    return {
        "place_max": PLACE_MAX,
        "place_scale_km": PLACE_SCALE_KM,
        "time_max": TIME_MAX,
        "time_scale_years": TIME_SCALE_YEARS,
        "block_years": BLOCK_YEARS,
        "block_snap": BLOCK_SNAP,
        "timeline_min": TIMELINE_MIN,
        "timeline_max": TIMELINE_MAX,
        "block_start_max": BLOCK_START_MAX,
        "rounds_per_game": ROUNDS_PER_GAME,
        "hints_per_game": HINTS_PER_GAME,
    }
