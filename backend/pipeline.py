# backend/pipeline.py

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from backend.extractor import extract_information_from_images
from backend.normalizer import check_duplicate, normalize_record
from backend.schema import IMDBRecordWithConfidence

load_dotenv()


class PipelineResult:
    """Wraps the final output of one product extraction for the UI layer."""

    def __init__(
        self,
        record: IMDBRecordWithConfidence,
        normalized_fields: list[str],
        duplicate_suggestions: list[dict],
        image_path: str,
        image_paths: list[str] | None = None,
    ):
        self.record = record
        self.normalized_fields = normalized_fields
        self.duplicate_suggestions = duplicate_suggestions
        self.image_path = image_path                        # primary image (thumbnail)
        self.image_paths: list[str] = image_paths or [image_path]  # all grouped images

    @property
    def low_confidence_fields(self) -> list[str]:
        return self.record.get_low_confidence_fields(threshold=0.6)

    @property
    def has_duplicates(self) -> bool:
        return len(self.duplicate_suggestions) > 0

    @property
    def has_low_confidence(self) -> bool:
        return len(self.low_confidence_fields) > 0

    def to_dict(self) -> dict:
        """Return a flat dict in the dataset submission column format."""
        data = self.record.model_dump()
        export_keys = [
            "barcode",
            "category_type",
            "segment_type",
            "manufacturer",
            "brand",
            "product_name",
            "weight",           # already combined e.g. "100G", "1.5 KG"
            "packaging_type",
            "country_of_origin",
            "promotional_messages",
            "variant",
            "fragrance_flavor",
            "addons",
            "tagline",
        ]
        return {k: data[k] for k in export_keys}


async def run_pipeline(
    image_paths: list[str] | list[Path],
    existing_records: list[dict] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[PipelineResult]:
    """Run the full extraction pipeline on a list of product images.

    Steps:
    1. Verify all paths exist.
    2. If IMDB_USE_DUMMY_EXTRACTION=YES, return a fixed sample record (for demos).
    3. Otherwise: extract via VLM → normalize → check duplicates.

    Args:
        image_paths:      Paths to the product images (multiple angles welcome).
        existing_records: Current IMDB rows used for duplicate detection.
                          Pass empty list or None if there are no existing rows.

    Returns:
        One PipelineResult per distinct product found across the images.
    """
    verified_paths = []
    for img in image_paths:
        img = Path(img)
        if not img.exists():
            raise FileNotFoundError(f"Image not found: {img}")
        verified_paths.append(img)

    # Dummy mode returns a fixed record so the UI can be demonstrated without
    # a running model. Enabled via the IMDB_USE_DUMMY_EXTRACTION env var.
    if os.getenv("IMDB_USE_DUMMY_EXTRACTION") == "YES":
        print("[pipeline] Using dummy extraction result...")
        record = IMDBRecordWithConfidence(
            barcode=None,
            category_type="CEREAL",
            segment_type="family size",
            manufacturer="Kellogg's",
            brand="Apple Jacks",
            product_name="Apple Jacks Sweetened Cereal with Apple & Cinnamon",
            weight="23OZ",
            unit=None,
            packaging_type="BOX",
            country_of_origin=None,
            promotional_messages="FAMILY SIZE + 25% MORE FREE; 1 BOX = 1 FREE BOOK",
            variant="FAMILY SIZE",
            fragrance_flavor="APPLE & CINNAMON",
            addons=None,
            tagline="Taste the fun!",
            barcode_confidence=0.3,
            category_type_confidence=0.9,
            segment_type_confidence=0.9,
            manufacturer_confidence=0.9,
            brand_confidence=0.9,
            product_name_confidence=0.9,
            weight_confidence=0.9,
            unit_confidence=0.0,
            packaging_type_confidence=0.9,
            country_of_origin_confidence=0.0,
            promotional_messages_confidence=0.9,
            variant_confidence=0.9,
            fragrance_flavor_confidence=0.9,
            addons_confidence=0.0,
            tagline_confidence=0.9,
        )
        return [
            PipelineResult(
                record=record,
                normalized_fields=[],
                duplicate_suggestions=[],
                image_path=str(verified_paths[0]),
                image_paths=[str(p) for p in verified_paths],
            )
        ]

    print("[pipeline] Extracting from uploaded images...")
    extracted_products: list[tuple[IMDBRecordWithConfidence, list[str]]] = (
        await extract_information_from_images(verified_paths)
    )

    if on_progress:
        on_progress("Normalizing fields…")
        await asyncio.sleep(0)  # yield so NiceGUI flushes the label update
    pipeline_results: list[PipelineResult] = []
    for record, group_paths in extracted_products:
        print("[pipeline] Normalizing fields...")
        record, normalized_fields = normalize_record(record)

        # Drop records with no identifying information — these are empty VLM
        # extractions from background/partial images with no readable product.
        if not record.brand and not record.product_name and not record.manufacturer:
            print("[pipeline] Skipping empty record (no brand, product_name, or manufacturer)")
            continue

        print("[pipeline] Checking for duplicates...")
        duplicates = check_duplicate(record, existing_records=existing_records or [])

        print(
            f"[pipeline] Done. "
            f"Low confidence fields: {record.get_low_confidence_fields()} | "
            f"Normalized: {normalized_fields} | "
            f"Duplicates found: {len(duplicates)}"
        )
        pipeline_results.append(
            PipelineResult(
                record=record,
                normalized_fields=normalized_fields,
                duplicate_suggestions=duplicates,
                image_path=str(group_paths[0] if group_paths else verified_paths[0]),
                image_paths=[str(p) for p in group_paths] if group_paths else [str(verified_paths[0])],
            )
        )

    return pipeline_results
