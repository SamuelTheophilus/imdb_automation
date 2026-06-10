import base64
import os
from dataclasses import dataclass
from io import BytesIO

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel, Field

load_dotenv()

# Model name is set via the VL_MODEL env var so it can be swapped without
# touching code. Defaults to qwen3-vl:4b for backward compatibility, but
# llama3.2-vision:11b is the recommended model for this pipeline.
MODEL: str = os.getenv("VL_MODEL", "qwen3-vl:4b")

# OpenAI-compatible client pointed at local Ollama. Used only as a fallback
# reference — active extraction goes through vlm_call_w_ollama.
client = AsyncOpenAI(base_url="http://localhost:11434/v1", api_key="ollama")


class VLMImageData(BaseModel):
    """One encoded image to include in a VLM request."""
    img_path: str
    encoded_data: str  # base64-encoded JPEG bytes


class VLMCallParams(BaseModel):
    """All inputs needed to fire one VLM request."""
    system_prompt: str
    user_prompt: str
    image_data_list: list[VLMImageData] = Field(default_factory=list)
    description: str = Field(default="VLM call executing ...")
    # When set, Ollama enforces this JSON schema on the response, eliminating
    # parse failures. llama3.2-vision supports this; not all models do.
    format_schema: dict | None = Field(default=None)


# ── Compat wrapper returned by vlm_call_w_ollama ───────────────────────────
# Mirrors the shape of an OpenAI ChatCompletion response so callers don't
# need separate code paths for native vs OpenAI-compat responses.

@dataclass
class _NativeMessage:
    content: str
    reasoning: str = ""

@dataclass
class _NativeChoice:
    message: _NativeMessage
    finish_reason: str = "stop"

@dataclass
class _NativeResponse:
    choices: list


async def vlm_call_w_ollama(params: VLMCallParams) -> _NativeResponse:
    """Fire one extraction request against Ollama's native /api/chat endpoint.

    Using the native endpoint (rather than the OpenAI-compat /v1 layer) lets us
    pass `think: false` and `format` (structured output schema) as top-level
    request fields, which the compat layer does not reliably forward.

    Returns a _NativeResponse whose shape matches OpenAI ChatCompletion so the
    rest of the pipeline can treat both interchangeably.
    """
    print(
        f"[vlm call] sending request to ollama (native) for "
        f"{params.description}... {MODEL} in use....."
    )

    # Build the message: interleave path labels and base64 images so the model
    # knows which image each label refers to.
    text_parts = []
    images = []
    for idx, image_data in enumerate(params.image_data_list, start=1):
        text_parts.append(f"Image {idx}\n Image Path: {image_data.img_path}")
        images.append(image_data.encoded_data)
    text_parts.append(params.user_prompt)

    payload: dict = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": params.system_prompt},
            {"role": "user", "content": "\n\n".join(text_parts), "images": images},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,     # low temperature for deterministic extraction
            "repeat_penalty": 1.1,  # mild penalty reduces repetitive filler tokens
            "num_predict": 2048,
        },
    }

    # Attach the JSON schema when provided. Ollama uses grammar-based sampling
    # to guarantee the response matches the schema exactly.
    if params.format_schema:
        payload["format"] = params.format_schema

    async with httpx.AsyncClient(timeout=300.0) as http_client:
        resp = await http_client.post("http://localhost:11434/api/chat", json=payload)
        if resp.status_code != 200:
            print(f"[vlm call] Ollama error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

    msg = data.get("message", {})
    content = msg.get("content", "")
    print(f"[vlm call] got response from ollama (native) | content_len={len(content)}")
    return _NativeResponse(choices=[_NativeChoice(message=_NativeMessage(content=content))])


def _encode_pil_image(image: Image.Image, format: str = "JPEG") -> str:
    """Base64-encode a PIL image for embedding in a VLM request payload."""
    buffer = BytesIO()
    image.save(buffer, format=format)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
