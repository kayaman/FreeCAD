# SPDX-License-Identifier: LGPL-2.1-or-later
"""Dock panel and native signed-in CLI integration, following Bancada."""

import json
from pathlib import Path
import tempfile
import weakref

import FreeCAD as App
import FreeCADGui as Gui

try:
    from PySide import QtCore, QtGui
    try:
        from PySide import QtWidgets
    except ImportError:
        QtWidgets = QtGui
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide6 import QtCore, QtGui, QtWidgets

from .core import (API_ENVIRONMENT_VARIABLES, MAX_BRIEF_CHARS, PROVIDER_NAMES, AssistantError,
                   BriefOverflow, NativeEvents, ProviderSettings, load_settings, save_settings,
                   validate_native_login)
from .document import DocumentSnapshot, execute_step, external_links, model_context
from .execution import apply_delta, make_worker_job
from .validation import fingerprint, summarize
from .processes import START_FAILED, ProcessSupervisor
from .providers import CACHE, Failure, Readiness, Status, adapter_for, classify
from .session import Outcome, SessionRegistry, TaskState, UndoRefused, plan_undo

PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/AIAssistant"
WORKER_TIMEOUT_SECONDS = 300
_panel = None


def native_environment():
    """The user's environment without variables that would select API billing."""
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in API_ENVIRONMENT_VARIABLES:
        env.remove(key)
    env.remove("CLAUDECODE")
    env.insert("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    env.insert("COPILOT_ALLOW_ALL", "false")
    env.insert("COPILOT_AUTO_UPDATE", "false")
    env.insert("GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS", "false")
    return env


class _Run:
    """One provider request: a login check (if any), then the inference turn."""
    def __init__(self, settings, body, token):
        self.settings = settings
        self.body = body
        self.token = token
        self.events = NativeEvents(settings.provider)
        self.scratch = tempfile.TemporaryDirectory(prefix="freecad-ai-native-")
        self.scratch_path = self.scratch.name
        self.phase = ""
        self.supervisor = None
        self.auth_stdout = bytearray()
        self.auth_stderr = bytearray()
        self.turn_stderr = bytearray()
        self.cleaned = False

    def cleanup(self):
        if not self.cleaned:
            self.cleaned = True
            self.scratch.cleanup()


class Transport(QtCore.QObject):
    """Runs native provider turns. Every emission carries the request token."""
    completed = QtCore.Signal(object, object)
    failed = QtCore.Signal(object, str)
    progress = QtCore.Signal(object, str)
    turn_started = QtCore.Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timed_out)
        self.run = None
        self.closing = []  # Ended runs whose processes are still being stopped.
        self.last_failure = None

    def _timed_out(self):
        if self.run is not None:
            self.last_failure = Failure.TIMEOUT
            self._fail(self.run, adapter_for(self.run.settings.provider).failure_message(
                Failure.TIMEOUT))

    @property
    def idle(self):
        return self.run is None

    def send(self, settings, body, token=None):
        self.cancel()
        settings.validate()
        run = _Run(settings, body, token)
        self.run = run
        self.last_failure = None
        self.timer.start(settings.timeout_seconds * 1000)
        auth_args = settings.auth_arguments()
        if auth_args is not None and CACHE.get(settings.provider, settings.executable) is None:
            self._start(run, auth_args, "auth")
        else:
            self._start_turn(run)
        return run

    @property
    def checking(self):
        """True while the current request is in its login check."""
        return self.run is not None and self.run.phase == "auth"

    def _environment(self):
        return native_environment()

    def _start_turn(self, run):
        config_path = Path(run.scratch_path) / "mcp.json"
        config_path.write_text(json.dumps({"mcpServers": {}}))
        self._start(run, run.settings.arguments(str(config_path)), "turn")
        self.turn_started.emit(run.token)

    def _start(self, run, arguments, phase):
        run.phase = phase
        supervisor = ProcessSupervisor(self)
        run.supervisor = supervisor
        supervisor.stdout.connect(lambda data: self._stdout(run, data))
        supervisor.stderr.connect(lambda data: self._stderr(run, data))
        supervisor.finished.connect(lambda outcome: self._finished(run, supervisor, outcome))
        supervisor.start(run.settings.executable, arguments, self._environment(), run.scratch_path,
                         stdin=run.body if phase == "turn" else b"")

    def _stdout(self, run, data):
        if run is not self.run:
            return
        if run.phase == "auth":
            run.auth_stdout.extend(data)
            if len(run.auth_stdout) > 65536:
                self._fail(run, "Native login check returned too much data.")
            return
        try:
            for text in run.events.feed(data):
                self.progress.emit(run.token, text)
        except AssistantError as error:
            self._fail(run, str(error))

    def _stderr(self, run, data):
        # Native stderr may contain local paths or credentials; do not display it.
        buffer = run.auth_stderr if run.phase == "auth" else run.turn_stderr
        buffer.extend(data[:max(0, 65536 - len(buffer))])

    def _finished(self, run, supervisor, outcome):
        supervisor.deleteLater()
        if run is not self.run or supervisor is not run.supervisor:
            return
        run.supervisor = None
        settings = run.settings
        adapter = adapter_for(settings.provider)
        if outcome.kind == START_FAILED:
            self._fail(run, "Could not run {}. Install its CLI, sign in, and check the executable "
                            "path in Settings.".format(adapter.label))
            return
        stderr = bytes(run.auth_stderr if run.phase == "auth" else run.turn_stderr).decode(
            "utf-8", "replace")
        if not outcome.ok:
            self._fail(run, self._diagnose(run, classify(stderr, run.events.error_text), run.phase))
            return
        if run.phase == "auth":
            output = bytes(run.auth_stdout)
            if settings.provider == "codex":
                output += bytes(run.auth_stderr)
            try:
                validate_native_login(settings.provider, output.decode("utf-8"))
            except (AssistantError, UnicodeError) as error:
                CACHE.invalidate(settings.provider)
                self.last_failure = Failure.AUTH
                self._fail(run, str(error))
                return
            CACHE.put(settings.provider, settings.executable,
                      Status(Readiness.READY, "Signed in.", None))
            self._start_turn(run)
            return
        self.run = None
        self.timer.stop()
        run.cleanup()
        try:
            result = run.events.finish()
        except AssistantError as error:
            if run.events.error:
                message = self._diagnose(run, classify(stderr, run.events.error_text), "turn")
            else:
                self.last_failure, message = Failure.PROTOCOL, str(error)
            self.failed.emit(run.token, message)
        else:
            CACHE.put(settings.provider, settings.executable,
                      Status(Readiness.READY, "A request succeeded.", None, live=True))
            self.completed.emit(run.token, result)

    def _diagnose(self, run, category, phase):
        """A message from evidence; raw stderr and credentials never reach the panel."""
        self.last_failure = category
        if category is Failure.AUTH:
            CACHE.invalidate(run.settings.provider)
        return adapter_for(run.settings.provider).failure_message(category, phase)

    def _close(self, run):
        """End a run; its scratch directory outlives every process it started."""
        supervisor = run.supervisor
        run.supervisor = None
        if supervisor is not None and supervisor.running:
            self.closing.append(run)
            supervisor.add_cleanup(run.cleanup)
            supervisor.add_cleanup(lambda: self.closing.remove(run) if run in self.closing else None)
            supervisor.cancel()
        else:
            run.cleanup()

    def cancel(self):
        """Stop the current request asynchronously. Repeated calls are harmless."""
        self.timer.stop()
        run, self.run = self.run, None
        if run is not None:
            self._close(run)

    def _fail(self, run, message):
        if run is None or run is not self.run:
            return
        self.cancel()
        self.failed.emit(run.token, message)

    def shutdown(self):
        """Application exit: end every owned process now and remove scratch files."""
        self.timer.stop()
        runs = self.closing + ([self.run] if self.run is not None else [])
        self.run, self.closing = None, []
        for supervisor in self.findChildren(ProcessSupervisor):
            supervisor.shutdown()
        for run in runs:
            run.cleanup()


