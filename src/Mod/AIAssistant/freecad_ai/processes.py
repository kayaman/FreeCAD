# SPDX-License-Identifier: LGPL-2.1-or-later
"""Own native provider and worker processes, including all their descendants.

On POSIX every process starts in its own session, so Stop can signal the whole
process group without touching other CLI sessions. On Windows the process tree
is ended by PID with taskkill /T. Stopping never blocks the GUI thread: it asks
for graceful termination, forces termination after a grace period, and reports
exactly one outcome once no owned process remains.
"""

import os
import shutil
import signal
import subprocess
import sys

try:
    from PySide import QtCore
except ImportError:
    try:
        from PySide2 import QtCore
    except ImportError:
        from PySide6 import QtCore

GRACE_MS = 500
POLL_MS = 50
REAP_LIMIT_MS = 3000

EXITED = "exited"
CRASHED = "crashed"
CANCELLED = "cancelled"
TIMED_OUT = "timed out"
START_FAILED = "start failed"


class Outcome:
    def __init__(self, kind, code=None):
        self.kind = kind
        self.code = code

    @property
    def ok(self):
        return self.kind == EXITED and self.code == 0

    def __repr__(self):
        return "Outcome({!r}, {!r})".format(self.kind, self.code)


def _session_method():
    """How this Qt build starts a child in a new session, or None."""
    if sys.platform == "win32":
        return None
    parameters = getattr(QtCore.QProcess, "UnixProcessParameters", None)
    if parameters is not None and hasattr(QtCore.QProcess, "setUnixProcessParameters"):
        flags = getattr(QtCore.QProcess, "UnixProcessFlag", None)
        if flags is not None and hasattr(flags, "CreateNewSession"):
            return "qt"
    if shutil.which("setsid"):
        return "setsid"
    return None


