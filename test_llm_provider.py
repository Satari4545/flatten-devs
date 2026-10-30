"""Offline checks for LLM provider selection and OpenAI replies."""

import contextlib
import io
import os
import sys
import types
import unittest
from unittest import mock

import fuse


class LLMProviderTests(unittest.TestCase):
    def settings(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def test_openai_requires_its_own_key_and_uses_requested_model(self):
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", GEMINI_API_KEY="gemini-key"):
            with self.assertRaisesRegex(fuse.LLMError, "OPENAI_API_KEY"):
                fuse.llm_settings()
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", OPENAI_API_KEY="openai-key"):
            self.assertEqual(fuse.llm_settings(),
                             ("openai", "openai-key", ["gpt-6-sol"], 3))
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", OPENAI_API_KEY="openai-key",
                OPENAI_MODEL=" "):
            with self.assertRaisesRegex(fuse.LLMError, "OPENAI_MODEL"):
                fuse.llm_settings()

    def test_openai_reply_reaches_existing_parser(self):
        answer = ("```json\n{\"fused\": {}}\n```\n"
                  "```json\n{\"init_states\": {}}\n```")
        client = mock.Mock()
        client.responses.create.return_value = types.SimpleNamespace(
            output_text=answer)
        sdk = types.ModuleType("openai")
        sdk.OpenAI = mock.Mock(return_value=client)
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", OPENAI_API_KEY="test-key",
                OPENAI_MODEL="chosen-model", OPENAI_RETRIES="1"), \
                mock.patch.dict(sys.modules, {"openai": sdk}), \
                contextlib.redirect_stdout(io.StringIO()):
            reply = fuse.call_llm("fuse this model")
        self.assertEqual(fuse.parse_reply(reply)[1],
                         [{"fused": {}}, {"init_states": {}}])
        sdk.OpenAI.assert_called_once_with(api_key="test-key", max_retries=0)
        client.responses.create.assert_called_once_with(
            model="chosen-model", input="fuse this model")

    def test_openai_empty_reply_falls_back_to_next_model(self):
        client = mock.Mock()
        client.responses.create.side_effect = [
            types.SimpleNamespace(output_text=""),
            types.SimpleNamespace(output_text="answer")]
        sdk = types.ModuleType("openai")
        sdk.OpenAI = mock.Mock(return_value=client)
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", OPENAI_API_KEY="test-key",
                OPENAI_MODELS="first, second", OPENAI_RETRIES="1"), \
                mock.patch.dict(sys.modules, {"openai": sdk}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fuse.call_llm("prompt"), "answer")
        self.assertEqual([c.kwargs["model"]
                          for c in client.responses.create.call_args_list],
                         ["first", "second"])

    def test_openai_exhaustion_reports_the_last_model(self):
        client = mock.Mock()
        client.responses.create.side_effect = RuntimeError("service unavailable")
        sdk = types.ModuleType("openai")
        sdk.OpenAI = mock.Mock(return_value=client)
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="openai", OPENAI_API_KEY="test-key",
                OPENAI_MODEL="selected", OPENAI_RETRIES="2"), \
                mock.patch.dict(sys.modules, {"openai": sdk}), \
                mock.patch.object(fuse.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(fuse.LLMError, "selected: service unavailable"):
                fuse.call_llm("prompt")
        self.assertEqual(client.responses.create.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_gemini_remains_the_default(self):
        client = mock.Mock()
        client.interactions.create.return_value = [
            types.SimpleNamespace(output_text="same reply")]
        genai = types.ModuleType("google.genai")
        genai.Client = mock.Mock(return_value=client)
        google = types.ModuleType("google")
        google.genai = genai
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                GEMINI_API_KEY="gemini-key", GEMINI_MODEL="gemini-test",
                GEMINI_RETRIES="1"), mock.patch.dict(
                    sys.modules, {"google": google, "google.genai": genai}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fuse.call_llm("prompt"), "same reply")
        genai.Client.assert_called_once_with(api_key="gemini-key")
        client.interactions.create.assert_called_once_with(
            model="gemini-test", input="prompt", stream=True)

    def test_unknown_provider_is_rejected(self):
        with mock.patch.object(fuse, "load_env_file"), self.settings(
                LLM_PROVIDER="other"):
            with self.assertRaisesRegex(fuse.LLMError, "LLM_PROVIDER"):
                fuse.llm_settings()


if __name__ == "__main__":
    unittest.main()
