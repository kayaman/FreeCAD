# SPDX-License-Identifier: LGPL-2.1-or-later
"""Worker feasibility gate. Run run() on the GUI thread in FreeCAD.

Proves that a modeling step can run in headless FreeCADCmd on a document copy
and be applied to the open GUI document as one undoable transaction that keeps
the parametric graph: constrained sketches, expressions, PartDesign features,
internal links, object identity and appearance. A flattened shape fails.
"""
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))

STEP = '''
import Part, Sketcher
body = doc.getObject("Body")
doc.getObject("Sketch").setDatum("width", App.Units.Quantity("50 mm"))
pad = doc.getObject("Pad")
pad.setExpression("Length", "Sketch.Constraints.height / 2")
hole = body.newObject("Sketcher::SketchObject", "HoleSketch")
hole.AttachmentSupport = (pad, ["Face6"])
hole.MapMode = "FlatFace"
hole.addGeometry(Part.Circle(App.Vector(20, 15, 0), App.Vector(0, 0, 1), 5))
radius = hole.addConstraint(Sketcher.Constraint("Radius", 0, 5))
hole.renameConstraint(radius, "hole_r")
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Profile = hole
pocket.Type = "ThroughAll"
doc.recompute()
print("worker volume", round(pocket.Shape.Volume, 3))
'''

SLOW = '''
import Part
shape = Part.makeBox(1, 1, 1)
started = __import__("time").monotonic()
count = 0
while __import__("time").monotonic() - started < 1.5:
    shape = shape.fuse(Part.makeSphere(0.4, App.Vector(count % 7, count % 5, 0))).removeSplitter()
    count += 1
box = doc.addObject("Part::Feature", "SlowResult")
box.Shape = shape
print("fused", count)
'''


def _wait(milliseconds):
    from freecad_ai.gui import QtCore
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def _fixture(App):
    import Part
    import Sketcher
    doc = App.newDocument("AIAssistantWorker")
    body = doc.addObject("PartDesign::Body", "Body")
    sketch = body.newObject("Sketcher::SketchObject", "Sketch")
    sketch.AttachmentSupport = (body.Origin.OriginFeatures[3], [""])
    sketch.MapMode = "FlatFace"
    points = [App.Vector(0, 0, 0), App.Vector(40, 0, 0), App.Vector(40, 30, 0), App.Vector(0, 30, 0)]
    for index in range(4):
        sketch.addGeometry(Part.LineSegment(points[index], points[(index + 1) % 4]))
    for index in range(4):
        sketch.addConstraint(Sketcher.Constraint("Coincident", index, 2, (index + 1) % 4, 1))
    for kind, index in (("Horizontal", 0), ("Horizontal", 2), ("Vertical", 1), ("Vertical", 3)):
        sketch.addConstraint(Sketcher.Constraint(kind, index))
    sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))
    sketch.renameConstraint(sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 40)), "width")
    sketch.renameConstraint(sketch.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, 30)), "height")
    pad = body.newObject("PartDesign::Pad", "Pad")
    pad.Profile = sketch
    pad.Length = 10
    link = doc.addObject("App::Link", "BodyLink")
    link.LinkedObject = body
    link.Placement.Base = App.Vector(100, 0, 0)
    doc.recompute()
    pad.ViewObject.ShapeAppearance = (App.Material(DiffuseColor=(0.8, 0.2, 0.2)),)
    return doc


def _run_job(job_factory, App, doc, code, cancel_after=None, timeout_seconds=60):
    from freecad_ai.gui import QtCore
    job = job_factory()
    outcome = []
    job.finished.connect(lambda token, result, error: outcome.append((token, result, error)))
    beats = []
    heartbeat = QtCore.QTimer()
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start(50)
    job.start(App, doc, code, token="gate", timeout_seconds=timeout_seconds)
    started = time.monotonic()
    if cancel_after is not None:
        QtCore.QTimer.singleShot(cancel_after, job.cancel)
    while not outcome and time.monotonic() - started < timeout_seconds + 5:
        _wait(20)
    heartbeat.stop()
    assert len(outcome) == 1, "Worker did not finish exactly once"
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    directory = job.directory.name
    job.deleteLater()
    return outcome[0], (max(gaps) if gaps else 0.0), job.timings, directory