SESSION_METHOD = _session_method()
_FAILED_TO_START = getattr(getattr(QtCore.QProcess, "ProcessError", QtCore.QProcess), "FailedToStart")
_CRASH_EXIT = getattr(getattr(QtCore.QProcess, "ExitStatus", QtCore.QProcess), "CrashExit")
# True when Stop reaches descendants, not only the direct child.
TREE_TERMINATION = sys.platform == "win32" or SESSION_METHOD is not None


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _windows_tree_kill(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


class ProcessSupervisor(QtCore.QObject):
    """Runs one process. Emits stdout/stderr chunks and one finished(Outcome)."""

    stdout = QtCore.Signal(bytes)
    stderr = QtCore.Signal(bytes)
    finished = QtCore.Signal(object)

    def __init__(self, parent=None, grace_ms=GRACE_MS):
        super().__init__(parent)
        self.grace_ms = grace_ms
        self.process = None
        self.pid = None
        self.pgid = None
        self.outcome = None
        self._stdin = b""
        self._ending = None       # Outcome kind requested by cancel/timeout.
        self._leader_done = False
        self._exit = None
        self._reap_elapsed = 0
        self._cleanups = []
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._end(TIMED_OUT))
        self._force = QtCore.QTimer(self)
        self._force.setSingleShot(True)
        self._force.timeout.connect(self._kill)
        self._reaper = QtCore.QTimer(self)
        self._reaper.setInterval(POLL_MS)
        self._reaper.timeout.connect(self._reap)

    @property
    def running(self):
        return self.process is not None and self.outcome is None

    def add_cleanup(self, callback):
        """Run callback after every owned process has exited (or immediately if done)."""
        if self.outcome is not None:
            callback()
        else:
            self._cleanups.append(callback)

    def start(self, program, arguments, environment=None, working_directory=None,
              stdin=b"", timeout_ms=None):
        if self.process is not None:
            raise RuntimeError("A supervisor runs one process.")
        self._stdin = bytes(stdin or b"")
        process = QtCore.QProcess(self)
        self.process = process
        if environment is not None:
            process.setProcessEnvironment(environment)
        if working_directory:
            process.setWorkingDirectory(working_directory)
        if SESSION_METHOD == "qt":
            parameters = QtCore.QProcess.UnixProcessParameters()
            parameters.flags = QtCore.QProcess.UnixProcessFlag.CreateNewSession
            process.setUnixProcessParameters(parameters)
        elif SESSION_METHOD == "setsid":
            # setsid execs directly because the child is not a group leader;
            # --wait keeps the exit status of the provider.
            arguments = ["--wait", program] + list(arguments)
            program = shutil.which("setsid")
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.started.connect(self._started)
        process.finished.connect(self._leader_finished)
        process.errorOccurred.connect(self._error)
        if timeout_ms:
            self._timeout.start(int(timeout_ms))
        process.start(program, list(arguments))

    def _started(self):
        self.pid = int(self.process.processId()) or None
        if self.pid and SESSION_METHOD is not None:
            self.pgid = self.pid  # New session: the child leads its own group.
        if self._ending is not None:
            self._terminate()
            return
        if self._stdin:
            self.process.write(QtCore.QByteArray(self._stdin))
        self.process.closeWriteChannel()

    def _read_stdout(self):
        if self.process is not None:
            data = bytes(self.process.readAllStandardOutput())
            if data and self._ending is None:
                self.stdout.emit(data)

    def _read_stderr(self):
        if self.process is not None:
            data = bytes(self.process.readAllStandardError())
            if data and self._ending is None:
                self.stderr.emit(data)

    def _error(self, error):
        if error == _FAILED_TO_START:
            self._leader_done = True
            self._exit = Outcome(START_FAILED)
            self._finish()

    def _leader_finished(self, code, status):
        if self.outcome is not None:
            return
        self._read_stdout()
        self._read_stderr()
        self._exit = Outcome(CRASHED if status == _CRASH_EXIT else EXITED, int(code))
        self._leader_done = True
        if self.pgid is not None and self._ending is None:
            # Descendants must not outlive the run, even after a normal exit.
            _signal_group(self.pgid, signal.SIGTERM)
            self._force.start(self.grace_ms)
        self._reap_elapsed = 0
        self._reaper.start()
        self._reap()

    def cancel(self):
        self._end(CANCELLED)

    def _end(self, kind):
        if self.outcome is not None or self._ending is not None or self.process is None:
            return
        self._ending = kind
        self._timeout.stop()
        if self.pid is not None:
            self._terminate()

    def _terminate(self):
        if sys.platform == "win32":
            # Console CLIs ignore WM_CLOSE, and taskkill /T needs the parent alive
            # to find descendants, so end the tree now. Untested on Windows.
            _windows_tree_kill(self.pid)
        elif self.pgid is not None:
            _signal_group(self.pgid, signal.SIGTERM)
        else:
            self.process.terminate()
        self._force.start(self.grace_ms)
        self._reap_elapsed = 0
        self._reaper.start()

    def _kill(self):
        if sys.platform == "win32" and self.pid is not None:
            _windows_tree_kill(self.pid)
        elif self.pgid is not None:
            _signal_group(self.pgid, signal.SIGKILL)
        if self.process is not None and not self._leader_done:
            self.process.kill()

    def _reap(self):
        self._reap_elapsed += POLL_MS
        group_alive = self.pgid is not None and _group_alive(self.pgid)
        if self._leader_done and not group_alive:
            self._finish()
        elif self._reap_elapsed > REAP_LIMIT_MS + self.grace_ms:
            self._kill()
            if self._leader_done:
                self._finish()  # Bounded: report rather than wait forever.

    def _finish(self):
        if self.outcome is not None:
            return
        self._timeout.stop()
        self._force.stop()
        self._reaper.stop()
        if self._ending is not None:
            self.outcome = Outcome(self._ending, self._exit.code if self._exit else None)
        else:
            self.outcome = self._exit or Outcome(CRASHED)
        process, self.process = self.process, None
        if process is not None:
            process.deleteLater()
        self._run_cleanups()
        self.finished.emit(self.outcome)

    def _run_cleanups(self):
        callbacks, self._cleanups = self._cleanups, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def shutdown(self):
        """Application exit: end the tree immediately and clean up synchronously."""
        if self.outcome is not None or self.process is None:
            return
        self._ending = self._ending or CANCELLED
        if sys.platform == "win32" and self.pid is not None:
            _windows_tree_kill(self.pid)
        elif self.pgid is not None:
            _signal_group(self.pgid, signal.SIGKILL)
        if not self._leader_done:
            self.process.kill()
            self.process.waitForFinished(500)
        self._leader_done = True
        self._finish()