LIVE_PROBE = ('Reply ONLY with {"message":"Native sign-in works", "python":""}. '
              'This is a chat-only connection test; do not propose any modeling code.')


class ReadinessCheck(QtCore.QObject):
    """Non-inference check: CLI present, version supported, native sign-in status."""
    finished = QtCore.Signal(object)

    def __init__(self, parent=None, timeout_ms=20000):
        super().__init__(parent)
        self.timeout_ms = timeout_ms
        self.settings = None
        self.supervisor = None
        self.output = bytearray()
        self.errors = bytearray()
        self.version = None
        self.done = False

    def start(self, settings, use_cache=True):
        self.settings = settings
        cached = CACHE.get(settings.provider, settings.executable) if use_cache else None
        if cached is not None:
            QtCore.QTimer.singleShot(0, lambda: self._emit(cached))
            return
        self._run(["--version"], self._versioned)

    def _run(self, arguments, then):
        self.output, self.errors = bytearray(), bytearray()
        supervisor = ProcessSupervisor(self)
        self.supervisor = supervisor
        supervisor.stdout.connect(lambda data: self.output.extend(data[:65536]))
        supervisor.stderr.connect(lambda data: self.errors.extend(data[:65536]))
        supervisor.finished.connect(lambda outcome: then(outcome))
        supervisor.start(self.settings.executable, arguments, native_environment(),
                         tempfile.gettempdir(), timeout_ms=self.timeout_ms)

    def _text(self):
        return bytes(self.output).decode("utf-8", "replace"), bytes(self.errors).decode("utf-8", "replace")

    def _versioned(self, outcome):
        adapter = adapter_for(self.settings.provider)
        if outcome.kind == START_FAILED:
            self._emit(Status(Readiness.MISSING_CLI, "{} was not found. Install it or set its "
                              "path in Settings.".format(adapter.label)))
            return
        if outcome.kind != "exited":
            self._emit(Status(Readiness.UNAVAILABLE, "{} did not respond.".format(adapter.label)))
            return
        stdout, stderr = self._text()
        self.version, state, message = adapter.check_version(stdout + "\n" + stderr)
        if state is not None:
            self._emit(Status(state, message, self.version))
        elif not adapter.status_arguments:
            self._emit(Status(*adapter.interpret_status("", "", 0), self.version))
        else:
            self._run(list(adapter.status_arguments), self._status)

    def _status(self, outcome):
        adapter = adapter_for(self.settings.provider)
        if outcome.kind != "exited":
            self._emit(Status(Readiness.UNAVAILABLE, "The {} status check did not finish.".format(
                adapter.label), self.version))
            return
        stdout, stderr = self._text()
        state, message = adapter.interpret_status(stdout, stderr, outcome.code)
        status = Status(state, message, self.version)
        CACHE.put(self.settings.provider, self.settings.executable, status)
        self._emit(status)

    def _emit(self, status):
        if not self.done:
            self.done = True
            self.finished.emit(status)

    def cancel(self):
        self.done = True
        if self.supervisor is not None:
            self.supervisor.cancel()