def _gui_helpers():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ai_gui_smoke_helpers", str(Path(__file__).resolve().with_name("gui_smoke.py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _panel_checks(App, Gui, checks):
    """C production through the real panel: autonomous steps run in the worker."""
    from freecad_ai.core import Proposal
    from freecad_ai.gui import AssistantPanel, QtCore
    from freecad_ai.session import Outcome, TaskState as S
    from freecad_ai.validation import fingerprint
    helpers = _gui_helpers()
    doc = App.newDocument("AIAssistantPanelWorker")
    panel = AssistantPanel(Gui.getMainWindow())
    fake = helpers._fake_transport_class()(panel)
    panel.attach_transport(fake)
    beats = []
    heartbeat = QtCore.QTimer()
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))

    def send(prompt, proposal):
        panel.prompt.setPlainText(prompt)
        panel._send()
        fake.deliver(proposal)
        return panel.task

    def max_gap(since):
        times = [t for t in beats if t >= since]
        return max([b - a for a, b in zip(times, times[1:])] or [0.0])

    try:
        Gui.Selection.clearSelection()
        task = send("Make a box", Proposal("Box", "b = doc.addObject('Part::Box', 'Box')\n"
                                                  "b.Length = 40"))
        helpers._wait_for(lambda: task.steps and task.steps[0].outcome is Outcome.EXECUTED, timeout=30)
        assert doc.Box.Length.Value == 40 and doc.UndoNames[0] == "AI assistant " + task.steps[0].id
        fake.deliver(Proposal("Done"))
        helpers._wait_for(lambda: task.state is S.COMPLETED)
        checks.append("panel: automatic step ran in the worker and applied as a named transaction")

        heartbeat.start(50)
        before = fingerprint(doc)
        task = send("Spin", Proposal("Loop", "while True:\n    pass\n"))
        helpers._wait_for(lambda: panel.worker_job is not None and panel.worker_job.running, timeout=30)
        _wait(300)
        stop_time = time.monotonic()
        panel._stop()
        helpers._wait_for(lambda: panel.worker_job is None or not panel.worker_job.running, timeout=5)
        stopped_in = time.monotonic() - stop_time
        assert task.state is S.STOPPED and task.steps[-1].outcome is Outcome.CANCELLED
        assert fingerprint(doc) == before and max_gap(stop_time - 0.4) < 0.25 and stopped_in < 2
        checks.append("panel: Stop ends a looping worker in {:.2f}s; GUI responsive; "
                      "document unchanged".format(stopped_in))

        sent = len(fake.sent)
        task = send("Crash", Proposal("Crash", "import os\nos.abort()\n"))
        helpers._wait_for(lambda: len(fake.sent) == sent + 2, timeout=30)
        assert task.steps[-1].outcome is Outcome.FAILED and "crashed" in task.steps[-1].result
        assert fingerprint(doc) == before
        panel._stop()
        checks.append("panel: worker crash reported to the provider; document unchanged")

        task = send("Slow", Proposal("Slow", SLOW))
        helpers._wait_for(lambda: panel.worker_job is not None and panel.worker_job.running, timeout=30)
        since, finished_at = time.monotonic(), []
        panel.worker_job.finished.connect(lambda *args: finished_at.append(time.monotonic()))
        helpers._wait_for(lambda: task.steps and task.steps[-1].outcome is Outcome.EXECUTED, timeout=60)
        computing = [t for t in beats if since <= t <= finished_at[0]]
        computing_gap = max([b - a for a, b in zip(computing, computing[1:])] or [0.0])
        applied_gap = max_gap(finished_at[0] - 0.05)
        assert doc.getObject("SlowResult") is not None and computing_gap < 0.25, computing_gap
        panel._stop()
        # After applying, FreeCAD's 3D view tessellates the new solid for display; that
        # rendering is FreeCAD's own and happens with or without the assistant.
        checks.append("panel: slow geometry applied; max GUI gap {:.3f}s while computing, "
                      "{:.3f}s while applying and displaying".format(computing_gap, applied_gap))

        task = send("Slow edit", Proposal("Wait", "import time\ntime.sleep(1.5)\n"
                                                  "doc.Box.Width = 25\n"))
        helpers._wait_for(lambda: panel.worker_job is not None and panel.worker_job.running, timeout=30)
        doc.Box.Height = 33  # The user edits while the worker computes.
        doc.recompute()
        helpers._wait_for(lambda: task.finished, timeout=30)
        assert task.steps[-1].outcome is Outcome.REJECTED and doc.Box.Width.Value == 10
        assert doc.Box.Height.Value == 33
        checks.append("panel: manual edit during the run rejects the candidate")

        task = send("Fit view", Proposal("Fit", "Gui.SendMsgToActiveView('ViewFit')\n"
                                                "doc.Box.Length = 41\n"))
        helpers._wait_for(lambda: task.state is S.AWAITING_REVIEW, timeout=30)
        assert doc.Box.Length.Value == 40 and "Run step" in panel.chat.plain_text()
        panel._run_reviewed()
        assert doc.Box.Length.Value == 41
        panel._stop()
        checks.append("panel: GUI-dependent step stops and offers reviewed mode")

        task = send("Lengthen", Proposal("Lengthen", "doc.Box.Length = 45"))
        helpers._wait_for(lambda: panel.worker_job is not None, timeout=30)
        job = panel.worker_job
        original = panel._finish_step

        def finish_then_stop(*args):
            original(*args)
            panel._stop()  # Stop pressed while changes were being applied.
        panel._finish_step = finish_then_stop
        sent = len(fake.sent)
        helpers._wait_for(lambda: task.finished, timeout=30)
        _wait(100)
        panel._finish_step = original
        assert doc.Box.Length.Value == 45 and len(fake.sent) == sent and task.state is S.STOPPED
        checks.append("panel: Stop during apply prevents the next step")
    finally:
        heartbeat.stop()
        panel.shutdown()
        panel.deleteLater()
        App.closeDocument(doc.Name)


