"""
Run the full extraction pipeline over all sessions and measure field-level accuracy
against the ground truth Excel.

Usage:
    python eval/run_eval.py                     # all sessions
    python eval/run_eval.py --limit 5           # first N sessions
    python eval/run_eval.py --out results.csv   # save predictions
"""
import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline import run_pipeline
from eval.metrics import GT_FIELD_MAP, compute_report, match_to_gt, _norm

IMAGE_DIR  = Path(__file__).parent.parent / "imdb_images"
GT_FILE    = Path(__file__).parent / "eval_from_org.xlsx"


def load_gt(path: Path) -> list[dict]:
    df = pd.read_excel(path)
    df.columns = [c.strip() for c in df.columns]
    # normalise barcode to plain string
    def _bc(v):
        if pd.isna(v):
            return None
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


async def run_all(sessions: dict[str, list[Path]]) -> list[dict]:
    predictions = []
    for i, (sid, paths) in enumerate(sessions.items(), 1):
        print(f"[{i}/{len(sessions)}] {sid} ({len(paths)} images) ...", end=" ", flush=True)
        t0 = time.perf_counter()
        try:
            results = await run_pipeline(paths, existing_records=[])
            elapsed = time.perf_counter() - t0
            print(f"{elapsed:.1f}s  → {len(results)} product(s)")
            for r in results:
                d = r.to_dict()
                d["_session"] = sid
                predictions.append(d)
        except Exception as e:
            print(f"ERROR: {e}")
    return predictions


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
    ov_acc = f"{ov['accuracy']:.1%}"
    print(f"{'OVERALL':<25} {ov['correct']:>8} {ov['total']:>8} {ov_acc:>10}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Max sessions to run")
    parser.add_argument("--out",   type=str, help="Save predictions CSV to this path")
    args = parser.parse_args()

    gt_rows = load_gt(GT_FILE)
    print(f"Loaded {len(gt_rows)} ground truth rows")

    sessions = group_sessions(IMAGE_DIR)
    if args.limit:
        sessions = dict(list(sessions.items())[: args.limit])
    print(f"Running {len(sessions)} sessions\n")

    predictions = await run_all(sessions)
    print(f"\nTotal predictions: {len(predictions)}")

    # Match predictions to ground truth
    matched_pairs: list[tuple[dict, dict]] = []
    unmatched: list[dict] = []

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
        df_out = pd.DataFrame(predictions)
        df_out.to_csv(args.out, index=False)
        print(f"\nPredictions saved to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
