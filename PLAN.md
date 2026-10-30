# ArtifactGuessr — running plan

Living document: update it as work happens. How to run and play the game is in `README.md`.
Dated facts are as of the date stated beside them.

## Status

Last updated 2026-10-30.

**Game: playable.** Unlimited and daily modes, Met pool, polygon and sub-national regions, tests.

**Model: not started.** Nothing in Part 2 or Part 3 has been built yet, and their boxes are unchecked.

## Part 1. Game backlog

- [ ] Historical regions: replace modern-country regions with historical-basemaps / OpenHistoricalMap polygons, so a Sasanian object is scored against the Sasanian Empire.
- [ ] Grow `geolite.FINE_PLACES`: about a third of the catalogue has no sub-national match, some of it fixable. Check the most common unmatched strings first.
- [ ] Hand-check about 300 region assignments, as the model plan's QA gate requires. Sample with `python -m game.pool --no-fetch` output.
- [ ] Use the Locale, Locus and Excavation fields, unused so far. They mostly name tombs and need a site lookup.
- [ ] Decide whether the medium should stay visible before guessing. It helps players, but the model is image-only.
- [ ] Larger pool: rerun `python -m game.pool --per-bucket N`. Sub-Saharan Africa and Oceania are limited by the catalogue itself.

## Part 2. Model plan

**Task.** Image-only prediction of an object's geographic origin (point + uncertainty)
and creation date (distribution over years). Met Open Access is the training corpus;
game-style scoring (great-circle distance km, |year error|) is one reported metric.



#### 0. Verified facts (as of 2026-10-29)

##### 0.1 Data sources — all confirmed live

| Source | What | Details |
|---|---|---|
| Met bulk CSV | `MetObjects.csv`, **340 MB**, **503,012 rows** | `github.com/metmuseum/openaccess`, Git LFS. 54 columns. UTF-8 **with BOM**. **No image URLs in the CSV.** Updated 2024-08. CC0. |
| Met Collection API | Object JSON + image URLs | `https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}`. No key. **Limit: 80 req/s.** `/v1/search` retired 2026-10-01; use `/v1.1/search` (paginated, max 10k results) or `/objects` for enumeration. |
| Met images | `primaryImageSmall` (web-large, ~200–400 KB) | Use for training. `primaryImage` (original, multi-MB) only if a specific investigation needs it. |
| Cleveland CMA API | Held-out museum (optional) | `https://openaccess-api.clevelandart.org/api/artworks/?has_image=1&cc0=1`. Fields: `culture`, `creation_date_earliest/latest`, images on `openaccess-cdn.clevelandart.org/{acc}/{acc}_web.jpg`. CC0 filter: `share_license_status=CC0`. |
| AIC API | Backup held-out museum | JSON API + weekly data dumps (`github.com/art-institute-of-chicago/api-data`), IIIF images `/full/843,/0/default.jpg`, CC0 via `is_public_domain`. Throttled to 60 req/min anonymous — prefer the data dump. |

##### 0.2 Met dataset slice — measured from the CSV

| Quantity | Count |
|---|---|
| Total objects | 503,012 |
| `Is Public Domain` | **260,424** |
| PD with *any* geography (`Country/Region/Subregion/Locale/Locus/Culture`) | **180,157** — the training set |
| PD with `Country` alone | 48,094 (Culture carries the bulk of geo signal) |
| PD with valid `begin<=end` dates | ~180k modulo cleaning |
| Date-label issues | 142 `begin>end` rows; sentinels at **-700000** and **9999**; ~9.8k rows with span >500 y; median span 10 y, p90 199 y, p99 800 y |
| Top geo strings (notable for geocoding) | `Egypt` 17.0k, `Byzantine Egypt` 1.7k, `present-day France` 177, `United States\|United States` 209, `Egypt or Syria` 183, `Greek, Attic` 16.8k, `Japan` 15.7k |

**Implications:**
1. ~180k objects is the hard ceiling for the Tab; after cleaning and image downloads expect **~150k usable objects / ~200–350k images** (many objects have multiple views).
2. Geographic skew is extreme: Egypt, Attic Greece, Japan, Europe dominate. Africa/Oceania/SE Asia are tiny slices. This *must* drive both sampling and per-region metric reporting.
3. Country strings are noisy ("Byzantine Egypt", pipes, "present-day X") — geocoding needs a normalization pass before lookup.

##### 0.3 Environment

| Item | Value | Consequence |
|---|---|---|
| GPU | RTX 4050 Laptop, **6 GB VRAM**, driver 610.60, CC 8.9 | Enough for Stage 1 (frozen embeddings) comfortably and Stage 2 (LoRA / last-block fine-tune) at 224–336 px with bf16 + grad checkpointing. |
| torch | **2.11.0+cpu** installed | CPU build → **must reinstall CUDA wheel** (`uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126`) or Stage 1 takes days instead of hours. |
| Python | 3.13.4, pandas 3.0, opencv, sklearn, matplotlib present | Add: `imagehash` (dedup), `h3` (geocells), `pyarrow` (tables), `tqdm`, `aiohttp` (fetcher), `timm` or `transformers` (backbones). |
| Disk | **143 GB free** on C: | Web-large images for ~350k images ≈ 45–70 GB. Originals would not fit. Never download originals. |

---