def run():
    import os
    import FreeCAD as App
    import FreeCADGui as Gui
    from freecad_ai.document import DocumentSnapshot, _fingerprint
    from freecad_ai.execution import apply_delta, discover_worker, make_worker_job

    checks = []
    notes = []
    executable = discover_worker(App)
    checks.append("worker discovered: " + executable)
    doc = _fixture(App)
    save_dir = tempfile.TemporaryDirectory(prefix="freecad-ai-worker-test-")
    try:
        # A saved file with an unsaved edit: the copy must include the edit while
        # the original keeps its file name and modified state.
        path = os.path.join(save_dir.name, "fixture.FCStd")
        doc.saveAs(path)
        doc.getObject("Pad").Length = 10  # Same value; re-recorded below.
        doc.getObject("BodyLink").Placement.Base = App.Vector(120, 0, 0)
        doc.recompute()
        gui_doc = Gui.getDocument(doc.Name)
        modified_before = gui_doc.Modified
        assert modified_before and doc.FileName == path

        pad = doc.getObject("Pad")
        pad_identity = id(pad)
        before = _fingerprint(doc)
        undo_before = doc.UndoCount
        (token, result, error), max_gap, timings, directory = _run_job(
            make_worker_job, App, doc, STEP + "\nprint('link x', doc.BodyLink.Placement.Base.x)\n")
        assert error is None and result["ok"], (error, result and result.get("error"))
        assert "link x 120.0" in result["output"], "Snapshot lacked unsaved edits"
        assert doc.FileName == path and gui_doc.Modified == modified_before
        assert not os.path.exists(directory), "Worker scratch directory left behind"
        assert _fingerprint(doc) == before, "Worker changed the original document"
        checks.append("snapshot with unsaved edits; original file, state and model unchanged")

        delta = result["delta"]
        beats = []
        started = time.monotonic()
        doc.openTransaction("AI assistant gate step")
        try:
            apply_delta(doc, delta)
            doc.commitTransaction()
        except Exception:
            doc.abortTransaction()
            raise
        timings["apply"] = time.monotonic() - started
        after = _fingerprint(doc)
        pocket = doc.getObject("Pocket")
        body = doc.getObject("Body")
        assert pocket is not None and pocket.TypeId == "PartDesign::Pocket"
        assert pocket.Profile[0].Name == "HoleSketch" and pocket.BaseFeature.Name == "Pad"
        assert body.Tip.Name == "Pocket" and [o.Name for o in body.Group] == [
            "Sketch", "Pad", "HoleSketch", "Pocket"]
        assert abs(pocket.Shape.Volume - (50 * 30 * 15 - 3.14159265 * 25 * 15)) < 0.5
        assert doc.getObject("HoleSketch").getDatum("hole_r").Value == 5
        assert pad.ExpressionEngine == [("Length", "Sketch.Constraints.height / 2")]
        assert id(doc.getObject("Pad")) == pad_identity
        assert tuple(round(c, 2) for c in pad.ViewObject.ShapeAppearance[0].DiffuseColor[:3]) == (0.8, 0.2, 0.2)
        assert doc.getObject("BodyLink").LinkedObject is body
        assert not [o.Name for o in doc.Objects if "Touched" in o.State or "Invalid" in o.State]
        assert doc.UndoCount == undo_before + 1
        checks.append("parametric graph transferred: constraints, expression, Pad/Pocket, "
                      "link, identity, appearance; nothing left to recompute")

        doc.undo()
        assert doc.getObject("Pocket") is None and _fingerprint(doc) == before
        doc.redo()
        assert doc.getObject("Pocket") is not None and _fingerprint(doc) == after
        checks.append("one transaction; Undo restores and Redo reapplies exactly")

        doc.getObject("Sketch").setDatum("height", App.Units.Quantity("40 mm"))
        doc.recompute()
        assert pad.Length.Value == 20
        assert abs(body.Shape.Volume - (50 * 40 * 20 - 3.14159265 * 25 * 20)) < 0.5
        checks.append("model stays parametric after transfer")
        notes.append("snapshot {:.3f}s, worker {:.3f}s, apply {:.3f}s, max GUI gap {:.3f}s".format(
            timings["snapshot"], timings["worker"], timings["apply"], max_gap))

        (token, result, error), max_gap, timings, _ = _run_job(make_worker_job, App, doc, SLOW)
        assert error is None and result["ok"], (error, result)
        assert timings["worker"] > 1.5 and max_gap < 0.25, (timings, max_gap)
        checks.append("slow geometry ({:.2f}s in worker) leaves the GUI responsive "
                      "(max heartbeat gap {:.3f}s)".format(timings["worker"], max_gap))

        unchanged = _fingerprint(doc)
        (token, result, error), max_gap, timings, directory = _run_job(
            make_worker_job, App, doc, "while True:\n    pass\n", cancel_after=400)
        assert result is None and error.kind == "cancelled" and timings["worker"] < 2.5
        assert _fingerprint(doc) == unchanged and not os.path.exists(directory)
        checks.append("infinite loop cancelled in {:.2f}s; document unchanged".format(timings["worker"]))

        (token, result, error), max_gap, timings, _ = _run_job(
            make_worker_job, App, doc, "import os\nos.abort()\n")
        assert result is None and error.kind in ("crashed", "protocol"), error
        assert _fingerprint(doc) == unchanged
        checks.append("worker crash reported; document unchanged")

        (token, result, error), max_gap, timings, _ = _run_job(
            make_worker_job, App, doc, "cut = doc.addObject('Part::Cut', 'BrokenInWorker')\n"
                                       "cut.Base = doc.getObject('Pad')\n")
        assert error is None and not result["ok"] and result["kind"] == "validation", result
        assert [e["name"] for e in result["diagnostics"]["new_invalid"]] == ["BrokenInWorker"]
        assert _fingerprint(doc) == unchanged and doc.getObject("BrokenInWorker") is None
        checks.append("worker rejects a newly broken feature; nothing applied")

        (token, result, error), max_gap, timings, _ = _run_job(
            make_worker_job, App, doc, "Gui.SendMsgToActiveView('ViewFit')\n")
        assert error is None and not result["ok"] and result["kind"] == "unsupported"
        checks.append("GUI-dependent code is reported as unsupported")
        _panel_checks(App, Gui, checks)
        print("PASS: " + ", ".join(checks))
        print("TIMINGS: " + "; ".join(notes))
    finally:
        App.closeDocument(doc.Name)
        save_dir.cleanup()
