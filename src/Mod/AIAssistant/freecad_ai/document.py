# SPDX-License-Identifier: LGPL-2.1-or-later
"""Read model summaries and run agent steps on FreeCAD's GUI thread."""

from dataclasses import dataclass
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import math

from .core import AssistantError
from .validation import (capture_diagnostics, fingerprint, input_digests, summarize,
                         validate_step)

MAX_CONTEXT_OBJECTS = 80
MAX_CONTEXT_PROPERTIES = 24
MAX_SELECTED_PROPERTIES = 48
MAX_CONTEXT_BYTES = 60000
MAX_SELECTION = 40
MAX_SUBELEMENTS = 12
MAX_DEPENDENCIES = 20
MAX_CONSTRAINTS = 60
UNITS = {"Length": "mm", "Angle": "deg", "Area": "mm^2", "Volume": "mm^3",
         "Mass": "kg", "Density": "kg/m^3", "Force": "N", "Pressure": "MPa",
         "Velocity": "mm/s", "Acceleration": "mm/s^2", "TimeSpan": "s",
         "Temperature": "K", "Frequency": "1/s"}
# Never sent automatically: free text, paths, raw shapes, images, credentials.
EXCLUDED_CONTENT = ["free-text properties", "file paths", "raw shape data", "screenshots"]
_VALUE_TYPES = ("App::PropertyEnumeration",)


class _Output(io.TextIOBase):
    """Keep execution feedback bounded even when generated code prints a lot."""
    def __init__(self):
        self.text = ""
        self.truncated = False

    def write(self, text):
        remaining = max(0, 8000 - len(self.text))
        self.text += text[:remaining]
        self.truncated = self.truncated or len(text) > remaining
        return len(text)

    def getvalue(self):
        return self.text + ("\n[output truncated]" if self.truncated else "")


def _round(value):
    return round(float(value), 6)


def _quantity(value):
    """Format a FreeCAD Quantity with its own unit; angles are never millimeters."""
    unit_type = str(getattr(value.Unit, "Type", "") or "")
    unit = UNITS.get(unit_type)
    if unit is None:
        return {"value": _round(value.Value), "unit": str(value.Unit)}
    try:
        number = value.getValueAs(unit) if hasattr(value, "getValueAs") else value.Value
        number = float(getattr(number, "Value", number))
    except Exception:
        number = float(value.Value)
    return "{:g} {}".format(_round(number), unit)


def _editable(obj, name):
    try:
        mode = obj.getEditorMode(name)
    except Exception:
        return True
    return not ("Hidden" in mode or "ReadOnly" in mode)


def _properties(obj, limit=MAX_CONTEXT_PROPERTIES):
    """Numeric, boolean, quantity and enumeration values the provider may edit."""
    values = {}
    for name in sorted(obj.PropertiesList):
        if len(values) >= limit:
            break
        if not _editable(obj, name):
            continue
        try:
            value = getattr(obj, name)
            if hasattr(value, "Value") and hasattr(value, "Unit"):
                values[name] = _quantity(value)
            elif isinstance(value, bool) or isinstance(value, int):
                values[name] = value
            elif isinstance(value, float) and math.isfinite(value):
                values[name] = _round(value)
            elif isinstance(value, str) and obj.getTypeIdOfProperty(name) in _VALUE_TYPES:
                values[name] = value[:80]
        except (AttributeError, RuntimeError, ValueError, TypeError):
            continue
    return values


def _owners(obj):
    """Enclosing Bodies/Parts, innermost first; safe against malformed cycles."""
    owners, seen = [], {obj.Name}
    current = obj
    while True:
        try:
            parent = current.getParentGeoFeatureGroup()
        except Exception:
            parent = None
        if parent is None or parent.Name in seen:
            return owners
        seen.add(parent.Name)
        owners.append(parent)
        current = parent


def _out_list(obj):
    try:
        return list(obj.OutList)
    except Exception:
        return []


def order_objects(objects, selected_names):
    """Selected objects, their owners, their dependencies, then everything else."""
    by_name = {obj.Name: obj for obj in objects}
    ordered, seen = [], set()

    def add(obj, reason):
        if obj.Name in by_name and obj.Name not in seen:
            seen.add(obj.Name)
            ordered.append((by_name[obj.Name], reason))

    selected = [by_name[name] for name in selected_names if name in by_name]
    for obj in selected:
        add(obj, "selected")
    for obj in selected:
        for owner in _owners(obj):
            add(owner, "owner")
    visited = {obj.Name for obj in selected}
    frontier = list(selected)
    while frontier:  # Breadth first; the visited set makes cycles harmless.
        following = []
        for obj in frontier:
            for dependency in _out_list(obj):
                if dependency.Name in by_name and dependency.Name not in visited:
                    visited.add(dependency.Name)
                    add(dependency, "dependency")
                    following.append(dependency)
        frontier = following
    for obj in objects:
        add(obj, "other")
    return ordered


def _placement(obj):
    try:
        placement = obj.Placement
        rotation = placement.Rotation
        return {"position": [_round(v) for v in placement.Base] + ["mm"],
                "rotation_axis": [_round(v) for v in rotation.Axis],
                "rotation_angle": "{:g} deg".format(_round(math.degrees(rotation.Angle)))}
    except Exception:
        return None


