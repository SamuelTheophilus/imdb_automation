import base64
import os
from dataclasses import dataclass
from io import BytesIO

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel, Field

load_dotenv()

# Ollama model, set via VL_MODEL env var.
MODEL: str = os.getenv("VL_MODEL", "qwen2.5vl:32b")

# OpenAI model, set via OPENAI_VL_MODEL env var.
OPENAI_MODEL: str = os.getenv("OPENAI_VL_MODEL", "gpt-4o")

# Gemini model, set via GEMINI_MODEL env var.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Anthropic model, set via ANTHROPIC_MODEL env var.
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


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


async def vlm_call_w_openai(params: VLMCallParams) -> _NativeResponse:
    """Fire one extraction request against the OpenAI API.

    Images are sent as base64 data-URLs. Returns the same _NativeResponse
    shape as vlm_call_w_ollama so the rest of the pipeline is unchanged.
    """
    print(
        f"[vlm call] sending request to openai for "
        f"{params.description}... {OPENAI_MODEL} in use....."
    )

    # Build a content array with interleaved image labels and base64 images.
    content: list[dict] = []
    for idx, image_data in enumerate(params.image_data_list, start=1):
        content.append({
            "type": "text",
            "text": f"Image {idx}\nImage Path: {image_data.img_path}",
        })
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{image_data.encoded_data}",
                "detail": "high",
            },
        })
    content.append({"type": "text", "text": params.user_prompt})

    messages = [
        {"role": "system", "content": params.system_prompt},
        {"role": "user",   "content": content},
    ]

    kwargs: dict = {
        "model":                 OPENAI_MODEL,
        "messages":              messages,
        "max_completion_tokens": 4096,
    }

    if params.format_schema:
        kwargs["response_format"] = {"type": "json_object"}

    openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = await openai_client.chat.completions.create(**kwargs)

    content_out = response.choices[0].message.content or ""
    usage = response.usage
    if usage:
        print(
            f"[vlm call] got response from openai | "
            f"prompt_tokens={usage.prompt_tokens} "
            f"completion_tokens={usage.completion_tokens} "
            f"total_tokens={usage.total_tokens} | "
            f"content_len={len(content_out)}"
        )
    else:
        print(f"[vlm call] got response from openai | content_len={len(content_out)}")
    return _NativeResponse(choices=[_NativeChoice(message=_NativeMessage(content=content_out))])


async def vlm_call_w_gemini(params: VLMCallParams) -> _NativeResponse:
    """Fire one extraction request against the Gemini API via its OpenAI-compatible endpoint.

    Google exposes an OpenAI-format endpoint so we can reuse the same client
    and message structure without adding extra dependencies.
    """
    print(
        f"[vlm call] sending request to gemini for "
        f"{params.description}... {GEMINI_MODEL} in use....."
    )

    content: list[dict] = []
    for idx, image_data in enumerate(params.image_data_list, start=1):
        content.append({
            "type": "text",
            "text": f"Image {idx}\nImage Path: {image_data.img_path}",
        })
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{image_data.encoded_data}",
            },
        })
    content.append({"type": "text", "text": params.user_prompt})

    messages = [
        {"role": "system", "content": params.system_prompt},
        {"role": "user",   "content": content},
    ]

    kwargs: dict = {
        "model":      GEMINI_MODEL,
        "messages":   messages,
        "max_tokens": 4096,
    }

    if params.format_schema:
        kwargs["response_format"] = {"type": "json_object"}

    gemini_client = AsyncOpenAI(
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    response = await gemini_client.chat.completions.create(**kwargs)

    content_out = response.choices[0].message.content or ""
    usage = response.usage
    if usage:
        print(
            f"[vlm call] got response from gemini | "
            f"prompt_tokens={usage.prompt_tokens} "
            f"completion_tokens={usage.completion_tokens} "
            f"total_tokens={usage.total_tokens} | "
            f"content_len={len(content_out)}"
        )
    else:
        print(f"[vlm call] got response from gemini | content_len={len(content_out)}")
    return _NativeResponse(choices=[_NativeChoice(message=_NativeMessage(content=content_out))])


async def vlm_call_w_anthropic(params: VLMCallParams) -> _NativeResponse:
    """Fire one extraction request against the Anthropic API (Claude).

    Images are sent as base64-encoded JPEG blocks interleaved with text labels.
    The system prompt is passed as the top-level `system` parameter (Anthropic's
    preferred placement, rather than a system-role message).
    """
    print(
        f"[vlm call] sending request to anthropic for "
        f"{params.description}... {ANTHROPIC_MODEL} in use....."
    )

    content: list[dict] = []
    for idx, image_data in enumerate(params.image_data_list, start=1):
        content.append({
            "type": "text",
            "text": f"Image {idx}\nImage Path: {image_data.img_path}",
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_data.encoded_data,
            },
        })
    content.append({"type": "text", "text": params.user_prompt})

    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=params.system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    content_out = response.content[0].text if response.content else ""
    usage = response.usage
    print(
        f"[vlm call] got response from anthropic | "
        f"input_tokens={usage.input_tokens} "
        f"output_tokens={usage.output_tokens} | "
        f"content_len={len(content_out)}"
    )
    return _NativeResponse(choices=[_NativeChoice(message=_NativeMessage(content=content_out))])


def _encode_pil_image(image: Image.Image, format: str = "JPEG") -> str:
    """Base64-encode a PIL image for embedding in a VLM request payload."""
    buffer = BytesIO()
    image.save(buffer, format=format)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
