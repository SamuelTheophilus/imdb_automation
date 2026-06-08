from pathlib import Path

import pyzbar.pyzbar as pyzbar
from PIL import Image, ImageEnhance, ImageFilter


def decode_barcode(image_paths: list[str] | list[Path]) -> tuple[str | None, float]:
    """
    Attempts to decode a barcode from a product image.
    Returns (barcode_value, confidence) tuple.
    Confidence is 1.0 if found deterministically, 0.0 if not found.
    """
    images = [Image.open(img_pth).convert("RGB") for img_pth in image_paths]

    # image = Image.open(image_path).convert("RGB")

    for itr, image in enumerate(images, start=1):
        print(f"[Barcode extraction] Trying out image {itr}/{len(images)}]")
        # Attempt 1: raw image
        result = _try_decode(image)
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Attempt 2: grayscale + contrast boost
        result = _try_decode(_enhance(image))
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Attempt 3: crop center 60% (barcode is usually centered on label)
        result = _try_decode(_center_crop(image))
        if result:
            print("[Barcode extraction] Barcode Extraction Passed")
            return result, 1.0

        # Attempt 4: upscale small images
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
    decoded = pyzbar.decode(image)
    if decoded:
        return decoded[0].data.decode("utf-8")
    return None


def _enhance(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    contrast = ImageEnhance.Contrast(gray).enhance(2.0)
    sharpened = contrast.filter(ImageFilter.SHARPEN)
    return sharpened


def _center_crop(image: Image.Image) -> Image.Image:
    w, h = image.size
    left = int(w * 0.2)
    top = int(h * 0.2)
    right = int(w * 0.8)
    bottom = int(h * 0.8)
    return image.crop((left, top, right, bottom))


def _bottom_crop(image: Image.Image, ratio: float = 0.2) -> Image.Image:
    w, h = image.size
    left = 0
    top = int(h * (1 - ratio))
    right = w
    bottom = h
    return image.crop((left, top, right, bottom))


if __name__ == "__main__":
    img_path = f"{Path(__file__).resolve().parent}/9185570.png"
    barcode, confidence = decode_barcode(img_path)
    print("Barcode Number ", barcode)
