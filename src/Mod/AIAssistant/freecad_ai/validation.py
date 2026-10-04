# SPDX-License-Identifier: LGPL-2.1-or-later
"""Revision digests and validation of a step against the document's existing errors.

No GUI imports: the headless worker uses this module too. A step fails when it
introduces or worsens feature errors; errors that already existed and were not
touched are reported separately and do not block unrelated work.
"""

import hashlib
import io
import re
import zipfile

MAX_DIAGNOSTICS = 50
MAX_GEOMETRY_CHECKS = 50
SHAPE_PROPERTY = "Part::PropertyPartShape"
# Shape properties embed element maps in Content, which saving rewrites.
_SHAPE_XML = re.compile(r'<Property name="[^"]*" type="Part::PropertyPartShape"[^>]*>.*?</Property>',
                        re.DOTALL)
# Status bits (touched, editor modes) change on undo and recompute, not with values.
_STATUS_XML = re.compile(r' status="\d+"')
# BREP TShape flag lines (checked, modified, ...) change when an algorithm merely
# inspects a shape, so they are not part of its geometry.
_BREP_FLAGS = re.compile(r"^[01]{7}$", re.MULTILINE)
# Feature kinds that must produce at least one valid solid.
_SOLID_FEATURES = ("PartDesign::FeatureAddSub", "Part::Primitive", "Part::Boolean",
                   "Part::MultiFuse", "Part::MultiCommon", "Part::Extrusion")


def content_digest(content):
    """Digest a dumped property by its archive members, not the archive bytes.

    dumpPropertyContent returns a ZIP whose headers carry a modification time,
    so identical values dumped two seconds apart would otherwise differ.
    """
    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                digest.update(info.filename.encode("utf-8") + b"\0")
                digest.update(archive.read(info))
                digest.update(b"\0")
    except (zipfile.BadZipFile, ValueError, OSError):
        digest.update(content)
    return digest.hexdigest()


def _is_shape(obj, name):
    try:
        return obj.getTypeIdOfProperty(name) == SHAPE_PROPERTY
    except Exception:
        return False


def property_digest(obj, name):
    """A digest that changes with the value, not with saving or rendering."""
    if _is_shape(obj, name):
        try:
            # Geometry only: a shape's element map changes when its document is saved.
            brep = _BREP_FLAGS.sub("", getattr(obj, name).exportBrepToString())
            return hashlib.sha256(brep.encode("utf-8")).hexdigest()
        except Exception:
            pass
    try:
        content = bytes(obj.dumpPropertyContent(name))
    except Exception:
        return None
    return content_digest(content)


def input_digest(obj):
    """Digest of an object's non-shape property values (its inputs)."""
    digest = hashlib.sha256((obj.Name + "\0" + obj.TypeId + "\0").encode("utf-8"))
    try:
        digest.update(_STATUS_XML.sub("", _SHAPE_XML.sub("", obj.Content)).encode("utf-8"))
    except Exception:
        for name in sorted(obj.PropertiesList):
            if not _is_shape(obj, name):
                digest.update((name + "\0" + str(property_digest(obj, name))).encode("utf-8"))
    return digest.hexdigest()


def shape_digest(obj):
    digest = hashlib.sha256()
    for name in obj.PropertiesList:
        if _is_shape(obj, name):
            digest.update((name + "\0" + str(property_digest(obj, name))).encode("utf-8"))
    return digest.hexdigest()


def input_digests(doc):
    return {obj.Name: input_digest(obj) for obj in doc.Objects}


def fingerprint(doc):
    """Local revision digest over every property of every object."""
    if doc is None:
        return ""
    digest = hashlib.sha256()
    for obj in doc.Objects:
        digest.update(input_digest(obj).encode("ascii"))
        digest.update(shape_digest(obj).encode("ascii"))
    return digest.hexdigest()


def _message(obj):
    try:
        return (obj.getStatusString() or "")[:300]
    except Exception:
        return ""


