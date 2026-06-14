---
title: IMDB AutoFill
emoji: 🔎
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# IMDB AutoFill

AI-driven image-to-item-master-data tool for retail product cataloging.

Built for the GDSS-Maverick Hackathon: auto-fill an Item Master Database (IMDB) row from product images. Upload multiple photos of a product, let the AI extract brand, weight, manufacturer, country, and 10+ other fields, review and edit the result, then export as CSV or Excel.

## How It Works

```
Product images (multiple angles)
        │
        ▼
Image grouping  ←── edge label (tag_text) similarity
        │
        ▼
Barcode decoding  ←── pyzbar with preprocessing fallbacks
        │
        ▼
VLM extraction  ←── GPT-5.5 vision model (batch of up to 8 images per call)
        │
        ▼
Field aggregation  ←── majority vote across image faces
        │
        ▼
Normalisation  ←── country corrections, barcode cleaning, fuzzy brand matching
        │
        ▼
Duplicate detection  ←── barcode + fuzzy brand/name matching
        │
        ▼
Editable UI review → CSV / Excel export
```

## Extracted Fields

| Export Column    | Description                                   |
|------------------|-----------------------------------------------|
| ITEM_NAME        | Full product name as printed                  |
| BARCODE          | Numeric barcode (pyzbar decoded)              |
| MANUFACTURER     | Legal company name                            |
| BRAND            | Brand name on pack                            |
| WEIGHT           | Combined weight + unit e.g. `100G`, `1.5 KG` |
| PACKAGING TYPE   | e.g. `BOX`, `SACHET`, `TUB`, `GLASS JAR`     |
| COUNTRY          | Country of manufacture                        |
| VARIANT          | e.g. `ORIGINAL`, `LOW FAT`                   |
| TYPE             | Product category e.g. `MARGARINE`, `SOAP`    |
| FRAGRANCE_FLAVOR | Flavour or scent where applicable             |
| PROMOTION        | On-pack promotion text                        |
| ADDONS           | Extra pack contents                           |
| TAGLINE          | Short slogan or descriptive tagline           |

## Environment Variables (Secrets)

Set these as Hugging Face Spaces secrets (Settings → Variables and secrets):

| Variable             | Required | Description                                             |
|----------------------|----------|---------------------------------------------------------|
| `OPENAI_API_KEY`     | Yes      | OpenAI API key for GPT-5.5 vision extraction           |
| `OPENAI_VL_MODEL`    | Yes      | Model name — `gpt-5.5-2026-04-23`                      |
| `VLM_BACKEND`        | Yes      | Set to `openai`                                         |
| `GMAIL_USER`         | Yes      | Gmail address for password reset emails                 |
| `GMAIL_APP_PASSWORD` | Yes      | Gmail App Password (not your real password)             |
| `STORAGE_SECRET`     | Yes      | Any random string for NiceGUI session encryption        |
| `VLM_BATCH_SIZE`     | No       | Images per API call (default `8`)                       |

## Running Locally

**Requirements:** Python 3.13+, `uv`, `libzbar0`

```bash
# macOS
brew install zbar

# Linux
sudo apt-get install libzbar0
```

```bash
uv sync
cp .env.example .env   # fill in your API keys
uv run python -m frontend.app
```

Open `http://localhost:5200`.

## Rebuilding with Docker

```bash
docker build -t imdb-autofill .
docker run -p 7860:7860 \
  -e OPENAI_API_KEY=sk-... \
  -e OPENAI_VL_MODEL=gpt-5.5-2026-04-23 \
  -e VLM_BACKEND=openai \
  -e GMAIL_USER=you@gmail.com \
  -e GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
  -e STORAGE_SECRET=some-random-string \
  imdb-autofill
```

Open `http://localhost:7860`.

## Usage

1. Sign up or log in.
2. Upload one or more product images — multiple angles of the same product work best. The pipeline groups them automatically by the dataset label on the edge of the packaging.
3. Wait for extraction — the grid updates when done.
4. Review row confidence:
   - **Green** — high confidence, likely correct
   - **Yellow** — one or more fields need review
   - **Red** — possible duplicate
5. Click **Review** to open the side drawer: view the image carousel and edit any field.
6. Export CSV or Excel from the header.

## Model Notes

The pipeline uses **GPT-5.5** (`gpt-5.5-2026-04-23`) via the OpenAI API. Up to 8 images are sent in a single batched call, which groups multi-angle shots of the same product more reliably than per-image calls.

### Eval results (45-product dataset)

| Model | Matched pairs | Overall accuracy |
|-------|--------------|-----------------|
| llama3.2-vision 11b (local) | 41 | 48.1% |
| qwen2.5vl 32b (local) | 55 | 51.9% |
| Gemini 2.5 Flash | 31 | 47.8% |
| **GPT-5.5 (current)** | **44** | **~65%** |

See `docs/model_selection.md` for the full model selection writeup.

## Project Structure

```
backend/
  pipeline.py         end-to-end orchestration
  extractor.py        VLM calls, batching, field aggregation, backend routing
  barcode.py          pyzbar decoding with preprocessing fallbacks
  normalizer.py       country corrections, barcode cleaning, fuzzy matching
  db.py               SQLite persistence and edit version history
  schema.py           Pydantic models with per-field confidence scores
  utils.py            VLM call functions (OpenAI, Ollama, Gemini backends)
core/prompts/
  vlm_system_prompt.j2        field definitions and output contract
  vlm_extraction_prompt.j2    per-image extraction instructions
frontend/
  app.py              NiceGUI entry point and page registration
  components.py       grid, review drawer, carousel, upload zone
  auth_pages.py       login, signup, password reset pages
  handlers.py         upload, export, edit, and delete handlers
  state.py            shared state, row mapping, export formatting
eval/
  run_eval.py         full dataset pipeline run + accuracy report
  metrics.py          field-level scoring against ground truth
docs/
  model_selection.md  VLM model comparison and selection rationale
```
