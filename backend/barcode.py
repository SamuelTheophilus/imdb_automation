from pathlib import Path

import cv2
import numpy as np
import pyzbar.pyzbar as pyzbar
import zxingcpp
from PIL import Image, ImageEnhance, ImageFilter


def ean_checksum_valid(code: str) -> bool:
    """Return True if code is a valid EAN-8, UPC-A (12-digit), or EAN-13 checksum."""
    if not code or not code.isdigit():
        return False
    n = len(code)
    if n not in (8, 12, 13):
        return False
    digits = [int(d) for d in code]
    total = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:-1]))
    return (10 - total % 10) % 10 == digits[-1]


def decode_barcode(image_paths: list[str] | list[Path]) -> tuple[str | None, float]:
    """Attempt to decode a barcode from one or more product images.

    Pass 1 — pyzbar: raw, enhanced, cropped, upscaled, + 90/180/270° rotations.
    Pass 2 — zxing-cpp: same variants.
    Pass 3 — CLAHE + adaptive threshold (both decoders).
    Pass 4 — OpenCV BarcodeDetector: gradient-coherence algorithm + rotations.
    Pass 5 — Gradient region localisation: crop to barcode ROI, then all decoders.

    Returns:
        (barcode_value, confidence) — confidence is 1.0 on success, 0.0 if
        no barcode could be read from any of the provided images.
    """
    images = [Image.open(p).convert("RGB") for p in image_paths]

    for itr, image in enumerate(images, start=1):
        print(f"[Barcode extraction] Trying image {itr}/{len(images)}")

        variants = _build_variants(image)

        # Pass 1: pyzbar
        for label, variant in variants:
            result = _pyzbar_decode(variant)
            if result:
                print(f"[Barcode extraction] pyzbar+{label}")
                return result, 1.0

        # Pass 2: zxing-cpp
        for label, variant in variants:
            result = _zxing_decode(variant)
            if result:
                print(f"[Barcode extraction] zxing+{label}")
                return result, 1.0

        # Pass 3: CLAHE + adaptive threshold — handles uneven lighting and low contrast
        clahe_variants = [
            ("clahe",             _clahe(image)),
            ("adaptive",          _adaptive_threshold(image)),
            ("clahe+adaptive",    _adaptive_threshold(_clahe(image))),
            ("clahe+crop",        _clahe(_center_crop(image))),
            ("clahe+rot180",      _clahe(image.rotate(180, expand=True))),
            ("adaptive+rot180",   _adaptive_threshold(image.rotate(180, expand=True))),
        ]
        for label, variant in clahe_variants:
            for decoder_name, fn in [("pyzbar", _pyzbar_decode), ("zxing", _zxing_decode)]:
                result = fn(variant)
                if result:
                    print(f"[Barcode extraction] {decoder_name}+{label}")
                    return result, 1.0

        # Pass 4: OpenCV BarcodeDetector — gradient-direction-coherence algorithm
        for label, variant in variants:
            result = _opencv_decode(variant)
            if result:
                print(f"[Barcode extraction] opencv+{label}")
                return result, 1.0

        # Pass 5: Gradient-based region localisation → all decoders on the crop
        roi = _localize_barcode(image)
        if roi is not None:
            roi_variants = [
                roi,
                _enhance(roi),
                _clahe(roi),
                _adaptive_threshold(_clahe(roi)),
                roi.rotate(90, expand=True),
                roi.rotate(180, expand=True),
                roi.rotate(270, expand=True),
            ]
            for variant in roi_variants:
                for fn in (_pyzbar_decode, _zxing_decode, _opencv_decode):
                    result = fn(variant)
                    if result:
                        print(f"[Barcode extraction] localised+{fn.__name__}")
                        return result, 1.0

    print("[Barcode extraction] Failed on all passes")
    return None, 0.0


# ── Decoders ─────────────────────────────────────────────────────────────────

def _pyzbar_decode(image: Image.Image) -> str | None:
    try:
        decoded = pyzbar.decode(image)
        if not decoded:
            return None
        text = decoded[0].data.decode("utf-8")
        return text if len(text) >= 8 else None  # reject partial/noise reads
    except Exception:
        return None


