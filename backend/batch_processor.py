"""Bulk batch processing via provider Batch APIs.

Providers:
  - anthropic: Anthropic Message Batches API (async, 24h turnaround, 50% cheaper)
  - openai:    OpenAI Batch API via JSONL file upload (/v1/chat/completions)
  - gemini:    Concurrent inline calls (native Batch API requires Vertex AI billing)

Flow:
  1. submit_bulk_batch() -- dispatches to the right provider, stores the job.
  2. poll_pending_jobs() -- background loop; checks status, processes completed
     jobs, sends email.
"""
from __future__ import annotations

import asyncio
import uuid
import json
import logging
import os
import tempfile
from pathlib import Path

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from backend.db import (
    create_batch_job,
    create_extraction,
    get_user_by_id,
    list_pending_batch_jobs,
    update_batch_job_status,
)
from backend.extractor import (
    EXTRACTION_PROMPT,
    MODEL_OPTIONS,
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

_BATCH_SIZE      = int(os.getenv("VLM_BATCH_SIZE", "8"))
_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_SEP = "─" * 60


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_items(raw: str, expected: int, label: str) -> list[dict] | None:
    if not raw.strip():
        return None
    try:
        items = [_normalize_item(i) for i in _extract_json_array(raw)]
        if len(items) != expected:
            log.warning("[batch] %s — expected %d items, got %d", label, expected, len(items))
            return None
        return items
    except Exception as exc:
        log.warning("[batch] %s — parse error: %s", label, exc)
        return None


def _model_id_for_provider(provider: str) -> str:
    from backend.utils import ANTHROPIC_MODEL, GEMINI_MODEL, OPENAI_MODEL
    return {"anthropic": ANTHROPIC_MODEL, "openai": OPENAI_MODEL, "gemini": GEMINI_MODEL}.get(
        provider, ANTHROPIC_MODEL
    )


def _build_results(
    request_map: dict[str, list[str]],
    raw_results: dict[str, str],
    token_usage: dict[str, tuple[int, int]] | None = None,
    model_id: str = "",
) -> tuple[list[PipelineResult], int, list[str]]:
    """Turn raw VLM text into grouped PipelineResults. Returns (results, n_skipped, skipped_names)."""
    from backend.extractor import calculate_cost

    # Per-image cost: distribute each sub-batch's cost evenly across its images.
    image_cost: dict[str, float] = {}
    if token_usage and model_id:
        for cid, path_strs in request_map.items():
            if cid in token_usage and path_strs:
                in_tok, out_tok = token_usage[cid]
                per_img = calculate_cost(in_tok, out_tok, model_id) / len(path_strs)
                for p in path_strs:
                    image_cost[p] = per_img

    all_items: list[dict] = []
    n_skip = 0
    skip_names: list[str] = []

    for custom_id, path_strs in sorted(request_map.items(), key=lambda kv: int(kv[0][1:])):
        sub = [Path(p) for p in path_strs]
        items = _parse_items(raw_results.get(custom_id, ""), len(sub), custom_id)
        if items is None:
            n_skip += len(sub)
            skip_names.extend(Path(p).name for p in sub)
            continue
        for path, item in zip(sub, items):
            item["image_path"] = str(path)
            item["tag_text"] = item.get("tag_text") or ""
            all_items.append(item)

    results: list[PipelineResult] = []
    for g_idx, group in enumerate(group_by_tag_similarity(all_items)):
        group_paths = [item["image_path"] for item in group]
        try:
            record = _record_from_group(group, group_paths)
            record, norm_fields = normalize_record(record)
            if not record.brand and not record.product_name and not record.manufacturer:
                n_skip += len(group_paths)
                skip_names.extend(Path(p).name for p in group_paths)
                continue
        except Exception as exc:
            log.warning("[batch] group %d failed: %s", g_idx, exc)
            n_skip += len(group_paths)
            skip_names.extend(Path(p).name for p in group_paths)
            continue
        group_cost = sum(image_cost.get(p, 0.0) for p in group_paths)
        results.append(PipelineResult(
            record=record,
            normalized_fields=norm_fields,
            duplicate_suggestions=[],
            image_path=group_paths[0],
            image_paths=group_paths,
            cost_usd=group_cost,
            model_used=model_id,
        ))

    return results, n_skip, skip_names


def _persist_results(job: dict, results: list[PipelineResult], n_skip: int, skip_names: list[str]) -> None:
    job_id = job["id"]
    for r in results:
        create_extraction(
            user_id=job["user_id"],
            original_filename=f"[Batch #{job_id}] {Path(r.image_path).name}",
            result=r,
            source="batch",
            batch_job_id=job_id,
            barcode_audit=r.barcode_audit,
        )
    update_batch_job_status(job_id, "completed",
                            result_count=len(results),
                            skipped_count=n_skip,
                            skipped_names=skip_names or None)
    log.info("[batch] job_id=%d — %d extracted, %d skipped", job_id, len(results), n_skip)


async def _notify(job: dict, result_count: int) -> None:
    email = job.get("notify_email") or ""
    if not email:
        return
    user = get_user_by_id(job["user_id"])
    try:
        from backend.email_service import send_batch_complete
        send_batch_complete(email, (user or {}).get("username", "User"), result_count, job["id"])
        log.info("[batch] email sent to %s", email)
    except Exception as exc:
        log.warning("[batch] email failed: %s", exc)


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _anthropic_content(batch: list[Path]) -> list[dict]:
    content: list[dict] = []
    for idx, path in enumerate(batch, 1):
        content.append({"type": "text", "text": f"Image {idx}\nImage Path: {path.name}"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": _encode_image(path),
        }})
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    return content


