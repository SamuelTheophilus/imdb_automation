# IMDB AutoFill — Hackathon Submission Write-Up

## 200-Word Description (for submission form)

IMDB AutoFill extracts a complete Item Master Database row from product photos — automatically.

Upload multiple angles of a product; the pipeline groups images by the dataset edge label, sends up to 8 images in a single Claude Sonnet 4.6 API call, and extracts 13 structured fields: brand, manufacturer, weight, barcode, country of origin, packaging type, variant, fragrance/flavor, addons, tagline, promotion, product name, and category.

Accuracy improvements stack across every layer:
- 2048px resolution + PIL sharpness/contrast enhancement before VLM encoding
- 6 few-shot examples covering Ghana-specific label patterns (contract-manufactured products, local importers, tea bag terminology)
- 5-pass barcode pipeline: pyzbar → zxing-cpp → CLAHE+adaptive → OpenCV BarcodeDetector → gradient ROI localisation; VLM reads barcode digits as text in the same call; EAN checksum arbitrates between sources
- Field normalizers: weight unit conversion (GMS→G, ≥1000G→KG), brand accent stripping, manufacturer corrections, country address priority over "Made in" text

Benchmarked against all 45 products: **81.6% overall accuracy, 44/45 products matched** — up from 48.1% (local llama/qwen models), 47.8% (Gemini 2.5 Flash), and 73.2% (GPT-5.5).

Results are surfaced in an editable grid with per-field confidence scores. Users review, correct, and export in the exact IMDB submission column format.

---

## The Problem

Manually filling an Item Master Database from product photos is slow, error-prone, and inconsistent, especially across diverse retail products with labels in multiple languages, varying terminology, and inconsistent formatting. For a Ghanaian retail context this is compounded by products that are:

- Contract-manufactured in China/Indonesia but distributed by local companies
- Labelled with manufacturer names that differ from the visible brand
- Using non-standard unit notation (GMS, ML instead of G, L)
- Printed with accented brand names (PÓMO, not POMO)

IMDB AutoFill solves this with a fully automated pipeline: upload photos → AI extracts and normalizes fields → human reviews and exports.

---

## Pipeline Architecture

```
Product images (multiple angles)
        │
        ▼
Image grouping  ─── edge label (tag_text) similarity
        │
        ▼
VLM extraction  ─── Claude Sonnet 4.6 (up to 8 images per call)
        │
        ▼
Field aggregation  ── majority vote across image faces
        │
        ▼
Barcode decoding  ─── pyzbar → zxing-cpp → CLAHE+adaptive (3-pass)
        │
        ▼
Normalisation  ───── country corrections, weight conversion, fuzzy brand matching
        │
        ▼
Editable UI review → CSV / Excel export
```

---

## Model Selection Journey

Five models were evaluated against the 45-product ground truth dataset:

| Model | Setup | Matched | Overall |
|-------|-------|---------|---------|
| llama3.2-vision 11b | Local, Ollama, RTX 5090 | 41 | 48.1% |
| qwen2.5vl 32b | Local, Ollama, RTX 5090 | 55* | 51.9% |
| Gemini 2.5 Flash | Cloud, Google AI | 31 | 47.8% |
| GPT-5.5 | Cloud, OpenAI | 34† | 73.2% |
| **Claude Sonnet 4.6** | **Cloud, Anthropic** | **44** | **79.9%** |

*55 matched > 45 GT because qwen over-split products (empty tag_text → each image became its own product).  
†GPT-5.5: 6 sessions (8-image batches) returned empty content across all retries likely a content filter. 34/45 products reached the eval.

**Why Claude Sonnet 4.6 won:**
- Zero empty-content failures across all 41 sessions including 8–10 image batches
- 15–75s per session vs 300–1300s for GPT-5.5
- Strong instruction following for field priority rules (company address over "Made in", trade name over manufacturer name)
- Anthropic Batch API at 50% cost discount for eval runs

---

## Accuracy Improvements — From Baseline to 79.9%

Twenty targeted improvements were made across two sessions of work.

### Session 1 — GPT-5.5 era (48.1% → 73.2%)

