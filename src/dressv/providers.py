"""Select the provider for a Dress V process without changing its HTTP API."""

from __future__ import annotations

import os
import re
import time
from io import BytesIO
from typing import Callable

from PIL import Image

from .images import media_type
from .jobs import Payload
from .openai_images import (
    OPENAI_ENDPOINT, ImageEditError, ImageInput, edit_image, read_credential,
    validate_endpoint,
)

Generator = Callable[[Payload, float], bytes]
DEFAULT_MODEL = "gpt-image-2.6-sunburst"


def configured_generator(*, force_mock: bool = False) -> Generator:
    mode = "mock" if force_mock else os.environ.get("DRESSV_PROVIDER", "mock")
    if mode == "mock":
        with BytesIO() as mock:
            Image.new("RGB", (64, 96), (78, 80, 128)).save(mock, "PNG")
            result = mock.getvalue()
        return lambda payload, deadline: result
    if mode != "image-edits":
        raise ImageEditError("Unknown Dress V provider mode")

    endpoint = validate_endpoint(os.environ.get("DRESSV_PROVIDER_ENDPOINT", OPENAI_ENDPOINT))
    model = os.environ.get("DRESSV_PROVIDER_MODEL", DEFAULT_MODEL)
    if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", model):
        raise ImageEditError("Invalid image provider model")
    key = read_credential("provider-api-key")

    def generate(payload: Payload, deadline: float) -> bytes:
        portrait = ImageInput(payload.portrait, media_type(payload.portrait))
        references = tuple(ImageInput(data, media_type(data)) for data in payload.references)
        timeout = min(90, deadline - time.monotonic())
        if timeout <= 0:
            raise ImageEditError("Image job expired before submission")
        return edit_image(
            portrait, references, payload.prompt, model=model, timeout=timeout,
            endpoint=endpoint, api_key=key,
        )

    return generate


def valid_result(payload: Payload, result: bytes, deadline: float) -> bool:
    """Validate the PNG until a visual review provider is wired in."""
    try:
        return media_type(result) == "image/png"
    except ValueError:
        return False
