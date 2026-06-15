# Model Selection Journey — IMDB AutoFill VLM Pipeline

## Context

The IMDB AutoFill pipeline extracts structured product data (brand, manufacturer, weight, country of origin, etc.) from retail product images. Each product has 3–5 images taken from different angles; images are grouped by a dataset label (`tag_text`) stuck to the edge of the packaging before being sent to the VLM.

The target dataset is 45 products from a Ghanaian retail context — brands imported from China, Indonesia, Vietnam, Côte d'Ivoire, Nigeria, and Sri Lanka, typically sold and distributed by local importers (e.g. LGD LIMITED, FAGIP VENTURES).

Accuracy is measured field-by-field against a ground-truth Excel sheet (`eval/eval_from_org.xlsx`).

---

## Phase 1 — llama3.2-vision:11b (Local, Ollama)

**Why we started here:** Zero API cost, runs on the local RTX 5090 32GB GPU via Ollama. Fast iteration without rate limits or spend.

**Configuration:**
- Model: `llama3.2-vision:11b`
- Backend: Ollama (localhost:11434)
- Concurrency: 4 parallel workers, 1 image per call
- Image grouping: by `tag_text` similarity

**Results:** 41 matched pairs / 45 GT products — **48.1% overall accuracy**

**Why we moved on:**
- The 11B model struggled to read small edge labels (`tag_text`) reliably, causing images to split into separate products instead of grouping correctly.
- Field extraction quality was low — manufacturer, country, fragrance_flavor all scored poorly.
- The model's vision capability at 11B was simply insufficient for dense retail label text.

---

## Phase 2 — qwen2.5vl:32b (Local, Ollama)

**Why we tried this:** Qwen2.5-VL is a purpose-built vision-language model with significantly better OCR and label-reading capability than llama3.2-vision. At 32B it fits within the 32GB VRAM of the RTX 5090.

**Configuration:**
- Model: `qwen2.5vl:32b`
- Backend: Ollama (localhost:11434)
- Concurrency: reduced to 2 (32B model is slower per call)
- Image grouping: by `tag_text` similarity (unchanged)

**Results:** 55 matched pairs / 45 GT products — **51.9% overall accuracy**

Note: 55 matched pairs > 45 GT because the model was over-splitting some products (empty `tag_text` → each image became its own product). Matched pairs counts how many predictions align to a GT row, not unique products.

**What improved:**
- Brand and product name accuracy up significantly
- `tag_text` extraction more reliable → better image grouping

**Why we moved on:**
- Despite better OCR, the model was still leaving `tag_text` empty on ~30% of images, causing over-splitting (82+ predictions for 45 actual products).
- Inference speed was slow (32B model, 2 concurrent workers).
- The accuracy ceiling for local 32B models seemed to be around 52–55% given the visual complexity of the labels.

---

## Phase 3 — Gemini 2.5 Flash & 3.5 Flash (Cloud, Google AI)

**Why we tried this:** Gemini offered a cost-effective path to a stronger frontier model. Integrated via Google's OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) — no new SDK required.

**Configuration:**
- Backend: `gemini` (via `VLM_BACKEND=gemini`)
- Models tested: `gemini-2.5-flash`, `gemini-3.5-flash`
- Batch size: 8 images per API call

**Results:**

| Model | Matched pairs | Overall |
|-------|--------------|---------|
| Gemini 2.5 Flash | 31 | 47.8% |
| Gemini 3.5 Flash | 32 | 41.6% |

**Why we moved on:**
- Gemini models consistently failed to read edge labels (`tag_text`) even with explicit prompting, causing severe over-splitting (116–122 predictions for 45 products).
- With only 31–32 matched pairs vs. 45 GT products, many products simply couldn't be evaluated.
- The larger Gemini 3.5 model actually scored *lower* than 2.5 — likely a different training emphasis.
- Overall accuracy was worse than the local qwen model.

---