def _shape(obj):
    try:
        shape = obj.Shape
        if shape is None or shape.isNull():
            return None
        return shape
    except Exception:
        return None


def _bounds(shape):
    box = shape.BoundBox
    return {"size": [_round(box.XLength), _round(box.YLength), _round(box.ZLength), "mm"],
            "min": [_round(box.XMin), _round(box.YMin), _round(box.ZMin), "mm"]}


def _subelement(obj, name):
    try:
        element = obj.getSubObject(name)
    except Exception:
        element = None
    summary = {"name": name[:80]}
    if element is None:
        return summary
    kind = getattr(element, "ShapeType", "")
    summary["kind"] = kind
    try:
        if kind == "Face":
            surface = element.Surface
            summary["surface"] = type(surface).__name__
            summary["area"] = "{:g} mm^2".format(_round(element.Area))
            center = element.CenterOfMass
            summary["center"] = [_round(v) for v in center] + ["mm"]
            if summary["surface"] == "Plane":
                u, v = element.ParameterRange[0], element.ParameterRange[2]
                summary["normal"] = [_round(c) for c in element.normalAt(u, v)]
            if hasattr(surface, "Radius"):
                summary["radius"] = "{:g} mm".format(_round(surface.Radius))
        elif kind == "Edge":
            curve = element.Curve
            summary["curve"] = type(curve).__name__
            summary["length"] = "{:g} mm".format(_round(element.Length))
            if hasattr(curve, "Radius"):
                summary["radius"] = "{:g} mm".format(_round(curve.Radius))
        elif kind == "Vertex":
            summary["point"] = [_round(element.X), _round(element.Y), _round(element.Z), "mm"]
    except Exception:
        pass
    return summary


def _constraints(obj):
    result = []
    try:
        constraints = list(obj.Constraints)
    except Exception:
        return None
    for index, constraint in enumerate(constraints[:MAX_CONSTRAINTS]):
        entry = {"index": index, "type": constraint.Type}
        if constraint.Name:
            entry["name"] = constraint.Name[:60]
        try:
            datum = obj.getDatum(index)
            entry["value"] = _quantity(datum)
            entry["driving"] = bool(constraint.Driving)
        except Exception:
            pass
        result.append(entry)
    return {"items": result, "count": len(constraints)}


def object_summary(obj, reason, subelements=()):
    """A bounded, unit-labelled description of one object."""
    detailed = reason in ("selected", "owner")
    summary = {"name": obj.Name, "label": obj.Label[:160], "type": obj.TypeId, "reason": reason,
               "properties": _properties(obj, MAX_SELECTED_PROPERTIES if detailed
                                         else MAX_CONTEXT_PROPERTIES)}
    dependencies = [dep.Name for dep in _out_list(obj)]
    if dependencies:
        summary["dependencies"] = dependencies[:MAX_DEPENDENCIES]
    owners = _owners(obj)
    if owners:
        summary["owner"] = owners[0].Name
    if reason != "other":
        placement = _placement(obj)
        if placement is not None:
            summary["placement"] = placement
        shape = _shape(obj)
        if shape is not None:
            summary["bounds"] = _bounds(shape)
            if reason == "selected" and getattr(shape, "Solids", None):
                summary["volume"] = "{:g} mm^3".format(_round(shape.Volume))
        if obj.TypeId.startswith("Sketcher::SketchObject"):
            constraints = _constraints(obj)
            if constraints is not None:
                summary["constraints"] = constraints
    if subelements:
        summary["selected_subelements"] = [_subelement(obj, name)
                                           for name in list(subelements)[:MAX_SUBELEMENTS]]
    return summary


def build_context(doc, selection, version, max_objects=MAX_CONTEXT_OBJECTS,
                  max_bytes=MAX_CONTEXT_BYTES):
    """selection: [(object name, [subelement names])] in the target document."""
    context = {"freecad_version": version,
               "units": {"length": "mm", "angle": "deg", "area": "mm^2", "volume": "mm^3"},
               "document": None, "selection": [],
               "omitted": {"content": EXCLUDED_CONTENT}}
    if doc is None:
        context["omitted"]["document"] = "No document is open."
        return context
    selection = list(selection)
    context["selection"] = [{"object": name, "subelements": list(subs)[:MAX_SUBELEMENTS]}
                            for name, subs in selection[:MAX_SELECTION]]
    subelements = dict((name, subs) for name, subs in selection)
    objects = list(doc.Objects)
    ordered = order_objects(objects, [name for name, _ in selection])
    included, used, omitted = [], 0, {}
    omitted_selected = [name for name, _ in selection[MAX_SELECTION:]]
    for obj, reason in ordered:
        summary = object_summary(obj, reason, subelements.get(obj.Name, ()))
        size = len(json.dumps(summary, ensure_ascii=False))
        if len(included) >= max_objects or used + size > max_bytes:
            omitted[reason] = omitted.get(reason, 0) + 1
            if reason == "selected":
                omitted_selected.append(obj.Name)
            continue
        included.append(summary)
        used += size
    context["document"] = {"name": doc.Name, "label": doc.Label[:160],
                           "object_count": len(objects), "objects": included,
                           "truncated": len(included) < len(objects)}
    if omitted:
        context["omitted"]["objects"] = omitted
    if omitted_selected:
        context["omitted"]["selected"] = omitted_selected[:MAX_SELECTION]
        context["omitted"]["note"] = ("The selection exceeds the context budget; ask the user "
                                      "to select fewer objects before editing them.")
    return context


