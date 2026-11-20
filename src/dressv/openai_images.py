"""Submit one image edit without writing images or API credentials to disk."""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OPENAI_ENDPOINT = "https://api.openai.com/v1/images/edits"
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * ((_MAX_OUTPUT_BYTES + 2) // 3) + 64 * 1024


class ImageEditError(RuntimeError):
    """A sanitized provider failure. The adapter never retries a submission."""


@dataclass(frozen=True)
class ImageInput:
    content: bytes
    media_type: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_key() -> str:
    return read_credential("openai-api-key")


def read_credential(name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise ImageEditError("Image provider credential is unavailable")
    try:
        with (Path(directory) / name).open(encoding="ascii") as credential:
            key = credential.read(4098).strip()
    except (OSError, UnicodeError):
        raise ImageEditError("Image provider credential is unavailable") from None
    if not 1 <= len(key) <= 4096 or not all(33 <= ord(char) <= 126 for char in key):
        raise ImageEditError("Image provider credential has invalid format")
    return key


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ImageEditError("Invalid Images Edits endpoint") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or not parsed.path.startswith("/")
        or port == 0
        or any(not 33 <= ord(char) <= 126 or char in "\\?#" for char in value)
    ):
        raise ImageEditError("Invalid Images Edits endpoint")
    return value


def edit_image(
    portrait: ImageInput,
    references: tuple[ImageInput, ...],
    prompt: str,
    *,
    model: str,
    timeout: float = 90,
    endpoint: str = OPENAI_ENDPOINT,
    api_key: str | None = None,
) -> bytes:
    """Return one PNG. Inputs must already have been decoded and validated upstream."""
    inputs = (portrait, *references)
    if not 2 <= len(inputs) <= 5:
        raise ValueError("One to four outfit references are required")
    if any(not image.content or image.media_type not in _EXTENSIONS for image in inputs):
        raise ValueError("Unsupported or empty input image")
    if sum(len(image.content) for image in inputs) > _MAX_INPUT_BYTES:
        raise ValueError("Image payload is too large")
    if not prompt.strip() or len(prompt.encode("utf-8")) > 24_000:
        raise ValueError("Invalid prompt length")

    boundary = secrets.token_hex(24)
    marker = f"--{boundary}\r\n".encode("ascii")
    chunks = []
    for name, value in {
        "model": model,
        "prompt": prompt,
        "n": "1",
        "quality": "high",
        "size": "1024x1536",
        "output_format": "png",
    }.items():
        chunks.extend((marker, f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                       value.encode("utf-8"), b"\r\n"))
    for index, image in enumerate(inputs):
        extension = _EXTENSIONS[image.media_type]
        headers = (
            f'Content-Disposition: form-data; name="image[]"; filename="reference-{index}.{extension}"\r\n'
            f"Content-Type: {image.media_type}\r\n\r\n"
        )
        chunks.extend((marker, headers.encode("ascii"), image.content, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    request = Request(
        validate_endpoint(endpoint),
        data=b"".join(chunks),
        headers={
            "Authorization": f"Bearer {api_key if api_key is not None else _read_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise ImageEditError(f"Image provider rejected the request (HTTP {error.code})") from None
    except (URLError, OSError):
        raise ImageEditError("Image provider connection failed or timed out") from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ImageEditError("Image provider response exceeds the byte limit")
    try:
        items = json.loads(body)["data"]
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("Expected one image")
        result = base64.b64decode(items[0]["b64_json"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise ImageEditError("Image provider returned an invalid image response") from None
    if len(result) > _MAX_OUTPUT_BYTES or not result.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageEditError("Image provider returned an unexpected image format or size")
    return result