#### 1. Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Input | **Image only** | Metadata leaks the answer (Department → geocells). Metadata retained as targets/aux heads. |
| Output | **Distributions**, collapsed to points at eval | Location: geocell classification with distance-softened labels. Date: binned ordinal with soft `[begin,end]` targets. |
| Eval target | Raw distribution **and** region/era-balanced distribution, per-slice metrics always | A single average hides everything. |
| Test museum | **Held-out Met** (primary, group-split). Cleveland = optional milestone-6 health check | In-Met tests are the game-relevant eval; cross-museum only adjudicates whether representations are museum-agnostic. Cheap to add later because the API is verified. |
| Backbone | Stage 1: **SigLIP2-B-16 or SigLIP-B-16 / DINOv2-B**, frozen. Stage 2: fine-tune last blocks + LoRA. Stage 4 (optional): small open VLM via QLoRA | 6 GB VRAM steers toward ≤1B-parameter backbones; B-scale (86M–400M) is the sweet spot. |

---

#### 2. Directory layout

```
project root/
├── README.md
├── PLAN.md
├── configs/
│   └── base.yaml               ← cell sizes, date bins, sampling α, split seeds
├── data/
│   ├── MetObjects.csv
│   ├── met_objects.parquet     ← cleaned/cast CSV
│   ├── met_images_index.parquet ← one row per URL: objectID, url, sha1, split
│   ├── objects_all.jsonl       ← cached API object JSONs (metadata re-fetchable)
│   ├── images/met/large/{oid}_{n}.jpg
│   ├── geobase/                ← geocode cache + culture→centroid map
│   └── labels.parquet          ← final per-image labels: cell, tier, soft targets
├── src/aep/
│   ├── fetch/      (download_api.py, download_images.py, verify.py)
│   ├── clean/      (dedupe.py, repro_filter.py, date_clean.py)
│   ├── geo/        (normalize_geostrings.py, geocode.py, geocells.py)
│   ├── labels/     (build_labels.py — writes labels.parquet)
│   ├── splits/     (make_splits.py — group + cluster-aware)
│   ├── model/      (extract_embeddings.py, heads.py, train.py, vlm_qlora.py)
│   ├── eval/       (metrics.py, evaluate.py, slices.py, game_score.py)
│   └── common/     (io, images, config)
├── runs/                       ← checkpoints, training logs
└── artifacts/                  ← embeddings cache, split manifests, eval reports
```

---

#### 3. Milestone 1 — Data pipeline (the big one; budget ~2–3 weeks clock time)

##### 3.1 Fetcher (`fetch/`)
1. `download_api.py`: walk all ObjectIDs via `/objects`, `GET /objects/{id}` at ~40 req/s (under the 80 cap),
   cache **JSONL**. One metadata pass ≈ 30–60 min. Infinite retry, resumable, sha256 metadata snapshot.
2. `download_images.py`: for PD objects with geo+dates, queue `primaryImageSmall` (web-large),
   4–8 concurrent workers, ~10–20 img/s → **6–10 h background** for ~300k images, ~45–60 GB.
   Write `met_images_index.parquet` (objectID, url, bytes, sha1, ok).
3. `verify.py`: PIL-open every file, flag corrupt placeholders; images without exactly one
   participating ≥256px channel get re-checked (Met sometimes serves "Image not available" JPEGs).

##### 3.2 Cleaning (`clean/`)
- **Dedup (two passes):** exact by sha1 → near-dup by perceptual hash (imagehash, threshold 0.9)
  → cluster by DINOv2 embedding cosine > 0.95. **Images of the same object share geocells implicitly**
  (same oid, same split) but near-dups across oids (ledger pages, portfolios, casts kept apart)
  must not straddle splits.
- **Reproductions/casts/copies:** flag via regex on `Medium`/`Object Name`/`Artist Prefix`
  ("cast", "copy", "facsimile", "reproduction", "after"). Keep, but assign `weight tier R = 0.5`.
- **Non-object images:** drop `The Libraries` dept, InSitu "photo of gallery" leftovers surfaced by verify pass.
- **Date cleaning:** drop sentinels (-700000, 9999); fix `begin>end` by swap (verify against `Object Date` text);
  rows with `Object Date` unparseable and span >2000 y → drop; span>1000 y → confidence tier C (see labels).

##### 3.3 Geocoding (`geo/`) — the highest-risk step; QA 300 labels by hand before scaling
1. **Normalize** geo strings: strip "present-day", split pipe-joins (`A|B` → keep A), "Egypt or Syria" → keep A but mark tier B, "Byzantine Egypt" → {lat/lon of "Egypt", tier B}.
2. **Control-point geocoding** rather than per-object API calls:
   extract unique normalized keys (expect ~2–6k) → geocode each once against
   Wikidata SPARQL / OSM Nominatim (1 req/s max, cached to `geobase/`).
3. **Culture→centroid map** for the Culture-only holdings (Greek/Attic 16.8k, Japan 15.7k, Cypriot,
   Sasanian, Etruscan…): hand-build ~120–200 entries with centroid + radius. This is where
   correctness matters most; keep it as a visible, version-controlled table `culture_map.yaml`.
4. **Confidence tiers:** A = locale/subregion/locus-level label; B = country/state; C = culture-region only.
   Store centroid + radius per object. Radii seed the geocell soft labels (§5.2).

