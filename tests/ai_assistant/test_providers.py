# SPDX-License-Identifier: LGPL-2.1-or-later
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.core import ProviderSettings
from freecad_ai.providers import (ADAPTERS, Failure, Readiness, ReadinessCache, Status, classify,
                                  parse_version)


class VersionTests(unittest.TestCase):
    def test_parse_installed_version_strings(self):
        self.assertEqual(parse_version("2.1.289 (Claude Code)"), (2, 1, 289))
        self.assertEqual(parse_version("codex-cli 0.160.0"), (0, 160, 0))
        self.assertEqual(parse_version("GitHub Copilot CLI 1.0.91."), (1, 0, 91))
        self.assertIsNone(parse_version("no version here"))

    def test_obsolete_supported_and_malformed(self):
        claude = ADAPTERS["claude"]
        self.assertEqual(claude.check_version("2.0.1 (Claude Code)")[1], Readiness.UNSUPPORTED_VERSION)
        self.assertIsNone(claude.check_version("2.1.289 (Claude Code)")[1])
        self.assertIsNone(claude.check_version("3.0.0 (Claude Code)")[1])
        self.assertEqual(claude.check_version("garbage")[1], Readiness.UNKNOWN)


class StatusTests(unittest.TestCase):
    def test_claude_states(self):
        claude = ADAPTERS["claude"]
        ready = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        self.assertEqual(claude.interpret_status(ready, "", 0)[0], Readiness.READY)
        billed = json.dumps({"loggedIn": True, "authMethod": "api_key"})
        self.assertEqual(claude.interpret_status(billed, "", 0)[0], Readiness.SIGN_IN_REQUIRED)
        out = json.dumps({"loggedIn": False})
        self.assertEqual(claude.interpret_status(out, "", 1)[0], Readiness.SIGN_IN_REQUIRED)
        self.assertEqual(claude.interpret_status("<html>", "", 0)[0], Readiness.UNKNOWN)

    def test_codex_states(self):
        codex = ADAPTERS["codex"]
        self.assertEqual(codex.interpret_status("", "Logged in using ChatGPT", 0)[0], Readiness.READY)
        self.assertEqual(codex.interpret_status("Logged in using an API key", "", 0)[0],
                         Readiness.SIGN_IN_REQUIRED)
        self.assertEqual(codex.interpret_status("Not logged in", "", 1)[0], Readiness.SIGN_IN_REQUIRED)
        self.assertEqual(codex.interpret_status("Something new", "", 0)[0], Readiness.UNKNOWN)

    def test_copilot_has_no_status_command(self):
        copilot = ADAPTERS["copilot"]
        self.assertEqual(copilot.status_arguments, ())
        state, message = copilot.interpret_status("", "", 0)
        self.assertEqual(state, Readiness.UNKNOWN)
        self.assertIn("Live check", message)
        self.assertIsNone(ProviderSettings(provider="copilot").auth_arguments())


class ClassificationTests(unittest.TestCase):
    def test_categories_from_evidence(self):
        self.assertEqual(classify("Error: 429 Too Many Requests"), Failure.LIMIT)
        self.assertEqual(classify("Claude AI usage limit reached"), Failure.LIMIT)
        self.assertEqual(classify("", "Not logged in · Please run /login"), Failure.AUTH)
        self.assertEqual(classify("Error: No authentication information found."), Failure.AUTH)
        self.assertEqual(classify("model_not_found: gpt-x"), Failure.MODEL)
        self.assertEqual(classify("panic at src/main.rs:12"), Failure.GENERIC)
        self.assertEqual(classify("forced_login_method=chatgpt"), Failure.GENERIC)

    def test_messages_never_echo_raw_output(self):
        secret = "sk-ant-secret /home/user/.config"
        for adapter in ADAPTERS.values():
            for category in Failure:
                message = adapter.failure_message(category)
                self.assertNotIn(secret, message)
                self.assertIn(adapter.label, message)
        self.assertIn("claude auth login", ADAPTERS["claude"].failure_message(Failure.AUTH))
        self.assertIn("timed out", ADAPTERS["codex"].failure_message(Failure.TIMEOUT))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.cache = ReadinessCache(seconds=300, clock=lambda: self.now[0])
        handle, self.cli = tempfile.mkstemp()
        os.close(handle)

    def tearDown(self):
        os.unlink(self.cli)

    def test_only_successful_checks_are_cached_and_expire(self):
        self.cache.put("claude", self.cli, Status(Readiness.SIGN_IN_REQUIRED))
        self.assertIsNone(self.cache.get("claude", self.cli))
        self.cache.put("claude", self.cli, Status(Readiness.READY))
        self.assertEqual(self.cache.get("claude", self.cli).state, Readiness.READY)
        self.now[0] += 301
        self.assertIsNone(self.cache.get("claude", self.cli))

    def test_cli_update_and_invalidation(self):
        self.cache.put("codex", self.cli, Status(Readiness.READY))
        stamp = os.stat(self.cli).st_mtime_ns
        os.utime(self.cli, ns=(stamp + 10**9, stamp + 10**9))  # The CLI was updated.
        self.assertIsNone(self.cache.get("codex", self.cli))
        self.cache.put("codex", self.cli, Status(Readiness.READY))
        self.cache.invalidate("codex")
        self.assertIsNone(self.cache.get("codex", self.cli))


if __name__ == "__main__":
    unittest.main()
