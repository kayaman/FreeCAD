# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run modeling steps in a headless worker and apply their results on the GUI thread.

Expensive work (generated code, recomputation) happens in FreeCADCmd on a
disposable copy. The GUI only restores the resulting property values inside one
undoable transaction, without recomputing.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .core import AssistantError
from .worker import PROTOCOL

REQUIRED_MODULES = ("Part", "Sketcher", "PartDesign")
_SHAPE_PROPERTY = "Part::PropertyPartShape"
_discovered = {}


def worker_script():
    return str(Path(__file__).resolve().with_name("worker.py"))


def worker_candidates(app):
    names = ("FreeCADCmd", "freecadcmd") + (("FreeCADCmd.exe",) if sys.platform == "win32" else ())
    home = Path(app.getHomePath())
    candidates = [home / "bin" / name for name in names]
    executable = Path(sys.executable)
    candidates += [executable.with_name(name) for name in names]
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    unique = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in unique:
            unique.append(candidate)
    return unique


def discover_worker(app, timeout=30):
    """Return a FreeCADCmd matching this FreeCAD, verified once per session."""
    version = ".".join(app.Version()[:3])
    cached = _discovered.get(version)
    if cached:
        return cached
    probe = ("import sys, json, FreeCAD\nmods = {}\nfor name in %r:\n"
             "    try:\n        __import__(name); mods[name] = True\n"
             "    except Exception:\n        mods[name] = False\n"
             "sys.stdout.write('\\n@@' + json.dumps({'version': '.'.join(FreeCAD.Version()[:3]),"
             " 'modules': mods}) + '@@\\n')\n") % (REQUIRED_MODULES,)
    reasons = []
    with tempfile.TemporaryDirectory(prefix="freecad-ai-probe-") as directory:
        script = Path(directory) / "probe.py"
        script.write_text(probe)
        for candidate in worker_candidates(app):
            try:
                result = subprocess.run([str(candidate), "--safe-mode", str(script)],
                                        capture_output=True, timeout=timeout,
                                        stdin=subprocess.DEVNULL)
                text = result.stdout.decode("utf-8", "replace")
                info = json.loads(text.split("\n@@", 1)[1].split("@@\n", 1)[0])
            except (OSError, subprocess.SubprocessError, IndexError, ValueError):
                reasons.append(candidate.name + ": could not be started")
                continue
            if info["version"] != version:
                reasons.append("{} reports FreeCAD {}".format(candidate.name, info["version"]))
                continue
            missing = [name for name, ok in info["modules"].items() if not ok]
            if missing:
                reasons.append("{} lacks {}".format(candidate.name, ", ".join(missing)))
                continue
            _discovered[version] = str(candidate)
            return str(candidate)
    raise AssistantError("No compatible headless FreeCAD (FreeCADCmd {}) was found{}.".format(
        version, ": " + "; ".join(reasons[:3]) if reasons else ""))


def worker_arguments():
    return ["--safe-mode", worker_script()]


def worker_environment():
    return {"FREECAD_AI_WORKER": str(PROTOCOL)}


def write_snapshot(doc, directory):
    """Copy the document, including unsaved edits, without touching its file or state."""
    path = os.path.join(directory, "snapshot.FCStd")
    file_name = doc.FileName
    modified = _modified(doc)
    doc.saveCopy(path)
    if doc.FileName != file_name or _modified(doc) != modified:
        raise AssistantError("Could not copy the document without changing it.")
    return path


def _modified(doc):
    try:
        import FreeCADGui
        gui_doc = FreeCADGui.getDocument(doc.Name)
        return bool(gui_doc.Modified) if gui_doc is not None else None
    except Exception:
        return None


def request_payload(snapshot, code):
    return json.dumps({"protocol": PROTOCOL, "snapshot": snapshot, "code": code}).encode("utf-8")


