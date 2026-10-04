# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run run() on the GUI thread in FreeCAD. Uses only disposable documents."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))


def _wait(milliseconds):
    from freecad_ai.gui import QtCore
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def _wait_for(predicate, timeout=5.0, settle=3, describe=None):
    """Spin the event loop until predicate holds, then let queued work run.

    Fixed waits are unreliable: the first paint after a new document can take
    longer than a short timer, so the loop may quit before queued steps run.
    """
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for the panel" +
                                 (": " + describe() if describe else ""))
        _wait(10)
    for _ in range(settle):
        _wait(10)


def _fake_transport_class():
    from freecad_ai.gui import QtCore

    class FakeTransport(QtCore.QObject):
        """A provider that answers only when the test says so, possibly too late."""
        completed = QtCore.Signal(object, object)
        failed = QtCore.Signal(object, str)
        progress = QtCore.Signal(object, str)

        def __init__(self, parent):
            super().__init__(parent)
            self.sent = []
            self.cancelled = 0
            self.delivered = 0

        def send(self, settings, body, token=None):
            self.sent.append((token, body))

        def deliver(self, proposal, index=-1, delay=0):
            token = self.sent[index][0]

            def emit():
                self.delivered += 1
                self.completed.emit(token, proposal)
            QtCore.QTimer.singleShot(delay, emit)

        def cancel(self):
            self.cancelled += 1

        def shutdown(self):
            pass

    return FakeTransport


