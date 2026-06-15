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

## Live Demo

**[https://samueltheophilus-imdb-autofill.hf.space](https://samueltheophilus-imdb-autofill.hf.space)**

> Open the link directly — the embedded HF Spaces preview uses a sandboxed iframe that blocks navigation.

## How It Works

```
Product images (multiple angles)
        │
        ▼
Image grouping  ←── edge label (tag_text) similarity
        │
        ▼
VLM extraction  ←── Claude Sonnet 4.6 (batch of up to 8 images per call)
        │
        ▼
Field aggregation  ←── majority vote across image faces
        │
        ▼
Barcode decoding  ←── pyzbar → zxing-cpp → CLAHE+adaptive threshold (3-pass)
        │
        ▼
Normalisation  ←── country corrections, weight conversion, fuzzy brand matching
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
| COUNTRY          | Country of origin                             |
| VARIANT          | e.g. `ORIGINAL`, `LOW FAT`                   |
| TYPE             | Product category e.g. `MARGARINE`, `SOAP`    |
| FRAGRANCE_FLAVOR | Flavour or scent where applicable             |
| PROMOTION        | On-pack promotion text                        |
| ADDONS           | Extra pack contents                           |
| TAGLINE          | Short slogan or descriptive tagline           |

---

## Quick Start (from git clone)

### Prerequisites

- Python 3.13+
- [`uv`](https://github.com/astral-sh/uv) — fast Python package manager
- `zbar` system library (for barcode decoding)

```bash
# macOS
brew install zbar

# Ubuntu / Debian
sudo apt-get install libzbar0
```

### 1. Clone and install

```bash
git clone https://github.com/SamuelTheophilus/imdb-autofill.git
cd imdb-autofill
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```env
VLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...     # get from console.anthropic.com
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google App Password
STORAGE_SECRET=any-random-string
```

> **Gmail App Password:** Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), create an app password for "Mail", and paste the 16-character code.

### 3. Run

```bash
uv run python -m frontend.app
```

Open [http://localhost:5200](http://localhost:5200).

---

## Environment Variables

### Hugging Face Spaces secrets

Set these under **Settings → Variables and secrets** on your HF Space:

| Variable             | Required | Description                                              |
|----------------------|----------|----------------------------------------------------------|
| `VLM_BACKEND`        | Yes      | `anthropic` (recommended) or `openai`                    |
| `ANTHROPIC_API_KEY`  | Yes*     | Anthropic API key — required if `VLM_BACKEND=anthropic`  |
| `ANTHROPIC_MODEL`    | No       | Default: `claude-sonnet-4-6`                             |
| `OPENAI_API_KEY`     | Yes*     | OpenAI API key — required if `VLM_BACKEND=openai`        |
| `OPENAI_VL_MODEL`    | No       | Default: `gpt-5.5-2026-04-23`                            |
| `GMAIL_USER`         | Yes      | Gmail address for password reset emails                  |
| `GMAIL_APP_PASSWORD` | Yes      | Gmail App Password (not your account password)           |
| `STORAGE_SECRET`     | Yes      | Any random string for NiceGUI session encryption         |
| `VLM_BATCH_SIZE`     | No       | Images per API call (default `8`)                        |
| `COMING_SOON_MODE`   | No       | Set to `YES` to show a public info page and redirect all routes — useful to block access while paying for API costs (default `NO`) |

*Only one of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is required, depending on `VLM_BACKEND`.

---

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

---

## Model Notes

The pipeline uses **Claude Sonnet 4.6** (`claude-sonnet-4-6`) via the Anthropic API. Up to 8 images are sent in a single batched call, which groups multi-angle shots of the same product more reliably than per-image calls.

### Eval results (45-product dataset)

| Model | Matched pairs | Overall accuracy |
|-------|--------------|-----------------|
| llama3.2-vision 11b (local) | 41 | 48.1% |
| qwen2.5vl 32b (local) | 55 | 51.9% |
| Gemini 2.5 Flash | 31 | 47.8% |
| GPT-5.5 | 34 | 73.2% |
| **Claude Sonnet 4.6 (current)** | **44** | **79.9%** |

See `docs/model_selection.md` for the full model selection writeup and `docs/extraction_improvements.md` for all accuracy improvements.

---

## Docker (self-hosted)

```bash
docker build -t imdb-autofill .
docker run -p 7860:7860 \
  -e VLM_BACKEND=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GMAIL_USER=you@gmail.com \
  -e GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
  -e STORAGE_SECRET=some-random-string \
  imdb-autofill
```

Open [http://localhost:7860](http://localhost:7860).

---

## Project Structure

```
backend/
  pipeline.py         end-to-end orchestration
  extractor.py        VLM calls, batching, field aggregation, backend routing
  barcode.py          3-pass barcode extraction (pyzbar, zxing-cpp, CLAHE+adaptive)
  normalizer.py       country corrections, weight conversion, barcode cleaning, fuzzy matching
  db.py               SQLite persistence and edit version history
  schema.py           Pydantic models with per-field confidence scores
  utils.py            VLM call functions (Anthropic, OpenAI, Gemini, Ollama backends)
core/prompts/
  vlm_system_prompt.j2        field definitions, output contract, and few-shot examples
  vlm_extraction_prompt.j2    per-image extraction instructions
frontend/
  app.py              NiceGUI entry point and page registration
  components.py       grid, review drawer, carousel, upload zone
  auth_pages.py       login, signup, password reset pages
  handlers.py         upload, export, edit, and delete handlers
  state.py            shared state, row mapping, export formatting
eval/
  run_eval.py         parallel eval runner with --sessions flag
  run_eval_batch.py   Anthropic Batch API eval (50% cost vs standard API)
  metrics.py          field-level scoring against ground truth
docs/
  model_selection.md        VLM model comparison and selection rationale
  extraction_improvements.md all accuracy improvements with diagnosis and impact
```