def apply_delta(doc, delta):
    """Restore a worker delta into doc. The caller owns the transaction."""
    for name in delta["removed"]:
        if doc.getObject(name) is not None:
            doc.removeObject(name)
    for item in delta["added"]:
        obj = doc.addObject(item["type"], item["name"])
        if obj.Name != item["name"]:
            raise AssistantError("Object {} could not keep its name; the model changed.".format(
                item["name"]))
    for item in delta["dynamic"]:
        doc.getObject(item["object"]).addProperty(item["type"], item["name"],
                                                  item["group"], item["doc"])
    shapes = []
    for change in delta["changed"]:
        obj = doc.getObject(change["object"])
        if obj is None:
            raise AssistantError("Object {} disappeared; the model changed.".format(change["object"]))
        for name, content in change["properties"].items():
            if name not in obj.PropertiesList:
                raise AssistantError("{}.{} is not available in this document.".format(obj.Name, name))
            if obj.getTypeIdOfProperty(name) == _SHAPE_PROPERTY:
                shapes.append((obj, name, content))  # After links and placements.
            else:
                obj.restorePropertyContent(name, base64.b64decode(content))
    for change in delta["changed"]:
        obj = doc.getObject(change["object"])
        current = dict(obj.ExpressionEngine)
        wanted = dict(change.get("expressions", []))
        for path in current:
            if path not in wanted:
                obj.setExpression(path, None)
        for path, expression in wanted.items():
            if current.get(path) != expression:
                obj.setExpression(path, expression)
    for obj, name, content in shapes:
        obj.restorePropertyContent(name, base64.b64decode(content))
    # The worker recomputed everything; only its still-touched objects need work.
    still_touched = set(delta.get("touched", []))
    for obj in doc.Objects:
        if obj.Name not in still_touched:
            obj.purgeTouched()
    return sorted({change["object"] for change in delta["changed"]} |
                  {item["name"] for item in delta["added"]})


def _qt():
    try:
        from PySide import QtCore
    except ImportError:
        try:
            from PySide2 import QtCore
        except ImportError:
            from PySide6 import QtCore
    return QtCore


class WorkerFailure(AssistantError):
    def __init__(self, message, kind="worker", output=""):
        super().__init__(message)
        self.kind = kind
        self.output = output


def make_worker_job(parent=None):
    """Create a WorkerJob; Qt is imported lazily so unit tests need no Qt."""
    QtCore = _qt()
    from .processes import EXITED, ProcessSupervisor
    from .worker import MAX_RESULT_BYTES, WorkerError, decode_message

    class WorkerJob(QtCore.QObject):
        """One modeling step in FreeCADCmd. Emits finished(token, result, error)."""
        finished = QtCore.Signal(object, object, object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.supervisor = None
            self.token = None
            self.output = bytearray()
            self.directory = None
            self.timings = {}
            self.done = False
            self.oversized = False
            self._started = 0.0

        def start(self, app, doc, code, token=None, timeout_seconds=120):
            import time
            self.token = token
            started = time.monotonic()
            executable = discover_worker(app)
            self.directory = tempfile.TemporaryDirectory(prefix="freecad-ai-worker-")
            snapshot = write_snapshot(doc, self.directory.name)
            self.timings["snapshot"] = time.monotonic() - started
            self._started = time.monotonic()
            environment = QtCore.QProcessEnvironment.systemEnvironment()
            for key, value in worker_environment().items():
                environment.insert(key, value)
            supervisor = ProcessSupervisor(self)
            self.supervisor = supervisor
            supervisor.stdout.connect(self._stdout)
            supervisor.finished.connect(self._finished)
            supervisor.add_cleanup(self.directory.cleanup)
            supervisor.start(executable, worker_arguments(), environment, self.directory.name,
                             stdin=request_payload(snapshot, code),
                             timeout_ms=timeout_seconds * 1000)

        @property
        def running(self):
            return self.supervisor is not None and self.supervisor.running

        def _stdout(self, data):
            self.output.extend(data)
            if len(self.output) > MAX_RESULT_BYTES + 1024:
                self.oversized = True
                self.supervisor.cancel()

        def cancel(self):
            if self.supervisor is not None:
                self.supervisor.cancel()

        def shutdown(self):
            if self.supervisor is not None:
                self.supervisor.shutdown()

        def _finished(self, outcome):
            import time
            if self.done:
                return
            self.done = True
            self.timings["worker"] = time.monotonic() - self._started
            result, error = None, None
            if self.oversized:
                error = WorkerFailure("The modeling result exceeded the transfer limit.")
            elif outcome.kind != EXITED:
                kinds = {"cancelled": "Stopped.", "timed out": "The modeling step timed out.",
                         "crashed": "The modeling worker crashed; the document was not changed.",
                         "start failed": "The modeling worker could not be started."}
                error = WorkerFailure(kinds.get(outcome.kind, "The modeling worker failed."),
                                      kind=outcome.kind.replace(" ", "_"))
            else:
                try:
                    result = decode_message(self.output)
                except WorkerError as failure:
                    error = WorkerFailure(str(failure) if outcome.code == 0 else
                                          "The modeling worker crashed; the document was not changed.",
                                          kind="crashed" if outcome.code else "protocol")
            self.output = bytearray()
            self.finished.emit(self.token, result, error)

    return WorkerJob(parent)
