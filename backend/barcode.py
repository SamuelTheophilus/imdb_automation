from pathlib import Path

import pyzbar.pyzbar as pyzbar
from PIL import Image, ImageEnhance, ImageFilter


def decode_barcode(image_paths: list[str] | list[Path]) -> tuple[str | None, float]:
    """Attempt to decode a barcode from one or more product images.

    Tries four preprocessing strategies per image in order of increasing
    aggressiveness. Returns on the first successful decode.

    Returns:
        (barcode_value, confidence) — confidence is 1.0 on success, 0.0 if
        no barcode could be read from any of the provided images.
    """
    images = [Image.open(p).convert("RGB") for p in image_paths]

    for itr, image in enumerate(images, start=1):
        print(f"[Barcode extraction] Trying out image {itr}/{len(images)}]")

        # Strategy 1: raw image
        result = _try_decode(image)
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Strategy 2: grayscale + contrast boost + sharpen
        result = _try_decode(_enhance(image))
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Strategy 3: center crop — barcodes are usually in the middle of labels
        result = _try_decode(_center_crop(image))
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Strategy 4: 2× upscale for small/low-res images
        if image.width < 800:
            result = _try_decode(
                image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
            )
            if result:
                print("[Barcode extraction] Barcode Extraction Passed")
                return result, 1.0

    print("[Barcode extraction] Barcode Extraction Failed")
    return None, 0.0


def _try_decode(image: Image.Image) -> str | None:
    """Run pyzbar on a single image and return the first decoded value."""
    decoded = pyzbar.decode(image)
    if decoded:
        return decoded[0].data.decode("utf-8")
    return None


def _enhance(image: Image.Image) -> Image.Image:
    """Convert to grayscale, boost contrast 2×, and sharpen."""
    gray = image.convert("L")
    contrast = ImageEnhance.Contrast(gray).enhance(2.0)
    return contrast.filter(ImageFilter.SHARPEN)


def _center_crop(image: Image.Image) -> Image.Image:
    """Crop the middle 60% of the image in both dimensions."""
    w, h = image.size
    return image.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))
