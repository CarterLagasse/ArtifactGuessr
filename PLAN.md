# Anthropeum — Formalized Plan: Image → (Where, When)

Adapts and concretizes `anthropeum_model_plan.md` with verified data facts and the actual
computing environment. Plan freeze date: 2026-10-29.

**Task.** Image-only prediction of an object's geographic origin (point + uncertainty)
and creation date (distribution over years). Met Open Access is the training corpus;
game-style scoring (great-circle distance km, |year error|) is one reported metric.

---

## 0. Verified facts (as of 2026-10-29)

### 0.1 Data sources — all confirmed live

| Source | What | Details |
|---|---|---|
| Met bulk CSV | `MetObjects.csv`, **340 MB**, **503,012 rows** | `github.com/metmuseum/openaccess`, Git LFS. 54 columns. UTF-8 **with BOM**. **No image URLs in the CSV.** Updated 2024-08. CC0. |
| Met Collection API | Object JSON + image URLs | `https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}`. No key. **Limit: 80 req/s.** `/v1/search` retired 2026-10-01; use `/v1.1/search` (paginated, max 10k results) or `/objects` for enumeration. |
| Met images | `primaryImageSmall` (web-large, ~200–400 KB) | Use for training. `primaryImage` (original, multi-MB) only if a specific investigation needs it. |
| Cleveland CMA API | Held-out museum (optional) | `https://openaccess-api.clevelandart.org/api/artworks/?has_image=1&cc0=1`. Fields: `culture`, `creation_date_earliest/latest`, images on `openaccess-cdn.clevelandart.org/{acc}/{acc}_web.jpg`. CC0 filter: `share_license_status=CC0`. |
| AIC API | Backup held-out museum | JSON API + weekly data dumps (`github.com/art-institute-of-chicago/api-data`), IIIF images `/full/843,/0/default.jpg`, CC0 via `is_public_domain`. Throttled to 60 req/min anonymous — prefer the data dump. |

### 0.2 Met dataset slice — measured from the CSV

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

### 0.3 Environment

| Item | Value | Consequence |
|---|---|---|
| GPU | RTX 4050 Laptop, **6 GB VRAM**, driver 610.60, CC 8.9 | Enough for Stage 1 (frozen embeddings) comfortably and Stage 2 (LoRA / last-block fine-tune) at 224–336 px with bf16 + grad checkpointing. |
| torch | **2.11.0+cpu** installed | CPU build → **must reinstall CUDA wheel** (`uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126`) or Stage 1 takes days instead of hours. |
| Python | 3.13.4, pandas 3.0, opencv, sklearn, matplotlib present | Add: `imagehash` (dedup), `h3` (geocells), `pyarrow` (tables), `tqdm`, `aiohttp` (fetcher), `timm` or `transformers` (backbones). |
| Disk | **143 GB free** on C: | Web-large images for ~350k images ≈ 45–70 GB. Originals would not fit. Never download originals. |

---

## 1. Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Input | **Image only** | Metadata leaks the answer (Department → geocells). Metadata retained as targets/aux heads. |
| Output | **Distributions**, collapsed to points at eval | Location: geocell classification with distance-softened labels. Date: binned ordinal with soft `[begin,end]` targets. |
| Eval target | Raw distribution **and** region/era-balanced distribution, per-slice metrics always | A single average hides everything. |
| Test museum | **Held-out Met** (primary, group-split). Cleveland = optional milestone-6 health check | In-Met tests are the game-relevant eval; cross-museum only adjudicates whether representations are museum-agnostic. Cheap to add later because the API is verified. |
| Backbone | Stage 1: **SigLIP2-B-16 or SigLIP-B-16 / DINOv2-B**, frozen. Stage 2: fine-tune last blocks + LoRA. Stage 4 (optional): small open VLM via QLoRA | 6 GB VRAM steers toward ≤1B-parameter backbones; B-scale (86M–400M) is the sweet spot. |

---

## 2. Directory layout

