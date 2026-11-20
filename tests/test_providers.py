import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dressv.images import media_type
from dressv.jobs import Payload
from dressv.openai_images import ImageEditError
from dressv.providers import DEFAULT_MODEL, configured_generator, valid_result


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.png = configured_generator()(None, 0)
        self.payload = Payload("sample", "test outfit", (), self.png, (self.png,))

    def test_mock_never_needs_credentials_or_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("dressv.providers.edit_image") as edit:
            result = configured_generator()(self.payload, time.monotonic() + 30)
            self.assertEqual(media_type(result), "image/png")
            self.assertTrue(valid_result(self.payload, result, 0))
            self.assertFalse(valid_result(self.payload, b"corrupted", 0))
            edit.assert_not_called()

    def test_same_generator_contract_with_two_endpoints_and_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch("dressv.providers.edit_image") as edit:
            edit.return_value = self.png
            endpoints = ("https://one.example/v1/images/edits", "https://two.example/images/edits")
            keys = ("test-key-first", "second.provider.key")
            for endpoint, key in zip(endpoints, keys):
                Path(directory, "provider-api-key").write_text(key + "\n", encoding="ascii")
                env = {
                    "DRESSV_PROVIDER": "image-edits",
                    "DRESSV_PROVIDER_ENDPOINT": endpoint,
                    "CREDENTIALS_DIRECTORY": directory,
                }
                with patch.dict(os.environ, env, clear=True):
                    generate = configured_generator()
                    self.assertEqual(generate(self.payload, time.monotonic() + 30), self.png)
                args, kwargs = edit.call_args
                self.assertEqual(args[0].content, self.payload.portrait)
                self.assertEqual(args[1][0].content, self.payload.references[0])
                self.assertEqual(kwargs["endpoint"], endpoint)
                self.assertEqual(kwargs["api_key"], key)
                self.assertEqual(kwargs["model"], DEFAULT_MODEL)
            self.assertEqual(edit.call_count, 2)

    def test_live_configuration_rejects_invalid_endpoint_or_missing_key_at_start(self):
        for endpoint, expected in (
            ("http://example.com/edit", "Invalid Images Edits endpoint"),
            ("https://user:secret@example.com/edit", "Invalid Images Edits endpoint"),
            ("https://example.com/edit?secret=123", "Invalid Images Edits endpoint"),
            ("https://example.com/edit", "credential is unavailable"),
        ):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {
                "DRESSV_PROVIDER": "image-edits", "DRESSV_PROVIDER_ENDPOINT": endpoint,
            }, clear=True):
                with self.assertRaisesRegex(ImageEditError, expected):
                    configured_generator()
