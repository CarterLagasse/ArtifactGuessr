# Plan: Predicting Where and When an Artifact Was Made from an Image

## 1. Goal and scope

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

## 2. Data

### 2.1 Primary source: Met Open Access

- CSV dump (GitHub: `metmuseum/openaccess`) plus the Collection API for images.
- Only objects flagged `isPublicDomain` have downloadable images. Filter on this first.
- Useful fields:
  - **Location signals**: `Country`, `Region`, `Subregion`, `Locale`, `Locus`, `Excavation`, `River`, `Culture`
  - **Time signals**: `objectBeginDate`, `objectEndDate` (BCE is negative), `Period`, `Dynasty`, `objectDate`
  - **Auxiliary targets**: `Department`, `Classification`, `Medium`, `Object Name`, `Tags`
- There are **no coordinates** in the data. You must geocode (see 2.3).

### 2.2 Secondary sources (for generalization tests and more data)

- Cleveland Museum of Art, Smithsonian Open Access, Rijksmuseum, Art Institute of Chicago, Europeana, Wikimedia Commons.
- Use at least one as a **held-out test museum**. This is the only clean way to measure generalization beyond Met-specific photography and cataloguing habits.
- Normalize their metadata into the same schema (country/culture string, begin/end year).

### 2.3 Building location labels

1. Build a text key per object from `Country`, `Region`, `Subregion`, `Locale`, falling back to `Culture` when geography is empty.
2. Geocode the key to lat/lon with GeoNames, Wikidata, or Nominatim. Cache everything.
3. Handle culture-only objects with a hand-built mapping from culture to centroid and a plausible radius (e.g. "Nazca" to coastal Peru, "Yoruba" to southwestern Nigeria).
4. Store a **confidence tier** and a **radius of uncertainty** with each label:
   - Tier A: specific find-spot or locale
   - Tier B: country-level
   - Tier C: culture/region-level only
5. Drop or down-weight objects whose geography is genuinely ambiguous ("Europe", "Near East", "Mediterranean").

Check a few hundred geocodes by eye. Systematic errors here (e.g. wrong "Georgia", wrong "Cyprus" region) will silently cap performance.

### 2.4 Building date labels

- Take the midpoint of `objectBeginDate` and `objectEndDate` as the point label, and keep the width as an **uncertainty** value.
- Filter out or flag objects with very wide ranges (e.g. > 500 years), or use the range directly as a soft target (see 5.2).
- Sanity checks: begin <= end, no sentinel values, BCE handled consistently, and no dates after the present.
- Plot the histogram. Expect heavy mass in recent centuries and spikes at conventional dates (e.g. exactly 1 or 1000).

### 2.5 Cleaning

- Remove non-object images (floor plans, galleries) and corrupted files.
- Deduplicate exact and near-duplicate images (perceptual hash, then embedding similarity).
- Tag **reproductions, casts, and later copies**. Their "origin" is ambiguous. Either exclude them or keep them with low weight.
- Group images by object so multi-view objects stay together (see 3).

---

## 3. Splits and leakage

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

## 4. Sampling

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

## 5. Modeling

### 5.1 Staged approach

| Stage | What | Purpose |
|---|---|---|
| 0 | Trivial baselines: predict the global median location and year; predict department-conditional medians | Sets the floor |
| 1 | Frozen embeddings (CLIP/SigLIP/DINOv2) + linear/MLP heads | Fast, shows how much signal is easy to get |
| 2 | Fine-tune a vision backbone (LoRA or last-N layers) with multi-task heads | Likely the main workhorse |
| 3 | kNN/retrieval over embeddings as an ensemble member | Strong, but only meaningful on `test_other` |
| 4 | Fine-tune an open VLM to emit country + date as text | Optional; test whether it beats stage 2 |
| 5 | Zero-shot frontier VLM as a reference line | Calibration of how hard the task is |

Don't skip stage 0 and stage 1. They tell you whether later complexity is earning anything.

### 5.2 Output representation

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

### 5.3 Auxiliary tasks

Add heads for `Culture`, `Classification`, `Medium`, and `Department`. They are cheap, strongly informative, and regularize the representation. Treat `Department` strictly as a **target**, never an input, if the goal is image-only.

### 5.4 Image handling

- Many objects have several views. Decide whether to use one random view per step (augmentation) or aggregate multiple views at inference (attention pooling or mean embedding).
- Augmentation: crops, color jitter (mild; color matters for pigment and material), no heavy blur. Avoid horizontal flips for objects with inscriptions or asymmetric iconography, or at least test the effect.
- Image resolution matters for fine texture and tool marks. Try 224, 336, and 448.
- Watch for **photographic shortcuts**: backgrounds, lighting setups, and scale bars correlate with department or collection era. Consider background-masking experiments to measure how much the model leans on them.

### 5.5 Loss weighting

- Total loss = location loss + date loss + auxiliary losses, with learned or tuned weights.
- Weight samples by label confidence tier (2.3) and date-range width (2.4).

---

## 6. Evaluation

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

## 7. Key decisions and my default choices

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

## 8. Milestones

1. **Data pipeline**: download, filter to public-domain, geocode, build labels and confidence tiers, QA a sample. *(Biggest time sink; budget accordingly.)*
2. **Splits and eval harness**: all test sets, metrics, slice reports, baseline numbers (stages 0 and 5).
3. **Embedding baseline** (stage 1) with first full evaluation report.
4. **Fine-tuned multi-task model** (stage 2) with sampling and loss ablations.
5. **Cross-museum evaluation** and error analysis (where does it fail, and is it label noise or a real model error?).
6. **Optional**: VLM fine-tune, retrieval ensemble, calibration, a demo that plays a day's game.

---

## 9. Risks

- **Label quality is the ceiling.** Spot-check, and report results by confidence tier.
- **Leakage inflating results.** Group splits, near-duplicate clustering, and a second museum are the defenses.
- **Shortcut learning** from photo style, backgrounds, and scale bars.
- **Geographic bias**: strong overall numbers that hide poor performance outside Europe/Mediterranean.
- **Overfitting rare regions** when oversampling.
- **Ambiguity is real**: some objects have no recoverable answer from an image. Don't chase accuracy that doesn't exist; measure against a human baseline instead.
- **Terms and etiquette**: respect API rate limits, and cache images locally. Check each secondary source's license before use.