def describe_status(status):
    if status is None:
        return "not checked"
    text = status.state.value
    if status.version:
        text += " · v" + ".".join(str(part) for part in status.version)
    if status.state is Readiness.READY and status.live:
        text += " · live request verified"
    return text


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("AI assistant settings")
        form = QtWidgets.QFormLayout(self)
        self.provider = QtWidgets.QComboBox()
        for value, label in PROVIDER_NAMES.items():
            self.provider.addItem(label, value)
        self.provider.setCurrentIndex(max(0, self.provider.findData(settings.provider)))
        self.model = QtWidgets.QLineEdit(settings.model)
        self.model.setPlaceholderText("Leave blank to use the provider's default model")
        self.executable = QtWidgets.QLineEdit(settings.cli_executable)
        self.executable.setPlaceholderText(settings.provider)
        self.timeout = QtWidgets.QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setValue(settings.timeout_seconds)
        form.addRow("Provider", self.provider)
        form.addRow("Model (optional)", self.model)
        form.addRow("CLI executable (optional)", self.executable)
        form.addRow("Request timeout (seconds)", self.timeout)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        form.addRow(self.note)
        checks = QtWidgets.QHBoxLayout()
        self.check_button = QtWidgets.QPushButton("Check connection")
        self.check_button.setToolTip("Checks the CLI, its version and its sign-in status. "
                                     "No model request is made.")
        self.live_button = QtWidgets.QPushButton("Live check")
        self.live_button.setToolTip("Sends one short chat-only request through your account.")
        checks.addWidget(self.check_button)
        checks.addWidget(self.live_button)
        form.addRow(checks)
        self.check_result = QtWidgets.QLabel()
        self.check_result.setWordWrap(True)
        form.addRow(self.check_result)
        self.check_button.clicked.connect(self.check_connection)
        self.live_button.clicked.connect(self.live_check)
        self.checker = None
        self.live_transport = None
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self._update_note()

    def _update_note(self):
        provider = self.provider.currentData()
        commands = {"claude": "claude auth login", "codex": "codex login", "copilot": "copilot login"}
        self.note.setText("Uses your signed-in {} account. Sign in once in a terminal with: {}. "
                          "FreeCAD does not request or store credentials.".format(
                              PROVIDER_NAMES[provider], commands[provider]))

    def current_settings(self):
        return ProviderSettings(
            provider=self.provider.currentData(), model=self.model.text(),
            timeout_seconds=self.timeout.value(), cli_executable=self.executable.text())

    def check_connection(self):
        """Explicit non-inference readiness check of the values in the dialog."""
        if self.checker is not None:
            self.checker.cancel()
        self.check_result.setText("Checking…")
        self.checker = ReadinessCheck(self)
        self.checker.finished.connect(self._checked)
        self.checker.start(self.current_settings(), use_cache=False)

    def _checked(self, status):
        self.last_status = status
        self.check_result.setText("{}: {}".format(describe_status(status), status.message))

    def live_check(self):
        """One explicit chat-only request; never run automatically."""
        from .core import Conversation
        settings = self.current_settings()
        try:
            settings.validate()
        except AssistantError as error:
            self.check_result.setText(str(error))
            return
        if self.live_transport is None:
            self.live_transport = Transport(self)
            self.live_transport.completed.connect(self._live_completed)
            self.live_transport.failed.connect(lambda token, message: self.check_result.setText(
                "Live check failed: " + message))
        conversation = Conversation()
        conversation.begin(LIVE_PROBE)
        self.check_result.setText("Sending one live request…")
        self.live_transport.send(settings, conversation.request_body(settings), "live")

    def _live_completed(self, token, proposal):
        if proposal.python or proposal.message != "Native sign-in works":
            self.check_result.setText("Live check reached the provider, but the reply did not "
                                      "match. No code was run.")
        else:
            self.check_result.setText("Live check passed: native signed-in inference works.")

    def done(self, result):
        for helper in (self.checker, self.live_transport):
            if helper is not None:
                (helper.shutdown if hasattr(helper, "shutdown") else helper.cancel)()
        super().done(result)

    def _provider_changed(self):
        self.check_result.clear()
        self.model.clear()
        self.executable.clear()
        self.executable.setPlaceholderText(self.provider.currentData())
        self._update_note()

    def _save(self):
        self.result_settings = self.current_settings()
        try:
            save_settings(App.ParamGet(PARAM_PATH), self.result_settings)
        except AssistantError as error:
            QtWidgets.QMessageBox.warning(self, "Settings", str(error))
            return
        self.accept()


