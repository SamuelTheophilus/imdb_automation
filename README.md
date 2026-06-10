# IMDB AutoFill

AI-driven image-to-item-master-data tool for retail product cataloging.

Built for the GDSS-Maverick Hackathon challenge: auto-fill an Item Master Database (IMDB) row from product images. The app accepts multiple product label images, groups them by product, extracts structured attributes using a local vision-language model, lets a user review and edit the result, and exports the final records as CSV or Excel.

## Problem

Retail teams fill item master records manually by reading product labels and typing values into spreadsheets. That process is slow and error-prone, especially at scale. IMDB AutoFill reduces that work by turning a set of product images into a structured product-master row.

## Pipeline Overview

```
Product images (multiple angles)
        |
        v
Image grouping  ←── tag similarity across image edges
        |
        v
Barcode decoding  ←── pyzbar with preprocessing fallbacks
        |
        v
VLM extraction  ←── llama3.2-vision:11b via Ollama structured outputs
        |
        v
Field aggregation  ←── majority vote across image faces
        |
        v
Normalization  ←── country corrections, barcode cleaning
        |
        v
Duplicate detection  ←── barcode + fuzzy brand/name matching
        |
        v
Editable UI review
        |
        v
CSV / Excel export
```

## Extracted Fields

Maps directly to the dataset submission format:

| Export Column     | Description                                      |
|-------------------|--------------------------------------------------|
| ITEM_NAME         | Full product name as printed                     |
| BARCODE           | Numeric barcode (pyzbar decoded)                 |
| MANUFACTURER      | Legal company name                               |
| BRAND             | Brand name on pack                               |
| WEIGHT            | Combined weight + unit e.g. `100G`, `1.5 KG`    |
| PACKAGING TYPE    | e.g. `BOX`, `SACHET`, `TUB`, `GLASS JAR`        |
| COUNTRY           | Country of manufacture                           |
| VARIANT           | e.g. `ORIGINAL`, `LOW FAT`                       |
| TYPE              | Product category e.g. `MARGARINE`, `SOAP`        |
| FRAGRANCE_FLAVOR  | Flavor or scent where applicable                 |
| PROMOTION         | On-pack promotion text                           |
| ADDONS            | Extra pack contents                              |
| TAGLINE           | Short slogan or descriptive tagline              |

## Requirements

- Python 3.13+
- `uv`
- Ollama
- `zbar` system library for barcode decoding

On macOS:

```bash
brew install zbar
```

On Linux:

```bash
sudo apt-get install libzbar0
```

Install Python dependencies:

```bash
uv sync
```

Pull the vision model:

```bash
ollama pull llama3.2-vision:11b
```

## Environment Variables

Copy `.env.example` to `.env` and set as needed:

| Variable                  | Default           | Purpose                                                      |
|---------------------------|-------------------|--------------------------------------------------------------|
| `VL_MODEL`                | `qwen3-vl:4b`     | Ollama model name — set to `llama3.2-vision:11b` for best results |
| `OLLAMA_CONCURRENCY`      | `2`               | Max simultaneous Ollama requests from Python — match to `OLLAMA_NUM_PARALLEL` |
| `VLM_BATCH_SIZE`          | `1`               | Images per VLM call — llama3.2-vision only supports 1        |
| `IMDB_USE_DUMMY_EXTRACTION` | —               | Set to `YES` to skip the VLM for UI demos                    |

## Running Ollama for Best Performance

By default Ollama processes one request at a time. For parallel inference set `OLLAMA_NUM_PARALLEL` before starting the server:

```bash
OLLAMA_NUM_PARALLEL=4 ollama serve
```

Also set `OLLAMA_CONCURRENCY=4` in your `.env` to match.

**GPU sizing guide for llama3.2-vision:11b (~8 GB VRAM per slot):**

| GPU VRAM  | Recommended NUM_PARALLEL | ~Time for 45 images |
|-----------|--------------------------|---------------------|
| 8 GB      | 1                        | ~104s               |
| 16 GB     | 2                        | ~52s                |
| 24 GB     | 3                        | ~35s                |
| 32 GB+    | 4                        | ~26s                |

Latency scales as `ceil(images / NUM_PARALLEL) × ~2.3s` per image.

## Run The App

```bash
uv run python -m frontend.app
```

Open `http://localhost:5200`, create an account, upload product images, review extracted rows, and export.

## Demo Mode

For a stable demo without a running model:

```bash
IMDB_USE_DUMMY_EXTRACTION=YES uv run python -m frontend.app
```

Returns a fixed sample record and exercises the full UI, persistence, review, and export workflow.

## Usage

1. Log in or create an account.
2. Upload one or more product images (multiple angles of the same product at once works best — the pipeline groups them automatically).
3. Wait for extraction — the grid updates when done.
4. Review the row status:
   - **Green** — high confidence, likely correct
   - **Yellow** — one or more fields need review
   - **Red** — possible duplicate of an existing record
5. Click **Review** to open the side drawer: view the image carousel and edit any field.
6. Save changes, then export CSV or Excel from the header.

## Model Notes

The pipeline uses **llama3.2-vision:11b** via Ollama's native `/api/chat` endpoint with structured output constraints (JSON schema). This eliminates JSON parsing failures and thinking-token overhead seen with other models.

Models evaluated during development:

| Model               | Accuracy  | Notes                                                      |
|---------------------|-----------|------------------------------------------------------------|
| llama3.2-vision:11b | Best      | No thinking mode, reliable structured output, 1 img/call  |
| qwen3-vl:4b         | Good      | Works but thinking tokens can consume token budget         |
| qwen3-vl:8b         | Unreliable| Thinking mode can't be suppressed via Ollama API           |
| qwen2.5-vl:7b       | Poor      | Doesn't follow structured extraction prompts reliably      |

**Batching note:** llama3.2-vision via Ollama is limited to one image per API call. Multi-image batching (`VLM_BATCH_SIZE > 1`) requires a model and backend that supports multiple images per request.

## Project Structure

```
backend/
  pipeline.py       end-to-end orchestration
  extractor.py      VLM calls, JSON parsing, field aggregation
  barcode.py        pyzbar decoding with preprocessing fallbacks
  normalizer.py     country corrections, barcode cleaning, duplicate detection
  db.py             SQLite persistence and edit version history
  schema.py         Pydantic models with per-field confidence scores
core/prompts/
  vlm_system_prompt.j2    field definitions and output contract
  vlm_extraction_prompt.j2 per-image extraction instructions
frontend/
  app.py            NiceGUI entry point and page registration
  components.py     grid, review drawer, carousel, upload zone
  handlers.py       upload, export, edit, and delete handlers
  state.py          shared state, row mapping, export formatting
eval/
  run_eval.py       full dataset pipeline run + accuracy report
  metrics.py        field-level scoring against ground truth
```
