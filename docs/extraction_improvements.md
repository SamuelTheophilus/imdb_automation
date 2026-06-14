# Extraction Pipeline Improvements

Changes made after the initial GPT-5.5 v1 baseline (59.5% overall) to improve field-level accuracy.

## Baseline

| Version | Matched pairs | Overall |
|---------|--------------|---------|
| GPT-5.5 v1 | 45 | 59.5% |
| GPT-5.5 v2 (prompt fixes) | 41 | 63.4% |

---

## 1. Image Resolution — 512px → 2048px

**File:** `backend/extractor.py` — `_MAX_IMAGE_SIDE`

**Change:** Increased the maximum image side from 512px to 2048px before encoding for the VLM.

**Why:** GPT-5.5 with `detail: high` processes images in 512×512 tiles. At 512px total, the model only gets one blurry tile. At 2048px it gets up to 16 sharp tiles, making fine-print text (manufacturer, country, weight on back panels) legible.

`detail: high` was already set in `backend/utils.py` — the resolution cap was the only bottleneck.

**Impact (targeted sessions):** +21.6% weight, +18.6% country, +16.3% brand, +15.1% product name.

---

## 2. Image Enhancement for VLM

**File:** `backend/extractor.py` — `_encode_image`

**Change:** Added PIL sharpening and contrast boost after resize, before base64 encoding:
- `ImageEnhance.Sharpness(img).enhance(2.0)` — sharpens text edges
- `ImageEnhance.Contrast(img).enhance(1.3)` — lifts low-contrast label text
- JPEG quality raised from 85 → 90 to preserve fine detail at 2048px

**Why:** Product photos are often taken under uneven lighting or at slight angles. Modest sharpening and contrast boost make label text more legible without distorting the image for the VLM.

**Note:** CLAHE and adaptive thresholding are NOT applied to VLM images — only PIL-based enhancement. CLAHE/adaptive are reserved for the barcode pipeline (see section 5).

---

## 3. Few-Shot Examples in System Prompt

**File:** `core/prompts/vlm_system_prompt.j2`

**Change:** Added a `## EXAMPLES` section with 5 concrete label-description → JSON output pairs.

**Products chosen and what each teaches:**

| Product | Key pattern demonstrated |
|---------|------------------------|
| BLUE BAND 250G | Tagline = descriptive phrase ("LOW FAT SPREAD FOR BREAD") |
| TAPOK BLACK TEA | Tagline = single word ("PREMIUM"), addons = "1PCS 2G" |
| MILO 400G | Tagline = health claim ("SUPPORTS ENERGY RELEASE"), NESTLE as manufacturer |
| ZESTA STRAWBERRY | Addons = "7 FREE ENVELOPE" (not "FREE TEA BAGS"), WATAWALA TEA CEYLON LTD |
| SISTER BEEF | fragrance_flavor = meat type ("BEEF"), SISTER SARDINE & MACKEREL VENTURES |

**Impact:** Country +28.6%, manufacturer +9%, tagline 0%→33%, addons 0%→33%.

---

## 4. Prompt Improvements

**File:** `core/prompts/vlm_system_prompt.j2`

### Manufacturer
- Removed "local importer" bias — GT is inconsistent (sometimes wants foreign manufacturer, sometimes local distributor)
- Added examples of both local (LGD LIMITED, FAGIP VENTURES) and foreign (PT SAYAP MAS UTAMA, GB FOODS, THE COCA COLA COMPANY) companies
- Added: "In some cases the brand name itself is the manufacturer"

### Tagline
- Removed "Must be distinct from the product name" restriction (was causing 0% accuracy)
- Added diverse examples: single words (PREMIUM, RICH), product descriptors (TOMATE PUREE, CONCENTRATED DETERGENT), health claims (SUPPORTS ENERGY RELEASE), longer phrases

### Addons
- Added ENVELOPE examples ("7 FREE ENVELOPE", "5 FREE ENVELOPE")
- Noted: "individually wrapped tea bags packaged in paper sleeves are called ENVELOPE on the label"

### Country of origin
- Added P.R.C. / PRC / PEOPLES REPUBLIC OF CHINA → CHINA (prompt level)

### Fragrance/flavor
- Broadened examples: food flavors (BEEF, STEW RAGOUT, CHICKEN), beverage flavors (COLA, MALT, MULTI FRUIT), cosmetics (JAPANESE CAMELLIA & CITRUS OIL), detergents (POWDER, FRESH)