class DesignBriefDialog(QtWidgets.QDialog):
    """Edit the brief and pinned requirements of the current document session."""

    def __init__(self, conversation, parent, overflow=False):
        super().__init__(parent)
        self.conversation = conversation
        self.setWindowTitle("Design brief")
        self.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(
            "Sent with every request in this document's chat. Requirements are your own "
            "words, kept verbatim; edit, supersede or remove them here. New chat clears them.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addWidget(QtWidgets.QLabel("Brief"))
        self.brief = QtWidgets.QPlainTextEdit(conversation.brief)
        self.brief.setMaximumHeight(90)
        layout.addWidget(self.brief)
        layout.addWidget(QtWidgets.QLabel("Pinned requirements"))
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["ID", "Status", "Requirement"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        layout.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.supersede_button = QtWidgets.QPushButton("Superseded by…")
        self.remove_button = QtWidgets.QPushButton("Remove")
        row.addWidget(self.supersede_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.summarize = QtWidgets.QCheckBox("Replace active requirements with this summary")
        self.summary = QtWidgets.QPlainTextEdit()
        self.summary.setMaximumHeight(110)
        layout.addWidget(self.summarize)
        layout.addWidget(self.summary)
        self.usage = QtWidgets.QLabel()
        layout.addWidget(self.usage)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.supersede_button.clicked.connect(self._supersede)
        self.remove_button.clicked.connect(self._remove)
        self.brief.textChanged.connect(self._update_usage)
        self._pending = []  # (action, requirement id, argument), applied on Save.
        self._load()
        if overflow:
            self.summarize.setChecked(True)
            self.summary.setPlainText("\n".join(r["text"] for r in conversation.active_requirements))
        self._update_usage()

    def _load(self):
        rows = [r for r in self.conversation.requirements if r["status"] != "removed"]
        self.table.setRowCount(len(rows))
        for index, requirement in enumerate(rows):
            status = requirement["status"]
            if requirement.get("superseded_by"):
                status += " by " + requirement["superseded_by"]
            for column, text in enumerate((requirement["id"], status, requirement["text"])):
                item = QtWidgets.QTableWidgetItem(text)
                editable = column == 2 and requirement["status"] == "active"
                flags = item.flags()
                item.setFlags(flags | QtCore.Qt.ItemIsEditable if editable
                              else flags & ~QtCore.Qt.ItemIsEditable)
                self.table.setItem(index, column, item)

    def _selected_id(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 else None

    def _supersede(self):
        requirement_id = self._selected_id()
        others = [r["id"] for r in self.conversation.active_requirements if r["id"] != requirement_id]
        if requirement_id is None or not others:
            return
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Superseded by", "Requirement that replaces " + requirement_id, others, 0, False)
        if ok:
            self.supersede(requirement_id, choice)

    def supersede(self, requirement_id, by_id):
        self._pending.append(("supersede", requirement_id, by_id))
        self._mark(requirement_id, "superseded by " + by_id)

    def _remove(self):
        requirement_id = self._selected_id()
        if requirement_id is not None:
            self.remove(requirement_id)

    def remove(self, requirement_id):
        self._pending.append(("remove", requirement_id, None))
        self._mark(requirement_id, "removed")

    def _mark(self, requirement_id, status):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == requirement_id:
                self.table.item(row, 1).setText(status)

    def _update_usage(self):
        self.usage.setText("{} of {} characters used".format(
            self.conversation.brief_chars() - len(self.conversation.brief)
            + len(self.brief.toPlainText()), MAX_BRIEF_CHARS))

    def _save(self):
        conversation = self.conversation
        try:
            for row in range(self.table.rowCount()):
                requirement_id = self.table.item(row, 0).text()
                text = self.table.item(row, 2).text().strip()
                current = conversation._requirement(requirement_id)
                if current["status"] == "active" and text != current["text"]:
                    conversation.edit_requirement(requirement_id, text)
            for action, requirement_id, argument in self._pending:
                if action == "supersede":
                    conversation.supersede(requirement_id, argument)
                else:
                    conversation.remove_requirement(requirement_id)
            if self.summarize.isChecked():
                conversation.summarize_requirements(self.summary.toPlainText())
            conversation.set_brief(self.brief.toPlainText())
        except AssistantError as error:
            QtWidgets.QMessageBox.warning(self, "Design brief", str(error))
            return
        self.accept()


class PromptKeys(QtCore.QObject):
    """Enter sends the prompt; Shift+Enter inserts a new line."""

    def __init__(self, prompt, send):
        super().__init__(prompt)
        self.prompt = prompt
        self.send = send

    def eventFilter(self, watched, event):
        if event.type() != QtCore.QEvent.KeyPress or event.key() not in (
                QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            return False
        modifiers = event.modifiers() & ~QtCore.Qt.KeypadModifier
        if modifiers == QtCore.Qt.ShiftModifier:
            return False  # The text box inserts the line break.
        if modifiers != QtCore.Qt.NoModifier:
            return False
        if self.prompt.toPlainText().strip():
            self.send()
        return True  # An empty prompt is ignored rather than reported as an error.


class _DocumentObserver:
    """Forwards App document events; holds the panel weakly."""
    def __init__(self, panel):
        self.panel = weakref.ref(panel)

    def _call(self, name, doc):
        panel = self.panel()
        if panel is not None:
            try:
                getattr(panel, name)(doc)
            except RuntimeError:
                pass  # The Qt panel was already destroyed.

    def slotCreatedDocument(self, doc):
        self._call("_document_created", doc)

    def slotActivateDocument(self, doc):
        self._call("_document_activated", doc)

    def slotDeletedDocument(self, doc):
        self._call("_document_deleted", doc)


S = TaskState


class AssistantPanel(QtWidgets.QDockWidget):
    def __init__(self, parent):
        super().__init__("AI Assistant", parent)
        self.setObjectName("FreeCAD_AIAssistantDock")
        self.setMinimumWidth(330)
        self.settings = load_settings(App.ParamGet(PARAM_PATH))
        self.registry = SessionRegistry()
        self.session = self.registry.for_document(App.ActiveDocument)
        self._binding = False
        self.provider_status = None
        self._checker = None
        self.worker_job = None
        self._received_chars = 0
        self.transport = None
        self.attach_transport(Transport(self))
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.transcript = QtWidgets.QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.document().setMaximumBlockCount(1500)
        layout.addWidget(self.transcript, 2)
        self.prompt = QtWidgets.QPlainTextEdit()
        self.prompt.setPlaceholderText("Create a box 40 × 30 × 10 mm, then drill a 5 mm hole… "
                                       "(Enter to send, Shift+Enter for a new line)")
        self.prompt.setMaximumHeight(100)
        self.prompt_keys = PromptKeys(self.prompt, self._send)
        self.prompt.installEventFilter(self.prompt_keys)
        layout.addWidget(self.prompt)
        self.share_context = QtWidgets.QCheckBox("Send model summary and selection to provider")
        self.share_context.setChecked(True)
        layout.addWidget(self.share_context)
        self.context_notice = QtWidgets.QLabel(
            "New requests will not include the model summary. Messages and summaries already "
            "sent remain part of this chat; use New chat to start a clean session.")
        self.context_notice.setWordWrap(True)
        self.context_notice.setVisible(False)
        layout.addWidget(self.context_notice)
        self.share_context.toggled.connect(lambda shared: self.context_notice.setVisible(not shared))
        self.autonomous = QtWidgets.QCheckBox("Run modeling steps automatically (up to 6)")
        self.autonomous.setChecked(True)
        layout.addWidget(self.autonomous)
        self.notice = QtWidgets.QLabel(
            "Automatic steps run in a separate FreeCAD process, so Stop works at any time; it "
            "isolates crashes but is not a security sandbox: generated code has your user "
            "permissions. Turn off automatic steps to review code, which then runs inside "
            "FreeCAD and cannot be stopped midway.")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        row = QtWidgets.QHBoxLayout()
        self.send_button = QtWidgets.QPushButton("Send")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.continue_button = QtWidgets.QPushButton("Continue")
        self.settings_button = QtWidgets.QPushButton("Settings")
        self.brief_button = QtWidgets.QPushButton("Design brief…")
        self.clear_button = QtWidgets.QPushButton("New chat")
        for widget in (self.send_button, self.stop_button, self.continue_button,
                       self.brief_button, self.settings_button, self.clear_button):
            row.addWidget(widget)
        layout.addLayout(row)
        layout.addWidget(QtWidgets.QLabel("Task timeline"))
        self.timeline = QtWidgets.QTreeWidget()
        self.timeline.setHeaderLabels(["Step", "State", "Explanation"])
        self.timeline.setRootIsDecorated(True)
        self.timeline.currentItemChanged.connect(lambda current, previous: self._show_step())
        layout.addWidget(self.timeline, 1)
        self.step_tabs = QtWidgets.QTabWidget()
        self.code = QtWidgets.QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.step_tabs.addTab(self.code, "Code")
        self.step_tabs.addTab(self.details, "Result")
        layout.addWidget(self.step_tabs, 1)
        actions = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run reviewed code")
        self.undo_button = QtWidgets.QPushButton("Undo last step")
        self.undo_task_button = QtWidgets.QPushButton("Undo task")
        actions.addWidget(self.run_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.undo_task_button)
        layout.addLayout(actions)
        self.setWidget(container)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self._stop)
        self.continue_button.clicked.connect(self._continue)
        self.settings_button.clicked.connect(self._settings)
        self.brief_button.clicked.connect(lambda: self.open_brief())
        self.clear_button.clicked.connect(self._clear)
        self.run_button.clicked.connect(self._run_reviewed)
        self.undo_button.clicked.connect(lambda: self._undo("step"))
        self.undo_task_button.clicked.connect(lambda: self._undo("task"))
        self._observer = _DocumentObserver(self)
        App.addDocumentObserver(self._observer)
        self._update_info()
        self._log("Assistant", "Configure a provider in Settings, then describe your model. "
                  "Automatic steps are enabled; each successful step can be undone.")
        self._refresh()
        self.check_provider()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.shutdown)

    def attach_transport(self, transport):
        """Use transport for provider requests (tests attach a fake provider here)."""
        if self.transport is not None:
            self.transport.cancel()
            for signal in (self.transport.completed, self.transport.failed, self.transport.progress):
                signal.disconnect()
        self.transport = transport
        transport.completed.connect(self._received)
        transport.failed.connect(self._request_failed)
        transport.progress.connect(self._progress)
        if hasattr(transport, "turn_started"):
            transport.turn_started.connect(self._turn_started)

    # Session and task helpers -------------------------------------------------

    @property
    def task(self):
        return self.session.task

    @property
    def conversation(self):
        return self.session.conversation

    @property
    def busy(self):
        return self.task is not None and self.task.running

    def _update_info(self):
        status = self.provider_status
        text = "{} · {} · {}".format(PROVIDER_NAMES.get(self.settings.provider, "Provider"),
                                     self.settings.model or "default model", describe_status(status))
        if status is not None and status.state not in (Readiness.READY, Readiness.CHECKING):
            text += "\n" + status.message
        self.info.setText(text)

    def check_provider(self, use_cache=True):
        """Non-inference readiness check; runs when the panel opens and settings change."""
        if self._checker is not None:
            self._checker.cancel()
        self.provider_status = Status(Readiness.CHECKING)
        self._update_info()
        self._checker = ReadinessCheck(self)
        self._checker.finished.connect(self._provider_checked)
        self._checker.start(self.settings, use_cache)

    def _provider_checked(self, status):
        self.provider_status = status
        self._update_info()

    def _log(self, speaker, text, session=None):
        session = session or self.session
        session.log(speaker, text)
        if session is self.session:
            self.transcript.appendPlainText("{}: {}\n".format(speaker, text))

    def _render_session(self):
        self.transcript.clear()
        for speaker, text in self.session.transcript:
            self.transcript.appendPlainText("{}: {}\n".format(speaker, text))
        self.code.clear()
        self.details.clear()
        self._refresh()

    def _refresh(self):
        task = self.task
        state = task.state if task is not None else S.IDLE
        running = state in (S.CHECKING_PROVIDER, S.THINKING, S.EXECUTING, S.APPLYING)
        for widget in (self.send_button, self.settings_button, self.clear_button,
                       self.brief_button, self.share_context, self.autonomous, self.undo_button,
                       self.undo_task_button):
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running or state in (S.PAUSED, S.AWAITING_REVIEW))
        self.continue_button.setVisible(state is S.PAUSED)
        self.run_button.setEnabled(state is S.AWAITING_REVIEW)
        names = {S.CHECKING_PROVIDER: "Checking provider…", S.THINKING: "Thinking…",
                 S.EXECUTING: "Running modeling step…", S.APPLYING: "Applying changes…",
                 S.AWAITING_REVIEW: "Review the proposed code, then run it or stop.",
                 S.PAUSED: "Paused. Continue resumes this task."}
        self.status.setText(names.get(state, ""))
        self._render_timeline()

    # Timeline ---------------------------------------------------------------------

    def _render_timeline(self):
        selected = self._selected_step()
        self.timeline.blockSignals(True)
        self.timeline.clear()
        current = None
        for task in self.session.tasks:
            parent = QtWidgets.QTreeWidgetItem([task.id, task.state.value, task.goal[:200]])
            parent.setData(0, QtCore.Qt.UserRole, None)
            self.timeline.addTopLevelItem(parent)
            for step in task.steps:
                item = QtWidgets.QTreeWidgetItem([step.id.split("/")[-1], step.outcome.value,
                                                  step.message[:200]])
                item.setData(0, QtCore.Qt.UserRole, step.id)
                parent.addChild(item)
                if step is selected or (selected is None and step is self._latest_step()):
                    current = item
            parent.setExpanded(task is self.task)
        if current is not None:
            self.timeline.setCurrentItem(current)
        self.timeline.blockSignals(False)
        self._show_step()

    def _latest_step(self):
        task = self.task
        return task.steps[-1] if task is not None and task.steps else None

    def _find_step(self, step_id):
        for task in self.session.tasks:
            for step in task.steps:
                if step.id == step_id:
                    return step
        return None

    def _selected_step(self):
        item = self.timeline.currentItem()
        step_id = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        return self._find_step(step_id) if step_id else None

    def _show_step(self):
        step = self._selected_step()
        if step is None:
            return
        self.code.setPlainText(step.code)
        lines = ["Step {}: {}".format(step.id, step.outcome.value), "", step.message]
        if step.changed:
            lines += ["", "Changed objects:"]
            for name in step.changed:
                properties = step.properties.get(name)
                lines.append("  {}{}".format(name, " ({})".format(", ".join(properties))
                                             if properties else ""))
        if step.result:
            lines += ["", step.result]
        self.details.setPlainText("\n".join(lines))

    def _record(self, step, session=None):
        """Keep the execution ledger in step with what actually happened."""
        if step is not None:
            (session or self.session).conversation.log_step(step.id, step.outcome.value, step.message)
            if session is None or session is self.session:
                self._render_timeline()

    def open_brief(self, overflow=False):
        """Open the design brief without blocking the GUI; returns the dialog."""
        dialog = DesignBriefDialog(self.conversation, self, overflow)
        dialog.open()
        return dialog

    def _transition(self, state):
        self.task.transition(state)
        self._refresh()

    # Requests -------------------------------------------------------------------

    def _send(self):
        prompt = self.prompt.toPlainText().strip()
        self._sync_session()
        if self.busy:
            return
        try:
            self.settings.validate()
            snapshot = DocumentSnapshot.capture(App, Gui)
            context = model_context(App, Gui) if self.share_context.isChecked() else None
            omitted = (context or {}).get("omitted", {}).get("selected")
            if omitted:
                raise AssistantError("The selection is too large to describe ({} objects omitted, "
                                     "for example {}). Select fewer objects and send again.".format(
                                         len(omitted), ", ".join(omitted[:3])))
            message_id = self.conversation.begin(prompt)
        except BriefOverflow as error:
            self._log("Error", str(error))
            self.open_brief(overflow=True)
            return
        except (AssistantError, RuntimeError) as error:
            self._log("Error", str(error))
            return
        previous = self.task
        if previous is not None and previous.pending_step is not None:
            self.conversation.record_not_executed("cancelled", "Superseded by a new request.")
        task = self.session.start_task(prompt, message_id)
        task.snapshot = snapshot
        task.context = context  # Reused by the first request instead of a second capture.
        self.code.clear()
        self.prompt.clear()
        self._log("You", prompt)
        self._request()

    def _request(self):
        task = self.task
        if task is None or task.state not in (S.IDLE, S.EXECUTING, S.APPLYING, S.PAUSED):
            return  # Stopped, paused or replaced while this was queued.
        self._transition(S.CHECKING_PROVIDER)
        token = task.new_request()
        self._received_chars = 0
        try:
            context, task.context = task.context, None
            if context is None and self.share_context.isChecked():
                context = model_context(App, Gui)
            body = self.conversation.request_body(self.settings, context)
            self.transport.send(self.settings, body, token)
        except Exception as error:
            self._fail_task(str(error))
            return
        if not getattr(self.transport, "checking", False) and task.state is S.CHECKING_PROVIDER:
            self._transition(S.THINKING)  # Sign-in was verified recently; no login check.

    def _turn_started(self, token):
        if self.session.accepts(token, S.CHECKING_PROVIDER):
            self._transition(S.THINKING)

    def _progress(self, token, text):
        # Readable status only; generated code appears once the turn fully succeeds.
        if not self.session.accepts(token, S.THINKING):
            return
        self._received_chars += len(text)
        self.status.setText("Receiving the response… {:,} characters".format(self._received_chars))

    def _received(self, token, proposal):
        if not self.session.accepts(token, S.THINKING):
            return  # A stale response can never reach the model.
        task = self.task
        task.invalidate()
        if self.provider_status is None or self.provider_status.state is not Readiness.READY:
            self.provider_status = Status(Readiness.READY, "A request succeeded.", live=True)
            self._update_info()
        self.conversation.accept(proposal)
        self._log("Assistant", proposal.message)
        if not proposal.python:
            self._transition(S.COMPLETED)
            return
        step = task.add_step(token.request, proposal.message, proposal.python,
                             task.snapshot.fingerprint if task.snapshot is not None else "")
        self._record(step)
        if not self.conversation.can_execute:
            self._transition(S.PAUSED)
            self._log("FreeCAD", "Reached the six-step limit. Continue grants another six steps "
                      "for this task; Stop ends it.")
            return
        if self.autonomous.isChecked():
            self._transition(S.EXECUTING)
            step = task.pending_step
            # Let Stop/paint events run before touching the document.
            QtCore.QTimer.singleShot(0, lambda: self._execute_pending(task, step))
        else:
            self._transition(S.AWAITING_REVIEW)

    def _request_failed(self, token, message):
        if self.session.accepts(token, S.THINKING, S.CHECKING_PROVIDER):
            if getattr(self.transport, "last_failure", None) is Failure.AUTH:
                self.provider_status = Status(Readiness.SIGN_IN_REQUIRED, message)
                self._update_info()
            self._fail_task(message)

    def _execute_pending(self, task=None, step=None, reviewed=False):
        """Run the pending step: in the worker when automatic, in FreeCAD when reviewed."""
        task = task or self.task
        step = step or (task.pending_step if task is not None else None)
        if (task is None or task is not self.task or step is None
                or step is not task.pending_step or task.state is not S.EXECUTING):
            return
        try:
            # Stale context must stop the task rather than become a repair loop.
            task.snapshot.validate(App, Gui)
        except AssistantError as error:
            self._reject(task, str(error))
            return
        if reviewed:
            self._execute_in_gui(task, step)
        else:
            self._execute_in_worker(task, step)

    def _reject(self, task, reason):
        self._record(task.close_pending(Outcome.REJECTED, reason))
        self.conversation.record_not_executed("rejected", reason)
        self._fail_task(reason)

    def _execute_in_gui(self, task, step):
        """Reviewed code runs on the GUI thread; it cannot be interrupted midway."""
        session = self.session
        step.source_revision = task.snapshot.fingerprint
        success = False
        self._binding = True
        try:
            outcome = execute_step(step.code, App, Gui, task.snapshot,
                                   transaction="AI assistant " + step.id,
                                   on_create=lambda doc: self.registry.bind(session, doc))
            result, step.diagnostics, step.changed = outcome.text, outcome.diagnostics, outcome.changed
            success = True
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            result = "{}: {}".format(type(error).__name__, error)
            step.diagnostics = getattr(error, "diagnostics", {})
        finally:
            self._binding = False
        self._finish_step(task, step, success, result)

    def _target_document(self):
        """The session's document; an unbound session creates and binds one."""
        doc = App.ActiveDocument
        if doc is None:
            self._binding = True
            try:
                doc = App.newDocument("AIModel")
                self.registry.bind(self.session, doc)
            finally:
                self._binding = False
        return doc

    def _unsupported(self, task, step, reason):
        """Automatic mode never falls back to GUI execution; offer review instead."""
        step.result = reason
        self._transition(S.AWAITING_REVIEW)
        self._log("FreeCAD", reason + " Automatic steps cannot run this. Review the code and "
                  "press Run reviewed code to run it inside FreeCAD (it cannot be stopped "
                  "midway there), or press Stop.")

    def _execute_in_worker(self, task, step):
        try:
            doc = self._target_document()
            if doc.HasPendingTransaction:
                raise AssistantError("Finish the current FreeCAD editing operation before "
                                     "running the assistant.")
            external = external_links(doc)
            if external:
                self._unsupported(task, step, "This document links to other documents ({}).".format(
                    ", ".join(external[:3])))
                return
            task.snapshot = DocumentSnapshot.capture(App, Gui)
            step.source_revision = task.snapshot.fingerprint
            token = task.new_request()
            job = make_worker_job(self)
            self.worker_job = job
            job.finished.connect(lambda token, result, error: self._worker_finished(
                job, token, result, error))
            job.start(App, doc, step.code, token, timeout_seconds=WORKER_TIMEOUT_SECONDS)
        except AssistantError as error:
            if "No compatible headless FreeCAD" in str(error):
                self._unsupported(task, step, str(error))
            else:
                self._reject(task, str(error))

    def _worker_finished(self, job, token, result, error):
        job.deleteLater()
        if job is self.worker_job:
            self.worker_job = None
        if not self.session.accepts(token, S.EXECUTING):
            return  # Stopped, paused or replaced: the candidate is discarded unapplied.
        task, step = self.task, self.task.pending_step
        task.invalidate()
        if error is not None:
            self._finish_step(task, step, False, str(error))
            return
        if not result.get("ok"):
            if result.get("kind") == "unsupported":
                self._unsupported(task, step, result.get("error", "Unsupported operation."))
                return
            step.diagnostics = result.get("diagnostics") or {}
            text = result.get("error", "The step failed.")
            if result.get("output"):
                text += "\nPython output:\n" + result["output"]
            self._finish_step(task, step, False, text)
            return
        doc = App.ActiveDocument
        current = DocumentSnapshot.capture(App, Gui)
        if doc is not task.snapshot.document or current.fingerprint != task.snapshot.fingerprint:
            self._reject(task, "The model changed while the step was being computed; its result "
                               "was discarded. Send a new request using the current model.")
            return
        self._transition(S.APPLYING)
        transaction = "AI assistant " + step.id
        doc.openTransaction(transaction)
        try:
            step.changed = tuple(apply_delta(doc, result["delta"]))
            doc.commitTransaction()
            step.properties = {change["object"]: sorted(change["properties"]) + (
                ["expressions"] if change.get("expressions") else [])
                for change in result["delta"]["changed"]}
        except Exception as failure:
            doc.abortTransaction()
            self._finish_step(task, step, False, "Could not apply the result: {}".format(failure))
            return
        step.diagnostics = result.get("diagnostics") or {}
        text = "Modeling step completed. Target document: {}. Object count: {}.".format(
            doc.Name, len(doc.Objects))
        notes = summarize(step.diagnostics)
        if notes:
            text += "\n" + notes
        if result.get("output", "").strip():
            text += "\nPython output:\n" + result["output"]
        self._finish_step(task, step, True, text)

    def _finish_step(self, task, step, success, result):
        step.outcome = Outcome.EXECUTED if success else Outcome.FAILED
        step.result = result
        if success:
            step.transaction = "AI assistant " + step.id
        self._record(step)
        self._log("FreeCAD", result)
        self.conversation.record_execution(success, result, step.diagnostics)
        task.snapshot = DocumentSnapshot.capture(App, Gui)
        if success and App.ActiveDocument is not None:
            step.revision_after = task.snapshot.fingerprint
            step.undo_count_after = App.ActiveDocument.UndoCount
        # Queued, so a Stop pressed while changes were applied prevents the next step.
        QtCore.QTimer.singleShot(0, self._request)

    def _run_reviewed(self):
        task = self.task
        if task is not None and task.state is S.AWAITING_REVIEW and task.pending_step is not None:
            self._transition(S.EXECUTING)
            self._execute_pending(task, task.pending_step, reviewed=True)

    def _continue(self):
        task = self.task
        if task is None or task.state is not S.PAUSED:
            return
        if not self.conversation.can_execute:
            self.conversation.grant_segment()
        step = task.pending_step
        if step is None:
            task.snapshot = DocumentSnapshot.capture(App, Gui)
            self._request()
        elif self.autonomous.isChecked():
            self._transition(S.EXECUTING)
            self._execute_pending(task, step)
        else:
            self._transition(S.AWAITING_REVIEW)

    # Stopping -------------------------------------------------------------------

    def _halt(self, session, state, reason):
        """Invalidate first, then cancel; a late callback can no longer apply."""
        task = session.task
        if task is None or task.finished:
            return False
        task.invalidate()
        if session is self.session:
            self.transport.cancel()
            if self.worker_job is not None:
                self.worker_job.cancel()
        step = task.close_pending(Outcome.CANCELLED, reason) if state is not S.PAUSED else None
        if step is not None:
            self._record(step, session)
            session.conversation.record_not_executed("cancelled", reason)
        task.transition(state)
        if session is self.session:
            self._refresh()
        return True

    def _stop(self):
        if self._halt(self.session, S.STOPPED, "Stopped by the user before it ran."):
            self._log("FreeCAD", "Stopped. Completed steps remain in the document and can be undone.")

    def _fail_task(self, message):
        task = self.task
        if task is not None and not task.finished:
            task.invalidate()
            self.transport.cancel()
            task.transition(S.FAILED)
        self._log("Error", message)
        self._refresh()

    # Documents ------------------------------------------------------------------

    def _sync_session(self):
        doc = App.ActiveDocument
        if self.session.document is not doc:
            self._document_activated(doc)

    def _document_created(self, doc):
        pass  # Activation follows; assistant-created documents bind in on_create.

    def _document_activated(self, doc):
        if self._binding or self.session.document is doc:
            return
        task = self.task
        if task is not None and task.running:
            self._halt(self.session, S.PAUSED, "")
            self._log("FreeCAD", "Paused because another document became active. "
                      "Switch back and press Continue to resume.")
        self.session = self.registry.for_document(doc)
        self._render_session()

    def _document_deleted(self, doc):
        current = self.session.document is doc
        for session in [s for s in self.registry.sessions if s.document is doc]:
            self._halt(session, S.STOPPED, "The document was closed.")
        self.registry.retire(doc)
        if current:
            QtCore.QTimer.singleShot(0, self._after_close)

    def _after_close(self):
        self.session = self.registry.for_document(App.ActiveDocument)
        self._render_session()

    # Settings, New chat, Undo ---------------------------------------------------

    def _settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.apply_settings(dialog.result_settings)

    def apply_settings(self, settings):
        """A provider change starts a new chat; pending work can no longer apply."""
        self.settings = settings
        self._clear()
        CACHE.invalidate()
        self.check_provider(use_cache=False)

    def _clear(self):
        self._halt(self.session, S.STOPPED, "A new chat was started.")
        self.session.reset()
        self.code.clear()
        self.details.clear()
        self.transcript.clear()
        self._refresh()

    def _undo(self, scope="step"):
        """Undo the last step or the whole task, only if exactly the assistant's
        transactions can be undone; otherwise change nothing."""
        doc = App.ActiveDocument
        task = self.session.undo_candidate()
        try:
            targets = plan_undo(task, scope, doc is not None and doc is self.session.document,
                                fingerprint(doc), doc.UndoNames if doc is not None else [])
        except UndoRefused as refusal:
            self._log("FreeCAD", str(refusal))
            return
        for _ in targets:
            doc.undo()
        for step in targets:
            step.outcome = Outcome.UNDONE
            self._record(step)
        self.conversation.record_undo([step.id for step in targets])
        restored = fingerprint(doc) == targets[-1].source_revision
        names = ", ".join(step.id for step in reversed(targets))
        self._log("FreeCAD", "Undid {}.".format(names) + ("" if restored else
                  " The model differs from its recorded state before these steps; check it "
                  "before continuing."))
        self._refresh()

    def shutdown(self):
        """Stop owned processes and detach from FreeCAD; safe to call repeatedly."""
        observer, self._observer = getattr(self, "_observer", None), None
        if observer is not None:
            try:
                App.removeDocumentObserver(observer)
            except Exception:
                pass
        for session in self.registry.sessions:
            if session.task is not None:
                session.task.invalidate()
        if self._checker is not None:
            self._checker.cancel()
        if self.worker_job is not None:
            self.worker_job.shutdown()
        self.transport.shutdown()

    def closeEvent(self, event):
        if self.busy:
            self._stop()
        super().closeEvent(event)


class AssistantCommand:
    def GetResources(self):
        return {"MenuText": "AI Assistant", "ToolTip": "Create and edit models with an AI assistant"}

    def IsActive(self):
        return True

    def Activated(self):
        global _panel
        if _panel is None:
            _panel = AssistantPanel(Gui.getMainWindow())
            Gui.getMainWindow().addDockWidget(QtCore.Qt.RightDockWidgetArea, _panel)
        _panel.show()
        _panel.raise_()


def register(gui):
    gui.addCommand("AI_Assistant", AssistantCommand())

    def add_menu():
        window = gui.getMainWindow()
        if window is None:
            QtCore.QTimer.singleShot(200, add_menu)
            return
        if not window.property("FreeCAD_AIMenuHook"):
            window.workbenchActivated.connect(lambda name: QtCore.QTimer.singleShot(0, add_menu))
            window.setProperty("FreeCAD_AIMenuHook", True)
        menu = window.findChild(QtWidgets.QMenu, "FreeCAD_AIMenu")
        if menu is None:
            menu = QtWidgets.QMenu("AI", window)
            menu.setObjectName("FreeCAD_AIMenu")
            action = menu.addAction("AI Assistant")
            action.triggered.connect(lambda: gui.runCommand("AI_Assistant"))
        if menu.menuAction() not in window.menuBar().actions():
            window.menuBar().addMenu(menu)

    QtCore.QTimer.singleShot(0, add_menu)
