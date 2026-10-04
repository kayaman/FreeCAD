# SPDX-License-Identifier: LGPL-2.1-or-later
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.core import (API_ENVIRONMENT_VARIABLES, AssistantError, Conversation,
                             MAX_AGENT_STEPS, MAX_RESPONSE_BYTES, NativeEvents,
                             ProviderSettings, Proposal, load_settings, parse_proposal,
                             save_settings, validate_native_login)


class Params:
    def __init__(self, **values):
        self.values = values

    def GetString(self, key, default):
        return self.values.get(key, default)

    GetInt = GetString

    def SetString(self, key, value):
        self.values[key] = value

    SetInt = SetString

    def RemString(self, key):
        self.values.pop(key, None)


def line(event):
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


class SettingsTests(unittest.TestCase):
    def test_default_is_native_claude_with_no_credentials(self):
        settings = ProviderSettings()
        settings.validate()
        self.assertTrue(settings.executable.endswith("claude"))
        self.assertEqual(set(settings.__dataclass_fields__),
                         {"provider", "model", "timeout_seconds", "cli_executable"})

    def test_old_api_preferences_migrate_to_native(self):
        for previous, native in (("anthropic", "claude"), ("openai", "codex")):
            settings = load_settings(Params(Provider=previous, Model="api-only-model"))
            self.assertEqual(settings.provider, native)
            self.assertEqual(settings.model, "")

    def test_settings_roundtrip_and_cleanup(self):
        params = Params(BaseURL="old-url", APIKey="old-key")
        settings = ProviderSettings("codex", "custom-model", 60, "/path with spaces/codex")
        save_settings(params, settings)
        self.assertEqual(load_settings(params), settings)
        self.assertNotIn("APIKey", params.values)
        self.assertNotIn("BaseURL", params.values)

    def test_optional_model_uses_native_default(self):
        for provider in ("claude", "codex", "copilot"):
            settings = ProviderSettings(provider=provider)
            settings.validate()
            self.assertFalse(any(arg.startswith("--model") for arg in settings.arguments("mcp.json")))

    def test_desktop_cli_discovery_and_explicit_override(self):
        with patch("freecad_ai.core.shutil.which", return_value=None), \
                patch("freecad_ai.core.Path.is_file", return_value=True):
            self.assertTrue(ProviderSettings().executable.endswith(".local/bin/claude"))
        self.assertEqual(ProviderSettings(cli_executable="/custom/claude").executable, "/custom/claude")

    def test_native_npm_binary_is_used_without_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "bin/codex.js"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/usr/bin/env node\n")
            binary = root / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"native binary fixture")
            with patch("freecad_ai.core.shutil.which", return_value=None), \
                    patch("freecad_ai.core.platform.machine", return_value="x86_64"), \
                    patch("freecad_ai.core.sys.platform", "linux"):
                settings = ProviderSettings(provider="codex", cli_executable=str(wrapper))
                self.assertEqual(settings.executable, str(binary))

    def test_claude_uses_bancada_stream_protocol_and_stored_login(self):
        args = ProviderSettings().arguments("mcp.json")
        self.assertIn("--input-format=stream-json", args)
        self.assertIn("--output-format=stream-json", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)
        self.assertNotIn("--bare", args)
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_codex_uses_chatgpt_signin(self):
        args = ProviderSettings(provider="codex").arguments()
        self.assertEqual(args[0], "exec")
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("--sandbox=read-only", args)
        self.assertEqual(args[-1], "-")

    def test_copilot_tools_disabled(self):
        args = ProviderSettings(provider="copilot").arguments()
        self.assertIn("--available-tools=", args)
        self.assertIn("--disable-builtin-mcps", args)
        self.assertFalse(any(arg.startswith("--allow-all") for arg in args))

    def test_api_environment_variable_exclusion_list(self):
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN"):
            self.assertIn(name, API_ENVIRONMENT_VARIABLES)

    def test_login_checks_accept_native_accounts(self):
        validate_native_login("claude", '{"loggedIn":true,"authMethod":"claude.ai"}')
        validate_native_login("codex", "Logged in using ChatGPT")

    def test_login_checks_refuse_api_billing(self):
        for method in ("api_key", "api_key_helper", "third_party", "none"):
            with self.assertRaises(AssistantError):
                validate_native_login("claude", json.dumps({"loggedIn": True, "authMethod": method}))
        with self.assertRaises(AssistantError):
            validate_native_login("codex", "Logged in using an API key")

    def test_invalid_login_shapes(self):
        for output in ("broken", "null", "[]", '{}'):
            with self.assertRaises(AssistantError):
                validate_native_login("claude", output)

    def test_invalid_settings(self):
        for settings in (ProviderSettings(provider="openai"), ProviderSettings(timeout_seconds=0),
                         ProviderSettings(cli_executable="bad\npath")):
            with self.assertRaises(AssistantError):
                settings.validate()


