# TODO — Anthropeum

Companion to `PLAN.md` (decisions/context live there). Work top-to-bottom; check items off.
Counts to sanity-check as you go are from the 2026-10-29 data audit.

---

## Phase 0 — Environment (do today, ~30 min)

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

## Phase 1 — Trivial baselines (Stage 0) — say "I did this" proudly, ~1 day

- [ ] **1.1** `src/aep/common/io.py` — CSV→parquet loader; strip the BOM; cast `Object Begin/End Date` to int, `Is Public Domain` to bool
- [ ] **1.2** `src/aep/clean/date_clean.py` — drop sentinels (-700000, 9999); fix the 142 `begin>end` rows; record per-row date-width
- [ ] **1.3** `src/aep/eval/game_score.py` — scoring stub from (km, |Δyears|); fill the constant later, structure now
- [ ] **1.4** Baseline notebook/script `runs/00_baselines.ipynb` reporting (on the PD+geo+dates slice, ~180k):
  - [ ] global median lat/lon + median year
  - [ ] department-conditional medians (labeled clearly as an upper-bound-with-leak comparison)
  - [ ] geocell prior (most-frequent cell) — need `geocells.py` from 3.3 first, so defer this checkbox
- [ ] **1.5** Write the numbers down. These are the floor you must beat; don't lose them.

---

## Phase 2 — Metadata fetch (~1 day of work, runs hours in background)

- [ ] **2.1** `src/aep/fetch/download_api.py`:
  - [ ] `GET /public/collection/v1/objects` → full ID list (~490k)
  - [ ] `GET /public/collection/v1/objects/{id}` at ≤40 req/s (cap is 80/shared), 4–8 concurrent, resumable (skip IDs already in `data/objects_all.jsonl`)
  - [ ] Append one JSON line per object; log failures separately for retry
- [ ] **2.2** Launch it in a background terminal and **leave it alone** … unless it stalls for >10 min. ETA ~40–60 min.
- [ ] **2.3** `src/aep/fetch/select_targets.py` — from JSONL, select: `isPublicDomain == true` **AND** any of `city/state/county/country/region/subregion/locale/locus/culture` non-empty **AND** valid dates. Target: ~180k. Output: `data/to_download.parquet` (objectID + URL + label-relevant fields)
- [ ] **2.4** Sanity-check selection against CSV-derived counts (PD 260,424; PD+geo 180,157) — if off by >5%, find out why before proceeding

---

## Phase 3 — Image fetch (long; run overnight, ~45–60 GB)

- [ ] **3.1** `src/aep/fetch/download_images.py`:
  - [ ] queue `primaryImageSmall` (web-large) — **never** `primaryImage` (originals, GB-scale each)
  - [ ] 4–8 workers × polite per-host concurrency; resumable by file-existence + sha1 log
  - [ ] write rows into `data/met_images_index.parquet` (objectID, url, path, bytes, sha1, ok)
- [ ] **3.2** Just launch it — this will take **6–10 hours** unattended for ~250–350k images. Run it overnight, don't babysit it.
- [ ] **3.3** `src/aep/fetch/verify.py` — PIL-open every downloaded file; Met serves "Image not available" placeholder JPEGs that look fine on disk but are useless; flag and re-check those that are exactly some tiny (<1KB or the known placeholder size)
- [ ] **3.4** Fill disk self-audit: `df -h ~` — abort & free space if <25 GB left at any point

---

## Phase 4 — Geocoding (the highest-risk step; human work, 3–5 days)

- [ ] **4.1** `src/aep/geo/normalize_geostrings.py` — strip "present-day", split `A|B` pipes, "or"-joins → keep A with downgrade to tier B, collapse whitespace, lowercase-compare to dedupe variants
- [ ] **4.2** Extract unique normalized keys → expect ~2–6k strings. Write them to `data/geobase/keys_to_geocode.parquet`
- [ ] **4.3** `src/aep/geo/geocode.py` — resolve the unique keys against Wikidata (SPARQL; no hard rate limit) and/or Nominatim (**max 1 req/s**, must send a real User-Agent). Cache every hit.Expect <120 min total; don't re-query a hit.
- [ ] **4.4** **Hand-build `src/aep/geo/culture_map.yaml`** for Culture-only objects (Greek/Attic 16.8k, Japan 15.7k, Cypriot 2.5k, Sasanian 906, Etruscan 1132 …). Target: top ~150 cultures → centroid (lat/lon) + plausible radius + one-line justification each. This file is the single most valuable asset in the repo; version it, comment it, treat it like code.
- [ ] **4.5** **QA gate (do not skip):** random stratified sample of 300 geocoded objects → check coordinates on a map yourself. >2% wrong ⇒ fix the systematic cause, then re-sample. Egypt vs. "Byzantine Egypt", Georgia (country vs. US state), and Cyprus-region gold-wine labels are known trap classes; specifically check those.

---

## Phase 5 — Labels + dedup + splits (~2–3 days)

