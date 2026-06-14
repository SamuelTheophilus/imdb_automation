from pathlib import Path

import cv2
import numpy as np
import pyzbar.pyzbar as pyzbar
import zxingcpp
from PIL import Image, ImageEnhance, ImageFilter


def decode_barcode(image_paths: list[str] | list[Path]) -> tuple[str | None, float]:
    """Attempt to decode a barcode from one or more product images.

    Pass 1 — pyzbar with standard preprocessing strategies.
    Pass 2 — zxing-cpp with the same strategies.
    Pass 3 — CLAHE + adaptive threshold (both decoders) for difficult lighting.

    Returns:
        (barcode_value, confidence) — confidence is 1.0 on success, 0.0 if
        no barcode could be read from any of the provided images.
    """
    images = [Image.open(p).convert("RGB") for p in image_paths]

    for itr, image in enumerate(images, start=1):
        print(f"[Barcode extraction] Trying out image {itr}/{len(images)}]")

        standard_variants = [
            ("raw",     image),
            ("enhance", _enhance(image)),
            ("crop",    _center_crop(image)),
            ("upscale", image.resize((image.width * 2, image.height * 2), Image.LANCZOS) if image.width < 800 else None),
        ]

        # Pass 1: pyzbar
        for label, variant in standard_variants:
            if variant is None:
                continue
            result = _pyzbar_decode(variant)
            if result:
                print(f"[Barcode extraction] Barcode Extraction Passed (pyzbar+{label})")
                return result, 1.0

        # Pass 2: zxing-cpp
        for label, variant in standard_variants:
            if variant is None:
                continue
            result = _zxing_decode(variant)
            if result:
                print(f"[Barcode extraction] Barcode Extraction Passed (zxing+{label})")
                return result, 1.0

        # Pass 3: CLAHE + adaptive threshold — handles uneven lighting and low contrast
        for label, variant in [
            ("clahe",          _clahe(image)),
            ("adaptive",       _adaptive_threshold(image)),
            ("clahe+adaptive", _adaptive_threshold(_clahe(image))),
            ("clahe+crop",     _clahe(_center_crop(image))),
        ]:
            for decoder, fn in [("pyzbar", _pyzbar_decode), ("zxing", _zxing_decode)]:
                result = fn(variant)
                if result:
                    print(f"[Barcode extraction] Barcode Extraction Passed ({decoder}+{label})")
                    return result, 1.0

    print("[Barcode extraction] Barcode Extraction Failed")
    return None, 0.0


def _pyzbar_decode(image: Image.Image) -> str | None:
    decoded = pyzbar.decode(image)
    return decoded[0].data.decode("utf-8") if decoded else None


_ZXING_FORMATS = (
    zxingcpp.BarcodeFormat.EAN13
    | zxingcpp.BarcodeFormat.EAN8
    | zxingcpp.BarcodeFormat.UPCA
    | zxingcpp.BarcodeFormat.UPCE
    | zxingcpp.BarcodeFormat.Code128
    | zxingcpp.BarcodeFormat.Code39
)

def _zxing_decode(image: Image.Image) -> str | None:
    results = zxingcpp.read_barcodes(image, formats=_ZXING_FORMATS)
    return results[0].text if results else None


def _enhance(image: Image.Image) -> Image.Image:
    """Grayscale + 2× contrast + sharpen."""
    gray = image.convert("L")
    return ImageEnhance.Contrast(gray).enhance(2.0).filter(ImageFilter.SHARPEN)


def _center_crop(image: Image.Image) -> Image.Image:
    """Crop the middle 60% of the image."""
    w, h = image.size
    return image.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))


def _clahe(image: Image.Image) -> Image.Image:
    """CLAHE — boosts local contrast without blowing out highlights."""
    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return Image.fromarray(enhanced)


def _adaptive_threshold(image: Image.Image) -> Image.Image:
    """Adaptive Gaussian threshold — binarizes per local region."""
    arr = np.array(image)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return Image.fromarray(binary)
