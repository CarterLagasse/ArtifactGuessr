# Provenance — local practice game

Updated 2026-10-30. Companion docs: `PLAN.md` (research model), `TODO.md` (data pipeline).

## 1. Goal

A self-contained local web app that plays like the daily museum-guessing game it is modelled on, and doubles as the
future agent's playground (TODO Phase 9): the agent calls this API instead of
screen-scraping the real site.

Run it: `python -m game.server`, then open http://localhost:8321.
Test it: `python -m pytest tests`.

## 2. What matches the real game

Taken from the original game's public client code on 2026-10-30.

| Rule | Value |
|---|---|
| Game | 10 objects per day, one attempt, resumes after reload |
| Place score | `round(5000 * exp(-km / 2000))`, km = distance to the region polygon, 0 inside |
| Time score | `round(5000 * exp(-gap / 1000))`, gap = years between the block and `[begin, end]`, 0 if they overlap |
| Maximum | 10,000 per object, 100,000 per game |
| Timeline | -3000 to 2025, linear; a 250-year block centred on the click, snapped to 125-year steps |
| Hints | 3 per day across all rounds, no penalty. Time: the 1,000-year span holding the midpoint. Place: world-region name |
| Share squares | blue ≥ 9,000, green ≥ 6,000, yellow ≥ 3,000, else red |

## 3. Known differences

- **Regions.** The real game draws historical polygons from historical-basemaps and
  OpenHistoricalMap plus custom files. Here, a region is a modern country (Natural
  Earth 50m) or a hand-placed circle for about 120 cultures (`geolite.STRONG_CULTURES`).
  Scores will differ when a historical polity doesn't match modern borders.
- **Ranking.** The real game ranks you against that day's players. There are no other
  players locally, so results compare against your own past games instead of showing an
  invented percentile.
- **Object pool.** Selection is approximated by rules in `pool.py`: the real game's
  picks and its by-hand removal of nondescript objects aren't reproduced. Objects
  also lack the Met's curatorial descriptions, which the API doesn't supply.
- **Medium shown up front.** The material is visible before guessing, which makes objects
  easier. Occasionally the text names a place (a "Kashan" tile, say).
- **Look.** Deliberately its own: dark by default, slate and teal, Bricolage Grotesque
  with IBM Plex Sans and Mono.
- **Practice mode** is an addition: ten random objects, any time. The real game is daily only.

## 4. Architecture

```
game/
├── scoring.py   # the rules above, all constants in one place
├── geolite.py   # Met geo strings → Place (region, hint, bucket); point-to-region distance
├── pool.py      # CSV filter + balanced sample + Met API fetch → data/game_pool.parquet
├── server.py    # FastAPI: daily picks, scoring, image proxy, static frontend
└── static/      # vanilla JS, canvas map, SVG timeline, no build step
tests/test_game.py
data/
├── MetObjects.csv
├── game_objects.jsonl   # cached API responses; safe to append to, never refetched
├── game_pool.parquet    # final pool
├── round_salt.txt       # per-install secret behind round ids
└── image_cache/         # proxied photos
```

Rebuild the pool with `python -m game.pool` (`--no-fetch` to use only the cache,
`--per-bucket N` to resize). The Met API throttles to roughly one object per second.
Rebuilding changes which objects each daily game picks.

## 5. API (the contract the agent will use)

| Endpoint | Meaning |
|---|---|
| `GET /api/daily?date=YYYY-MM-DD` | ten rounds for a date (2026-01-01 through tomorrow) |
| `GET /api/practice` | ten random rounds |
| `POST /api/hint` | `{round_id, kind: "time"\|"place"}` → `{kind, text}`; the 3-hint limit is enforced by the client, as in the real game |
| `POST /api/guess` | `{round_id, lat, lon, year}` where `year` is the block START → scores + full reveal |
| `GET /api/image/{round_id}/{n}` | proxied photo; `?full=true` for the original resolution |
| `GET /api/scoring` | rule constants |

A pre-guess round is only `{round_id, photos, medium, credit}`. Met URLs are never sent before
a guess, because their paths name the curatorial department. An agent must also not
read `data/` while playing, or its scores mean nothing.

## 6. Daily picks

Deterministic for a given pool. Each slot draws a region by `server.BUCKET_WEIGHTS`
(Europe about 30%), then the next unused object from that region's shuffled list, so
objects don't repeat until a region runs out.
