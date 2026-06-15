"""
Batch eval using Anthropic's Message Batches API (50% cost vs standard API).

All VLM calls for all sessions are submitted in a single batch, processed
asynchronously by Anthropic, then results are fed through the same
grouping/normalisation/barcode pipeline as the regular eval.

Usage:
    python eval/run_eval_batch.py                     # all sessions
    python eval/run_eval_batch.py --sessions S1,S2    # targeted subset
    python eval/run_eval_batch.py --out results.csv   # save predictions
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from backend.barcode import decode_barcode
from backend.extractor import (
    EXTRACTION_PROMPT,
    SYSTEM_PROMPT,
    _encode_image,
    _extract_json_array,
    _normalize_item,
    _record_from_group,
)
from backend.image_aggregation import group_by_tag_similarity
from backend.normalizer import check_duplicate, normalize_record
from backend.pipeline import PipelineResult
from eval.metrics import GT_FIELD_MAP, compute_report, match_to_gt, _norm

IMAGE_DIR = Path(__file__).parent.parent / "imdb_images"
GT_FILE   = Path(__file__).parent / "eval_from_org.xlsx"

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
BATCH_SIZE      = int(os.getenv("VLM_BATCH_SIZE", "8"))
POLL_INTERVAL   = 30  # seconds between status checks


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_gt(path: Path) -> list[dict]:
    df = pd.read_excel(path)
    df.columns = [c.strip() for c in df.columns]
    def _bc(v):
        if pd.isna(v): return None
        return str(int(float(v)))
    df["BARCODE"] = df["BARCODE"].apply(_bc)
    rows = []
    for _, row in df.iterrows():
        r = {}
        for col in df.columns:
            v = row[col]
            key = col.lower().replace("  ", "_").replace(" ", "_")
            r[key] = None if pd.isna(v) else str(v).strip()
        rows.append(r)
    return rows


def group_sessions(image_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(image_dir.glob("*.jpg")):
        groups[p.stem.split("_")[0]].append(p)
    return dict(groups)


def _build_content(batch: list[Path]) -> list[dict]:
    """Build Anthropic content blocks for a batch of images."""
    content: list[dict] = []
    for idx, image_path in enumerate(batch, start=1):
        content.append({"type": "text", "text": f"Image {idx}\nImage Path: {image_path.name}"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _encode_image(image_path),
            },
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    return content


# ── Batch submission & polling ────────────────────────────────────────────────

async def submit_batch(
    sessions: dict[str, list[Path]],
) -> tuple[str, dict[str, list[list[Path]]]]:
    """Encode all images, build all requests, submit one batch.

    Returns:
        batch_id: Anthropic batch ID for polling
        request_map: custom_id → list of image paths for that sub-batch
    """
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    requests = []
    request_map: dict[str, list[Path]] = {}

    total_images = sum(len(v) for v in sessions.values())
    print(f"Encoding {total_images} images across {len(sessions)} sessions...")
    t0 = time.perf_counter()

    for sid, paths in sessions.items():
        sub_batches = [paths[i: i + BATCH_SIZE] for i in range(0, len(paths), BATCH_SIZE)]
        for b_idx, sub_batch in enumerate(sub_batches):
            custom_id = f"{sid}_b{b_idx}"
            request_map[custom_id] = sub_batch
            requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 4096,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": _build_content(sub_batch)}],
                },
            })

    print(f"Encoding done in {time.perf_counter() - t0:.1f}s. Submitting {len(requests)} requests...")
    batch = await client.messages.batches.create(requests=requests)
    print(f"Batch submitted: {batch.id}  (status: {batch.processing_status})")
    return batch.id, request_map


async def poll_batch(batch_id: str) -> None:
    """Block until the batch reaches a terminal state."""
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"[batch] status={batch.processing_status} | "
            f"processing={counts.processing} succeeded={counts.succeeded} "
            f"errored={counts.errored} canceled={counts.canceled}",
            flush=True,
        )
        if batch.processing_status != "in_progress":
            break
        await asyncio.sleep(POLL_INTERVAL)


async def retrieve_results(batch_id: str) -> dict[str, str]:
    """Fetch results and return custom_id → raw VLM text."""
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    raw_results: dict[str, str] = {}
    async for result in await client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            msg = result.result.message
            raw_results[result.custom_id] = msg.content[0].text if msg.content else ""
        else:
            print(f"[batch] request {result.custom_id} failed: {result.result.type}")
            raw_results[result.custom_id] = ""
    return raw_results


# ── Result processing ─────────────────────────────────────────────────────────

def process_session(
    sid: str,
    paths: list[Path],
    raw_results: dict[str, str],
) -> list[PipelineResult]:
    """Reconstruct pipeline results for one session from batch outputs."""
    sub_batches = [paths[i: i + BATCH_SIZE] for i in range(0, len(paths), BATCH_SIZE)]
    valid_items: list[dict] = []

    for b_idx, sub_batch in enumerate(sub_batches):
        custom_id = f"{sid}_b{b_idx}"
        raw = raw_results.get(custom_id, "").strip()
        if not raw:
            print(f"[{sid}] batch {b_idx}: empty response")
            continue
        try:
            items = [_normalize_item(i) for i in _extract_json_array(raw)]
            if len(items) != len(sub_batch):
                print(f"[{sid}] batch {b_idx}: expected {len(sub_batch)} items, got {len(items)} — skipping")
                continue
            for image_path, item in zip(sub_batch, items):
                item["image_path"] = str(image_path)
                item["tag_text"] = item.get("tag_text") or ""
                valid_items.append(item)
        except Exception as e:
            print(f"[{sid}] batch {b_idx}: parse error — {e}")

    if not valid_items:
        return []

    grouped_items = group_by_tag_similarity(valid_items)

    # Ensure images with no grouped item still get an empty flagged record
    grouped_paths = {item["image_path"] for group in grouped_items for item in group}
    for path in paths:
        if str(path) not in grouped_paths:
            grouped_items.append([{"image_path": str(path), "tag_text": ""}])

    results: list[PipelineResult] = []
    for group in grouped_items:
        group_paths = [item["image_path"] for item in group]
        record = _record_from_group(group, group_paths)
        record, _ = normalize_record(record)
        if not record.brand and not record.product_name and not record.manufacturer:
            continue
        results.append(
            PipelineResult(
                record=record,
                normalized_fields=[],
                duplicate_suggestions=[],
                image_path=group_paths[0],
                image_paths=group_paths,
            )
        )

    print(f"[{sid}] → {len(results)} product(s)")
    return results


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(report: dict, matched: int, total_pred: int, total_gt: int) -> None:
    print(f"\n{'='*60}")
    print(f"EVAL SUMMARY")
    print(f"  Ground truth products : {total_gt}")
    print(f"  Pipeline predictions  : {total_pred}")
    print(f"  Matched pairs         : {matched}")
    print(f"{'='*60}")
    print(f"{'Field':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"{'-'*55}")
    for field, stats in report.items():
        if field == "__overall__":
            continue
        acc = stats["accuracy"]
        acc_str = f"{acc:.1%}" if acc is not None else "  n/a"
        marker = " ✓" if acc and acc >= 0.8 else (" ~" if acc and acc >= 0.5 else "  ")
        print(f"{field:<25} {stats['correct']:>8} {stats['total']:>8} {acc_str:>10}{marker}")
    ov = report["__overall__"]
    print(f"{'-'*55}")
    print(f"{'OVERALL':<25} {ov['correct']:>8} {ov['total']:>8} {ov['accuracy']:.1%}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=str, help="Comma-separated session IDs")
    parser.add_argument("--out",      type=str, help="Save predictions CSV to this path")
    parser.add_argument("--batch-id", type=str, help="Resume a previously submitted batch by ID")
    args = parser.parse_args()

    gt_rows = load_gt(GT_FILE)
    print(f"Loaded {len(gt_rows)} ground truth rows")

    sessions = group_sessions(IMAGE_DIR)
    if args.sessions:
        keep = set(args.sessions.split(","))
        sessions = {k: v for k, v in sessions.items() if k in keep}
    print(f"Running {len(sessions)} sessions\n")

    # Submit or resume
    if args.batch_id:
        batch_id = args.batch_id
        # Rebuild request_map from sessions (same logic as submit_batch)
        request_map: dict[str, list[Path]] = {}
        for sid, paths in sessions.items():
            sub_batches = [paths[i: i + BATCH_SIZE] for i in range(0, len(paths), BATCH_SIZE)]
            for b_idx, sub_batch in enumerate(sub_batches):
                request_map[f"{sid}_b{b_idx}"] = sub_batch
        print(f"Resuming batch {batch_id}")
    else:
        batch_id, request_map = await submit_batch(sessions)

    await poll_batch(batch_id)
    raw_results = await retrieve_results(batch_id)
    print(f"\nRetrieved {len(raw_results)} results")

    # Process all sessions
    predictions: list[dict] = []
    for sid, paths in sessions.items():
        results = process_session(sid, paths, raw_results)
        for r in results:
            predictions.append(dict(r.record.model_dump(), _session=sid))

    print(f"\nTotal predictions: {len(predictions)}")

    # Match and score
    matched_pairs, unmatched = [], []
    for pred in predictions:
        gt = match_to_gt(pred, gt_rows)
        if gt:
            matched_pairs.append((pred, gt))
        else:
            unmatched.append(pred)

    print(f"Matched: {len(matched_pairs)}  |  Unmatched: {len(unmatched)}")
    if unmatched:
        print("Unmatched predictions:")
        for p in unmatched:
            print(f"  brand={p.get('brand')}  barcode={p.get('barcode')}  session={p.get('_session')}")

    report = compute_report(matched_pairs)
    print_report(report, len(matched_pairs), len(predictions), len(gt_rows))

    if args.out:
        pd.DataFrame(predictions).to_csv(args.out, index=False)
        print(f"\nPredictions saved to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