##### 3.4 Success criteria for M1
- `labels.parquet`: ≥120k objects with tier-A/B geography and valid dates plus tier-C ≥25k;
- ≤2% hand-checked geocode errors in a 300-label stratified sample;
- download completeness ≥99% of the index; splits frozen and written to `artifacts/splits/`.

---

#### 4. Milestone 2 — Splits & eval harness (built **before** any model)

##### 4.1 Splits
Splits are by **object**, never by image; stratified jointly by geocell × era-decade × tier.
- `train` 80% / `val` 10% / `test_met` 10% of the ~180k-object set.
- Near-dup-cluster constraint: any cross-object cluster (§3.2 pass 3) lives entirely in one split.
- `test_met_unseen_culture`: Culture strings absent from train — measures generalization beyond
  memorized looks (many objects share culture with train; report both slices).
- `test_game_like`: build after sampling the real game's distribution (play it and log ~100 draws) —
  resample `test_met ∪ val` to match. Freeze once built.
- Split manifests written once, checksummed; **evals always run from manifests.**

##### 4.2 Metrics (`eval/`)
- Location: median & mean great-circle km; %≤25 / 200 / 750 / 2500 km.
- Date: median & mean |years|; %≤25 / 100 / 500 y.
- Per-slice mandatory tables: geocell (or continent/region), era (decile of |year|), tier, classification,
  seen/unseen culture.
- `game_score.py`: reuse `game/scoring.py`, which matches the live game's formulas. The inputs still need
  region polygons and `[begin, end]` ranges, not point labels, or the numbers won't be comparable.
- Zero-shot frontier VLM baseline on 2,000-image stratified subsample (`stage5.py` — run it early:
  it calibrates what "hard" means and will inform whether Stage 4 (VLM fine-tune) is worth it).

---

#### 5. Models

##### 5.1 Stage 0 — trivial baselines (write immediately, before training anything)
(a) global median geo + median year; (b) department-conditional medians (uses leaks only as a *baseline
ceiling* comparison — flagged as such); (c) geocell-prior (predict the most common cell; report %≤thresholds
expectably ~20–35%).

##### 5.2 Stage 1 — frozen embeddings + heads (first real model, hours not weeks)
- Extract DINOv2-B or SigLIP2-B embeddings once (2 forward passes/object; ~30–90 min on
  GPU even at 448px). Cache to `artifacts/emb_dino_b.parquet`.
- Train: geocell-classifier head + binned-ordinal date head + aux heads (culture, classification, medium, dept)
  on embeddings. Try a kNN/embedding-retrieval member as ensemble input.
- **Location output:** geocells via H3 (or adaptive quadtree) at ~300–500 km edge; soft target
  `m_j ∝ exp(−d_j / (1.5·radius_i))` normalized over cells within ~3 radii; point output =
  α-blended expected-location estimate from the predicted cell distribution.
- **Date output:** non-uniform bins **uneven** by construction — see pipeline spec: quantile-fitted
  edges with ≥50k samples per bin where possible; soft truth spreads mass across `[begin,end]`;
  inference = median of predicted dist (minimizes abs error) with mode as secondary point.
- Feasibility gate: if Stage-1 val beats Stage-0 by a factor you respect on both loc & date
  (e.g. loc %≤200 km > 40–55%, date median ALE < roughly 50–80 y for visible-object subsets), Stage 2 is worth it.

##### 5.3 Stage 2 — backbone fine-tune (the workhorse)
- **SigLIP2-B / DINOv2-B**, LoRA (r=16–32, attn+mlp) on last ~4 blocks + heads. bf16, grad-checkpointing,
  input 224 → 336 curriculum, batch 4–8 micro × accum 8. Fits in 6 GB.
- **Sampling:** geocell-balanced, weight ∝ n_cell^(−α), α∈{0.3, 0.5, 0.7} tuned on val; cap oversampling
  at 4×; heavy augmentation for cells with <2000 images (color jitter mild, no strong blur,
  **no horizontal flip when `Medium`/`Culture` says inscriptions/asymmetric iconography** — flip carries real risk; ablate).
- Loss: location CE(soft) + smoothed-L1-ordinal distance for date + Σ aux CE, weights learned via uncertainty weighting; sample weight ∝ tier (A=1, B=0.8, C=0.4, R(repro)=0.5).
- **Shortcut audit:** mask background/scale bars on 1k val images → measure metric drop; if >1.5× median-km
  or |yrs|, it's plan-theft not art, revisit augmentation/photography stratification.
- Multi-view: sample 1 random view per step during training; at eval, mean-pool embeddings across all views.

##### 5.4 Stage 3 — retrieval ensemble
- Same-taxonomy kNN over train embeddings at inference (top-20 distance-weighted vote on cells + date bins),
  blended with Stage 2's predictions (weighted by val reliability). Free win for rare cells; costs only embedding
  lookup.

##### 5.5 Stage 4 (optional) — VLM
- QLoRA 4-bit on a ~3B-parameter open VLM to emit `country / region / year-range` in structured text.
  Only pursue if Stage 2 val numbers are stale or a natural-language interface has value; the 6 GB
  card is the bottleneck, not the idea.

##### 5.6 Stage 5 (optional) — cross-museum
- Normalize Cleveland (API verified, `share_license_status=CC0`, `culture` + `creation_date_earliest/latest`,
  images at `openaccess-cdn.…/{acc}_web.jpg`) into the same schema; run the frozen Met model on ~5k CC0 objects
  and compare against the Met test numbers. One notebook-sized study.

