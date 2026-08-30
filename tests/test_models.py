from __future__ import annotations

import unittest
from types import SimpleNamespace

from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIResponsesModel,
)

from backend.models import resolve_model, resolve_model_settings


class PydanticAICompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            openai_api_key="test-openai-key",
            azure_openai_endpoint="https://example.openai.azure.com/openai/v1",
            azure_openai_api_key="test-azure-key",
            opencode_zen_api_key="test-zen-key",
        )

    def test_openai_uses_responses_api_model(self) -> None:
        model = resolve_model("openai/gpt-5.6-sol", self.settings)
        self.assertIsInstance(model, OpenAIResponsesModel)
        self.assertEqual(resolve_model_settings("openai/gpt-5.6-sol")["max_tokens"], 128_000)

    def test_openai_compatible_endpoints_use_chat_model(self) -> None:
        for provider in ("azure", "zen"):
            with self.subTest(provider=provider):
                model = resolve_model(f"{provider}/gpt-5.6-sol", self.settings)
                self.assertIsInstance(model, OpenAIChatModel)
                self.assertEqual(
                    resolve_model_settings(f"{provider}/gpt-5.6-sol")["max_tokens"],
                    128_000,
                )


if __name__ == "__main__":
    unittest.main()