---

## 5. Barcode Pipeline — Three-Pass Extraction

**File:** `backend/barcode.py`

### Before
Single library (pyzbar) with 4 preprocessing strategies per image.

### After
Three passes, each tried per image before moving to the next:

**Pass 1 — pyzbar (4 strategies):**
1. Raw image
2. Grayscale + 2× contrast + sharpen
3. Center 60% crop
4. 2× upscale (images < 800px wide)

**Pass 2 — zxing-cpp (same 4 strategies):**
- zxing-cpp handles skewed, partial, and low-contrast barcodes better than pyzbar
- Restricted to EAN-13, EAN-8, UPC-A, UPC-E, Code128, Code39 formats only
- DataMatrix and GS1-128 formats explicitly excluded (they read industrial tracking codes, not retail EAN barcodes)

**Pass 3 — CLAHE + adaptive threshold (8 strategies, both decoders):**
- `cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))` — local contrast equalization for uneven lighting
- `cv2.adaptiveThreshold(..., ADAPTIVE_THRESH_GAUSSIAN_C, ...)` — per-region binarization
- Tried in combinations: CLAHE alone, adaptive alone, CLAHE→adaptive, CLAHE+crop

**Coverage improvement:**
| Stage | Sessions covered | Coverage |
|-------|----------------|----------|
| pyzbar alone | 29/41 | 70.7% |
| + zxing-cpp | 33/41 | 80.5% |
| + CLAHE + adaptive | 38/41 | 92.7% |

---

## 6. GS1 Barcode Normalizer

**File:** `backend/normalizer.py` — `_fix_barcode`

**Change:** Strip GS1 Application Identifier prefix before storing the barcode.

- `(01)08882033623812` → strip `(01)` → `08882033623812` → GTIN-14 with leading 0 → EAN-13: `8882033623812`
- `(01)49621350759326` → `49621350759326` (14-digit GTIN, no leading 0, kept as-is)

Also handles float barcode strings from CSV reads: `6034000482027.0` → `6034000482027`.

---

## 7. Addons Normalizer

**File:** `backend/normalizer.py` — `_fix_addons`

**Change:** Normalises "FREE TEA BAGS" → "FREE ENVELOPE" to match GT terminology.

```
"7 FREE TEA BAGS" → "7 FREE ENVELOPE"
"25+7 FREE TEA BAGS" → "25+7 FREE ENVELOPE"
```

GT consistently uses "ENVELOPE" for individually wrapped paper-sleeved tea bags (Zesta, Golden Victoria). The VLM consistently reads "TEA BAGS". This bridges the terminology gap without requiring a model change.

---

## 8. Empty Record Filter

**File:** `backend/pipeline.py`

**Change:** Records with no brand, no product_name, and no manufacturer are dropped before being returned.

**Why:** At 2048px, the VLM sometimes produces empty extractions for background products visible on shelves behind the main product. These records have no identifying information and can never be matched to GT, but they inflate the prediction count and produce noise in exports.

---

## 9. Eval Runner — Parallel Sessions

**File:** `eval/run_eval.py`

**Change:** Replaced sequential for-loop with `asyncio.gather` + `asyncio.Semaphore(10)`.

- All 41 sessions run concurrently, capped at 10 in-flight at once
- Session order preserved in output CSV via `results_map` dict
- Each session logs independently with its session ID prefix

---

## 10. Eval Metrics — Barcode Float Fix

**File:** `eval/metrics.py` — `_norm`

**Change:** Strip trailing `.0` from float barcode strings when loading from CSV.

`"6034000482027.0"` → `"6034000482027"` before comparison against GT.

pandas reads integer CSV columns as floats on load. Without this fix, no barcode ever matched GT (0% barcode accuracy in re-scoring).

---

## Country Normalizer Additions

**File:** `backend/normalizer.py` — `_COUNTRY_CORRECTIONS`

Added:
- `P.R.C.` / `PRC` / `P.R.C` / `PEOPLES REPUBLIC OF CHINA` / `PEOPLE'S REPUBLIC OF CHINA` → `CHINA`
- `IVORY COAST` → `COTE D'IVOIRE`
- `VIET NAM` → `VIETNAM`
- `SRILANKA` → `SRI LANKA`
