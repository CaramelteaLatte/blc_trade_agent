import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from onchain_agent.api_config import ApiSettings, NoRedirects, check_model_catalog
from onchain_agent.runtime import make_harness


class ApiConfigTests(unittest.TestCase):
    def settings(self, **values):
        env = {"OPENAI_API_KEY": "gateway-test-key",
               "OPENAI_BASE_URL": "https://gateway.example/v1/", **values}
        with patch.dict(os.environ, env, clear=True), patch("onchain_agent.api_config.load_credentials"):
            return ApiSettings.from_environment()

    def test_catalog_defaults_to_same_api_base_and_secrets_stay_private(self):
        settings = self.settings()
        self.assertEqual(settings.base_url, "https://gateway.example/v1")
        self.assertEqual(settings.model_catalog_url, "https://gateway.example/v1/models")
        self.assertEqual(settings.model, "gpt-6.1-sol")
        self.assertNotIn(settings.api_key, repr(settings))
        self.assertNotIn(settings.api_key, json.dumps(settings.public_dict()))

    def test_invalid_endpoints_and_cross_origin_catalog_are_rejected(self):
        for values in [
            {"OPENAI_BASE_URL": "http://gateway.example/v1"},
            {"OPENAI_BASE_URL": "https://key@gateway.example/v1"},
            {"OPENAI_BASE_URL": "https://gateway.example/v1?key=secret"},
            {"OPENAI_MODEL_CATALOG_URL": "https://another.example/v1/models"},
            {"OPENAI_MODEL": ""},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.settings(**values)

    def test_harness_receives_explicit_gateway_key_url_and_model(self):
        settings = self.settings(OPENAI_MODEL="gateway-sol-alias")
        with tempfile.TemporaryDirectory() as folder:
            # Construction alone does not launch the runtime or call a model.
            harness = make_harness(Path(folder), use_memory=False, settings=settings)
            self.assertEqual(harness.config.provider, "research-api")
            self.assertEqual(harness.config.model, "gateway-sol-alias")
            self.assertEqual(harness.config.env["RESEARCH_API_BASE_URL"], settings.base_url)
            self.assertEqual(harness.config.env["OPENAI_API_KEY"], settings.api_key)
            self.assertEqual(harness.config.env["RESEARCH_READ_NOTES"], "0")
            self.assertIsNone(harness.config.api_key)  # SDK's api_key argument is DeepSeek-specific.

    def test_catalog_reports_requested_model_presence_without_substitution(self):
        settings = self.settings()
        for model_id, available in [("gpt-6.1-sol", True), ("different-model", False)]:
            response = io.BytesIO(json.dumps({"data": [{"id": model_id}]}).encode())
            opener = Mock()
            opener.open.return_value = response
            with patch("onchain_agent.api_config.build_opener", return_value=opener):
                result = check_model_catalog(settings)
            self.assertEqual(result["model_available"], available)
            self.assertEqual(result["requested_model"], "gpt-6.1-sol")
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, settings.model_catalog_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + settings.api_key)

    def test_catalog_error_does_not_echo_server_body_or_credentials(self):
        settings = self.settings()
        opener = Mock()
        opener.open.side_effect = HTTPError(settings.model_catalog_url, 401, "Unauthorized", {},
                                           io.BytesIO(settings.api_key.encode()))
        with patch("onchain_agent.api_config.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "HTTP 401") as error:
                check_model_catalog(settings)
        self.assertNotIn(settings.api_key, str(error.exception))
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "redirect", {}, "https://another.example"))

    def test_missing_key_fails_before_catalog_or_runtime_start(self):
        settings = self.settings(OPENAI_API_KEY="")
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            check_model_catalog(settings)


if __name__ == "__main__":
    unittest.main()
