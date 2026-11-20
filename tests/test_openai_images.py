import base64
import io
import json
import os
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from dressv import openai_images


PNG = b"\x89PNG\r\n\x1a\nsynthetic"
INPUTS = (
    openai_images.ImageInput(b"mock-portrait", "image/png"),
    (openai_images.ImageInput(b"mock-reference", "image/jpeg"),),
    "Synthetic outfit",
)


class OpenAITransportTests(unittest.TestCase):
    def setUp(self):
        self.opener = self.enter_patch(patch.object(openai_images, "build_opener")).return_value

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def submit(self):
        with patch.object(openai_images, "_read_key", return_value="sk-fake-test"):
            return openai_images.edit_image(*INPUTS, model="mock-image-model")

    def test_absent_key_never_reaches_transport(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(openai_images.ImageEditError, "credential is unavailable"):
                openai_images.edit_image(*INPUTS, model="mock-image-model")
        self.opener.open.assert_not_called()

    def test_order_and_valid_response(self):
        response = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        self.opener.open.return_value = io.BytesIO(response)
        self.assertEqual(self.submit(), PNG)
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertLess(request.data.index(b"mock-portrait"), request.data.index(b"mock-reference"))
        self.assertEqual(request.get_method(), "POST")

    def test_bad_base64_and_response_limit(self):
        for response in (b'{"data":[{"b64_json":"?"}]}', b"x" * 129):
            with self.subTest(response_size=len(response)):
                self.opener.open.reset_mock()
                self.opener.open.return_value = io.BytesIO(response)
                with patch.object(openai_images, "_MAX_RESPONSE_BYTES", 128):
                    with self.assertRaises(openai_images.ImageEditError):
                        self.submit()
                self.opener.open.assert_called_once()

    def test_http_failure_and_uncertain_timeout_are_not_retried(self):
        for error in (
            HTTPError("https://private", 429, "secret", {}, Mock()),
            HTTPError("https://private", 503, "secret", {}, Mock()),
            URLError("secret"),
            TimeoutError("secret"),
        ):
            with self.subTest(error=type(error)):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(openai_images.ImageEditError) as failure:
                    self.submit()
                self.assertNotIn("secret", str(failure.exception))
                self.opener.open.assert_called_once()