- [ ] **5.1** `src/aep/clean/dedupe.py` — exact sha1 → perceptual hash (threshold ~0.9 Hamming) → optional embedding-similarity cluster pass. Output: cluster IDs on every image row
- [ ] **5.2** `src/aep/clean/repro_filter.py` — regex on Medium/Object Name/Artist Prefix for cast/copy/facsimile/reproduction/"after" → `weight_tier = R` (0.5)
- [ ] **5.3** `src/aep/labels/build_labels.py` → `data/labels.parquet`:
  - per-image: `lat/lon`, `tier (A/B/C/R)`, `radius`, `geocell` (H3 or quadtree, ~300–500 km edge), `soft_cell_targets`
  - `date_bin` + soft `[begin,end]` spread; non-uniform bin edges fitted to the label distribution (≥50k samples/bin; constraint: p90 bin width ≈ 200 y is fine for the tail regions — don't overfit bin count)
- [ ] **5.4** `src/aep/splits/make_splits.py` — **group by objectID**, near-dup clusters never straddle splits, stratify by geocell × era × tier. Output: `artifacts/splits/train|val|test_met.parquet` + checksum manifest. **Do not re-touch after freezing.**
- [ ] **5.5** Also build `test_met_unseen_culture` (Culture strings never seen in train) and defer `test_game_like` until later (phase 8).

---

## Phase 6 — Stage 0/5 baselines + eval harness (2–3 days)

- [ ] **6.1** `src/aep/eval/metrics.py` — location (median/mean km; %≤25/200/750/2500) + date (median/mean yrs; %≤25/100/500) + per-slice tables (region / era / tier / seen-vs-unseen culture / classification)
- [ ] **6.2** `src/aep/model/zero_shot_vlm.py` — run a frontier VLM (API-based) on a 2,000-image stratified eval subsample; record loc+date numbers. This is your reference line: helps decide whether Stage 1/2 is even worth the compute
- [ ] **6.3** Rerun Phase-1 baselines with the Phase-4 real geocoding (now non-deferred)
- [ ] **6.4** Fill `artifacts/eval/baseline_report.md` with the table. This is the pre-training scoreboard.

---

## Phase 7 — Stage 1: frozen embeddings + heads (2–4 days)

- [ ] **7.1** `src/aep/model/extract_embeddings.py` — DINOv2-B *or* SigLIP2-B, frozen, bf16, 448px; one pass over ~300k images; cache to `artifacts/emb_*.parquet`. Expect 30–90 min on the 4050
- [ ] **7.2** `src/aep/model/heads.py` — geocell classifier + binned-ordinal date head + aux heads (culture, classification, medium, department). Quick MLP heads
- [ ] **7.3** `src/aep/model/train_cv.py` — train the heads; tune the geocell soft-label temperature (§ spec: `m_j ∝ exp(−d_j/(1.5·radius))`) and the date-bin choice. Iterate ≤1 day
- [ ] **7.4** `src/aep/eval/evaluate.py` — run on val; compare to the Phase-6 baseline table; decide: **is Stage 2 worth starting?** If Stage 1 is barely separating, go back to §4 QA — 2% label noise at tier A is worth more than a week of training

---

## Phase 8 — Stage 2: fine-tune (only after 7.4 positive) — 1–2 weeks

- [ ] **8.1** `src/aep/model/train_ft.py` — LoRA (r=16–32, attn+mlp) on last ~4 blocks + heads; bf16; grad-checkpointing; batch 4–8 × grad-accum 8; 224 → 336 curriculum
- [ ] **8.2** Sampling: geocell-balanced `w ∝ n^(-α)`, α ∈ {0.3, 0.5, 0.7}, capped 4× oversample on rare cells with heavy augmentation (mild color jitter; **no horizontal flips** on objects whose medium/culture suggests inscriptions/asymmetric iconography — ablate this)
- [ ] **8.3** Loss: soft-CE for cells + soft-ordinal for dates + Σ aux CE; sample weights per tier (A=1, B=0.8, C=0.4, R=0.5)
- [ ] **8.4** Ablations worth 1 GPU-day each: raw vs. balanced sampling; 224 vs. 336; soft vs. hard date targets; aux heads on vs. off
- [ ] **8.5** `src/aep/eval/shortcut_audit.py` — mask background/scale bars on 1k val images, measure metric drop; flag if the drop is >1.5× median km or >1.5× median yrs

---

## Phase 9 — Polish & extras (open-ended)

- [ ] `game_score.py` calibration: play Anthropeum, log ~100 of your own games, refine the formula
- [ ] `test_game_like` set (from Phase-9 logs), final eval with it
- [ ] Ensemble: kNN over train embeddings + Stage-2 head predictions (weighted by val reliability)
- [ ] Calibration check: does "70% confident" mean 70% right?
- [ ] Optional cross-museum check: Cleveland API (`https://openaccess-api.clevelandart.org/api/artworks/?has_image=1&cc0=1`; fields `culture`, `creation_date_earliest/latest`, image `openaccess-cdn.clevelandart.org/{acc}/{acc}_web.jpg`) — freeze Met-trained model, run once, write a short note, move on
- [ ] Optional QLoRA VLM fine-tune — only if Stage 2 numbers plateau *and* you have a reason (interface, natural-language output) that the CNN doesn't already cover

---

## Right-now action list for this session

1. **0.1–0.3** — Get CUDA torch + deps installed (required for anything ahead).
2. `git init` + `.gitignore`.
3. **2.1–2.2** — Write `download_api.py` and launch it in the background; metadata fetch finishes during this session.
4. While it runs: **1.1–1.5** baseline code + numbers.
5. End of session: commit. Next session: **3.x** image fetch (overnight), then **4.x** geocoding.