def external_links(doc):
    """Names of other documents this document depends on (not part of a worker copy)."""
    names = set()
    for obj in doc.Objects:
        for dependency in _out_list(obj):
            other = getattr(dependency, "Document", doc)
            if other is not doc:
                names.add(getattr(other, "Name", "?"))
    return sorted(names)


def selection_in(gui, doc):
    if doc is None:
        return []
    return [(sel.ObjectName, list(sel.SubElementNames))
            for sel in gui.Selection.getSelectionEx(doc.Name)]


def model_context(app, gui):
    doc = app.ActiveDocument
    return build_context(doc, selection_in(gui, doc), ".".join(app.Version()[:3]))


# Local revision digest over every property, so changes a bounded provider
# summary omits (constraints, links, placement) are still detected.
_fingerprint = fingerprint


@dataclass(frozen=True)
class DocumentSnapshot:
    document: object
    fingerprint: str
    selection: str

    @classmethod
    def capture(cls, app, gui):
        doc = app.ActiveDocument
        selection = json.dumps([(s.DocumentName, s.ObjectName, list(s.SubElementNames))
                                for s in gui.Selection.getSelectionEx()])
        return cls(doc, _fingerprint(doc), selection)

    def validate(self, app, gui):
        current = self.capture(app, gui)
        if (current.document is not self.document or current.fingerprint != self.fingerprint
                or current.selection != self.selection):
            raise AssistantError("The model or selection changed while the assistant was thinking. "
                                 "Send a new request using the current model.")


class StepFailed(AssistantError):
    """A step ran but was rolled back; diagnostics describe why."""
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class StepResult:
    text: str
    diagnostics: dict
    changed: tuple = ()


def _restore_states(doc, touched_before, baseline):
    """After a rollback, restore the recompute state FreeCAD does not undo.

    Values and shapes are restored by abortTransaction, but restored objects are
    left touched and lose their Invalid flag. Features that were invalid recompute
    again (they failed before, so their errors return); objects that were up to
    date are marked so again.
    """
    failed = [doc.getObject(name) for name in baseline if doc.getObject(name) is not None]
    for obj in doc.Objects:
        if obj.Name not in touched_before and obj.Name not in baseline:
            obj.purgeTouched()
    if failed:
        try:
            doc.recompute(failed)
        except Exception:
            pass


def execute_step(code, app, gui, snapshot, transaction="AI assistant modeling step", on_create=None):
    """Execute full-trust Python on the GUI thread, validated against existing errors.

    The step is rolled back if it introduces or worsens feature errors, fails to
    recompute, or produces invalid geometry. Transactions cover only the target
    document.
    """
    snapshot.validate(app, gui)
    compiled = compile(code, "<FreeCAD AI assistant>", "exec")
    doc = app.ActiveDocument
    if doc is not None and doc.HasPendingTransaction:
        raise AssistantError("Finish the current FreeCAD editing operation before running the assistant.")
    created = doc is None
    if created:
        doc = app.newDocument("AIModel")
        if on_create is not None:
            on_create(doc)
    baseline = capture_diagnostics(doc)
    inputs_before = input_digests(doc)
    touched_before = {obj.Name for obj in doc.Objects if "Touched" in obj.State}
    doc.openTransaction(transaction)
    output = _Output()
    try:
        namespace = {"App": app, "Gui": gui, "doc": doc,
                     "FreeCAD": app, "FreeCADGui": gui}
        with redirect_stdout(output), redirect_stderr(output):
            exec(compiled, namespace, namespace)
        if app.ActiveDocument is not doc:
            raise AssistantError("Assistant code changed the active document.")
        recompute_error = None
        try:
            doc.recompute()
        except Exception as error:
            recompute_error = "{}: {}".format(type(error).__name__, error)
        valid, diagnostics = validate_step(doc, baseline, inputs_before, recompute_error)
        if not valid:
            raise StepFailed("The step was rolled back.\n" + summarize(diagnostics), diagnostics)
        doc.commitTransaction()
    except BaseException:
        try:
            if app.getDocument(doc.Name) is doc:
                doc.abortTransaction()
                if created:
                    app.closeDocument(doc.Name)
                else:
                    _restore_states(doc, touched_before, baseline)
        except Exception:
            pass
        raise
    result = "Modeling step completed. Target document: {}. Object count: {}.".format(
        doc.Name, len(doc.Objects))
    notes = summarize(diagnostics)
    if notes:
        result += "\n" + notes
    if output.getvalue().strip():
        result += "\nPython output:\n" + output.getvalue()
    return StepResult(result, diagnostics, tuple(diagnostics.get("changed", ())))