_ZXING_FORMATS = (
    zxingcpp.BarcodeFormat.EAN13
    | zxingcpp.BarcodeFormat.EAN8
    | zxingcpp.BarcodeFormat.UPCA
    | zxingcpp.BarcodeFormat.UPCE
    | zxingcpp.BarcodeFormat.Code128
    | zxingcpp.BarcodeFormat.Code39
)


def _zxing_decode(image: Image.Image) -> str | None:
    try:
        results = zxingcpp.read_barcodes(image, formats=_ZXING_FORMATS)
        if not results:
            return None
        text = results[0].text
        return text if len(text) >= 8 else None
    except Exception:
        return None


_opencv_detector = cv2.barcode.BarcodeDetector()


def _opencv_decode(image: Image.Image) -> str | None:
    try:
        arr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        retval, decoded_info, _, _ = _opencv_detector.detectAndDecode(arr)
        if retval:
            for info in decoded_info:
                if info and len(info) >= 8:
                    return info
        return None
    except Exception:
        return None


# ── Preprocessing helpers ─────────────────────────────────────────────────────

def _build_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """Standard variants: rotations, quadrant crops, and upscales."""
    w, h = image.size
    variants: list[tuple[str, Image.Image]] = [
        ("raw",        image),
        ("enh",        _enhance(image)),
        ("crop",       _center_crop(image)),
        ("rot90",      image.rotate(90,  expand=True)),
        ("rot180",     image.rotate(180, expand=True)),
        ("rot270",     image.rotate(270, expand=True)),
        # Quadrant and half crops — barcode often lives in one corner/edge
        ("right-half", image.crop((w // 2, 0, w, h))),
        ("left-half",  image.crop((0, 0, w // 2, h))),
        ("bot-half",   image.crop((0, h // 2, w, h))),
        ("bot-right",  image.crop((w // 2, h // 2, w, h))),
        ("top-right",  image.crop((w // 2, 0, w, h // 2))),
        ("bot-left",   image.crop((0, h // 2, w // 2, h))),
        # 3× upscale — many barcodes are small relative to the image
        ("up3x",       image.resize((w * 3, h * 3), Image.LANCZOS)),
        ("up3x-right", image.crop((w // 2, 0, w, h)).resize((w * 3 // 2, h * 3), Image.LANCZOS)),
    ]
    if w < 800:
        variants.append(("up2x", image.resize((w * 2, h * 2), Image.LANCZOS)))
    return variants


def _enhance(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    return ImageEnhance.Contrast(gray).enhance(2.0).filter(ImageFilter.SHARPEN)


def _center_crop(image: Image.Image) -> Image.Image:
    w, h = image.size
    return image.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))


def _clahe(image: Image.Image) -> Image.Image:
    arr = np.array(image)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return Image.fromarray(clahe.apply(gray))


def _adaptive_threshold(image: Image.Image) -> Image.Image:
    arr = np.array(image)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return Image.fromarray(binary)


def _localize_barcode(image: Image.Image) -> Image.Image | None:
    """Scharr gradient + morphological close to crop to the barcode ROI.

    Based on the PyImageSearch gradient localisation approach. Works best for
    1-D linear barcodes (EAN/UPC) where horizontal gradients dominate.
    Returns None if no clear candidate region is found.
    """
    arr = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # Scharr gradient emphasises horizontal bar structure
    grad_x = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    grad_y = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    gradient = cv2.subtract(grad_x, grad_y)
    gradient = cv2.convertScaleAbs(gradient)

    blurred = cv2.blur(gradient, (9, 9))
    _, thresh = cv2.threshold(blurred, 225, 255, cv2.THRESH_BINARY)

    # Wide horizontal kernel connects the vertical bars of the barcode
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    closed = cv2.erode(closed, None, iterations=4)
    closed = cv2.dilate(closed, None, iterations=4)

    contours, _ = cv2.findContours(closed.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)

    # Sanity check: barcode ROI must be wider than tall with a reasonable ratio
    if w < 50 or h < 10 or w < h * 1.5:
        return None

    pad = 20
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(arr.shape[1], x + w + pad)
    y2 = min(arr.shape[0], y + h + pad)

    cropped = arr[y1:y2, x1:x2]
    if cropped.size == 0:
        return None

    # Upscale the ROI to give decoders a better chance
    roi = Image.fromarray(cropped)
    if roi.width < 400:
        roi = roi.resize((roi.width * 3, roi.height * 3), Image.LANCZOS)
    return roi