def run():
    import FreeCAD as App
    import FreeCADGui as Gui
    from freecad_ai.core import AssistantError, Proposal
    from freecad_ai.document import DocumentSnapshot, execute_step, model_context
    from freecad_ai.gui import AssistantPanel, SettingsDialog, ProviderSettings
    from freecad_ai.session import Outcome, TaskState as S

    doc = App.newDocument("AIAssistantSmoke")
    other = None
    panel = None
    checks = []
    try:
        panel = AssistantPanel(Gui.getMainWindow())
        checks.append("panel construction")
        settings = SettingsDialog(ProviderSettings(provider="claude", model="test"), panel)
        settings.provider.setCurrentIndex(settings.provider.findData("codex"))
        assert settings.executable.placeholderText() == "codex"
        assert "codex login" in settings.note.text()
        settings.provider.setCurrentIndex(settings.provider.findData("copilot"))
        assert settings.executable.placeholderText() == "copilot"
        assert not hasattr(settings, "key") and not hasattr(settings, "url")
        settings.executable.setText("/nonexistent/copilot")
        settings.check_connection()
        _wait_for(lambda: "Checking" not in settings.check_result.text())
        assert settings.check_result.text().startswith("CLI not found"), settings.check_result.text()
        settings.reject()
        settings.deleteLater()
        assert "Signed-in CLI" not in panel.info.text()
        _wait_for(lambda: panel.provider_status.state.value != "checking", timeout=30)
        assert panel.provider_status.state.value in panel.info.text()
        checks.append("provider switching; verified readiness shown, Check connection")

        snapshot = DocumentSnapshot.capture(App, Gui)
        execute_step("box = doc.addObject('Part::Box', 'AIBox')\n"
                     "box.Length = 40\nbox.Width = 30\nbox.Height = 10", App, Gui, snapshot)
        assert abs(doc.AIBox.Shape.Volume - 12000) < 0.001
        checks.append("parametric creation and recompute")
        result = execute_step("print('Volume:', doc.AIBox.Shape.Volume)\nprint('x' * 9000)",
                              App, Gui, DocumentSnapshot.capture(App, Gui)).text
        assert "Volume: 12000" in result and "[output truncated]" in result
        assert len(result) < 8500
        checks.append("bounded Python output feedback")
        snapshot = DocumentSnapshot.capture(App, Gui)
        try:
            execute_step("doc.AIBox.Length = 99\nraise RuntimeError('intentional')",
                         App, Gui, snapshot)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected code failure")
        assert doc.AIBox.Length.Value == 40
        checks.append("failed-step rollback")
        snapshot = DocumentSnapshot.capture(App, Gui)
        doc.AIBox.Width = 31
        doc.recompute()
        try:
            snapshot.validate(App, Gui)
        except AssistantError:
            pass
        else:
            raise AssertionError("Stale model was accepted")
        doc.AIBox.Width = 30
        doc.recompute()
        checks.append("stale model detection")
        context = model_context(App, Gui)
        assert "FileName" not in str(context)
        checks.append("bounded model context")

        # Validation against the existing error state.
        def step(code):
            return execute_step(code, App, Gui, DocumentSnapshot.capture(App, Gui))

        doc.addObject("Part::Box", "CutBase")
        doc.addObject("Part::Box", "CutTool").Placement.Base = App.Vector(5, 5, 5)
        broken = doc.addObject("Part::Cut", "BrokenCut")
        broken.Base = doc.CutBase
        doc.recompute()
        assert "Invalid" in broken.State, broken.State
        outcome = step("doc.AIBox.Height = 12")
        assert doc.AIBox.Height.Value == 12
        assert [e["name"] for e in outcome.diagnostics["preexisting"]] == ["BrokenCut"]
        assert "pre-existing" in outcome.text
        checks.append("unrelated edit succeeds beside a pre-existing broken cut")
        try:
            step("bad = doc.addObject('Part::Cut', 'NewBrokenCut')\nbad.Base = doc.AIBox\n"
                 "doc.AIBox.Height = 14")
        except AssistantError as failure:
            report = failure.diagnostics
            assert [e["name"] for e in report["new_invalid"]] == ["NewBrokenCut"], report
            assert [e["name"] for e in report["baseline"]] == ["BrokenCut"]
        else:
            raise AssertionError("A newly broken feature was accepted")
        assert doc.getObject("NewBrokenCut") is None and doc.AIBox.Height.Value == 12
        assert "Invalid" in doc.BrokenCut.State
        checks.append("newly broken feature rolls back; baseline diagnostics kept")
        from freecad_ai.validation import fingerprint
        before_repair = fingerprint(doc)
        try:
            step("doc.BrokenCut.Tool = doc.CutTool\nbad = doc.addObject('Part::Cut', 'Other')\n"
                 "bad.Base = doc.CutTool")
        except AssistantError as failure:
            report = failure.diagnostics
            assert [e["name"] for e in report["repaired"]] == ["BrokenCut"], report
            assert [e["name"] for e in report["new_invalid"]] == ["Other"], report
        else:
            raise AssertionError("A repair that broke another feature was accepted")
        # Rollback restores every value and shape, and the baseline error state.
        assert doc.BrokenCut.Tool is None and fingerprint(doc) == before_repair
        assert "Invalid" in doc.BrokenCut.State
        assert not [o.Name for o in doc.Objects if o.Name != "BrokenCut" and "Touched" in o.State]
        assert [e["name"] for e in report["baseline"]] == ["BrokenCut"]
        checks.append("repair that breaks another feature fails and rolls back exactly")
        outcome = step("doc.BrokenCut.Tool = doc.CutTool")
        assert "Invalid" not in doc.BrokenCut.State
        assert [e["name"] for e in outcome.diagnostics["repaired"]] == ["BrokenCut"]
        checks.append("repair that removes an error succeeds")
        # Deleting a boolean in the GUI opens a transaction (its view provider
        # restores child visibility), so fixture cleanup commits one explicitly.
        doc.openTransaction("Smoke cleanup")
        for name in ("BrokenCut", "CutTool", "CutBase"):
            doc.removeObject(name)
        doc.AIBox.Height = 10
        doc.recompute()
        doc.commitTransaction()

        # Real geometry: a Pad in a Body in a Part, selected through a face and an edge.
        import Part
        import Sketcher
        container = doc.addObject("App::Part", "AIPart")
        body = doc.addObject("PartDesign::Body", "AIBody")
        container.addObject(body)
        sketch = body.newObject("Sketcher::SketchObject", "AISketch")
        sketch.AttachmentSupport = (body.Origin.OriginFeatures[3], [""])
        sketch.MapMode = "FlatFace"
        corners = [App.Vector(0, 0, 0), App.Vector(20, 0, 0), App.Vector(20, 10, 0), App.Vector(0, 10, 0)]
        for index in range(4):
            sketch.addGeometry(Part.LineSegment(corners[index], corners[(index + 1) % 4]))
        for index in range(4):
            sketch.addConstraint(Sketcher.Constraint("Coincident", index, 2, (index + 1) % 4, 1))
        sketch.renameConstraint(sketch.addConstraint(
            Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 20)), "wall_length")
        sketch.addConstraint(Sketcher.Constraint("Angle", 0, 1, 1, 1, 1.5707963267948966))
        pad = body.newObject("PartDesign::Pad", "AIPad")
        pad.Profile = sketch
        pad.Length = 5
        for index in range(60):  # Push the target far beyond the old 80-object slice.
            doc.addObject("Part::Sphere", "Filler{:02d}".format(index))
        doc.recompute()
        Gui.Selection.clearSelection()
        top = next(name for name, face in (("Face{}".format(i + 1), f) for i, f in enumerate(pad.Shape.Faces))
                   if abs(face.CenterOfMass.z - 5) < 1e-6)
        Gui.Selection.addSelection(doc.Name, "AIPart", "AIBody.AIPad." + top)
        Gui.Selection.addSelection(doc.Name, "AIPart", "AIBody.AIPad.Edge1")
        context = model_context(App, Gui)
        entries = {entry["name"]: entry for entry in context["document"]["objects"]}
        reasons = [(entry["name"], entry["reason"]) for entry in context["document"]["objects"][:4]]
        # Selection through the Part/Body path resolves to the leaf feature.
        assert reasons[:3] == [("AIPad", "selected"), ("AIBody", "owner"),
                               ("AIPart", "owner")], reasons
        selected = entries["AIPad"]
        subs = selected.get("selected_subelements", [])
        face = next(sub for sub in subs if sub.get("kind") == "Face")
        assert face["surface"] == "Plane" and face["area"] == "200 mm^2", face
        assert face["normal"] == [0.0, 0.0, 1.0], face
        edge = next(sub for sub in subs if sub.get("kind") == "Edge")
        assert edge["length"].endswith(" mm"), edge
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, "AIPad", top)
        context = model_context(App, Gui)
        order = [(entry["name"], entry["reason"]) for entry in context["document"]["objects"]]
        assert order[:4] == [("AIPad", "selected"), ("AIBody", "owner"), ("AIPart", "owner"),
                             ("AISketch", "dependency")], order[:5]
        entries = {entry["name"]: entry for entry in context["document"]["objects"]}
        pad_entry = entries["AIPad"]
        assert pad_entry["properties"]["Length"] == "5 mm"
        assert pad_entry["properties"]["TaperAngle"] == "0 deg"
        assert pad_entry["volume"] == "1000 mm^3" and pad_entry["bounds"]["size"] == [20, 10, 5, "mm"]
        assert pad_entry["placement"]["rotation_angle"].endswith(" deg")
        constraints = {c.get("name", c["type"]): c for c in entries["AISketch"]["constraints"]["items"]}
        assert constraints["wall_length"]["value"] == "20 mm", constraints["wall_length"]
        assert constraints["Angle"]["value"] == "90 deg", constraints["Angle"]
        assert len(doc.Objects) > 80
        Gui.Selection.clearSelection()
        doc.openTransaction("Smoke cleanup")
        for index in range(60):
            doc.removeObject("Filler{:02d}".format(index))
        for name in ("AIPad", "AISketch", "AIBody", "AIPart"):
            doc.removeObject(name)
        doc.recompute()
        doc.commitTransaction()
        checks.append("selection-first context: owners, dependencies, faces/edges, "
                      "sketch constraints and units from real geometry")
        assert not doc.HasPendingTransaction, "pending after D"

        # Fake only the provider boundary; the real agent and FreeCAD executor run.
        fake = _fake_transport_class()(panel)
        panel.attach_transport(fake)
        Gui.Selection.clearSelection()

        def describe():
            task = panel.task
            return "state={} steps={} transcript={}".format(
                task.state if task else None,
                [(step.outcome.value, step.result[:200]) for step in task.steps] if task else [],
                panel.session.transcript[-4:])

        def send(prompt):
            panel.prompt.setPlainText(prompt)
            panel._send()
            return panel.task

        task = send("Make the box 50 mm long")
        assert task.state is S.THINKING and len(fake.sent) == 1
        fake.deliver(Proposal("Resize", "doc.AIBox.Length = 50"))
        _wait_for(lambda: len(fake.sent) == 2)
        assert doc.AIBox.Length.Value == 50, describe()
        assert task.state is S.THINKING and len(fake.sent) == 2
        assert task.steps[0].outcome is Outcome.EXECUTED
        fake.deliver(Proposal("Done"))
        _wait_for(lambda: task.state is S.COMPLETED)
        assert task.state is S.COMPLETED and not panel.busy
        checks.append("autonomous execution and feedback")
        panel._undo()
        assert doc.AIBox.Length.Value == 40, panel.session.transcript[-1]
        checks.append("undo")

        # Late responses after each interruption must never mutate the model.
        late = Proposal("Late", "doc.AIBox.Length = 77")
        task = send("Make it 77 mm")
        panel._stop()
        delivered = fake.delivered
        fake.deliver(late, delay=30)
        _wait_for(lambda: fake.delivered > delivered)
        assert doc.AIBox.Length.Value == 40 and task.state is S.STOPPED
        checks.append("late response after Stop ignored")

        task = send("Make it 77 mm")
        panel._clear()
        delivered = fake.delivered
        fake.deliver(late, delay=30)
        _wait_for(lambda: fake.delivered > delivered)
        assert doc.AIBox.Length.Value == 40 and panel.task is None
        checks.append("late response after New chat ignored")

        task = send("Make it 77 mm")
        panel.apply_settings(ProviderSettings(provider="codex"))
        delivered = fake.delivered
        fake.deliver(late, delay=30)
        _wait_for(lambda: fake.delivered > delivered)
        assert doc.AIBox.Length.Value == 40 and panel.task is None
        panel.apply_settings(ProviderSettings(provider="claude"))
        checks.append("late response after provider change ignored")

        other = App.newDocument("AIAssistantSmokeOther")
        _wait_for(lambda: panel.session.document is other)
        task = send("Add a box to the other document")
        App.closeDocument(other.Name)
        other = None
        App.setActiveDocument(doc.Name)
        _wait_for(lambda: panel.session.document is doc)
        delivered = fake.delivered
        fake.deliver(Proposal("Late", "doc.addObject('Part::Box', 'LateBox')"), delay=30)
        _wait_for(lambda: fake.delivered > delivered)
        assert task.state is S.STOPPED and doc.getObject("LateBox") is None
        assert panel.session.document is doc
        checks.append("late response after document close ignored")

        task = send("Make it 77 mm")
        own_session = panel.session
        other = App.newDocument("AIAssistantSmokeOther")
        _wait_for(lambda: panel.session is not own_session)
        assert task.state is S.PAUSED and panel.session is not own_session
        delivered = fake.delivered
        fake.deliver(late, delay=30)
        _wait_for(lambda: fake.delivered > delivered)
        assert doc.AIBox.Length.Value == 40 and other.getObject("AIBox") is None
        App.setActiveDocument(doc.Name)
        _wait_for(lambda: panel.session is own_session)
        assert panel.session is own_session and "Make it 77 mm" in panel.transcript.toPlainText()
        before = len(fake.sent)
        panel._continue()
        assert task.state is S.THINKING and len(fake.sent) == before + 1
        before = len(fake.sent)
        fake.deliver(Proposal("Resize", "doc.AIBox.Length = 60"))
        _wait_for(lambda: len(fake.sent) == before + 1, describe=describe)
        assert doc.AIBox.Length.Value == 60
        panel._stop()
        assert len(task.executed_steps()) == 1
        checks.append("document switch pauses; late response ignored; Continue resumes")

        # Design memory belongs to each document's session.
        task = send("Keep 2 mm walls everywhere")
        fake.deliver(Proposal("Noted"))
        _wait_for(lambda: task.state is S.COMPLETED)
        task = send("Walls are 3 mm instead")
        fake.deliver(Proposal("Noted"))
        _wait_for(lambda: task.state is S.COMPLETED)
        requirements = {r["text"]: r["id"] for r in panel.conversation.requirements}
        dialog = panel.open_brief()
        dialog.brief.setPlainText("Enclosure for a sensor node")
        dialog.supersede(requirements["Keep 2 mm walls everywhere"], requirements["Walls are 3 mm instead"])
        dialog._save()
        assert panel.conversation.brief == "Enclosure for a sensor node"
        task = send("Add a lid")
        import json
        body = json.loads(fake.sent[-1][1])["message"]["content"][0]["text"]
        assert "Enclosure for a sensor node" in body and '"superseded_by": "{}"'.format(
            requirements["Walls are 3 mm instead"]) in body
        panel._stop()
        own_session = panel.session
        other = App.newDocument("AIAssistantSmokeOther")
        _wait_for(lambda: panel.session is not own_session)
        assert panel.conversation.requirements == [] and panel.conversation.brief == ""
        App.closeDocument(other.Name)
        other = None
        App.setActiveDocument(doc.Name)
        _wait_for(lambda: panel.session is own_session)
        assert panel.conversation.brief == "Enclosure for a sensor node"
        panel.share_context.setChecked(False)
        assert panel.context_notice.isVisibleTo(panel) and "New chat" in panel.context_notice.text()
        panel.share_context.setChecked(True)
        overflow = panel.open_brief(overflow=True)
        assert overflow.summarize.isChecked() and "Add a lid" in overflow.summary.toPlainText()
        overflow.reject()
        panel._clear()
        assert panel.conversation.requirements == [] and panel.conversation.brief == ""
        checks.append("design brief: supersede, per-document memory, context notice, New chat")

        panel.autonomous.setChecked(False)
        task = send("Make the box 65 mm long")
        fake.deliver(Proposal("Review resize", "doc.AIBox.Length = 65"))
        _wait_for(lambda: task.state is S.AWAITING_REVIEW)
        assert task.state is S.AWAITING_REVIEW and panel.run_button.isEnabled()
        assert doc.AIBox.Length.Value == 60
        sent = len(fake.sent)
        panel._run_reviewed()
        assert doc.AIBox.Length.Value == 65
        _wait_for(lambda: len(fake.sent) == sent + 1)  # The next request is queued.
        fake.deliver(Proposal("Done"))
        _wait_for(lambda: task.state is S.COMPLETED)
        task = send("Make the box 70 mm long")
        fake.deliver(Proposal("Review resize", "doc.AIBox.Length = 70"))
        _wait_for(lambda: task.state is S.AWAITING_REVIEW)
        panel._stop()
        assert task.steps[-1].outcome is Outcome.CANCELLED and doc.AIBox.Length.Value == 65
        assert '"proposal_executed": false' in panel.conversation.messages[-1]["content"]
        checks.append("review mode; cancelled proposal recorded as not executed")
        print("PASS: " + ", ".join(checks))
    finally:
        if panel is not None:
            panel.shutdown()
            panel.deleteLater()
        if other is not None and other.Name in App.listDocuments():
            App.closeDocument(other.Name)
        if doc.Name in App.listDocuments():
            App.closeDocument(doc.Name)