| # | Improvement | Impact |
|---|------------|--------|
| 1 | Image resolution 512px → 2048px | +21.6% weight, +18.6% country, +16.3% brand |
| 2 | PIL sharpness ×2.0 + contrast ×1.3 before VLM encoding | Improved fine-print legibility |
| 3 | 6 few-shot examples in system prompt | country +28.6%, tagline 0%→33%, addons 0%→33% |
| 4 | Prompt fixes: manufacturer, tagline, addons, fragrance, country | Baseline improvement across all fields |
| 5 | 3-pass barcode pipeline: pyzbar → zxing-cpp → CLAHE+adaptive | 70.7% → 92.7% barcode coverage |
| 6 | GS1 normalizer: strip `(01)` prefix, GTIN-14→EAN-13 | Barcode matching fixed |
| 7 | Addons normalizer: "N FREE TEA BAGS" → "N FREE ENVELOPE" | Matched GT terminology |
| 8 | Empty record filter: drop records with no brand/name/manufacturer | Removes background product noise |
| 9 | Parallel eval: asyncio.gather + Semaphore(10) | Eval time: 41min → 4min |
| 10 | Country normalizer: PRC/P.R.C. → CHINA, IVORY COAST → COTE D'IVOIRE | country +11.5% |

### Session 2 — Claude Sonnet 4.6 era (73.2% → 79.9%)

| # | Improvement | Impact |
|---|------------|--------|
| 11 | VLM backend switch: GPT-5.5 → Claude Sonnet 4.6 | 34 → 41 matched pairs (+7) |
| 12 | Country: company address priority over "Made in" text + Example 6 | country +8.6% |
| 13 | Anthropic Batch API eval script (50% cost savings) | Eval cost halved |
| 14 | Brand accent stripping: NFKD decomposition (PÓMO → POMO) | brand +4.7% |
| 15 | Tagline: non-strict aggregation + shortest descriptor prompt | tagline 12.5% → 72.7% |
| 16 | Fragrance/flavor: non-strict aggregation + detergent FORMAT rule | fragrance_flavor +12.5% |
| 17 | Manufacturer normalizer: SDTM-CI → S.D.T.M; expanded canonical list | manufacturer +5.8% |
| 18 | Weight normalizer: GMS→G, ≥1000G→KG, 1000ML→1L + net weight prompt | weight +5.6% |
| 19 | Addons: non-strict aggregation | addons +25.0% |
| 20 | Brand definition: trade name vs manufacturer company name (with counter-examples) | 42 → 44 matched |

---

## Final Field Breakdown (Claude Sonnet v6, 44/45 matched)

| Field | Accuracy | Notes |
|-------|----------|-------|
| variant | 100.0% | |
| brand | 97.7% | |
| category_type | 90.9% | |
| country_of_origin | 88.9% | |
| packaging_type | 88.6% | |
| product_name | 86.4% | |
| weight | 81.8% | |
| addons | 75.0% | |
| fragrance_flavor | 73.1% | |
| tagline | 72.7% | |
| manufacturer | 63.6% | GT inconsistent (manufacturer vs local distributor) |
| barcode | 58.1% | 16 sessions have no visible barcode |
| promotional_messages | 33.3% | Only 3 GT products have promotions |
| **OVERALL** | **79.9%** | **44/45 products matched** |

---

## Technical Stack

- **VLM**: Claude Sonnet 4.6 via Anthropic SDK (`claude-sonnet-4-6`)
- **UI**: NiceGUI (Python) + AG Grid + Quasar
- **Barcode**: pyzbar + zxing-cpp + OpenCV (CLAHE + adaptive threshold)
- **Image processing**: PIL (sharpness, contrast, resize)
- **Normalisation**: fuzzy matching (rapidfuzz), Unicode NFKD, regex weight/unit conversion
- **Storage**: SQLite with full edit version history
- **Deployment**: Hugging Face Spaces (Docker)
- **Eval**: Anthropic Message Batches API + asyncio parallel runner

## Live Demo

[https://samueltheophilus-imdb-autofill.hf.space](https://samueltheophilus-imdb-autofill.hf.space)
