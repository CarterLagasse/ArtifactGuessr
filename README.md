# ArtifactGuessr

Unlimited artifact location and time guessing, using The Metropolitan Museum of Art's open data.
You see a photo of an object and its medium, drop a pin where you think it was made, and pick a
250-year block for when. It also serves as the test bed for an image-to-place-and-date model;
see `PLAN.md`.

## Run it

```
pip install fastapi uvicorn pandas pyarrow numpy
python -m game.server
```

Then open http://localhost:8321. Run the tests with `python -m pytest tests`.

The game needs `data/game_pool.parquet`. To build it, put the Met's `MetObjects.csv` in `data/`
and run `python -m game.pool`.

## Hosting on GitHub Pages

GitHub Pages only serves static files, so `python -m game.build_site` builds a server-free copy
into `docs/`, which Pages serves from the `main` branch. Rebuild and commit `docs/` whenever
`game/static/` or the pool changes. To try the build locally, run `python -m http.server 8000`
from the project root and open http://localhost:8000/docs/.

On the static site, scoring runs in the browser and answers are readable in the page's data.
That's fine for a casual game, but use the local server for anything that needs rounds kept
hidden, such as an agent.

## How it plays

Each game is ten objects, each worth up to 10,000 points. **Play** gives ten random objects, as
often as you like. There is also a daily game that is the same for everyone on a given date and
resumes after a reload.

| Rule | Value |
|---|---|
| Place score | `round(5000 * exp(-km / 2000))`, km = distance to the answer region, 0 inside it |
| Time score | `round(5000 * exp(-gap / 1000))`, gap = years between your block and the object's date range, 0 if they overlap |
| Timeline | 3000 BCE to 2025 CE; a 250-year block centred on your click, snapped to 125-year steps |
| Share squares | blue ≥ 9,000, green ≥ 6,000, yellow ≥ 3,000, else red |

Scoring constants are in `game/scoring.py`. Results compare against your own past games, since
there are no other players locally.

## Where answers come from

`game/geolite.py` turns the Met's text fields into an answer region:

1. **Named cultures** with a known homeland (Moche, Minoan, Yoruba) use a hand-placed region.
2. **Country fields**, or a demonym such as "French", give a whole modern country (Natural Earth 50m).
3. **Finer fields** then narrow that where they can: Subregion, City, County, State, Region and
   qualifiers in the culture ("India (Gujarat)", "German, Augsburg"). Places come from a hand-written
   table of sites and provinces and from GeoNames cities, and a match must lie inside the country.

Accuracy limits:

- **The Met's labels are the ceiling.** About two thirds of eligible objects still get a whole
  country, usually because the Met records only "Japan" or "China" and nothing finer exists.
- **Regions are modern.** A historical culture is approximated by today's borders or a circle.
- **City names can be ambiguous.** Several places in one country sharing a name resolve to the largest.
- **Objects are skipped, not guessed**, when the place can't be resolved or its alternatives span
  two world regions.

## Picking objects

`game/pool.py` keeps public-domain objects with a photo, usable dates (under 1,000 years wide, within
the timeline) and a resolvable place. It drops prints, books, photographs, fragments and similar. Each
world region is sampled separately, so Europe, Egypt and Attic vases don't dominate.

Daily games draw each slot by the region weights in `server.BUCKET_WEIGHTS`, then take the next unused
object from that region. Rebuilding the pool changes which objects each date gets.

Rebuild with `python -m game.pool` (`--no-fetch` to use only cached objects, `--per-bucket N` to resize).
The Met API throttles to about one object per second, and responses are cached in `data/game_objects.jsonl`.
Rebuild `game/cities.json` with `python -m game.build_cities path/to/cities15000.txt`.

## Layout

```
game/
├── scoring.py        rules and constants
├── geolite.py        Met text fields → answer region; distance to a region
├── cities.json       GeoNames cities named in the Met's data
├── build_cities.py   rebuilds cities.json
├── build_site.py     builds the static site into docs/
├── pool.py           CSV filter, balanced sample, API fetch → data/game_pool.parquet
├── server.py         FastAPI: daily picks, scoring, image proxy, static files
└── static/           vanilla JS, canvas map, SVG timeline, no build step
tests/                rules, geography, and browser-versus-server scoring (needs Node)
docs/                 generated static site, served by GitHub Pages
data/                 Met CSV, cached API responses, pool, photo cache (not in git)
```

## API

The local server only; the static site answers the same calls from `static_backend.js`.

| Endpoint | Meaning |
|---|---|
| `GET /api/practice` | ten random rounds |
| `GET /api/daily?date=YYYY-MM-DD` | ten rounds for a date (2026-01-01 through tomorrow) |
| `POST /api/guess` | `{round_id, lat, lon, year}`, where `year` is the block START; returns scores and the full answer |
| `GET /api/image/{round_id}/{n}` | photo; `?full=true` for original resolution |
| `GET /api/scoring` | rule constants |

Before a guess, a round is only `{round_id, photos, medium, credit}`. Met URLs and object ids are
withheld because they reveal the answer: image paths name the curatorial department. An agent playing
through this API must not read `data/`, or its scores mean nothing.

## Credits

Objects and photos: The Metropolitan Museum of Art Open Access, CC0. Borders: Natural Earth, public
domain. Cities: GeoNames, CC BY 4.0. Game rules are modelled on an existing daily artifact guessing game.