```
anthropeum_cnn/
├── PLAN.md                     ← this file
├── anthropeum_model_plan.md    ← strategy reference (read-only)
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

## 3. Milestone 1 — Data pipeline (the big one; budget ~2–3 weeks clock time)

### 3.1 Fetcher (`fetch/`)
1. `download_api.py`: walk all ObjectIDs via `/objects`, `GET /objects/{id}` at ~40 req/s (under the 80 cap),
   cache **JSONL**. One metadata pass ≈ 30–60 min. Infinite retry, resumable, sha256 metadata snapshot.
2. `download_images.py`: for PD objects with geo+dates, queue `primaryImageSmall` (web-large),
   4–8 concurrent workers, ~10–20 img/s → **6–10 h background** for ~300k images, ~45–60 GB.
   Write `met_images_index.parquet` (objectID, url, bytes, sha1, ok).
3. `verify.py`: PIL-open every file, flag corrupt placeholders; images without exactly one
   participating ≥256px channel get re-checked (Met sometimes serves "Image not available" JPEGs).

### 3.2 Cleaning (`clean/`)
- **Dedup (two passes):** exact by sha1 → near-dup by perceptual hash (imagehash, threshold 0.9)
  → cluster by DINOv2 embedding cosine > 0.95. **Images of the same object share geocells implicitly**
  (same oid, same split) but near-dups across oids (ledger pages, portfolios, casts kept apart)
  must not straddle splits.
- **Reproductions/casts/copies:** flag via regex on `Medium`/`Object Name`/`Artist Prefix`
  ("cast", "copy", "facsimile", "reproduction", "after"). Keep, but assign `weight tier R = 0.5`.
- **Non-object images:** drop `The Libraries` dept, InSitu "photo of gallery" leftovers surfaced by verify pass.
- **Date cleaning:** drop sentinels (-700000, 9999); fix `begin>end` by swap (verify against `Object Date` text);
  rows with `Object Date` unparseable and span >2000 y → drop; span>1000 y → confidence tier C (see labels).

### 3.3 Geocoding (`geo/`) — the highest-risk step; QA 300 labels by hand before scaling
1. **Normalize** geo strings: strip "present-day", split pipe-joins (`A|B` → keep A), "Egypt or Syria" → keep A but mark tier B, "Byzantine Egypt" → {lat/lon of "Egypt", tier B}.
2. **Control-point geocoding** rather than per-object API calls:
   extract unique normalized keys (expect ~2–6k) → geocode each once against
   Wikidata SPARQL / OSM Nominatim (1 req/s max, cached to `geobase/`).
3. **Culture→centroid map** for the Culture-only holdings (Greek/Attic 16.8k, Japan 15.7k, Cypriot,
   Sasanian, Etruscan…): hand-build ~120–200 entries with centroid + radius. This is where
   correctness matters most; keep it as a visible, version-controlled table `culture_map.yaml`.
4. **Confidence tiers:** A = locale/subregion/locus-level label; B = country/state; C = culture-region only.
   Store centroid + radius per object. Radii seed the geocell soft labels (§5.2).

### 3.4 Success criteria for M1
- `labels.parquet`: ≥120k objects with tier-A/B geography and valid dates plus tier-C ≥25k;
- ≤2% hand-checked geocode errors in a 300-label stratified sample;
- download completeness ≥99% of the index; splits frozen and written to `artifacts/splits/`.

---

## 4. Milestone 2 — Splits & eval harness (built **before** any model)

### 4.1 Splits
Splits are by **object**, never by image; stratified jointly by geocell × era-decade × tier.
- `train` 80% / `val` 10% / `test_met` 10% of the ~180k-object set.
- Near-dup-cluster constraint: any cross-object cluster (§3.2 pass 3) lives entirely in one split.
- `test_met_unseen_culture`: Culture strings absent from train — measures generalization beyond
  memorized looks (many objects share culture with train; report both slices).
- `test_game_like`: build after sampling the real game's distribution (play it and log ~100 draws) —
  resample `test_met ∪ val` to match. Freeze once built.
- Split manifests written once, checksummed; **evals always run from manifests.**

### 4.2 Metrics (`eval/`)
- Location: median & mean great-circle km; %≤25 / 200 / 750 / 2500 km.
- Date: median & mean |years|; %≤25 / 100 / 500 y.
- Per-slice mandatory tables: geocell (or continent/region), era (decile of |year|), tier, classification,
  seen/unseen culture.
- `game_score.py`: reimplement Anthropeum-style scoring from (km, |Δyears|); calibrate by comparing
  to human scores on a few real games rather than claiming exactness.
- Zero-shot frontier VLM baseline on 2,000-image stratified subsample (`stage5.py` — run it early:
  it calibrates what "hard" means and will inform whether Stage 4 (VLM fine-tune) is worth it).

---

## 5. Models

### 5.1 Stage 0 — trivial baselines (write immediately, before training anything)
(a) global median geo + median year; (b) department-conditional medians (uses leaks only as a *baseline
ceiling* comparison — flagged as such); (c) geocell-prior (predict the most common cell; report %≤thresholds
expectably ~20–35%).

### 5.2 Stage 1 — frozen embeddings + heads (first real model, hours not weeks)
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

### 5.3 Stage 2 — backbone fine-tune (the workhorse)
- **SigLIP2-B / DINOv2-B**, LoRA (r=16–32, attn+mlp) on last ~4 blocks + heads. bf16, grad-checkpointing,
  input 224 → 336 curriculum, batch 4–8 micro × accum 8. Fits in 6 GB.
- **Sampling:** geocell-balanced, weight ∝ n_cell^(−α), α∈{0.3, 0.5, 0.7} tuned on val; cap oversampling
  at 4×; heavy augmentation for cells with <2000 images (color jitter mild, no strong blur,
  **no horizontal flip when `Medium`/`Culture` says inscriptions/asymmetric iconography** — flip carries real risk; ablate).
- Loss: location CE(soft) + smoothed-L1-ordinal distance for date + Σ aux CE, weights learned via uncertainty weighting; sample weight ∝ tier (A=1, B=0.8, C=0.4, R(repro)=0.5).
- **Shortcut audit:** mask background/scale bars on 1k val images → measure metric drop; if >1.5× median-km
  or |yrs|, it's plan-theft not art, revisit augmentation/photography stratification.
- Multi-view: sample 1 random view per step during training; at eval, mean-pool embeddings across all views.

### 5.4 Stage 3 — retrieval ensemble
- Same-taxonomy kNN over train embeddings at inference (top-20 distance-weighted vote on cells + date bins),
  blended with Stage 2's predictions (weighted by val reliability). Free win for rare cells; costs only embedding
  lookup.

### 5.5 Stage 4 (optional) — VLM
- QLoRA 4-bit on a ~3B-parameter open VLM to emit `country / region / year-range` in structured text.
  Only pursue if Stage 2 val numbers are stale or a natural-language interface has value; the 6 GB
  card is the bottleneck, not the idea.

### 5.6 Stage 5 (optional) — cross-museum
- Normalize Cleveland (API verified, `share_license_status=CC0`, `culture` + `creation_date_earliest/latest`,
  images at `openaccess-cdn.…/{acc}_web.jpg`) into the same schema; run the frozen Met model on ~5k CC0 objects
  and compare against the Met test numbers. One notebook-sized study.

---

## 6. Milestones & ordering

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

## 7. Risks & mitigations (ranked)

1. **Geocode errors silently cap everything** → control-point cache, culture_map.yaml is version-controlled and hand-curated, 300-label QA gate before M3.
2. **Leakage via near-dupes / portfolio series / same-oid multi-view images** → three-pass dedup, object-grouped splits, checksummed manifests, cross-cluster constraint by construction.
3. **Data skew** (Egypt/Attic/Japan dominate; sub-Saharan Africa, Oceania, SE Asia are few) → balanced sampling with capped oversampling + mandatory per-region tables, never collapse to a health number.
4. **Date-label noise** (sentinels, ▶500 y spans, 142 bad rows) → cleaned with per-row tier; soft targets so the model doesn't pretend precision the labels lack.
5. **Photographic shortcuts** (Met background, scale bars, lighting era) → background-mask ablation at M7; treat metric drop as a red flag, not a bug.
6. **Game score is an approximation** → built from (km, |Δyears|); explicitly calibrated against hand-played examples once, never claimed exact.
7. **Disk / GPU limits** → never fetch `primaryImage` (originals); B-scale backbones only; bf16 + grad checkpointing; Stage 4 excluded by default because 6 GB makes serious VLM fine-tuning infeasible.

---

## 8. Immediate next actions (in order)

1. `uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126 pillow imagehash h3 pyarrow tqdm aiohttp pyyaml` — verify `torch.cuda.is_available()`.
2. Re-download `MetObjects.csv` into `data/` (the copy used for analysis lived in %TEMP%). Run M0 parquet + baselines.
3. Write and launch `fetch/download_api.py` (background; snapshot JSONL continuously).
4. While it runs, build `src/aep/geo/` normalization table + first 50 `culture_map.yaml` entries by hand.
5. Kick off `fetch/download_images.py` once M1's API pass finishes; it runs unattended for ~a day.
