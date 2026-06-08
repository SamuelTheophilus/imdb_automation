# IMDB AutoFill

AI-driven image-to-item-master-data tool for retail product cataloging.

This project was built for the GDSS-Maverick Hackathon challenge: auto-fill an Item Master Database (IMDB) row from a product image. The app accepts product label images, extracts structured product attributes, lets a user review and edit the result, and exports the final records as CSV or Excel for database upload.

## Problem

Retail teams often fill item master records manually by reading product labels and typing values into spreadsheets or database forms. That process is slow and error-prone, especially when teams need consistent names for brands, categories, packaging, weights, and promotional messages.

IMDB AutoFill reduces that manual work by turning a product image into a structured product-master row.

## Target IMDB Fields

The app extracts and exports these product attributes:

1. Barcode
2. Category type
3. Segment type
4. Manufacturer
5. Brand
6. Product name
7. Weight
8. Unit
9. Packaging type
10. Country of origin
11. Promotional or marketing messages

The hackathon brief lists weight and unit as one IMDB attribute. This implementation stores them separately for editing and also exports a combined `weight_display` field.

## What Is Implemented

- Multi-image upload from the web UI
- Local vision-language model extraction through Ollama's OpenAI-compatible API
- Deterministic barcode decoding before model fallback
- Prompt-based extraction into a strict JSON schema
- Per-field confidence flags for human review
- Fuzzy normalization for category, segment, brand, and packaging labels
- Duplicate suggestion logic using barcode and fuzzy brand/product matching
- Editable AG Grid preview table
- Review drawer with larger image preview and one input per IMDB field
- CSV and Excel export
- SQLite persistence for users, uploads, edits, confidence metadata, and history
- Login/signup flow for separating user records
- Version history for edited extractions
- Dummy extraction mode for reliable demos without a running model

## Architecture

```text
Product image
    |
    v
Upload and save image
    |
    v
Barcode decoder
    |
    v
Vision-language extraction with prompt templates
    |
    v
Pydantic IMDB schema
    |
    v
Normalization and duplicate checks
    |
    v
Editable UI preview
    |
    v
CSV / Excel export
```

Main modules:

- `frontend/app.py`: NiceGUI app entry point and page registration
- `frontend/components.py`: upload area, header, grid, review drawer, and history UI
- `frontend/handlers.py`: upload processing, export, edit persistence, and delete handlers
- `backend/extractor.py`: image encoding, VLM call, JSON parsing, and field confidence assignment
- `backend/barcode.py`: barcode decoding with several image preprocessing attempts
- `backend/pipeline.py`: end-to-end extraction, normalization, duplicate detection, and export shaping
- `backend/normalizer.py`: fuzzy normalization and duplicate suggestion rules
- `backend/db.py`: SQLite persistence and extraction version history
- `core/prompts/`: system and extraction prompts used by the VLM

## Requirements

- Python 3.13+
- `uv`
- Ollama, for live VLM extraction
- Local model: `qwen3-vl:4b`
- `zbar` system library for `pyzbar` barcode decoding

On macOS, install the native barcode dependency with:

```bash
brew install zbar
```

Install Python dependencies:

```bash
uv sync
```

Pull the local vision model:

```bash
ollama pull qwen3-vl:4b
```

Make sure Ollama is running before live extraction:

```bash
ollama serve
```

## Run The App

```bash
uv run python frontend/app.py
```

Open:

```text
http://localhost:5200
```

Create an account, upload one or more product images, review the extracted rows, edit any low-confidence fields, then export CSV or Excel from the header.

## Demo Mode

For a stable demo without relying on a running VLM, enable dummy extraction:

```bash
IMDB_USE_DUMMY_EXTRACTION=YES uv run python frontend/app.py
```

Dummy mode returns a fixed sample product record and still exercises the UI, persistence, review, confidence flagging, and export workflow.

## Export Output

Exports are written to the `data/` directory and downloaded by the browser:

- `data/imdb_export.csv`
- `data/imdb_export.xlsx`

Exported columns:

- `barcode`
- `category_type`
- `segment_type`
- `manufacturer`
- `brand`
- `product_name`
- `weight`
- `unit`
- `packaging_type`
- `country_of_origin`
- `promotional_messages`
- `weight_display`

## Demo Walkthrough

1. Log in or create a new account.
2. Upload a product image.
3. Wait for extraction to complete.
4. Review the row status:
   - Green: high confidence
   - Yellow: needs review
   - Red: possible duplicate
5. Click `Review` to compare the image with extracted fields.
6. Edit incorrect or missing fields.
7. Save changes.
8. Export CSV or Excel.
9. Show the exported file as the product-master import table.

## Hackathon Fit

This project satisfies the core deliverable: a web UI and backend pipeline that accepts product images, extracts IMDB fields, allows human review, and exports structured CSV/Excel files for product-master ingestion.

It also includes bonus-oriented features such as standardized naming, low-confidence review flags, duplicate suggestion logic, persistence, and edit history.
