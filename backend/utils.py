import base64
import os
from io import BytesIO

from dotenv import load_dotenv
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel, Field

load_dotenv()
use_local: bool = os.getenv("USE_LOCAL_MODEL", "YES") == "YES"
FILE_NAME_LOG = "[backend/utils]"

client = AsyncOpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "qwen3-vl:4b" if use_local else "qwen3-vl:7b"


class VLMImageData(BaseModel):
    img_path: str
    encoded_data: str


class VLMCallParams(BaseModel):
    system_prompt: str
    user_prompt: str
    image_data_list: list[VLMImageData] = Field(default_factory=list)
    description: str = Field(default="VLM call executing ...")


async def vlm_call(params: VLMCallParams):
    print(f"[vlm call] sending request to ollama for {params.description}... ")

    # /no_think MUST be the first token of the user turn for Qwen3 to honour it.
    # Placing it after images (as part of the trailing prompt) is too late —
    # the model has already committed to thinking mode by the time it reads it.
    content: list[dict] = [{"type": "text", "text": "/no_think\n\n"}]
    for idx, image_data in enumerate(params.image_data_list, start=1):
        img_info: str = f"Image {idx}\n Image Path: {image_data.img_path}"
        content.append({"type": "text", "text": img_info})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_data.encoded_data}"
                },
            }
        )
    content.append({"type": "text", "text": params.user_prompt})

    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": params.system_prompt},
            {
                "role": "user",
                "content": content,
            },
        ],
        temperature=0.1,
        max_tokens=2048,
        extra_body={"think": False, "repeat_penalty": 1.1},
    )

    print("[vlm call] got response from ollama")
    return response


def _encode_pil_image(image: Image.Image, format: str = "JPEG") -> str:
    buffer = BytesIO()
    image.save(buffer, format=format)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
