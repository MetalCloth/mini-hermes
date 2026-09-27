"""Validate private PNG/JPEG attachments for chat history and model requests."""

import base64
from io import BytesIO
import os
import selectors
import shutil
import subprocess
import sys
from time import monotonic
import warnings
from typing import Any

from PIL import Image, ImageOps


MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_EDGE = 8192
MAX_CONTEXT_IMAGE_BYTES = MAX_IMAGES * MAX_IMAGE_BYTES
_MIME_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG"}
_MAX_CLIPBOARD_TYPES = 4096


def _validate_decoded(data: bytes) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=("PNG", "JPEG")) as image:
                mime = Image.MIME.get(image.format)
                width, height = image.size
                valid = (image.format in {"PNG", "JPEG"} and getattr(image, "n_frames", 1) == 1)
                too_large = (
                    width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE
                    or width * height > MAX_IMAGE_PIXELS
                )
                if valid and not too_large:
                    image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("Image data is not a valid, supported PNG or JPEG image.") from exc
    if not valid or too_large:
        raise ValueError("Image dimensions or frame count exceed Oryn's attachment limits.")
    return mime, width, height


def _clipboard_output(
    command: list[str], limit: int, *, missing_ok: bool = False,
    too_large_message: str = "Clipboard image exceeds Oryn's 5 MiB limit.",
) -> bytes:
    """Read bounded command output, with a timeout, from the desktop clipboard."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ValueError("Could not read the desktop clipboard.") from exc
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = monotonic() + 5
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("Reading the clipboard timed out; try pasting again.")
                chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError(too_large_message)
        try:
            return_code = process.wait(timeout=max(0, deadline - monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Reading the clipboard timed out; try pasting again.") from exc
        if return_code:
            if missing_ok:
                return b""
            raise ValueError("Could not read the image from the desktop clipboard.")
        return bytes(output)
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        if process.stdout:
            process.stdout.close()


def read_clipboard_image() -> bytes | None:
    """Return PNG/JPEG bytes from a Linux desktop clipboard, if present."""
    if sys.platform != "linux":
        raise ValueError("Image paste is currently supported on Linux desktops.")
    wl_paste = shutil.which("wl-paste")
    xclip = shutil.which("xclip")
    if os.environ.get("WAYLAND_DISPLAY") and wl_paste:
        list_command = [wl_paste, "--list-types"]
        image_command = lambda mime: [wl_paste, "--no-newline", "--type", mime]
    elif os.environ.get("DISPLAY") and xclip:
        list_command = [xclip, "-selection", "clipboard", "-t", "TARGETS", "-o"]
        image_command = lambda mime: [xclip, "-selection", "clipboard", "-t", mime, "-o"]
    elif wl_paste:
        list_command = [wl_paste, "--list-types"]
        image_command = lambda mime: [wl_paste, "--no-newline", "--type", mime]
    elif xclip:
        list_command = [xclip, "-selection", "clipboard", "-t", "TARGETS", "-o"]
        image_command = lambda mime: [xclip, "-selection", "clipboard", "-t", mime, "-o"]
    else:
        raise ValueError("Install wl-clipboard on Wayland or xclip on X11 to paste images.")

    types = _clipboard_output(
        list_command, _MAX_CLIPBOARD_TYPES, missing_ok=True,
        too_large_message="Clipboard offered too many content types.",
    ).decode("ascii", errors="ignore").splitlines()
    mime = next((candidate for candidate in _MIME_FORMATS if candidate in types), None)
    if mime is None:
        unsupported = next((kind for kind in types if kind.startswith("image/")), None)
        if unsupported:
            raise ValueError("Paste a PNG or JPEG image; this clipboard image format is not supported.")
        return None
    return _clipboard_output(image_command(mime), MAX_IMAGE_BYTES)


def prepare_image(source: bytes, name: str = "Pasted image") -> dict[str, Any]:
    """Validate, orient, and strip metadata from clipboard image bytes."""
    if not isinstance(source, bytes) or not source or len(source) > MAX_IMAGE_BYTES:
        raise ValueError("Choose a non-empty PNG/JPEG image no larger than 5 MiB.")

    mime, _, _ = _validate_decoded(source)
    try:
        with Image.open(BytesIO(source), formats=("PNG", "JPEG")) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image)
            try:
                has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                mode = "RGBA" if mime == "image/png" and has_alpha else "RGB"
                cleaned = oriented.convert(mode)
            finally:
                if oriented is not image:
                    oriented.close()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("Image could not be decoded safely; choose another PNG or JPEG.") from exc
    output = BytesIO()
    fmt = _MIME_FORMATS[mime]
    try:
        cleaned.info.clear()
        cleaned.save(output, format=fmt, quality=95, optimize=False)
    except (OSError, ValueError) as exc:
        raise ValueError("Image could not be normalized; choose another PNG or JPEG.") from exc
    finally:
        cleaned.close()
    data = output.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("The normalized image exceeds Oryn's 5 MiB attachment limit.")
    _, width, height = _validate_decoded(data)
    return {
        "name": "".join(char for char in name if char.isprintable())[:120] or "Pasted image",
        "mime_type": mime,
        "width": width,
        "height": height,
        "size_bytes": len(data),
        "base64_data": base64.b64encode(data).decode("ascii"),
    }


def image_context_bytes(messages: list[dict[str, Any]]) -> int:
    """Bound images independently from the transcript's legacy character budget."""
    total = 0
    for message in messages:
        images = message.get("images", [])
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise ValueError("Saved chat contains an invalid image attachment list.")
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("Saved chat contains an invalid image attachment.")
            size = image.get("size_bytes")
            encoded = image.get("base64_data")
            if (type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES
                    or not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3)):
                raise ValueError("Saved chat contains invalid image data; remove the attachment and retry.")
            total += size
    return total


def provider_image_parts(images: Any) -> list[dict[str, str]]:
    """Build Responses input_image parts after revalidating saved attachment bytes."""
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError("A message can contain at most four image attachments.")
    parts = []
    total = 0
    for record in images:
        if not isinstance(record, dict):
            raise ValueError("Saved chat contains an invalid image attachment.")
        mime = next((mime for mime in _MIME_FORMATS if record.get("mime_type") == mime), None)
        encoded = record.get("base64_data")
        if not mime or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("Saved image data must be PNG or JPEG no larger than 5 MiB.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("Saved image attachment has invalid Base64 data.") from exc
        if len(data) != record.get("size_bytes") or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Saved image attachment size is invalid.")
        total += len(data)
        if total > MAX_CONTEXT_IMAGE_BYTES:
            raise ValueError("The message's image attachments exceed Oryn's image context limit.")
        actual_mime, width, height = _validate_decoded(data)
        if (actual_mime != mime or width != record.get("width") or height != record.get("height")):
            raise ValueError("Saved image attachment metadata does not match its contents.")
        parts.append({
            "type": "input_image",
            "image_url": f"data:{mime};base64,{encoded}",
            "detail": "auto",
        })
    return parts
