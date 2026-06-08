from pydantic import BaseModel, Field, field_validator
from typing import Optional


class IMDBRecord(BaseModel):
    barcode: Optional[str] = Field(None)
    category_type: Optional[str] = Field(None)
    segment_type: Optional[str] = Field(None)
    manufacturer: Optional[str] = Field(None)
    brand: Optional[str] = Field(None)
    product_name: Optional[str] = Field(None)
    weight: Optional[str] = Field(None)
    unit: Optional[str] = Field(None)
    packaging_type: Optional[str] = Field(None)
    country_of_origin: Optional[str] = Field(None)
    promotional_messages: Optional[str] = Field(None)
    variant: Optional[str] = Field(None, description="Product variant, e.g. ORIGINAL, LOW FAT")
    fragrance_flavor: Optional[str] = Field(None, description="Flavor or fragrance, e.g. RICH, VANILLA")
    addons: Optional[str] = Field(None, description="Extra pack contents, e.g. SPOON INCLUDED")
    tagline: Optional[str] = Field(None, description="Short promotional or descriptive tagline")



class IMDBRecordWithConfidence(IMDBRecord):
    """Extends IMDBRecord with per-field confidence scores for the UI."""

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
        """Returns field names where confidence is below threshold."""
        fields = [
            "barcode", "category_type", "segment_type", "manufacturer",
            "brand", "product_name", "weight", "unit",
            "packaging_type", "country_of_origin", "promotional_messages",
            "variant", "fragrance_flavor", "addons", "tagline",
        ]
        return [f for f in fields if getattr(self, f"{f}_confidence") < threshold]
