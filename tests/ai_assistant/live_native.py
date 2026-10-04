# SPDX-License-Identifier: LGPL-2.1-or-later
"""Explicit opt-in checks that make real inference requests using native CLI logins.

run(provider) sends one chat-only request. run_modeling(provider) runs one real
modeling task through the panel in a disposable document. Neither runs in the
normal integration suite, and a mock provider never counts as either.
"""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))


def _wait(milliseconds):
    from freecad_ai.gui import QtCore
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def run(provider="claude", timeout_seconds=60):
    from freecad_ai.core import Conversation, ProviderSettings
    from freecad_ai.gui import LIVE_PROBE, QtWidgets, Transport
    transport = Transport(QtWidgets.QApplication.instance())
    outcomes = []
    transport.completed.connect(lambda token, result: outcomes.append((True, result)))
    transport.failed.connect(lambda token, message: outcomes.append((False, message)))
    conversation = Conversation()
    conversation.begin(LIVE_PROBE)
    settings = ProviderSettings(provider=provider, timeout_seconds=max(10, timeout_seconds))
    try:
        started = time.monotonic()
        transport.send(settings, conversation.request_body(settings))
        while not outcomes and time.monotonic() - started < timeout_seconds + 5:
            _wait(50)
        if not outcomes:
            raise RuntimeError("Native inference test timed out.")
        success, result = outcomes[0]
        if not success:
            raise RuntimeError(result)
        if result.python:
            raise RuntimeError("Chat-only probe returned code; it was not executed.")
        if result.message != "Native sign-in works":
            raise RuntimeError("Native response did not match the expected structured message.")
        message = "PASS: {} real inference using native signed-in CLI in {:.1f}s; no modeling " \
                  "code executed".format(provider, time.monotonic() - started)
        print(message)
        return message
    finally:
        transport.shutdown()
        transport.deleteLater()


def run_modeling(provider="claude", timeout_seconds=600):
    """One real autonomous modeling task in a disposable document."""
    import FreeCAD as App
    import FreeCADGui as Gui
    from freecad_ai.core import ProviderSettings
    from freecad_ai.gui import AssistantPanel
    from freecad_ai.session import Outcome
    original = App.ActiveDocument.Name if App.ActiveDocument is not None else None
    doc = App.newDocument("AIAssistantLive")
    panel = AssistantPanel(Gui.getMainWindow())
    try:
        # Not saved to preferences; the user's own settings stay as they are.
        panel.apply_settings(ProviderSettings(provider=provider, timeout_seconds=180))
        panel.prompt.setPlainText(
            "Create a parametric Part::Box named LiveBox, 30 mm long, 20 mm wide and 10 mm "
            "high, then a 4 mm radius Part::Cylinder named LiveHole 10 mm high at x=15, y=10, "
            "and a Part::Cut named LiveCut that subtracts LiveHole from LiveBox. Print the "
            "volume of LiveCut to confirm.")
        started = time.monotonic()
        panel._send()
        task = panel.task
        while task is not None and not task.finished and time.monotonic() - started < timeout_seconds:
            _wait(200)
        steps = [(step.id, step.outcome.value) for step in task.steps] if task else []
        if task is None or task.state.value != "completed":
            raise RuntimeError("Live modeling did not complete: state={} steps={} log={}".format(
                task.state.value if task else None, steps, panel.session.transcript[-3:]))
        cut = doc.getObject("LiveCut")
        expected = 30 * 20 * 10 - 3.14159265 * 16 * 10
        if cut is None or abs(cut.Shape.Volume - expected) > 1.0:
            raise RuntimeError("LiveCut missing or wrong volume: {} steps={}".format(
                cut.Shape.Volume if cut else None, steps))
        if doc.getObject("LiveBox").Length.Value != 30:
            raise RuntimeError("LiveBox is not parametric with the requested length.")
        executed = [s for s in task.steps if s.outcome is Outcome.EXECUTED]
        message = ("PASS: {} live modeling task in {:.0f}s: {} executed step(s) via the worker, "
                   "LiveCut volume {:.1f} mm^3; steps {}".format(
                       provider, time.monotonic() - started, len(executed), cut.Shape.Volume, steps))
        print(message)
        return message
    finally:
        panel.shutdown()
        panel.deleteLater()
        App.closeDocument(doc.Name)
        if original is not None and original in App.listDocuments():
            App.setActiveDocument(original)