async def _submit_anthropic_batch(paths: list[Path], notify_email: str, user_id: int, model: str) -> str:
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    sub_batches = [paths[i:i + _BATCH_SIZE] for i in range(0, len(paths), _BATCH_SIZE)]
    requests, request_map = [], {}
    for i, sub in enumerate(sub_batches):
        cid = f"b{i}"
        request_map[cid] = [str(p) for p in sub]
        requests.append({"custom_id": cid, "params": {
            "model": model, "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _anthropic_content(sub)}],
        }})
    batch = await client.messages.batches.create(requests=requests)
    job_id = create_batch_job(user_id=user_id, anthropic_batch_id=batch.id,
                              image_paths=paths, request_map=request_map,
                              notify_email=notify_email or None, provider="anthropic")
    log.info("[batch:anthropic] submitted %s  job_id=%d  model=%s", batch.id, job_id, model)
    return batch.id


async def _process_anthropic(job: dict) -> None:
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    request_map = json.loads(job["request_map_json"])
    raw: dict[str, str] = {}
    token_usage: dict[str, tuple[int, int]] = {}
    async for result in await client.messages.batches.results(job["anthropic_batch_id"]):
        if result.result.type == "succeeded":
            msg = result.result.message
            raw[result.custom_id] = msg.content[0].text if msg.content else ""
            if hasattr(msg, "usage") and msg.usage:
                token_usage[result.custom_id] = (msg.usage.input_tokens, msg.usage.output_tokens)
        else:
            log.warning("[batch:anthropic] request %s: %s", result.custom_id, result.result.type)
            raw[result.custom_id] = ""
    model_id = _model_id_for_provider("anthropic")
    results, n_skip, names = _build_results(request_map, raw, token_usage, model_id)
    _persist_results(job, results, n_skip, names)
    await _notify(job, len(results))


# ── OpenAI-compat JSONL (OpenAI + Gemini) ────────────────────────────────────

def _openai_compat_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _jsonl_request(custom_id: str, sub_batch: list[Path], model: str) -> str:
    """Build one JSONL line for a chat completions batch request with vision."""
    content: list[dict] = []
    for idx, path in enumerate(sub_batch, 1):
        content.append({"type": "text", "text": f"Image {idx}\nImage Path: {path.name}"})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{_encode_image(path)}", "detail": "high",
        }})
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    return json.dumps({
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": content},
            ],
            "max_completion_tokens": 16384,
        },
    })


