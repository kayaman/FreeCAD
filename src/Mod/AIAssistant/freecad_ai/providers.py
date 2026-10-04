# SPDX-License-Identifier: LGPL-2.1-or-later
"""Native provider adapters: discovery, version, login probe, commands and diagnosis.

Commands and flags were verified against the installed CLIs (`--help`) on
2026-10-03: Claude Code 2.1.289, codex-cli 0.160.0, GitHub Copilot CLI 1.0.91.
Older versions are reported as unsupported rather than assumed to work, because
CLI flags are not a permanent contract. No credential is ever read or stored.
"""

from dataclasses import dataclass
import enum
import json
import os
import re
import time


class Readiness(str, enum.Enum):
    CHECKING = "checking"
    READY = "ready"
    MISSING_CLI = "CLI not found"
    SIGN_IN_REQUIRED = "sign-in required"
    UNSUPPORTED_VERSION = "unsupported version"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class Failure(str, enum.Enum):
    AUTH = "authentication"
    MODEL = "unsupported model"
    LIMIT = "rate or usage limit"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    GENERIC = "generic"


CACHE_SECONDS = 300
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_PATTERNS = (
    (Failure.LIMIT, ("rate limit", "rate_limit", "429", "usage limit", "quota", "too many requests",
                     "limit reached", "credits")),
    (Failure.AUTH, ("not logged in", "not signed in", "please log in", "please sign in",
                    "auth login", "codex login", "copilot login", "unauthorized", "401",
                    "authentication failed", "authentication required",
                    "no authentication information found", "token expired", "invalid api key")),
    (Failure.MODEL, ("model not found", "unknown model", "invalid model", "model is not",
                     "unsupported model", "does not have access to model", "not available for",
                     "model_not_found")),
)