---

#### 6. Milestones & ordering

| # | Milestone | Output | Est. effort |
|---|---|---|---|
| 0 | env setup (cu126 torch, h3, imagehash, pyarrow, tqdm); CSV → parquet; Stage-0 baselines | number on 3 baselines | 1–2 days |
| 1 | fetch JSONL + images (background); verify pass | met_images_index.parquet | 1 day setup + ~2 days unattended |
| 2 | normalize geo strings; geocode cache; `culture_map.yaml`; QA 300 labels | geobase/verified | 3–5 days mostly human |
| 3 | build `labels.parquet` (cells, soft targets, date bins, tiers, dedup flags) | labels.parquet | 1–2 days |
| 4 | splits + full eval harness + Stage-0/5 baselines done | artifacts/splits, metrics sheet | 2–3 days |
| 5 | Stage 1: embeddings → heads → eval report | first real numbers | 2–4 days |
| 6 | Stage 2: fine-tune + sampling/soft-target ablations | paper-worthy runs | 1–2 weeks |
| 7 | ensemble, calibration, shortcut audit, per-slice report | final report | 3–5 days |
| 8 | optional: game-like test set, Cleveland, VLM QLoRA | extras | open-ended |

Critical path is 1 → 3; download of images can overlap with geocoding/QA work (run fetcher in background).

---

#### 7. Risks & mitigations (ranked)

1. **Geocode errors silently cap everything** → control-point cache, culture_map.yaml is version-controlled and hand-curated, 300-label QA gate before M3.
2. **Leakage via near-dupes / portfolio series / same-oid multi-view images** → three-pass dedup, object-grouped splits, checksummed manifests, cross-cluster constraint by construction.
3. **Data skew** (Egypt/Attic/Japan dominate; sub-Saharan Africa, Oceania, SE Asia are few) → balanced sampling with capped oversampling + mandatory per-region tables, never collapse to a health number.
4. **Date-label noise** (sentinels, ▶500 y spans, 142 bad rows) → cleaned with per-row tier; soft targets so the model doesn't pretend precision the labels lack.
5. **Photographic shortcuts** (Met background, scale bars, lighting era) → background-mask ablation at M7; treat metric drop as a red flag, not a bug.
6. **Game score depends on regions** → the formulas are known (`game/scoring.py`), but the real game's region polygons aren't ours, so local scores remain close rather than identical.
7. **Disk / GPU limits** → never fetch `primaryImage` (originals); B-scale backbones only; bf16 + grad checkpointing; Stage 4 excluded by default because 6 GB makes serious VLM fine-tuning infeasible.

---

#### 8. Immediate next actions (in order)

1. `uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126 pillow imagehash h3 pyarrow tqdm aiohttp pyyaml` — verify `torch.cuda.is_available()`.
2. Re-download `MetObjects.csv` into `data/` (the copy used for analysis lived in %TEMP%). Run M0 parquet + baselines.
3. Write and launch `fetch/download_api.py` (background; snapshot JSONL continuously).
4. While it runs, build `src/aep/geo/` normalization table + first 50 `culture_map.yaml` entries by hand.
5. Kick off `fetch/download_images.py` once M1's API pass finishes; it runs unattended for ~a day.

## Part 3. Checklist


#### Phase 0 — Environment (do today, ~30 min)

- [ ] **0.1** Install CUDA PyTorch (current install is CPU-only — nothing will train on GPU until this is fixed):
  ```bash
  pip uninstall -y torch torchvision
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
  ```
- [ ] **0.2** Verify GPU: `python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"` → should print `True` + RTX 4050
- [ ] **0.3** Install small deps:
  ```bash
  pip install pyarrow imagehash h3 pyyaml tqdm aiohttp timm
  ```
