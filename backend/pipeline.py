# backend/pipeline.py

import os
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel, Field

from backend.barcode import _bottom_crop
from backend.extractor import extract_information_from_images
from backend.normalizer import check_duplicate, normalize_record
from backend.schema import IMDBRecordWithConfidence
from backend.utils import _encode_pil_image, vlm_call

load_dotenv()


class PipelineResult:
    """Wraps the final output of the pipeline for the UI layer."""

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
        self.image_path = image_path
        self.image_paths: list[str] = image_paths or [image_path]

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
        """Flat dict for CSV/Excel export — one row per product."""
        data = self.record.model_dump()

        # combine weight + unit into single readable string for export
        if data.get("weight") and data.get("unit"):
            data["weight_display"] = f"{data['weight']} {data['unit']}"
        else:
            data["weight_display"] = None

        # strip confidence fields from export
        export_keys = [
            "barcode",
            "category_type",
            "segment_type",
            "manufacturer",
            "brand",
            "product_name",
            "weight",
            "unit",
            "weight_display",
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
    use_local: bool = True,
) -> list[PipelineResult]:
    """
    Full pipeline: image → barcode + VLM extraction → normalization → duplicate check → PipelineResult.
    Args:
        image_path:       Path to product image.
        existing_records: Current IMDB rows for duplicate checking.
                          Pass empty list or None if no existing data.
        use_local:        True = Ollama local, False = Modal endpoint.
    Returns:
        PipelineResult with record, confidence scores,
        normalized fields, and duplicate suggestions.
    """

    verified_paths = []

    for img in image_paths:
        img = Path(img)
        if not img.exists():
            raise FileNotFoundError(f"Image not found: {img}")
        verified_paths.append(img)

    # if not image_path.exists():
    #     raise FileNotFoundError(f"Image not found: {image_path}")
    #
    use_dummy = os.getenv("IMDB_USE_DUMMY_EXTRACTION")

    if use_dummy == "YES":
        print("[pipeline] Using dummy extraction result...")
        record = IMDBRecordWithConfidence(
            barcode=None,
            category_type="CEREAL",
            segment_type="family size",
            manufacturer="Kellogg's",
            brand="Apple Jacks",
            product_name="Apple Jacks Sweetened Cereal with Apple & Cinnamon",
            weight="23",
            unit="OZ",
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
            unit_confidence=0.9,
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
    extracted_products: list[
        tuple[IMDBRecordWithConfidence, list[str]]
    ] = await extract_information_from_images(
        verified_paths,
        use_local=use_local,
    )
    pipeline_results: list[PipelineResult] = []

    for record, group_paths in extracted_products:
        print(f"[pipeline] Extraction result type: {type(record)}")
        print(f"[pipeline] Extraction result: {record}")

        print("[pipeline] Normalizing fields...")
        record, normalized_fields = normalize_record(record)

        print("[pipeline] Checking for duplicates...")
        duplicates = check_duplicate(
            record,
            existing_records=existing_records or [],
        )

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

    # return PipelineResult(
    #     record=record,
    #     normalized_fields=normalized_fields,
    #     duplicate_suggestions=duplicates,
    #     image_path=str(verified_paths),
    # )


# def run_batch_pipeline(
#     image_paths: list[str | Path],
#     existing_records: list[dict] | None = None,
#     use_local: bool = True,
# ) -> list[PipelineResult]:
#     """
#     Runs the pipeline on multiple images sequentially.
#     Used by the eval script to process the full dataset.
#     """
#     results = []
#     try:
#         result = run_pipeline(
#             image_paths,
#             existing_records=existing_records,
#             use_local=use_local,
#         )
#         results.append(result)
#     except Exception as e:
#         print(f"[pipeline] Failed on {image_paths}: {e}")

#     return results
