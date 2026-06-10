from pydantic import BaseModel, Field
from typing import Optional


class IMDBRecord(BaseModel):
    """Flat product-master record matching the dataset submission columns."""
    barcode: Optional[str] = Field(None)
    category_type: Optional[str] = Field(None)       # maps to TYPE column
    segment_type: Optional[str] = Field(None)
    manufacturer: Optional[str] = Field(None)
    brand: Optional[str] = Field(None)
    product_name: Optional[str] = Field(None)        # maps to ITEM_NAME column
    # Weight includes the unit as a single string e.g. "100G", "1.5 KG".
    weight: Optional[str] = Field(None)
    # unit is kept for DB backward-compatibility with existing records but is no
    # longer populated by the extraction pipeline.
    unit: Optional[str] = Field(None)
    packaging_type: Optional[str] = Field(None)
    country_of_origin: Optional[str] = Field(None)   # maps to COUNTRY column
    promotional_messages: Optional[str] = Field(None) # maps to PROMOTION column
    variant: Optional[str] = Field(None)
    fragrance_flavor: Optional[str] = Field(None)
    addons: Optional[str] = Field(None)
    tagline: Optional[str] = Field(None)


class IMDBRecordWithConfidence(IMDBRecord):
    """Extends IMDBRecord with per-field confidence scores used by the UI.

    Confidence is binary: 0.9 when the VLM returned a non-null value for that
    field, 0.0 when the field is null or was not extracted. Fields below the
    threshold (default 0.6) are flagged for human review in the grid.
    """
    barcode_confidence: float = Field(0.0, ge=0.0, le=1.0)
    category_type_confidence: float = Field(0.0, ge=0.0, le=1.0)
    segment_type_confidence: float = Field(0.0, ge=0.0, le=1.0)
    manufacturer_confidence: float = Field(0.0, ge=0.0, le=1.0)
    brand_confidence: float = Field(0.0, ge=0.0, le=1.0)
    product_name_confidence: float = Field(0.0, ge=0.0, le=1.0)
    weight_confidence: float = Field(0.0, ge=0.0, le=1.0)
    unit_confidence: float = Field(0.0, ge=0.0, le=1.0)
    packaging_type_confidence: float = Field(0.0, ge=0.0, le=1.0)
    country_of_origin_confidence: float = Field(0.0, ge=0.0, le=1.0)
    promotional_messages_confidence: float = Field(0.0, ge=0.0, le=1.0)
    variant_confidence: float = Field(0.0, ge=0.0, le=1.0)
    fragrance_flavor_confidence: float = Field(0.0, ge=0.0, le=1.0)
    addons_confidence: float = Field(0.0, ge=0.0, le=1.0)
    tagline_confidence: float = Field(0.0, ge=0.0, le=1.0)

    def get_low_confidence_fields(self, threshold: float = 0.6) -> list[str]:
        """Return field names whose confidence score is below threshold."""
        fields = [
            "barcode", "category_type", "segment_type", "manufacturer",
            "brand", "product_name", "weight",
            "packaging_type", "country_of_origin", "promotional_messages",
            "variant", "fragrance_flavor", "addons", "tagline",
        ]
        return [f for f in fields if getattr(self, f"{f}_confidence") < threshold]
