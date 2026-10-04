# SPDX-License-Identifier: LGPL-2.1-or-later
"""Single FreeCAD integration entry point. Run run() on the GUI thread.

Each smoke uses disposable documents and fake providers. The active document and
selection are restored afterward, whether the checks pass or fail.
"""
import importlib.util
from pathlib import Path
import sys
import traceback

TEST_ROOT = Path(__file__).resolve().parent
MODULE_ROOT = TEST_ROOT.parents[1] / "src/Mod/AIAssistant"
SMOKES = ("gui_smoke", "transport_smoke", "worker_smoke", "timeline_smoke")


def _purge():
    """Reload edited sources instead of reusing modules from a previous run."""
    for name in list(sys.modules):
        if name == "freecad_ai" or name.startswith("freecad_ai."):
            del sys.modules[name]


def _scratch():
    import tempfile
    root = Path(tempfile.gettempdir())
    return {path.name for path in root.glob("freecad-ai-*")}


def _children():
    """Live (non-zombie) child processes of this FreeCAD, on Linux."""
    import os
    children = set()
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == os.getpid() and fields[0] != "Z":
            children.add(int(stat.parent.name))
    return children


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TEST_ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(names=None):
    import FreeCAD as App
    import FreeCADGui as Gui
    if str(MODULE_ROOT) not in sys.path:
        sys.path.insert(0, str(MODULE_ROOT))
    _purge()
    original = App.ActiveDocument.Name if App.ActiveDocument is not None else None
    selection = [(s.DocumentName, s.ObjectName, list(s.SubElementNames))
                 for s in Gui.Selection.getSelectionEx()]
    documents = set(App.listDocuments())
    scratch, children = _scratch(), _children()
    results = []
    try:
        for name in names or SMOKES:
            if not (TEST_ROOT / (name + ".py")).is_file():
                results.append("SKIP " + name + ": not present")
                continue
            try:
                _load(name).run()
                results.append("PASS " + name)
            except Exception:
                results.append("FAIL " + name + "\n" + traceback.format_exc())
    finally:
        for doc_name in set(App.listDocuments()) - documents:
            if doc_name.startswith("AIAssistant") or doc_name.startswith("AIModel"):
                App.closeDocument(doc_name)
        if original is not None and original in App.listDocuments():
            App.setActiveDocument(original)
            Gui.ActiveDocument = Gui.getDocument(original)
        Gui.Selection.clearSelection()
        for doc_name, obj_name, subs in selection:
            if doc_name not in App.listDocuments():
                continue
            for sub in subs or [""]:
                Gui.Selection.addSelection(doc_name, obj_name, sub)
    leftovers = sorted(_scratch() - scratch)
    orphans = sorted(_children() - children)
    if leftovers or orphans:
        results.append("FAIL cleanup: scratch {} processes {}".format(leftovers, orphans))
    else:
        results.append("PASS cleanup: no scratch directories or child processes left")
    report = "\n".join(results)
    print(report)
    return report