def capture_diagnostics(doc):
    """Invalid features and their messages, keyed by object name."""
    result = {}
    for obj in doc.Objects:
        state = list(getattr(obj, "State", []) or [])
        if "Invalid" in state or "Error" in state:
            result[obj.Name] = {"label": obj.Label[:80], "type": obj.TypeId,
                                "message": _message(obj)}
    return result


def _entries(diagnostics, names):
    return [dict(name=name, **diagnostics[name]) for name in names[:MAX_DIAGNOSTICS]]


def compare_diagnostics(before, after, changed=()):
    """Classify feature errors after a step relative to its baseline.

    A changed feature that remains invalid is rejected: without detailed
    diagnostics, repair progress cannot be verified.
    """
    changed = set(changed)
    new_invalid = [name for name in after if name not in before]
    repaired = [name for name in before if name not in after]
    still_invalid, worsened, preexisting = [], [], []
    for name in after:
        if name not in before:
            continue
        if name in changed:
            still_invalid.append(name)
        elif after[name]["message"] != before[name]["message"]:
            worsened.append(name)
        else:
            preexisting.append(name)
    return {
        "ok": not (new_invalid or worsened or still_invalid),
        "new_invalid": _entries(after, new_invalid),
        "worsened": _entries(after, worsened),
        "changed_still_invalid": _entries(after, still_invalid),
        "repaired": _entries(before, repaired),
        "preexisting": _entries(after, preexisting),
        "baseline": _entries(before, list(before)),
    }


def _derived(obj, type_name):
    try:
        return obj.isDerivedFrom(type_name)
    except Exception:
        return False


def geometry_checks(doc, names):
    """Bounded checks of the geometry a step created or edited."""
    errors = []
    for name in list(names)[:MAX_GEOMETRY_CHECKS]:
        obj = doc.getObject(name)
        if obj is None or not _derived(obj, "Part::Feature"):
            continue
        if not any(_derived(obj, kind) for kind in _SOLID_FEATURES):
            continue
        try:
            shape = obj.Shape
            if shape.isNull() or not shape.Solids:
                errors.append({"name": name, "problem": "produced no solid"})
            elif not all(solid.isValid() for solid in shape.Solids):
                errors.append({"name": name, "problem": "produced an invalid solid"})
        except Exception as error:
            errors.append({"name": name, "problem": "geometry check failed: {}".format(error)})
    return errors


def validate_step(doc, before, inputs_before, recompute_error=None):
    """Return (ok, report) for a recomputed document against its baseline."""
    after = capture_diagnostics(doc)
    inputs_after = input_digests(doc)
    changed = [name for name, digest in inputs_after.items() if inputs_before.get(name) != digest]
    report = compare_diagnostics(before, after, changed)
    report["geometry_errors"] = geometry_checks(doc, changed)
    report["recompute_error"] = recompute_error
    report["changed"] = changed[:MAX_DIAGNOSTICS]
    report["added"] = [name for name in changed if name not in inputs_before][:MAX_DIAGNOSTICS]
    report["ok"] = bool(report["ok"] and not report["geometry_errors"] and not recompute_error)
    return report["ok"], report


def summarize(report):
    """One readable line per problem, for the provider and the timeline."""
    lines = []
    if report.get("recompute_error"):
        lines.append("Recompute failed: " + report["recompute_error"])
    for key, label in (("new_invalid", "Newly invalid"), ("worsened", "Worsened"),
                       ("changed_still_invalid", "Changed but still invalid")):
        for entry in report.get(key, []):
            lines.append("{}: {} ({}){}".format(label, entry["name"], entry["type"],
                                                ": " + entry["message"] if entry["message"] else ""))
    for entry in report.get("geometry_errors", []):
        lines.append("Geometry: {} {}".format(entry["name"], entry["problem"]))
    if report.get("repaired"):
        lines.append("Repaired: " + ", ".join(entry["name"] for entry in report["repaired"]))
    if report.get("preexisting"):
        lines.append("Unchanged pre-existing errors (not caused by this step): " + ", ".join(
            entry["name"] for entry in report["preexisting"]))
    return "\n".join(lines)
