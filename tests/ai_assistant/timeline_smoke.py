# SPDX-License-Identifier: LGPL-2.1-or-later
"""Timeline, Continue and task Undo through the real panel. Run run() in FreeCAD."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))


def _helpers():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ai_gui_smoke_helpers", str(Path(__file__).resolve().with_name("gui_smoke.py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    import FreeCAD as App
    import FreeCADGui as Gui
    from freecad_ai.core import MAX_AGENT_STEPS, Proposal
    from freecad_ai.gui import AssistantPanel
    from freecad_ai.session import Outcome, TaskState as S
    from freecad_ai.validation import fingerprint

    helpers = _helpers()
    wait_for = helpers._wait_for
    doc = App.newDocument("AIAssistantTimeline")
    base = doc.addObject("Part::Box", "Base")
    base.Length = 20
    doc.recompute()
    other = None
    panel = AssistantPanel(Gui.getMainWindow())
    fake = helpers._fake_transport_class()(panel)
    panel.attach_transport(fake)
    checks = []

    def begin(prompt):
        panel.prompt.setPlainText(prompt)
        panel._send()
        return panel.task

    def step(code, message="Step"):
        """Deliver a proposal and wait until it ran and the next request went out."""
        sent = len(fake.sent)
        fake.deliver(Proposal(message, code))
        wait_for(lambda: len(fake.sent) == sent + 1 or panel.task.state is S.PAUSED
                 or panel.task.finished, timeout=30)

    def finish(task):
        fake.deliver(Proposal("Done"))
        wait_for(lambda: task.state is S.COMPLETED)

    def last_log():
        return panel.session.transcript[-1][1]

    try:
        Gui.Selection.clearSelection()
        start = fingerprint(doc)
        task = begin("Three steps")
        step("doc.addObject('Part::Box', 'A').Length = 11", "Add A")
        step("doc.Base.Length = 25\ndoc.Base.setExpression('Width', 'Length / 5')", "Resize base")
        step("doc.addObject('Part::Cylinder', 'C').Radius = 3", "Add C")
        finish(task)
        cards = [panel.chat.card(s.id) for s in task.steps]
        assert all(card is not None and card.status == "executed" for card in cards)
        assert not any(card.expanded for card in cards)  # Done steps stay collapsed.
        assert cards[-1].undo_button.isVisibleTo(cards[-1]) and not cards[0].undo_button.isVisibleTo(cards[0])
        cards[1].expand()
        assert "doc.Base.Length = 25" in cards[1].code_text()
        details = cards[1].details_text()
        assert "Base (" in details and "Length" in details and "expressions" in details, details
        assert doc.Base.Width.Value == 5
        panel._undo("task")
        assert fingerprint(doc) == start and doc.getObject("A") is None and doc.getObject("C") is None
        assert doc.Base.Length.Value == 20 and doc.Base.ExpressionEngine == []
        assert all(s.outcome is Outcome.UNDONE for s in task.steps)
        assert '"undone_steps"' in panel.conversation.messages[-1]["content"]
        assert all(panel.chat.card(s.id).status == "undone" for s in task.steps)
        checks.append("three-step task inspected in its step cards, then fully undone to its "
                      "starting geometry and parameters")

        def manual_edit(height):
            doc.openTransaction("User edit")
            doc.Base.Height = height
            doc.recompute()
            doc.commitTransaction()

        task = begin("Two steps")
        step("doc.addObject('Part::Box', 'M1')")
        manual_edit(7)  # Between steps of one task: the next step is rejected as stale.
        fake.deliver(Proposal("Step", "doc.addObject('Part::Box', 'M2')"))
        wait_for(lambda: task.finished)
        assert task.state is S.FAILED and task.steps[-1].outcome is Outcome.REJECTED
        assert doc.getObject("M2") is None
        before = fingerprint(doc)
        panel._undo("task")
        assert fingerprint(doc) == before and "changed after" in last_log(), last_log()
        task = begin("Next task")  # Across tasks: the newer task undoes, not past the edit.
        step("doc.addObject('Part::Box', 'M3')")
        finish(task)
        panel._undo("task")
        assert doc.getObject("M3") is None and doc.getObject("M1") is not None
        panel._undo("task")
        assert doc.getObject("M1") is not None and doc.Base.Height.Value == 7
        assert "changed after" in last_log()
        checks.append("manual edit between steps: stale step rejected; undo never reaches "
                      "past the user's edit")

        task = begin("Outside undo")
        step("doc.addObject('Part::Box', 'O1')")
        finish(task)
        doc.undo()
        panel._undo("task")
        assert "changed after" in last_log() and doc.getObject("O1") is None
        doc.redo()
        panel._undo("task")
        assert doc.getObject("O1") is None and task.steps[0].outcome is Outcome.UNDONE
        checks.append("Undo/Redo outside the panel detected; undo allowed again after Redo")

        task = begin("Truncated")
        step("doc.addObject('Part::Box', 'T1')")
        finish(task)
        doc.clearUndos()
        panel._undo("task")
        assert doc.getObject("T1") is not None and "Nothing was changed" in last_log()
        checks.append("missing undo history refused")

        start = fingerprint(doc)
        task = begin("Failed middle step")
        step("doc.addObject('Part::Box', 'F1')")
        step("raise RuntimeError('broken step')")
        step("doc.addObject('Part::Box', 'F3')")
        finish(task)
        assert [s.outcome for s in task.steps] == [Outcome.EXECUTED, Outcome.FAILED, Outcome.EXECUTED]
        panel._undo("task")
        assert fingerprint(doc) == start and doc.getObject("F1") is None
        checks.append("failed middle step: task undo restores the start")

        task = begin("Stopped")
        step("doc.addObject('Part::Box', 'S1')")
        panel._stop()
        assert task.state is S.STOPPED and doc.getObject("S1") is not None
        assert panel.chat.card(task.steps[0].id).status == "executed"
        assert "Stopped." in panel.chat.plain_text()
        own = panel.session
        other = App.newDocument("AIAssistantTimelineOther")
        wait_for(lambda: panel.session is not own)
        panel._undo("task")
        assert doc.getObject("S1") is not None and "No assistant step" in last_log()
        App.setActiveDocument(doc.Name)
        wait_for(lambda: panel.session is own)
        panel._undo("task")
        assert doc.getObject("S1") is None
        checks.append("Stop keeps completed steps; undo refused from another document, "
                      "then allowed after switching back")

        task = begin("Many boxes")
        for index in range(MAX_AGENT_STEPS):
            step("doc.addObject('Part::Box', 'Many{}')".format(index))
        fake.deliver(Proposal("Seventh", "doc.addObject('Part::Box', 'Many6')"))
        wait_for(lambda: task.state is S.PAUSED)
        assert panel.continue_button.isVisibleTo(panel) and doc.getObject("Many6") is None
        sent = len(fake.sent)
        panel._continue()
        wait_for(lambda: len(fake.sent) == sent + 1, timeout=30)
        names = sorted(o.Name for o in doc.Objects if o.Name.startswith("Many"))
        assert names == ["Many{}".format(i) for i in range(7)], names
        step("doc.addObject('Part::Box', 'Many7')")
        finish(task)
        assert len([o for o in doc.Objects if o.Name.startswith("Many")]) == 8
        assert '"Many0' not in panel.conversation.messages[-1]["content"]
        checks.append("Continue past six steps; pending step runs once, no duplicate geometry")
        print("PASS: " + ", ".join(checks))
    finally:
        panel.shutdown()
        panel.deleteLater()
        if other is not None and other.Name in App.listDocuments():
            App.closeDocument(other.Name)
        App.closeDocument(doc.Name)