- [ ] **0.4** Initialize the repo — this folder isn't a git repo yet:
  ```bash
  git init
  git config user.name "carte"
  git config user.email "you@example.com"
  ```
  Then add a `.gitignore` with at least: `data/`, `artifacts/`, `runs/`, `*.parquet`, `*.jpg`, `*.pt`, `*.safetensors` (you'll have tens of GB of binary data — none of it belongs in git)
- [ ] **0.5** Get the 340 MB `MetObjects.csv` into `data/`:
  ```bash
  git lfs install
  git lfs clone https://github.com/metmuseum/openaccess data/openaccess_clone
  ```
  *(or download via the GitHub "Raw" download button; a copy from today exists in `%TEMP%\opencode\MetObjects.csv` but TEMP gets cleaned)*

---

#### Phase 1 — Trivial baselines (Stage 0) — say "I did this" proudly, ~1 day

- [ ] **1.1** `src/aep/common/io.py` — CSV→parquet loader; strip the BOM; cast `Object Begin/End Date` to int, `Is Public Domain` to bool
- [ ] **1.2** `src/aep/clean/date_clean.py` — drop sentinels (-700000, 9999); fix the 142 `begin>end` rows; record per-row date-width
- [ ] **1.3** `src/aep/eval/game_score.py` — reuse `game/scoring.py`: distance to a region, year gap from a 250-year block to `[begin, end]`
- [ ] **1.4** Baseline notebook/script `runs/00_baselines.ipynb` reporting (on the PD+geo+dates slice, ~180k):
  - [ ] global median lat/lon + median year
  - [ ] department-conditional medians (labeled clearly as an upper-bound-with-leak comparison)
  - [ ] geocell prior (most-frequent cell) — need `geocells.py` from 3.3 first, so defer this checkbox
- [ ] **1.5** Write the numbers down. These are the floor you must beat; don't lose them.

---

#### Phase 2 — Metadata fetch (~1 day of work, runs hours in background)

- [ ] **2.1** `src/aep/fetch/download_api.py`:
  - [ ] `GET /public/collection/v1/objects` → full ID list (~490k)
  - [ ] `GET /public/collection/v1/objects/{id}` at ≤40 req/s (cap is 80/shared), 4–8 concurrent, resumable (skip IDs already in `data/objects_all.jsonl`)
  - [ ] Append one JSON line per object; log failures separately for retry
- [ ] **2.2** Launch it in a background terminal and **leave it alone** … unless it stalls for >10 min. ETA ~40–60 min.
- [ ] **2.3** `src/aep/fetch/select_targets.py` — from JSONL, select: `isPublicDomain == true` **AND** any of `city/state/county/country/region/subregion/locale/locus/culture` non-empty **AND** valid dates. Target: ~180k. Output: `data/to_download.parquet` (objectID + URL + label-relevant fields)
- [ ] **2.4** Sanity-check selection against CSV-derived counts (PD 260,424; PD+geo 180,157) — if off by >5%, find out why before proceeding

---

#### Phase 3 — Image fetch (long; run overnight, ~45–60 GB)

- [ ] **3.1** `src/aep/fetch/download_images.py`:
  - [ ] queue `primaryImageSmall` (web-large) — **never** `primaryImage` (originals, GB-scale each)
  - [ ] 4–8 workers × polite per-host concurrency; resumable by file-existence + sha1 log
  - [ ] write rows into `data/met_images_index.parquet` (objectID, url, path, bytes, sha1, ok)
- [ ] **3.2** Just launch it — this will take **6–10 hours** unattended for ~250–350k images. Run it overnight, don't babysit it.
- [ ] **3.3** `src/aep/fetch/verify.py` — PIL-open every downloaded file; Met serves "Image not available" placeholder JPEGs that look fine on disk but are useless; flag and re-check those that are exactly some tiny (<1KB or the known placeholder size)
- [ ] **3.4** Fill disk self-audit: `df -h ~` — abort & free space if <25 GB left at any point

---

#### Phase 4 — Geocoding (the highest-risk step; human work, 3–5 days)

- [ ] **4.1** `src/aep/geo/normalize_geostrings.py` — strip "present-day", split `A|B` pipes, "or"-joins → keep A with downgrade to tier B, collapse whitespace, lowercase-compare to dedupe variants
- [ ] **4.2** Extract unique normalized keys → expect ~2–6k strings. Write them to `data/geobase/keys_to_geocode.parquet`
- [ ] **4.3** `src/aep/geo/geocode.py` — resolve the unique keys against Wikidata (SPARQL; no hard rate limit) and/or Nominatim (**max 1 req/s**, must send a real User-Agent). Cache every hit.Expect <120 min total; don't re-query a hit.
- [ ] **4.4** **Hand-build `src/aep/geo/culture_map.yaml`** for Culture-only objects (Greek/Attic 16.8k, Japan 15.7k, Cypriot 2.5k, Sasanian 906, Etruscan 1132 …). Target: top ~150 cultures → centroid (lat/lon) + plausible radius + one-line justification each. This file is the single most valuable asset in the repo; version it, comment it, treat it like code.
- [ ] **4.5** **QA gate (do not skip):** random stratified sample of 300 geocoded objects → check coordinates on a map yourself. >2% wrong ⇒ fix the systematic cause, then re-sample. Egypt vs. "Byzantine Egypt", Georgia (country vs. US state), and Cyprus-region gold-wine labels are known trap classes; specifically check those.

---

#### Phase 5 — Labels + dedup + splits (~2–3 days)

- [ ] **5.1** `src/aep/clean/dedupe.py` — exact sha1 → perceptual hash (threshold ~0.9 Hamming) → optional embedding-similarity cluster pass. Output: cluster IDs on every image row
- [ ] **5.2** `src/aep/clean/repro_filter.py` — regex on Medium/Object Name/Artist Prefix for cast/copy/facsimile/reproduction/"after" → `weight_tier = R` (0.5)
- [ ] **5.3** `src/aep/labels/build_labels.py` → `data/labels.parquet`:
  - per-image: `lat/lon`, `tier (A/B/C/R)`, `radius`, `geocell` (H3 or quadtree, ~300–500 km edge), `soft_cell_targets`
  - `date_bin` + soft `[begin,end]` spread; non-uniform bin edges fitted to the label distribution (≥50k samples/bin; constraint: p90 bin width ≈ 200 y is fine for the tail regions — don't overfit bin count)
- [ ] **5.4** `src/aep/splits/make_splits.py` — **group by objectID**, near-dup clusters never straddle splits, stratify by geocell × era × tier. Output: `artifacts/splits/train|val|test_met.parquet` + checksum manifest. **Do not re-touch after freezing.**
- [ ] **5.5** Also build `test_met_unseen_culture` (Culture strings never seen in train) and defer `test_game_like` until later (phase 8).

---

#### Phase 6 — Stage 0/5 baselines + eval harness (2–3 days)

- [ ] **6.1** `src/aep/eval/metrics.py` — location (median/mean km; %≤25/200/750/2500) + date (median/mean yrs; %≤25/100/500) + per-slice tables (region / era / tier / seen-vs-unseen culture / classification)
- [ ] **6.2** `src/aep/model/zero_shot_vlm.py` — run a frontier VLM (API-based) on a 2,000-image stratified eval subsample; record loc+date numbers. This is your reference line: helps decide whether Stage 1/2 is even worth the compute
- [ ] **6.3** Rerun Phase-1 baselines with the Phase-4 real geocoding (now non-deferred)
- [ ] **6.4** Fill `artifacts/eval/baseline_report.md` with the table. This is the pre-training scoreboard.

---

#### Phase 7 — Stage 1: frozen embeddings + heads (2–4 days)

- [ ] **7.1** `src/aep/model/extract_embeddings.py` — DINOv2-B *or* SigLIP2-B, frozen, bf16, 448px; one pass over ~300k images; cache to `artifacts/emb_*.parquet`. Expect 30–90 min on the 4050
- [ ] **7.2** `src/aep/model/heads.py` — geocell classifier + binned-ordinal date head + aux heads (culture, classification, medium, department). Quick MLP heads
- [ ] **7.3** `src/aep/model/train_cv.py` — train the heads; tune the geocell soft-label temperature (§ spec: `m_j ∝ exp(−d_j/(1.5·radius))`) and the date-bin choice. Iterate ≤1 day
- [ ] **7.4** `src/aep/eval/evaluate.py` — run on val; compare to the Phase-6 baseline table; decide: **is Stage 2 worth starting?** If Stage 1 is barely separating, go back to §4 QA — 2% label noise at tier A is worth more than a week of training

---

#### Phase 8 — Stage 2: fine-tune (only after 7.4 positive) — 1–2 weeks

- [ ] **8.1** `src/aep/model/train_ft.py` — LoRA (r=16–32, attn+mlp) on last ~4 blocks + heads; bf16; grad-checkpointing; batch 4–8 × grad-accum 8; 224 → 336 curriculum
- [ ] **8.2** Sampling: geocell-balanced `w ∝ n^(-α)`, α ∈ {0.3, 0.5, 0.7}, capped 4× oversample on rare cells with heavy augmentation (mild color jitter; **no horizontal flips** on objects whose medium/culture suggests inscriptions/asymmetric iconography — ablate this)
- [ ] **8.3** Loss: soft-CE for cells + soft-ordinal for dates + Σ aux CE; sample weights per tier (A=1, B=0.8, C=0.4, R=0.5)
- [ ] **8.4** Ablations worth 1 GPU-day each: raw vs. balanced sampling; 224 vs. 336; soft vs. hard date targets; aux heads on vs. off
- [ ] **8.5** `src/aep/eval/shortcut_audit.py` — mask background/scale bars on 1k val images, measure metric drop; flag if the drop is >1.5× median km or >1.5× median yrs

---

#### Phase 9 — Polish & extras (open-ended)

- [ ] `game_score.py`: formulas are settled (`game/scoring.py`); play the original game and log ~100 objects to compare our regions with theirs
- [ ] `test_game_like` set (from Phase-9 logs), final eval with it
- [ ] Ensemble: kNN over train embeddings + Stage-2 head predictions (weighted by val reliability)
- [ ] Calibration check: does "70% confident" mean 70% right?
- [ ] Optional cross-museum check: Cleveland API (`https://openaccess-api.clevelandart.org/api/artworks/?has_image=1&cc0=1`; fields `culture`, `creation_date_earliest/latest`, image `openaccess-cdn.clevelandart.org/{acc}/{acc}_web.jpg`) — freeze Met-trained model, run once, write a short note, move on
- [ ] Optional QLoRA VLM fine-tune — only if Stage 2 numbers plateau *and* you have a reason (interface, natural-language output) that the CNN doesn't already cover

---

#### Right-now action list for this session

1. **0.1–0.3** — Get CUDA torch + deps installed (required for anything ahead).
2. `git init` + `.gitignore`.
3. **2.1–2.2** — Write `download_api.py` and launch it in the background; metadata fetch finishes during this session.
4. While it runs: **1.1–1.5** baseline code + numbers.
5. End of session: commit. Next session: **3.x** image fetch (overnight), then **4.x** geocoding.

## Part 4. Original strategy notes

The strategy this plan grew from. Where it conflicts with Part 2, Part 2 wins.

#### 1. Goal and scope

Build a model that takes an image of a museum object and predicts:

- **Where**: a location (lat/lon, or a distribution over locations)
- **When**: a year (or a distribution over years)

The task mirrors a daily museum-object guessing game, which uses Met Museum objects and scores guesses by map distance and year error.

**Decide first (everything downstream depends on it):**

| Question | Options | Recommendation |
|---|---|---|
| Is the goal to play the game well, or to build a generally good model? | Game bot vs. general model | General model; the game is just one eval |
| What does the model see? | Image only vs. image + metadata | Image only. Metadata like Department leaks the answer |
| Output type | Point estimate vs. distribution | Distribution, collapsed to a point at eval time |
| Eval target | Raw Met distribution vs. game-like distribution | Both, reported separately |

---

#### 2. Data

##### 2.1 Primary source: Met Open Access

- CSV dump (GitHub: `metmuseum/openaccess`) plus the Collection API for images.
- Only objects flagged `isPublicDomain` have downloadable images. Filter on this first.
- Useful fields:
  - **Location signals**: `Country`, `Region`, `Subregion`, `Locale`, `Locus`, `Excavation`, `River`, `Culture`
  - **Time signals**: `objectBeginDate`, `objectEndDate` (BCE is negative), `Period`, `Dynasty`, `objectDate`
  - **Auxiliary targets**: `Department`, `Classification`, `Medium`, `Object Name`, `Tags`
- There are **no coordinates** in the data. You must geocode (see 2.3).

##### 2.2 Secondary sources (for generalization tests and more data)

- Cleveland Museum of Art, Smithsonian Open Access, Rijksmuseum, Art Institute of Chicago, Europeana, Wikimedia Commons.
- Use at least one as a **held-out test museum**. This is the only clean way to measure generalization beyond Met-specific photography and cataloguing habits.
- Normalize their metadata into the same schema (country/culture string, begin/end year).

##### 2.3 Building location labels

1. Build a text key per object from `Country`, `Region`, `Subregion`, `Locale`, falling back to `Culture` when geography is empty.
2. Geocode the key to lat/lon with GeoNames, Wikidata, or Nominatim. Cache everything.
3. Handle culture-only objects with a hand-built mapping from culture to centroid and a plausible radius (e.g. "Nazca" to coastal Peru, "Yoruba" to southwestern Nigeria).
4. Store a **confidence tier** and a **radius of uncertainty** with each label:
   - Tier A: specific find-spot or locale
   - Tier B: country-level
   - Tier C: culture/region-level only
5. Drop or down-weight objects whose geography is genuinely ambiguous ("Europe", "Near East", "Mediterranean").

Check a few hundred geocodes by eye. Systematic errors here (e.g. wrong "Georgia", wrong "Cyprus" region) will silently cap performance.

##### 2.4 Building date labels

- Take the midpoint of `objectBeginDate` and `objectEndDate` as the point label, and keep the width as an **uncertainty** value.
- Filter out or flag objects with very wide ranges (e.g. > 500 years), or use the range directly as a soft target (see 5.2).
- Sanity checks: begin <= end, no sentinel values, BCE handled consistently, and no dates after the present.
- Plot the histogram. Expect heavy mass in recent centuries and spikes at conventional dates (e.g. exactly 1 or 1000).

##### 2.5 Cleaning

- Remove non-object images (floor plans, galleries) and corrupted files.
- Deduplicate exact and near-duplicate images (perceptual hash, then embedding similarity).
- Tag **reproductions, casts, and later copies**. Their "origin" is ambiguous. Either exclude them or keep them with low weight.
- Group images by object so multi-view objects stay together (see 3).

---

#### 3. Splits and leakage

This is where most projects like this quietly fail.

- **Split by group, never by image.** Group = object ID, plus linked sets/series (same accession prefix, same donor lot, same excavation group).
- **Near-duplicate-aware split**: cluster by embedding similarity and keep clusters within one split.
- Splits:
  - `train` / `val` (from the Met, group-split)
  - `test_met` (group-split, plus a second version split by **culture/site** to measure true generalization)
  - `test_other` (a different museum entirely)
  - `test_game_like` (resampled to match the game's regional weighting, see 4)
- Freeze the test sets early and don't tune on them.

---

#### 4. Sampling

The Met collection is heavily skewed toward Europe, Egypt, Greece, and Rome. Training and evaluating on the raw distribution gives misleading results.

**Training sampling options (try in order):**

1. Raw distribution (baseline).
2. **Geocell-balanced sampling**: partition the globe into cells (e.g. S2 or adaptive-quadtree cells with a minimum object count), then sample with weight proportional to `n_cell^(-alpha)`, with alpha around 0.3 to 0.7. Tune on val.
3. **Joint balancing** across region and era so the model sees enough Bronze Age Anatolia, not just Roman Italy.
4. Loss reweighting instead of resampling (class-balanced or focal loss) if resampling overfits the rare classes.

**Evaluation sampling:**

- Report metrics on the raw distribution **and** on a region-balanced distribution.
- Estimate the game's real distribution (play it, record ~100+ objects' regions and eras, or use public write-ups) and build `test_game_like` to match.
- Always report **per-region and per-era breakdowns**. A single average hides everything interesting.

**Caveat:** very rare cells have so few objects that "balanced" training means repeating the same few images. Use stronger augmentation there, and cap the oversampling.

---

#### 5. Modeling

##### 5.1 Staged approach

| Stage | What | Purpose |
|---|---|---|
| 0 | Trivial baselines: predict the global median location and year; predict department-conditional medians | Sets the floor |
| 1 | Frozen embeddings (CLIP/SigLIP/DINOv2) + linear/MLP heads | Fast, shows how much signal is easy to get |
| 2 | Fine-tune a vision backbone (LoRA or last-N layers) with multi-task heads | Likely the main workhorse |
| 3 | kNN/retrieval over embeddings as an ensemble member | Strong, but only meaningful on `test_other` |
| 4 | Fine-tune an open VLM to emit country + date as text | Optional; test whether it beats stage 2 |
| 5 | Zero-shot frontier VLM as a reference line | Calibration of how hard the task is |

Don't skip stage 0 and stage 1. They tell you whether later complexity is earning anything.

##### 5.2 Output representation

**Location**
- Use **geocell classification** with haversine-smoothed soft labels (each cell's target mass decays with distance from the true location). Optionally add a hierarchy: continent to region to cell.
- Convert to a point at inference by taking the cell center of the argmax, or the point minimizing expected distance under the predicted distribution.
- Optionally refine within the top cell with a small regression head.

**Time**
- Use **ordinal or binned classification** over a nonuniform grid (finer for recent eras, coarser for deep time), or predict in a transformed space (e.g. log of years before present).
- Use soft targets that spread mass across the object's `[begin, end]` range rather than forcing a midpoint.
- At inference, output the median of the predicted distribution (minimizes absolute error) or the mode.

**Joint structure**
- Date and place are correlated. Consider conditioning the date head on the location distribution, or predict a shared "culture" latent and decode both from it.

##### 5.3 Auxiliary tasks

Add heads for `Culture`, `Classification`, `Medium`, and `Department`. They are cheap, strongly informative, and regularize the representation. Treat `Department` strictly as a **target**, never an input, if the goal is image-only.

##### 5.4 Image handling

- Many objects have several views. Decide whether to use one random view per step (augmentation) or aggregate multiple views at inference (attention pooling or mean embedding).
- Augmentation: crops, color jitter (mild; color matters for pigment and material), no heavy blur. Avoid horizontal flips for objects with inscriptions or asymmetric iconography, or at least test the effect.
- Image resolution matters for fine texture and tool marks. Try 224, 336, and 448.
- Watch for **photographic shortcuts**: backgrounds, lighting setups, and scale bars correlate with department or collection era. Consider background-masking experiments to measure how much the model leans on them.

##### 5.5 Loss weighting

- Total loss = location loss + date loss + auxiliary losses, with learned or tuned weights.
- Weight samples by label confidence tier (2.3) and date-range width (2.4).

---

#### 6. Evaluation

**Primary metrics**
- **Location**: median and mean great-circle distance (km); % within 25 / 200 / 750 / 2500 km.
- **Date**: median and mean absolute error (years); % within 25 / 100 / 500 years.
- **Game score proxy**: reimplement a scoring function like the game's (distance- and year-error-based) so you can report a number comparable to human play. Treat this as an approximation unless the exact formula is known.

**Slices (always report)**
- By region, by era, by classification (ceramics, textiles, metalwork, ...), by label-confidence tier.
- By whether the object's culture appeared in training (seen vs. unseen culture/site).

**Calibration**
- Are predicted distributions calibrated? (If the model says 70% for a cell, is it right 70% of the time?) Matters if you want to pick a point under uncertainty.

**Comparisons**
- Baselines from stage 0, the zero-shot VLM from stage 5, and (if possible) a small human baseline from you or friends playing the same held-out items.

**Ablations**
- Image-only vs. image + metadata (to quantify leakage if anyone later adds metadata).
- Raw vs. balanced sampling.
- Point regression vs. distribution output.
- With vs. without auxiliary heads.

---

#### 7. Key decisions and my default choices

| Decision | Default | Revisit if |
|---|---|---|
| Input modality | Image only | You want a pure "metadata-assisted" tool |
| Location output | Geocell classification with soft labels | Tiny dataset (then try regression + mixture) |
| Date output | Binned ordinal with soft range targets | Dates are mostly narrow (then regression is fine) |
| Backbone | SigLIP or DINOv2, partial fine-tune | You have lots of compute and data |
| Sampling | Geocell-balanced, alpha tuned on val | Rare classes overfit |
| Split | Group + cluster-aware, plus cross-museum | Never relax this |
| Wide-date or ambiguous objects | Down-weight, keep as soft labels | They dominate a slice you care about |
| Reproductions/casts | Flag and down-weight | Treat as a separate class if interesting |

---

#### 8. Milestones

1. **Data pipeline**: download, filter to public-domain, geocode, build labels and confidence tiers, QA a sample. *(Biggest time sink; budget accordingly.)*
2. **Splits and eval harness**: all test sets, metrics, slice reports, baseline numbers (stages 0 and 5).
3. **Embedding baseline** (stage 1) with first full evaluation report.
4. **Fine-tuned multi-task model** (stage 2) with sampling and loss ablations.
5. **Cross-museum evaluation** and error analysis (where does it fail, and is it label noise or a real model error?).
6. **Optional**: VLM fine-tune, retrieval ensemble, calibration, a demo that plays a day's game.

---

#### 9. Risks

- **Label quality is the ceiling.** Spot-check, and report results by confidence tier.
- **Leakage inflating results.** Group splits, near-duplicate clustering, and a second museum are the defenses.
- **Shortcut learning** from photo style, backgrounds, and scale bars.
- **Geographic bias**: strong overall numbers that hide poor performance outside Europe/Mediterranean.
- **Overfitting rare regions** when oversampling.
- **Ambiguity is real**: some objects have no recoverable answer from an image. Don't chase accuracy that doesn't exist; measure against a human baseline instead.
- **Terms and etiquette**: respect API rate limits, and cache images locally. Check each secondary source's license before use.