## Phase 4 — GPT-5.5 (OpenAI, Cloud)

**Why we chose this:** After Gemini's failure on `tag_text` reading, GPT-5.5 was evaluated as the strongest available vision model. It demonstrated noticeably better spatial awareness and OCR of small printed text, including the edge dataset labels.

**Configuration:**
- Model: `gpt-5.5-2026-04-23` via OpenAI API
- Backend: `openai` (via `VLM_BACKEND=openai`)
- Batch size: 8 images per API call (all session images in one request)
- API params: `max_completion_tokens=4096`, no `temperature` (GPT-5.5 only supports default)
- Response format: `{"type": "json_object"}`

**Key integration notes:**
- GPT-5.5 does **not** support `max_tokens` — must use `max_completion_tokens`
- GPT-5.5 does **not** support custom `temperature` — only the default (1) is supported
- Images are sent as base64 data URLs in the message content array

### v1 — Initial GPT-5.5 run

**Results:** 45 matched pairs / 45 GT products — **59.5% overall accuracy**

All 45 GT products matched — `tag_text` reading was reliable enough to group images correctly. This was the first run where we achieved a 1:1 mapping between predictions and GT.

### v2 — Prompt improvements

Three targeted changes based on failure analysis of v1 predictions:

1. **Manufacturer prompt** — Updated to emphasise local African importer/distributor over foreign factory. Added examples of the expected company name format (LGD LIMITED, FAGIP VENTURES, SISTER SARDINE & MACKEREL VENTURES). Directed model to look for "Manufactured by", "Manufactured for", "Imported by", "Distributed by", "Packed by" phrasing.

2. **Country normalisation** — Added `P.R.C.`, `PRC`, `P.R.C`, `PEOPLES REPUBLIC OF CHINA`, `PEOPLE'S REPUBLIC OF CHINA` → `CHINA` mappings to `backend/normalizer.py`. Many products from Chinese factories use the PRC abbreviation on pack. Country accuracy: 38.5% → 50%.

3. **Fragrance/flavor broadening** — Expanded the field definition to cover food flavors (BEEF, STEW RAGOUT, SOUP, NOODLES, CHICKEN), beverage flavors (COLA, LEMON, MALT, MULTI FRUIT), cosmetic fragrances (ROSE, CITRUS, JAPANESE CAMELLIA), and detergent scents (POWDER, FRESH). The GT definition is broader than traditional "fragrance" — any taste/scent descriptor on pack counts.

**Results:** 41 matched pairs / 45 GT products — **63.4% overall accuracy**

Note: matched pairs dropped from 45 → 41 in v2 (minor grouping regression to investigate). Overall accuracy still improved because field-level extraction quality went up.

### v2 Field breakdown

| Field | Accuracy | Notes |
|-------|----------|-------|
| category_type | 90.0% | Strong |
| brand | 85.4% | Strong |
| packaging_type | 82.9% | Strong |
| product_name | 78.0% | Good |
| weight | 63.4% | Good |
| barcode | 60.0% | Good |
| country_of_origin | 50.0% | PRC→CHINA fix helped |
| fragrance_flavor | 42.3% | Broadened definition helped |
| manufacturer | 39.0% | Still reading foreign factory |
| tagline | 0.0% | Regressed — definition too strict |
| promotional_messages | 0.0% | Not yet extracted |
| addons | 0.0% | Not yet extracted |

---

## Architecture

The backend is designed to be model-agnostic via `VLM_BACKEND` env var:

```
VLM_BACKEND=openai   → GPT-5.5 (current default)
VLM_BACKEND=ollama   → local model via Ollama (qwen2.5vl:32b)
VLM_BACKEND=gemini   → Gemini via OpenAI-compat endpoint
```

Batch size is set to 1 for Ollama (memory constraints) and up to 8 for cloud backends.

---

## Why GPT-5.5 Wins