def parse_version(text):
    match = _VERSION.search(text or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def version_text(version):
    return ".".join(str(part) for part in version) if version else "unknown"


def classify(*texts):
    """Map provider output to a category; raw text is never shown to the user."""
    joined = " ".join(text for text in texts if text).lower()
    for category, needles in _PATTERNS:
        if any(needle in joined for needle in needles):
            return category
    return Failure.GENERIC


@dataclass(frozen=True)
class Adapter:
    provider: str
    label: str
    login_command: str
    minimum_version: tuple
    status_arguments: tuple = ()     # Empty: no non-inference status command exists.

    def failure_message(self, category, phase="turn"):
        if category is Failure.AUTH:
            return "{} is not signed in with your account. Run {} in a terminal, then retry.".format(
                self.label, self.login_command)
        if category is Failure.MODEL:
            return "{} rejected the model. Clear the model in Settings to use its default.".format(
                self.label)
        if category is Failure.LIMIT:
            return "{} reported a rate or usage limit. Wait, or check your plan.".format(self.label)
        if category is Failure.TIMEOUT:
            return "{} request timed out. Increase the timeout in Settings or retry.".format(
                self.label)
        if category is Failure.PROTOCOL:
            return "{} returned output this version of the assistant cannot read. Update the CLI " \
                   "or the assistant.".format(self.label)
        return "{} {} failed. Check sign-in, model access, usage limits and CLI version.".format(
            self.label, "login check" if phase == "auth" else "turn")

    def interpret_status(self, stdout, stderr, exit_code):
        """(Readiness, message) from the native status command's output."""
        if self.provider == "claude":
            try:
                status = json.loads(stdout)
            except (ValueError, TypeError):
                return Readiness.UNKNOWN, "Could not read Claude Code's login status."
            if not isinstance(status, dict) or not status.get("loggedIn"):
                return Readiness.SIGN_IN_REQUIRED, self.failure_message(Failure.AUTH)
            if status.get("authMethod") not in ("claude.ai", "oauth_token"):
                return (Readiness.SIGN_IN_REQUIRED, "Claude Code uses API billing. Sign in with "
                        "your Claude account using claude auth login.")
            return Readiness.READY, "Signed in with a Claude account."
        if self.provider == "codex":
            text = (stdout + "\n" + stderr).lower()
            if "logged in using chatgpt" in text:
                return Readiness.READY, "Signed in with ChatGPT."
            if "api key" in text:
                return (Readiness.SIGN_IN_REQUIRED, "Codex uses an API key. Sign in with ChatGPT "
                        "using codex login.")
            if exit_code != 0 or "not logged in" in text:
                return Readiness.SIGN_IN_REQUIRED, self.failure_message(Failure.AUTH)
            return Readiness.UNKNOWN, "Could not read Codex's login status."
        return Readiness.UNKNOWN, ("{} has no status command; use Live check to verify "
                                   "sign-in with one request.".format(self.label))

    def check_version(self, output):
        version = parse_version(output)
        if version is None:
            return None, Readiness.UNKNOWN, "Could not read the {} version.".format(self.label)
        if version < self.minimum_version:
            return version, Readiness.UNSUPPORTED_VERSION, (
                "{} {} is older than {}, the oldest version verified with this assistant. "
                "Update the CLI.".format(self.label, version_text(version),
                                         version_text(self.minimum_version)))
        return version, None, ""


ADAPTERS = {
    "claude": Adapter("claude", "Claude Code", "claude auth login", (2, 1, 289),
                      ("auth", "status", "--json")),
    "codex": Adapter("codex", "Codex", "codex login", (0, 160, 0), ("login", "status")),
    "copilot": Adapter("copilot", "GitHub Copilot", "copilot login", (1, 0, 91)),
}


def adapter_for(provider):
    return ADAPTERS[provider]


def turn_arguments(settings, mcp_config_path=""):
    """Non-interactive turn with tools, user configuration and API billing disabled."""
    model = settings.model.strip()
    if settings.provider == "claude":
        args = ["-p", "--verbose", "--include-partial-messages",
                "--input-format=stream-json", "--output-format=stream-json",
                "--tools", "", "--strict-mcp-config", "--mcp-config", mcp_config_path,
                "--safe-mode", "--no-session-persistence"]
        return args + (["--model", model] if model else [])
    if settings.provider == "codex":
        args = ["exec", "--json", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "--sandbox=read-only", "--skip-git-repo-check",
                "--color=never", "-c", 'forced_login_method="chatgpt"',
                "-c", 'model_provider="openai"']
        return args + (["--model", model] if model else []) + ["-"]
    args = ["--silent", "--stream=off", "--output-format=text", "--available-tools=",
            "--disable-builtin-mcps", "--no-custom-instructions",
            "--no-ask-user", "--no-auto-update", "--no-color",
            "--no-remote", "--no-remote-export", "--log-level=none"]
    return args + (["--model=" + model] if model else [])


@dataclass
class Status:
    state: Readiness
    message: str = ""
    version: tuple = None
    checked_at: float = 0.0
    live: bool = False


class ReadinessCache:
    """Successful checks only, keyed by provider, executable and its modification time."""

    def __init__(self, seconds=CACHE_SECONDS, clock=time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self.entries = {}

    @staticmethod
    def key(provider, executable):
        try:
            stamp = os.stat(executable).st_mtime_ns
        except OSError:
            stamp = None
        return provider, executable, stamp

    def get(self, provider, executable):
        status = self.entries.get(self.key(provider, executable))
        if status is None or self.clock() - status.checked_at > self.seconds:
            return None
        return status

    def put(self, provider, executable, status):
        if status.state is Readiness.READY:
            status.checked_at = self.clock()
            self.entries[self.key(provider, executable)] = status

    def invalidate(self, provider=None):
        if provider is None:
            self.entries.clear()
        else:
            self.entries = {k: v for k, v in self.entries.items() if k[0] != provider}


CACHE = ReadinessCache()
