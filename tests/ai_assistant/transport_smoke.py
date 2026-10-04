# SPDX-License-Identifier: LGPL-2.1-or-later
"""Exercise actual Qt subprocesses using fake native providers, without inference."""
import json
from pathlib import Path
import shlex
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))


def _alive(pid):
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # A zombie awaiting its reaper is no longer running.
        with open("/proc/{}/stat".format(pid)) as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _wait(loop_class, timer_class, milliseconds):
    loop = loop_class()
    timer_class.singleShot(milliseconds, loop.quit)
    loop.exec_()


def run():
    import os
    import time
    from freecad_ai.core import API_ENVIRONMENT_VARIABLES, Conversation, ProviderSettings
    from freecad_ai.gui import ReadinessCheck, Transport, QtCore, QtWidgets
    from freecad_ai import processes
    from freecad_ai.providers import CACHE, Readiness
    CACHE.invalidate()
    if sys.platform == "win32":
        print("SKIP: Native transport smoke fixtures use Unix shell executables.")
        return
    transport = Transport(QtWidgets.QApplication.instance())
    conversation = Conversation()
    conversation.begin("Explain a box")
    outcomes = []
    progress = []
    checks = []
    loop = QtCore.QEventLoop()
    deadline = QtCore.QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def success(token, result):
        outcomes.append((True, result, token))
        loop.quit()

    def failed(token, message):
        outcomes.append((False, message, token))
        loop.quit()

    transport.completed.connect(success)
    transport.failed.connect(failed)
    transport.progress.connect(lambda token, text: progress.append(text))

    def request(settings, token="token"):
        outcomes.clear()
        progress.clear()
        deadline.start(6000)
        sent = transport.send(settings, conversation.request_body(settings), token)
        loop.exec_()
        deadline.stop()
        _wait(QtCore.QEventLoop, QtCore.QTimer, 50)  # A duplicate outcome would arrive here.
        assert len(outcomes) == 1, "Transport did not finish exactly once"
        assert outcomes[0][2] == token, "Outcome lost its request token"
        assert not os.path.exists(sent.scratch_path), "Scratch directory was left behind"
        return outcomes[0][:2]

    def fixture(directory, provider, auth=None, interrupted=False, sleep=False, version=None,
                name=None, fail_turn=None):
        path = Path(directory) / (name or "fake-" + provider)
        result = json.dumps({"message": provider + " transport works", "python": ""})
        if provider == "claude":
            auth = auth or json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
            auth_script = 'if [ "$1" = auth ]; then printf \'%s\\n\' ' + shlex.quote(auth) + '; exit 0; fi\n'
            records = [{"type": "system", "subtype": "init"},
                       {"type": "stream_event", "event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Preview"}}}]
            if not interrupted:
                records.append({"type": "result", "subtype": "success", "is_error": False,
                                "result": result})
            output = "\n".join(json.dumps(event) for event in records) + "\n"
        elif provider == "codex":
            auth = auth or "Logged in using ChatGPT"
            auth_script = 'if [ "$1" = login ]; then printf \'%s\\n\' ' + shlex.quote(auth) + ' >&2; exit 0; fi\n'
            records = [{"type": "item.completed", "item": {"type": "agent_message", "text": result}}]
            if not interrupted:
                records.append({"type": "turn.completed"})
            output = "\n".join(json.dumps(event) for event in records) + "\n"
        else:
            auth_script = ""
            output = result
        version = version or {"claude": "2.1.289 (Claude Code)", "codex": "codex-cli 0.160.0",
                              "copilot": "GitHub Copilot CLI 1.0.91."}[provider]
        version_script = 'if [ "$1" = --version ]; then echo ' + shlex.quote(version) + '; exit 0; fi\n'
        auth_script = 'echo "$1" >> "$0.calls"\n' + auth_script
        if fail_turn:
            auth_script += "cat >/dev/null; printf '%s\\n' " + shlex.quote(fail_turn) + " >&2; exit 1\n"
        path.write_text('#!/bin/sh\n' + version_script + auth_script +
                        'cat > "$0.input"\nprintf \'%s\\n\' "$@" > "$0.args"\n' +
                        ('sleep 10\n' if sleep else '') +
                        "printf '%s' " + shlex.quote(output) + "\n")
        path.chmod(0o700)
        return path

    try:
        env = transport._environment()
        assert all(not env.contains(name) for name in API_ENVIRONMENT_VARIABLES)
        checks.append("API-key environment excluded")
        with tempfile.TemporaryDirectory(prefix="freecad-ai-transport-test-") as directory:
            for provider in ("claude", "codex", "copilot"):
                cli = fixture(directory, provider)
                ok, result = request(ProviderSettings(provider=provider, cli_executable=str(cli)))
                assert ok and result.message == provider + " transport works"
                assert "Explain a box" in Path(str(cli) + ".input").read_text()
                assert transport.idle
                if provider != "copilot":
                    assert progress
                checks.append(provider + " native subprocess and stdin")
            for provider, auth in (("claude", '{"loggedIn":true,"authMethod":"api_key"}'),
                                   ("codex", "Logged in using an API key")):
                cli = fixture(directory, provider, auth=auth)
                Path(str(cli) + ".input").unlink()
                ok, result = request(ProviderSettings(provider=provider, cli_executable=str(cli)))
                assert not ok and "Sign in" in result
                assert not Path(str(cli) + ".input").exists()
            checks.append("API-billed login rejected before inference")
            for provider in ("claude", "codex"):
                cli = fixture(directory, provider, interrupted=True)
                ok, result = request(ProviderSettings(provider=provider, cli_executable=str(cli)))
                assert not ok and "interrupted" in result
            checks.append("incomplete native turns rejected")

            cli = Path(directory) / "signed-out-copilot"
            cli.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'%s\\n\' '
                           '\'Error: No authentication information found.\' >&2\nexit 1\n')
            cli.chmod(0o700)
            ok, result = request(ProviderSettings(provider="copilot", cli_executable=str(cli)))
            assert not ok and "copilot login" in result
            checks.append("Copilot native sign-in instruction")

            cli = fixture(directory, "copilot", sleep=True)
            outcomes.clear()
            sent = transport.send(ProviderSettings(provider="copilot", cli_executable=str(cli)),
                                  b"Stop test")
            QtCore.QTimer.singleShot(50, transport.cancel)
            QtCore.QTimer.singleShot(50, transport.cancel)  # Repeated Stop is harmless.
            _wait(QtCore.QEventLoop, QtCore.QTimer, 1200)
            assert not outcomes and transport.idle and not transport.closing
            assert not os.path.exists(sent.scratch_path)
            checks.append("Stop kills child and removes scratch directory")

            # A provider whose descendants ignore SIGTERM: the whole tree must go.
            assert processes.TREE_TERMINATION, "This platform cannot stop process trees"
            pids = Path(directory) / "tree.pids"
            tree = Path(directory) / "tree-copilot"
            tree.write_text("#!/bin/sh\ntrap '' TERM\ncat >/dev/null\n"
                            "sh -c 'trap \"\" TERM; sleep 60 & echo $! >> " + str(pids) +
                            "; wait' &\necho $! >> " + str(pids) + "\necho $$ >> " + str(pids) +
                            "\nwhile :; do sleep 1; done\n")
            tree.chmod(0o700)
            outcomes.clear()
            beats = []
            heartbeat = QtCore.QTimer()
            heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
            heartbeat.start(50)
            sent = transport.send(ProviderSettings(provider="copilot", cli_executable=str(tree)),
                                  b"Tree test")
            _wait(QtCore.QEventLoop, QtCore.QTimer, 600)
            started = [int(line) for line in pids.read_text().split()]
            assert len(started) == 3 and all(_alive(pid) for pid in started), started
            stop_time = time.monotonic()
            transport.cancel()
            while time.monotonic() - stop_time < 2.0 and any(_alive(pid) for pid in started):
                _wait(QtCore.QEventLoop, QtCore.QTimer, 50)
            elapsed = time.monotonic() - stop_time
            survivors = [pid for pid in started if _alive(pid)]
            assert not survivors, "Processes survived Stop: {}".format(survivors)
            gaps = [b - a for a, b in zip(beats, beats[1:]) if a >= stop_time - 0.1]
            heartbeat.stop()
            assert gaps and max(gaps) < 0.25, "GUI stalled during Stop: {:.3f}s".format(max(gaps or [0]))
            _wait(QtCore.QEventLoop, QtCore.QTimer, 200)
            assert not os.path.exists(sent.scratch_path) and not outcomes
            checks.append("Stop ends child and grandchild ignoring SIGTERM in {:.2f}s; "
                          "GUI responsive".format(elapsed))

            # Timeout reports once and also ends the tree.
            pids.unlink()
            outcomes.clear()
            sent = transport.send(ProviderSettings(provider="copilot", cli_executable=str(tree),
                                                   timeout_seconds=10), b"Timeout test", "timeout")
            transport.timer.start(400)
            deadline.start(5000)
            loop.exec_()
            deadline.stop()
            _wait(QtCore.QEventLoop, QtCore.QTimer, 1500)
            started = [int(line) for line in pids.read_text().split()]
            assert len(outcomes) == 1 and not outcomes[0][0] and "timed out" in outcomes[0][1]
            assert not [pid for pid in started if _alive(pid)]
            assert not os.path.exists(sent.scratch_path)
            checks.append("timeout ends the tree and reports once")

            # Application exit: synchronous, bounded cleanup.
            pids.unlink()
            sent = transport.send(ProviderSettings(provider="copilot", cli_executable=str(tree)),
                                  b"Exit test")
            _wait(QtCore.QEventLoop, QtCore.QTimer, 600)
            started = [int(line) for line in pids.read_text().split()]
            shutdown_time = time.monotonic()
            transport.shutdown()
            shutdown_elapsed = time.monotonic() - shutdown_time
            _wait(QtCore.QEventLoop, QtCore.QTimer, 300)
            assert not [pid for pid in started if _alive(pid)]
            assert not os.path.exists(sent.scratch_path) and shutdown_elapsed < 1.0
            checks.append("application shutdown cleanup in {:.2f}s".format(shutdown_elapsed))
        ok, result = request(ProviderSettings(provider="claude", cli_executable="missing-ai-test-cli"))
        assert not ok and "Could not run" in result
        checks.append("missing executable handling")

        def readiness(settings):
            statuses = []
            checker = ReadinessCheck()
            checker.finished.connect(statuses.append)
            checker.start(settings, use_cache=False)
            started = time.monotonic()
            while not statuses and time.monotonic() - started < 10:
                _wait(QtCore.QEventLoop, QtCore.QTimer, 20)
            checker.deleteLater()
            assert len(statuses) == 1, "Readiness check did not finish once"
            return statuses[0]

        with tempfile.TemporaryDirectory(prefix="freecad-ai-ready-test-") as directory:
            cases = [
                ("signed in", ProviderSettings("claude", cli_executable=str(fixture(directory, "claude"))),
                 Readiness.READY),
                ("signed out", ProviderSettings("claude", cli_executable=str(fixture(
                    directory, "claude", auth='{"loggedIn": false}', name="out"))), Readiness.SIGN_IN_REQUIRED),
                ("API billing", ProviderSettings("codex", cli_executable=str(fixture(
                    directory, "codex", auth="Logged in using an API key", name="billed"))),
                 Readiness.SIGN_IN_REQUIRED),
                ("missing", ProviderSettings("codex", cli_executable=str(Path(directory) / "absent")),
                 Readiness.MISSING_CLI),
                ("obsolete", ProviderSettings("claude", cli_executable=str(fixture(
                    directory, "claude", version="2.0.1 (Claude Code)", name="old"))),
                 Readiness.UNSUPPORTED_VERSION),
                ("malformed", ProviderSettings("claude", cli_executable=str(fixture(
                    directory, "claude", auth="<html>oops</html>", name="odd"))), Readiness.UNKNOWN),
                ("no status command", ProviderSettings("copilot", cli_executable=str(fixture(
                    directory, "copilot"))), Readiness.UNKNOWN),
            ]
            for label, settings, expected in cases:
                status = readiness(settings)
                assert status.state is expected, (label, status.state, status.message)
            checks.append("readiness: signed in, signed out, API billing, missing, obsolete, "
                          "malformed, no status command")

            CACHE.invalidate()
            cli = fixture(directory, "claude", name="cached")
            calls = Path(str(cli) + ".calls")
            for _ in range(2):
                ok, result = request(ProviderSettings("claude", cli_executable=str(cli)))
                assert ok
            assert calls.read_text().split().count("auth") == 1, calls.read_text()
            checks.append("successful sign-in check cached between requests")

            for name, stderr, needle in (("limited", "Error: 429 rate limit exceeded", "usage limit"),
                                         ("model", "Error: model_not_found", "rejected the model"),
                                         ("expired", "Not logged in. Please run claude auth login",
                                          "not signed in"),
                                         ("panic", "thread main panicked", "turn failed")):
                cli = fixture(directory, "claude", name=name, fail_turn=stderr)
                ok, result = request(ProviderSettings("claude", cli_executable=str(cli)))
                assert not ok and needle in result and stderr not in result, (name, result)
            assert CACHE.get("claude", str(fixture(directory, "claude", name="expired"))) is None
            checks.append("failures classified (limit, model, sign-in, generic) without raw stderr")
        print("PASS: " + ", ".join(checks))
    finally:
        deadline.stop()
        transport.cancel()
        transport.deleteLater()
