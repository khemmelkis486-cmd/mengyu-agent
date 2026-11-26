"""Generate real images through the existing local authenticated image endpoint."""

from data.plugins.mengyu_contact import config

import base64
import binascii
import io
import json
from pathlib import Path

import aiofiles
from PIL import Image

SERVICE_URL = config.env_text("MENGYU_SERVICE_URL", "http://127.0.0.1:8317").rstrip("/")
CREDENTIALS = config.portable_path("MENGYU_SERVICE_CREDENTIALS", config.path("credentials.json"))
MODEL = config.env_text("MENGYU_IMAGE_MODEL", "gpt-image-2")


async def generate_image(
    client,
    prompt,
    target,
    environment=None,
):
    """Generate, validate, and exclusively save one real image without retries.

    Args:
        client: HTTP client for the existing local image service.
        prompt: Actual image description.
        target: New destination selected by the activity store.
        environment: Optional fictional scene lighting and weather constraints.

    Returns:
        Generation metadata without credentials or base64 payloads.

    Raises:
        ValueError: The prompt or returned image is invalid.
        FileExistsError: The destination already exists.
        httpx.HTTPError: The generation request fails.
    """
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 6000:
        raise ValueError("Image generation needs a concrete prompt")
    target = Path(target)
    if target.exists():
        raise FileExistsError("Image destination already exists")
    credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8-sig"))
    payload = {
        "model": MODEL,
        "prompt": prompt.strip(),
        "n": 1,
        "size": "1024x1024",
        "quality": "medium",
        "output_format": "png",
    }
    if environment:
        payload["prompt"] += environment["guidance"]
    response = await client.post(
        SERVICE_URL + "/v1/images/generations",
        headers={"Authorization": "Bearer " + credentials["cpa_key"]},
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    items = response.json().get("data") or []
    encoded = items[0].get("b64_json") if items else None
    if not isinstance(encoded, str) or len(encoded) > 40_000_000:
        raise ValueError("Image endpoint returned no usable image")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG":
                raise ValueError("Image endpoint returned an unexpected format")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except (binascii.Error, OSError, SyntaxError) as exc:
        raise ValueError("Image endpoint returned invalid image data") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(target, "xb") as handle:
        await handle.write(raw)
    return {
        "model": MODEL,
        "prompt": payload["prompt"],
        "width": width,
        "height": height,
        "quality": "medium",
        "artifact": str(target),
    }