1. **tag_text reliability** — reads small edge labels that other models miss, enabling correct image grouping
2. **Structured JSON compliance** — consistently returns valid JSON arrays without retry failures
3. **OCR quality** — correctly reads dense small-print text (manufacturer, country, barcode) on diverse packaging
4. **Spatial awareness** — correctly identifies which text is on the front vs. back vs. edge of a product

The main cost consideration is GPT-5.5 pricing ($5/$30 per 1M input/output tokens). For a 45-product eval at batch_size=8, cost is modest, but production scale needs monitoring.

**Why we moved on:**
- 6 sessions (8-image batches) consistently returned empty content across all 3 retries — likely OpenAI's content filter silently refusing certain product images
- Each failed session took 1300s before giving up (3× retry × ~430s/attempt)
- 34/45 products matched due to the 6 total failures

---

## Phase 5 — Claude Sonnet 4.6 (Anthropic, Cloud) ← Current

**Why we chose this:** After diagnosing the GPT-5.5 empty content failures, Claude Sonnet 4.6 was tested on the 6 failed sessions. All 6 completed successfully in 15–75s each, with no empty responses. The decision was made to switch the full pipeline to Claude.

**Configuration:**
- Model: `claude-sonnet-4-6` via Anthropic SDK
- Backend: `anthropic` (via `VLM_BACKEND=anthropic`)
- Batch size: 8 images per API call (unchanged)
- API params: `max_tokens=4096`, system prompt as top-level `system` parameter
- Images: base64-encoded JPEG blocks in user content array

**Key integration notes:**
- Anthropic's native SDK uses a different image format than OpenAI (`source.type: "base64"` blocks)
- System prompt is passed as the `system` parameter, not as a `role: "system"` message
- No `response_format` equivalent — prompt instructs JSON output directly
- Message Batches API (50% off): `eval/run_eval_batch.py` submits all 42 VLM requests as one batch

**Eval progression (Claude Sonnet 4.6):**

| Version | Key changes | Matched | Overall |
|---------|-------------|---------|---------|
| v1 (baseline) | Switch from GPT-5.5 | 41 | 76.3% |
| v2 | Country: company address > "Made in" + Example 6 | 42 | 76.7% |
| v3 | Tagline: non-strict + shortest descriptor; brand accent norm | 42 | 77.3% |
| v4 | fragrance_flavor: non-strict + detergent POWDER; manufacturer normalizer | 42 | 79.0% |
| v5 | Weight normalizer (GMS→G, G→KG, ML→L); addons: non-strict | 42 | 79.6% |
| **v6** | Brand: trade name vs manufacturer distinction | **44** | **79.9%** |

### v6 Field Breakdown (final, 44/45 matched)

| Field | Accuracy |
|-------|----------|
| variant | 100.0% |
| brand | 97.7% |
| category_type | 90.9% |
| country_of_origin | 88.9% |
| packaging_type | 88.6% |
| product_name | 86.4% |
| weight | 81.8% |
| addons | 75.0% |
| tagline | 72.7% |
| fragrance_flavor | 73.1% |
| manufacturer | 63.6% |
| barcode | 58.1% |
| promotional_messages | 33.3% |
| **OVERALL** | **79.9%** |

### Why Claude Sonnet 4.6 Wins

1. **Zero empty-content failures** — processed all 41 sessions (including 8–10 image batches) with no silent refusals
2. **Speed** — 15–75s per session vs 300–1300s for GPT-5.5
3. **Instruction following** — responds well to field priority rules (tagline shortest-first, country company address, brand trade name)
4. **Cost** — Anthropic Batch API at 50% discount makes full evals ~2× cheaper than GPT-5.5 standard

**Remaining hard limits (not model-dependent):**
- Manufacturer (63.6%): GT is inconsistent — sometimes wants the manufacturer, sometimes the local distributor
- Barcode (58.1%): 16 sessions have no barcode visible in any image
- Promotional messages (33.3%): only 3 GT products have promos, high variance