async def _submit_openai_batch(
    paths: list[Path], notify_email: str, user_id: int, model: str
) -> str:
    client = _openai_compat_client()
    sub_batches = [paths[i:i + _BATCH_SIZE] for i in range(0, len(paths), _BATCH_SIZE)]
    request_map: dict[str, list[str]] = {}
    lines: list[str] = []
    for i, sub in enumerate(sub_batches):
        cid = f"b{i}"
        request_map[cid] = [str(p) for p in sub]
        lines.append(_jsonl_request(cid, sub, model))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(lines))
        tmp = f.name
    try:
        with open(tmp, "rb") as f:
            uploaded = await client.files.create(file=f, purpose="batch")
    finally:
        os.unlink(tmp)

    batch = await client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    job_id = create_batch_job(user_id=user_id, anthropic_batch_id=batch.id,
                              image_paths=paths, request_map=request_map,
                              notify_email=notify_email or None, provider="openai")
    log.info("[batch:openai] submitted %s  job_id=%d  model=%s", batch.id, job_id, model)
    return batch.id


async def _process_openai_batch(job: dict) -> None:
    client = _openai_compat_client()
    batch = await client.batches.retrieve(job["anthropic_batch_id"])
    if not batch.output_file_id:
        raise ValueError(f"{provider} batch {job['anthropic_batch_id']} has no output_file_id")
    content = await client.files.content(batch.output_file_id)
    request_map = json.loads(job["request_map_json"])
    raw: dict[str, str] = {}
    token_usage: dict[str, tuple[int, int]] = {}
    for line in content.text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        cid = obj.get("custom_id", "")
        try:
            text = obj["response"]["body"]["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            text = ""
        raw[cid] = text
        try:
            usage = obj["response"]["body"]["usage"]
            token_usage[cid] = (usage["prompt_tokens"], usage["completion_tokens"])
        except (KeyError, TypeError):
            pass
    model_id = _model_id_for_provider("openai")
    results, n_skip, names = _build_results(request_map, raw, token_usage, model_id)
    _persist_results(job, results, n_skip, names)
    await _notify(job, len(results))


# ── Gemini (concurrent inline calls -- native Batch API needs Vertex AI billing) ─

async def _submit_gemini_batch(paths: list[Path], notify_email: str, user_id: int, model: str) -> str:
    sub_batches = [paths[i:i + _BATCH_SIZE] for i in range(0, len(paths), _BATCH_SIZE)]
    request_map = {f"b{i}": [str(p) for p in sub] for i, sub in enumerate(sub_batches)}

    fake_id = f"gemini-inline-{uuid.uuid4().hex[:16]}"
    job_id = create_batch_job(
        user_id=user_id,
        anthropic_batch_id=fake_id,
        image_paths=paths,
        request_map=request_map,
        notify_email=notify_email or None,
        provider="gemini",
    )
    log.info("[batch:gemini] queued inline  job_id=%d  model=%s  images=%d", job_id, model, len(paths))

    job_record = {
        "id": job_id,
        "user_id": user_id,
        "notify_email": notify_email,
        "anthropic_batch_id": fake_id,
        "request_map_json": json.dumps(request_map),
    }
    asyncio.create_task(_run_gemini_inline(job_record, sub_batches, model))
    return fake_id


async def _run_gemini_inline(job: dict, sub_batches: list[list[Path]], model: str) -> None:
    """Process Gemini sub-batches concurrently via the standard completions API."""
    from backend.utils import VLMCallParams, VLMImageData, vlm_call_w_gemini

    async def _one(cid: str, sub: list[Path]) -> tuple[str, str, tuple[int, int]]:
        image_data_list = [
            VLMImageData(
                img_path=p.name,
                encoded_data=await asyncio.to_thread(_encode_image, p),
            )
            for p in sub
        ]
        params = VLMCallParams(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=EXTRACTION_PROMPT,
            image_data_list=image_data_list,
            description=f"gemini-batch-{cid}",
            model_override=model,
        )
        try:
            resp = await vlm_call_w_gemini(params)
            text = resp.choices[0].message.content or ""
            tokens: tuple[int, int] = (resp.input_tokens, resp.output_tokens)
        except Exception as exc:
            log.warning("[batch:gemini] inline %s failed: %s", cid, exc)
            text = ""
            tokens = (0, 0)
        return cid, text, tokens

    tasks = [_one(f"b{i}", sub) for i, sub in enumerate(sub_batches)]
    raw: dict[str, str] = {}
    token_usage: dict[str, tuple[int, int]] = {}
    try:
        for cid, text, tokens in await asyncio.gather(*tasks):
            raw[cid] = text
            token_usage[cid] = tokens
    except Exception as exc:
        log.error("[batch:gemini] inline processing failed: %s", exc)
        update_batch_job_status(job["id"], "failed", error_message=str(exc))
        return

    request_map = json.loads(job["request_map_json"])
    model_id = _model_id_for_provider("gemini")
    results, n_skip, names = _build_results(request_map, raw, token_usage, model_id)
    _persist_results(job, results, n_skip, names)
    await _notify(job, len(results))


async def _poll_gemini(job: dict) -> None:
    if job["anthropic_batch_id"].startswith("gemini-inline-"):
        # Inline jobs are processed by a background asyncio task; nothing to poll.
        return
    log.warning("[batch:gemini] unknown batch ID format: %s", job["anthropic_batch_id"])


# ── Video batch (background asyncio task, no external API) ────────────────────

async def submit_video_batch(
    paths: list[Path],
    notify_email: str,
    user_id: int,
    model_display_name: str | None = None,
) -> str:
    """Queue a video batch job that runs entirely as a background asyncio task.

    Each video is processed independently: frames are extracted, the sharpest
    ones selected, then all frames are sent together to the VLM as a single
    multi-view extraction call.  Results are persisted and an email sent when
    the whole job finishes.

    Uses prefix 'video-batch-' on the fake job ID so the poll loop skips it,
    the same pattern used for Gemini inline jobs.
    """
    from backend.extractor import get_default_display_name

    display_name = model_display_name or get_default_display_name()
    fake_id = f"video-batch-{uuid.uuid4().hex[:16]}"

    # request_map is not used for video jobs; store empty dict to satisfy schema.
    job_id = create_batch_job(
        user_id=user_id,
        anthropic_batch_id=fake_id,
        image_paths=paths,
        request_map={},
        notify_email=notify_email or None,
        provider="video",
    )
    log.info("[batch:video] queued  job_id=%d  model=%s  videos=%d", job_id, display_name, len(paths))

    job_record = {
        "id": job_id,
        "user_id": user_id,
        "notify_email": notify_email,
        "anthropic_batch_id": fake_id,
    }
    asyncio.create_task(_run_video_batch(job_record, paths, display_name))
    return fake_id


async def _run_video_batch(job: dict, video_paths: list[Path], model_display_name: str) -> None:
    """Background task: process each video and persist results when all are done.

    Skips individual videos that cannot be read or produce no frames, and
    counts them as skipped in the final job status.
    """
    from backend.extractor import MODEL_OPTIONS, extract_from_frames
    from backend.video_processor import extract_frames_from_video_async, select_best_frames_async

    backend_name, model_id = MODEL_OPTIONS.get(
        model_display_name, next(iter(MODEL_OPTIONS.values()))
    )

    job_id = job["id"]
    extracted: list[tuple[Path, "PipelineResult"]] = []
    n_skip = 0
    skip_names: list[str] = []

    for video_path in video_paths:
        log.info("[batch:video] job_id=%d  processing %s", job_id, video_path.name)
        try:
            frame_dir = video_path.parent / f"frames_{video_path.stem}"
            raw_frames = await extract_frames_from_video_async(video_path, frame_dir)
            if not raw_frames:
                raise ValueError("no frames extracted")

            best_frames = await select_best_frames_async(raw_frames, max_frames=12)
            name_hint = video_path.stem.replace("_", " ")
            result = await extract_from_frames(
                frames=best_frames,
                product_name=name_hint,
                backend=backend_name,
                model_id=model_id,
            )
            extracted.append((video_path, result))
        except Exception as exc:
            log.warning("[batch:video] job_id=%d  %s failed: %s", job_id, video_path.name, exc)
            n_skip += 1
            skip_names.append(video_path.name)

    # Persist all successful extractions under the video batch job.
    for video_path, result in extracted:
        create_extraction(
            user_id=job["user_id"],
            original_filename=f"[Video Batch #{job_id}] {video_path.name}",
            result=result,
            source="video",
            batch_job_id=job_id,
            video_path=str(video_path),
            barcode_audit=result.barcode_audit,
        )

    update_batch_job_status(
        job_id, "completed",
        result_count=len(extracted),
        skipped_count=n_skip,
        skipped_names=skip_names or None,
    )
    log.info("[batch:video] job_id=%d — %d extracted, %d skipped", job_id, len(extracted), n_skip)
    await _notify(job, len(extracted))


# ── Entry points ──────────────────────────────────────────────────────────────

async def submit_bulk_batch(
    paths: list[Path],
    notify_email: str,
    user_id: int,
    model_display_name: str | None = None,
) -> str:
    """Submit images to the appropriate provider's Batch API."""
    backend = "anthropic"
    model_id = _ANTHROPIC_MODEL
    if model_display_name and model_display_name in MODEL_OPTIONS:
        backend, model_id = MODEL_OPTIONS[model_display_name]

    log.info(_SEP)
    log.info("[batch:submit] provider=%s model=%s images=%d user_id=%d",
             backend, model_id, len(paths), user_id)

    if backend == "openai":
        return await _submit_openai_batch(paths, notify_email, user_id, model_id)
    elif backend == "gemini":
        return await _submit_gemini_batch(paths, notify_email, user_id, model_id)
    else:
        return await _submit_anthropic_batch(paths, notify_email, user_id, model_id)


async def poll_pending_jobs() -> None:
    """Check all pending cloud jobs; process any that are done."""
    jobs = list_pending_batch_jobs()
    if not jobs:
        log.info("[batch:poll] no pending jobs")
        return

    log.info(_SEP)
    log.info("[batch:poll] %d pending job(s)", len(jobs))

    for job in jobs:
        job_id   = job["id"]
        provider = job.get("provider") or "anthropic"
        batch_id = job["anthropic_batch_id"]

        # Video batch jobs and Gemini inline jobs are driven by background
        # asyncio tasks and self-complete -- there is nothing to poll.
        if provider == "video" or batch_id.startswith("video-batch-"):
            log.info("[batch:poll] job_id=%d  provider=video — handled by background task", job_id)
            continue

        log.info("[batch:poll] job_id=%d  provider=%s  batch=%s", job_id, provider, batch_id)
        try:
            if provider == "anthropic":
                await _poll_anthropic(job)
            elif provider == "gemini":
                await _poll_gemini(job)
            else:
                await _poll_openai(job)
        except Exception as exc:
            log.exception("[batch:poll] job_id=%d failed: %s", job_id, exc)
            update_batch_job_status(job_id, "failed", error_message=str(exc)[:500])

    log.info("[batch:poll] cycle complete")
    log.info(_SEP)


async def _poll_anthropic(job: dict) -> None:
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    batch = await client.messages.batches.retrieve(job["anthropic_batch_id"])
    c = batch.request_counts
    log.info("[batch:anthropic] status=%s  succeeded=%s  errored=%s",
             batch.processing_status, c.succeeded, c.errored)
    if batch.processing_status == "in_progress":
        return
    await _process_anthropic(job)


async def _poll_openai(job: dict) -> None:
    client = _openai_compat_client()
    batch = await client.batches.retrieve(job["anthropic_batch_id"])
    log.info("[batch:openai] status=%s", batch.status)
    if batch.status not in {"completed", "failed", "expired", "cancelled"}:
        return
    if batch.status != "completed":
        raise ValueError(f"OpenAI batch ended with status: {batch.status}")
    await _process_openai_batch(job)