class NativeEventTests(unittest.TestCase):
    def test_claude_events_across_arbitrary_byte_boundaries(self):
        events = NativeEvents("claude")
        content = json.dumps({"message": "Create café box", "python": "x=1"}, ensure_ascii=False)
        stream = line({"type": "system", "subtype": "init"}) + line({
            "type": "stream_event", "event": {"type": "content_block_delta", "delta": {
                "type": "text_delta", "text": "Preview"}}}) + line({
            "type": "result", "subtype": "success", "is_error": False, "result": content})
        progress = []
        for byte in stream:
            progress.extend(events.feed(bytes([byte])))
        self.assertEqual(progress, ["Preview"])
        self.assertEqual(events.finish(), Proposal("Create café box", "x=1"))

    def test_claude_failure_never_executes(self):
        events = NativeEvents("claude")
        events.feed(line({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": '{"message":"Unsafe", "python":"x=1"}'}))
        with self.assertRaises(AssistantError):
            events.finish()

    def test_codex_completed_agent_message(self):
        events = NativeEvents("codex")
        events.feed(line({"type": "item.completed", "item": {"type": "agent_message",
                         "text": '{"message":"Done", "python":""}'}}))
        events.feed(line({"type": "turn.completed", "usage": {}}))
        self.assertEqual(events.finish(), Proposal("Done"))

    def test_codex_failed_turn(self):
        events = NativeEvents("codex")
        events.feed(line({"type": "turn.failed", "error": {"message": "rate limit"}}))
        with self.assertRaises(AssistantError):
            events.finish()

    def test_partial_turn_does_not_execute(self):
        for provider in ("claude", "codex"):
            events = NativeEvents(provider)
            events.feed(line({"type": "assistant", "message": {"content": []}}))
            with self.assertRaises(AssistantError):
                events.finish()

    def test_unknown_and_new_events_are_ignored(self):
        events = NativeEvents("claude")
        for event in ({"type": "rate_limit_event"}, {"type": "future_event"},
                      {"type": "stream_event", "event": "future"}, [1, 2]):
            events.feed(line(event))
        events.feed(line({"type": "result", "subtype": "success", "result": "Helpful text"}))
        self.assertEqual(events.finish(), Proposal("Helpful text"))

    def test_copilot_native_text_output(self):
        events = NativeEvents("copilot")
        events.feed(b'{"message":"Box", "python":"x=1"}')
        self.assertEqual(events.finish(), Proposal("Box", "x=1"))

    def test_invalid_event_json(self):
        with self.assertRaises(AssistantError):
            NativeEvents("claude").feed(b'broken\n')

    def test_stream_size_limit(self):
        with self.assertRaises(AssistantError):
            NativeEvents("copilot").feed(b"x" * (MAX_RESPONSE_BYTES + 1))

    def test_final_record_without_newline(self):
        events = NativeEvents("claude")
        events.feed(line({"type": "result", "subtype": "success", "result": "Done"}).rstrip())
        self.assertEqual(events.finish(), Proposal("Done"))


class ProposalTests(unittest.TestCase):
    def test_plain_or_fenced_python_never_executes(self):
        for text in ("Use Part.", "```python\nimport os\n```", "{broken"):
            self.assertEqual(parse_proposal(text).python, "")

    def test_json_fence_supported(self):
        self.assertEqual(parse_proposal('```json\n{"message":"Done", "python":""}\n```'),
                         Proposal("Done"))

    def test_invalid_schema_and_large_code_rejected(self):
        for text in ('{"message":"M", "python":42}', '{"message":"M"}',
                     json.dumps({"message": "M", "python": "x" * 48001})):
            with self.assertRaises(AssistantError):
                parse_proposal(text)


class ConversationTests(unittest.TestCase):
    def test_claude_input_is_native_user_message(self):
        conversation = Conversation()
        conversation.begin("Make a box")
        body = json.loads(conversation.request_body(ProviderSettings(), {"document": None}))
        self.assertEqual(body["type"], "user")
        self.assertEqual(body["message"]["role"], "user")
        text = body["message"]["content"][0]["text"]
        self.assertIn("Make a box", text)
        self.assertIn("untrusted", text)

    def test_context_can_be_omitted(self):
        conversation = Conversation()
        conversation.begin("Help")
        for provider in ("claude", "codex", "copilot"):
            body = conversation.request_body(ProviderSettings(provider=provider))
            self.assertNotIn(b"Current model context", body)

    def test_execution_feedback_and_step_limit(self):
        conversation = Conversation()
        conversation.begin("Box")
        for _ in range(MAX_AGENT_STEPS):
            conversation.accept(Proposal("Box", "x=1"))
            conversation.record_execution(False, "RuntimeError: repair this")
        self.assertFalse(conversation.can_execute)
        feedback = json.loads(conversation.messages[-1]["content"])
        self.assertFalse(feedback["execution_success"])
        self.assertEqual(feedback["code_steps_remaining"], 0)
        with self.assertRaises(AssistantError):
            conversation.record_execution(True, "done")
        conversation.begin("Continue")
        self.assertTrue(conversation.can_execute)

    def test_prompt_validation(self):
        for prompt in (" ", "x" * 24001):
            with self.assertRaises(AssistantError):
                Conversation().begin(prompt)


if __name__ == "__main__":
    unittest.main()
