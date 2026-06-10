"""
Quick pipeline test — runs against images in imdb_images/ without the UI.

Usage:
    python test_pipeline.py                     # first session found
    python test_pipeline.py --limit 3           # first 3 sessions
    python test_pipeline.py --session S221234199  # specific session
    python test_pipeline.py --images a.jpg b.jpg  # arbitrary image paths
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from backend.pipeline import run_pipeline

IMAGE_DIR = Path(__file__).parent / "imdb_images"


def group_by_session(image_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(image_dir.glob("*.jpg")):
        session = p.stem.split("_")[0]
        groups[session].append(p)
    return dict(groups)


def print_result(result, idx: int) -> None:
    r = result.record
    print(f"\n--- Product {idx} ---")
    print(f"  images       : {[Path(p).name for p in result.image_paths]}")
    print(f"  barcode      : {r.barcode}  (conf={r.barcode_confidence:.2f})")
    print(f"  brand        : {r.brand}")
    print(f"  product_name : {r.product_name}")
    print(f"  manufacturer : {r.manufacturer}")
    print(f"  category     : {r.category_type} / {r.segment_type}")
    print(f"  weight       : {r.weight}  pkg={r.packaging_type}")
    print(f"  variant      : {r.variant}  flavor={r.fragrance_flavor}")
    print(f"  origin       : {r.country_of_origin}")
    print(f"  promo        : {r.promotional_messages}")
    print(f"  tagline      : {r.tagline}")
    if result.low_confidence_fields:
        print(f"  LOW CONF     : {result.low_confidence_fields}")
    if result.normalized_fields:
        print(f"  normalized   : {result.normalized_fields}")
    if result.has_duplicates:
        print(f"  duplicates   : {len(result.duplicate_suggestions)} found")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", help="Run a specific session prefix (e.g. S221234199)")
    parser.add_argument("--limit", type=int, default=1, help="Number of sessions to run (default 1)")
    parser.add_argument("--images", nargs="+", help="Explicit image paths to pass directly")
    args = parser.parse_args()

    if args.images:
        image_paths = [Path(p) for p in args.images]
        sessions = {"custom": image_paths}
    else:
        sessions = group_by_session(IMAGE_DIR)
        if not sessions:
            sys.exit(f"No images found in {IMAGE_DIR}")
        if args.session:
            if args.session not in sessions:
                sys.exit(f"Session '{args.session}' not found. Available: {list(sessions)[:5]}")
            sessions = {args.session: sessions[args.session]}
        else:
            keys = list(sessions)[: args.limit]
            sessions = {k: sessions[k] for k in keys}

    for session_id, paths in sessions.items():
        print(f"\n{'='*60}")
        print(f"Session: {session_id}  ({len(paths)} images)")
        print(f"{'='*60}")

        t0 = time.perf_counter()
        results = await run_pipeline(paths)
        elapsed = time.perf_counter() - t0

        for i, result in enumerate(results, 1):
            print_result(result, i)

        print(f"\n  [{len(results)} product(s) in {elapsed:.1f}s]")


if __name__ == "__main__":
    asyncio.run(main())
