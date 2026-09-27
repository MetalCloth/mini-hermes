"""Local image validation, size bounds, and multimodal context retention."""

import base64
from io import BytesIO
import sys
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from src.agent.context import select_context
from src.images import (
    MAX_CONTEXT_IMAGE_BYTES, MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, _clipboard_output,
    prepare_image, provider_image_parts, read_clipboard_image,
)


class ImageInputTests(unittest.TestCase):
    def _png(self, *, size=(2, 3), private_text=None):
        output = BytesIO()
        image = Image.new("RGBA", size, (255, 4, 5, 123))
        metadata = PngImagePlugin.PngInfo()
        if private_text:
            metadata.add_text("Private metadata", private_text)
        image.save(output, format="PNG", pnginfo=metadata)
        return output.getvalue()

    def test_normalizes_metadata_and_creates_a_responses_image_part(self):
        image = prepare_image(self._png(private_text="gps-like-private-text"), "Pasted image 1")
        jpeg_bytes = BytesIO()
        exif = Image.Exif()
        exif[274] = 6  # Rotate 90 degrees clockwise when displayed.
        Image.new("RGB", (2, 3), "blue").save(jpeg_bytes, format="JPEG", exif=exif)
        jpeg = prepare_image(jpeg_bytes.getvalue())

        self.assertEqual((image["name"], image["mime_type"], image["width"], image["height"]),
                         ("Pasted image 1", "image/png", 2, 3))
        encoded = base64.b64decode(image["base64_data"], validate=True)
        self.assertNotIn(b"gps-like-private-text", encoded)
        self.assertEqual(image["size_bytes"], len(encoded))
        normalized_png = Image.open(BytesIO(encoded))
        self.assertEqual(normalized_png.getpixel((0, 0))[3], 123)
        normalized_png.close()
        self.assertEqual((jpeg["mime_type"], jpeg["width"], jpeg["height"]), ("image/jpeg", 3, 2))
        rotated = Image.open(BytesIO(base64.b64decode(jpeg["base64_data"], validate=True)))
        self.assertIsNone(rotated.getexif().get(274))
        rotated.close()
        parts = provider_image_parts([image])
        self.assertEqual(parts[0]["type"], "input_image")
        self.assertEqual(parts[0]["detail"], "auto")
        self.assertTrue(parts[0]["image_url"].startswith("data:image/png;base64,"))
        for bad_image in [
            {**image, "base64_data": "!"},
            {**image, "width": image["width"] + 1},
        ]:
            with self.assertRaises(ValueError):
                provider_image_parts([bad_image])
        with self.assertRaisesRegex(ValueError, "at most four"):
            provider_image_parts([image] * 5)

    def test_rejects_corrupt_oversized_and_overdimensioned_images(self):
        with self.assertRaisesRegex(ValueError, "valid, supported"):
            prepare_image(b"not a PNG or JPEG")
        with self.assertRaisesRegex(ValueError, "5 MiB"):
            prepare_image(b"x" * (MAX_IMAGE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "dimensions or frame count"):
            prepare_image(self._png(size=(MAX_IMAGE_EDGE + 1, 1)))

    def test_reads_png_from_wayland_clipboard_and_leaves_other_clipboards_alone(self):
        self.assertEqual(_clipboard_output([
            sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc')",
        ], 3), b"abc")
        with self.assertRaisesRegex(ValueError, "output too large"):
            _clipboard_output([
                sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcde')",
            ], 4, too_large_message="Clipboard output too large.")
        png = self._png()
        with patch("src.images.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch.dict("src.images.os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), \
             patch("src.images._clipboard_output", side_effect=[b"image/png\ntext/plain\n", png]) as read:
            self.assertEqual(read_clipboard_image(), png)
            self.assertEqual(read.call_args_list[0].args[0], ["/usr/bin/wl-paste", "--list-types"])
            self.assertEqual(read.call_args_list[1].args[0], [
                "/usr/bin/wl-paste", "--no-newline", "--type", "image/png",
            ])
        with patch("src.images.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch.dict("src.images.os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), \
             patch("src.images._clipboard_output", return_value=b"text/plain\n"):
            self.assertIsNone(read_clipboard_image())

    def test_context_keeps_recent_images_under_a_separate_byte_budget(self):
        one = {
            "name": "photo.png", "mime_type": "image/png", "width": 1, "height": 1,
            "size_bytes": MAX_IMAGE_BYTES,
            "base64_data": "A" * (4 * ((MAX_IMAGE_BYTES + 2) // 3)),
        }
        history = [
            {"role": "user", "content": "Old image", "images": [one] * 4},
            {"role": "assistant", "content": "Old answer"},
            {"role": "user", "content": "Current image", "images": [one]},
        ]
        selected = select_context(history)
        self.assertEqual([message.get("content") for message in selected], ["Current image"])
        self.assertEqual(MAX_CONTEXT_IMAGE_BYTES, 4 * MAX_IMAGE_BYTES)
