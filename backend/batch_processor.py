"""Bulk batch processing via Anthropic Message Batches API.

Flow:
  1. submit_bulk_batch() — groups images into sub-batches, submits to the
     Anthropic Batch API, stores the job record in the DB.
  2. poll_pending_jobs() — called every few minutes by the background loop;
     checks job status, processes completed jobs, sends email notifications.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from anthropic import AsyncAnthropic

from backend.db import (
    create_batch_job,
    create_extraction,
    get_user_by_id,
    list_pending_batch_jobs,
    update_batch_job_status,
)
from backend.extractor import (
    EXTRACTION_PROMPT,
    SYSTEM_PROMPT,
    _encode_image,
    _extract_json_array,
    _normalize_item,
    _record_from_group,
)
from backend.image_aggregation import group_by_tag_similarity
from backend.normalizer import normalize_record
from backend.pipeline import PipelineResult

log = logging.getLogger(__name__)

_BATCH_SIZE = int(os.getenv("VLM_BATCH_SIZE", "8"))
_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_SEP = "─" * 60


# ── Request building ──────────────────────────────────────────────────────────

def _build_content(batch: list[Path]) -> list[dict]:
    """Build Anthropic content blocks for one sub-batch of images."""
    content: list[dict] = []
    for idx, path in enumerate(batch, start=1):
        content.append({"type": "text", "text": f"Image {idx}\nImage Path: {path.name}"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _encode_image(path),
            },
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    return content


# ── Submission ────────────────────────────────────────────────────────────────

async def submit_bulk_batch(
    paths: list[Path],
    notify_email: str,
    user_id: int,
) -> str:
    """Encode all images, build Batch API requests, submit, and store the job."""
    log.info(_SEP)
    log.info("[batch:submit] user_id=%d  images=%d  notify=%s",
             user_id, len(paths), notify_email or "none")

    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    sub_batches = [paths[i: i + _BATCH_SIZE] for i in range(0, len(paths), _BATCH_SIZE)]

    log.info("[batch:submit] model=%s  batch_size=%d  sub-batches=%d",
             _MODEL, _BATCH_SIZE, len(sub_batches))
    log.info("[batch:submit] encoding images…")

    t0 = time.perf_counter()
    requests: list[dict] = []
    request_map: dict[str, list[str]] = {}

    for b_idx, sub_batch in enumerate(sub_batches):
        custom_id = f"b{b_idx}"
        request_map[custom_id] = [str(p) for p in sub_batch]
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": _MODEL,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": _build_content(sub_batch)}],
            },
        })
        log.info("[batch:submit]   sub-batch %d/%d  (%d images)  encoded",
                 b_idx + 1, len(sub_batches), len(sub_batch))

    encode_secs = time.perf_counter() - t0
    log.info("[batch:submit] encoding done in %.1fs — submitting %d request(s) to Anthropic…",
             encode_secs, len(requests))

    batch = await client.messages.batches.create(requests=requests)

    job_id = create_batch_job(
        user_id=user_id,
        anthropic_batch_id=batch.id,
        image_paths=paths,
        request_map=request_map,
        notify_email=notify_email or None,
    )

    log.info("[batch:submit] ✓ submitted  anthropic_batch_id=%s", batch.id)
    log.info("[batch:submit] ✓ job stored  job_id=%d", job_id)
    log.info("[batch:submit] status=%s — Anthropic is now processing asynchronously",
             batch.processing_status)
    log.info(_SEP)
    return batch.id


# ── Polling ───────────────────────────────────────────────────────────────────

async def poll_pending_jobs() -> None:
    """Check all pending jobs; process any that have completed."""
    jobs = list_pending_batch_jobs()
    if not jobs:
        log.info("[batch:poll] no pending jobs — nothing to do")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("[batch:poll] ANTHROPIC_API_KEY not set — skipping poll")
        return

    log.info(_SEP)
    log.info("[batch:poll] woke up — %d pending job(s) to check", len(jobs))

    client = AsyncAnthropic(api_key=api_key)

    for job in jobs:
        job_id = job["id"]
        batch_id = job["anthropic_batch_id"]
        n_images = len(json.loads(job.get("image_paths_json") or "[]"))

        log.info("[batch:poll] checking job_id=%d  batch=%s  (%d images)",
                 job_id, batch_id, n_images)

        try:
            batch = await client.messages.batches.retrieve(batch_id)
        except Exception as exc:
            log.error("[batch:poll]   ✗ retrieve failed: %s", exc)
            continue

        counts = batch.request_counts
        log.info(
            "[batch:poll]   status=%-12s  processing=%-4s succeeded=%-4s "
            "errored=%-4s canceled=%s",
            batch.processing_status,
            counts.processing, counts.succeeded,
            counts.errored, counts.canceled,
        )

        if batch.processing_status == "in_progress":
            log.info("[batch:poll]   still running — will check again next cycle")
            continue

        log.info("[batch:poll]   terminal state detected — starting result processing")
        try:
            await _process_completed_job(client, job)
        except Exception as exc:
            log.exception("[batch:poll]   ✗ processing failed: %s", exc)
            update_batch_job_status(job_id, "failed", error_message=str(exc)[:500])

    log.info("[batch:poll] cycle complete")
    log.info(_SEP)


# ── Result processing ─────────────────────────────────────────────────────────

async def _process_completed_job(client: AsyncAnthropic, job: dict) -> None:
    """Fetch results, run the extraction pipeline, save records, send email."""
    job_id = job["id"]
    batch_id = job["anthropic_batch_id"]
    request_map: dict[str, list[str]] = json.loads(job["request_map_json"])
    total_requests = len(request_map)

    log.info("[batch:process] job_id=%d  batch=%s", job_id, batch_id)
    log.info("[batch:process] fetching results for %d request(s)…", total_requests)

    # ── Fetch raw VLM text ─────────────────────────────────────────────────────
    raw_results: dict[str, str] = {}
    succeeded = 0
    failed = 0
    async for result in await client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            msg = result.result.message
            raw_results[result.custom_id] = msg.content[0].text if msg.content else ""
            succeeded += 1
        else:
            log.warning("[batch:process]   request %s — result type: %s",
                        result.custom_id, result.result.type)
            raw_results[result.custom_id] = ""
            failed += 1

    log.info("[batch:process] results fetched — succeeded=%d  failed=%d",
             succeeded, failed)

    # ── Parse per-image items ──────────────────────────────────────────────────
    log.info("[batch:process] parsing VLM output…")
    all_items: list[dict] = []
    parse_ok = 0
    parse_skip = 0

    for custom_id, path_strs in sorted(request_map.items(), key=lambda kv: int(kv[0][1:])):
        sub_batch = [Path(p) for p in path_strs]
        raw = raw_results.get(custom_id, "").strip()

        if not raw:
            log.warning("[batch:process]   %s — empty response, skipping %d image(s)",
                        custom_id, len(sub_batch))
            parse_skip += len(sub_batch)
            continue

        try:
            items = [_normalize_item(i) for i in _extract_json_array(raw)]
            if len(items) != len(sub_batch):
                log.warning("[batch:process]   %s — expected %d items, got %d — skipping",
                            custom_id, len(sub_batch), len(items))
                parse_skip += len(sub_batch)
                continue

            for path, item in zip(sub_batch, items):
                item["image_path"] = str(path)
                item["tag_text"] = item.get("tag_text") or ""
                all_items.append(item)

            parse_ok += len(sub_batch)
            log.info("[batch:process]   %s — parsed %d item(s)  brand=%s",
                     custom_id, len(items),
                     ", ".join({i.get("brand", "?") for i in items if i.get("brand")}) or "n/a")

        except Exception as exc:
            log.warning("[batch:process]   %s — parse error: %s", custom_id, exc)
            parse_skip += len(sub_batch)

    log.info("[batch:process] parse complete — ok=%d  skipped=%d  total_items=%d",
             parse_ok, parse_skip, len(all_items))

    if not all_items:
        log.warning("[batch:process] ✗ no parseable items — marking job completed with 0 results")
        update_batch_job_status(job_id, "completed", result_count=0)
        return

    # ── Group → records ────────────────────────────────────────────────────────
    log.info("[batch:process] grouping %d items by product similarity…", len(all_items))
    grouped = group_by_tag_similarity(all_items)
    log.info("[batch:process] grouped into %d product group(s)", len(grouped))

    results: list[PipelineResult] = []
    for g_idx, group in enumerate(grouped):
        group_paths = [item["image_path"] for item in group]
        try:
            record = _record_from_group(group, group_paths)
            record, norm_fields = normalize_record(record)
            if not record.brand and not record.product_name and not record.manufacturer:
                log.info("[batch:process]   group %d — empty record, skipping", g_idx)
                continue
        except Exception as exc:
            log.warning("[batch:process]   group %d — record build failed: %s", g_idx, exc)
            continue

        log.info("[batch:process]   group %d — brand=%-16s  product=%s",
                 g_idx,
                 (record.brand or "?")[:16],
                 (record.product_name or "?")[:40])

        results.append(
            PipelineResult(
                record=record,
                normalized_fields=norm_fields,
                duplicate_suggestions=[],
                image_path=group_paths[0],
                image_paths=group_paths,
            )
        )

    # ── Persist to DB ──────────────────────────────────────────────────────────
    log.info("[batch:process] saving %d product(s) to database…", len(results))
    user_id = job["user_id"]
    for result in results:
        create_extraction(
            user_id=user_id,
            original_filename=f"[Batch #{job_id}] {Path(result.image_path).name}",
            result=result,
        )

    result_count = len(results)
    update_batch_job_status(job_id, "completed", result_count=result_count)
    log.info("[batch:process] ✓ job_id=%d complete — %d product(s) saved to user_id=%d",
             job_id, result_count, user_id)

    # ── Email notification ─────────────────────────────────────────────────────
    notify_email = job.get("notify_email") or ""
    if notify_email:
        user = get_user_by_id(user_id)
        username = user["username"] if user else "User"
        log.info("[batch:process] sending completion email to %s…", notify_email)
        try:
            from backend.email_service import send_batch_complete
            send_batch_complete(notify_email, username, result_count, job_id)
            log.info("[batch:process] ✓ email sent to %s", notify_email)
        except Exception as exc:
            log.warning("[batch:process] ✗ email failed: %s", exc)
    else:
        log.info("[batch:process] no notify email set — skipping email")
