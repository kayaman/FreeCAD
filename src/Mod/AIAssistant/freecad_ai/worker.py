# SPDX-License-Identifier: LGPL-2.1-or-later
"""Headless modeling worker, run by FreeCADCmd on a disposable document copy.

The worker never imports FreeCADGui. It executes one generated step, recomputes
it, and returns a candidate object/property delta that the GUI applies in one
transaction without recomputing. It isolates crashes and cancellation; it is
not a security sandbox, because generated Python keeps the user's permissions.
"""

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import time
import traceback

try:
    from .validation import (capture_diagnostics, input_digests, property_digest,
                             summarize, validate_step)
except ImportError:  # Run by FreeCADCmd as a script, outside the package.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from freecad_ai.validation import (capture_diagnostics, input_digests, property_digest,
                                       summarize, validate_step)

PROTOCOL = 1
BEGIN = b"\n<<<FREECAD-AI-WORKER-BEGIN>>>\n"
END = b"\n<<<FREECAD-AI-WORKER-END>>>\n"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 192 * 1024 * 1024
MAX_OUTPUT_CHARS = 8000

# Expressions do not survive dumpPropertyContent/restorePropertyContent, so
# they travel as (path, expression) pairs and are applied with setExpression.
_EXPRESSIONS = "ExpressionEngine"


class WorkerError(RuntimeError):
    pass


def encode_message(payload):
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return BEGIN + data + END


def decode_message(output, limit=MAX_RESULT_BYTES):
    """Return the last framed message; FreeCADCmd prints banners around it."""
    output = bytes(output)
    start = output.rfind(BEGIN)
    if start < 0:
        raise WorkerError("The modeling worker returned no result.")
    end = output.find(END, start + len(BEGIN))
    if end < 0:
        raise WorkerError("The modeling worker result was incomplete.")
    data = output[start + len(BEGIN):end]
    if len(data) > limit:
        raise WorkerError("The modeling worker result exceeded the size limit.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise WorkerError("The modeling worker returned invalid data.") from None
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise WorkerError("The modeling worker uses an incompatible protocol.")
    return payload


class _Output(io.TextIOBase):
    def __init__(self):
        self.text = ""
        self.truncated = False

    def write(self, text):
        remaining = max(0, MAX_OUTPUT_CHARS - len(self.text))
        self.text += text[:remaining]
        self.truncated = self.truncated or len(text) > remaining
        return len(text)

    def getvalue(self):
        return self.text + ("\n[output truncated]" if self.truncated else "")


class _NoGui:
    """Generated code that needs the GUI cannot run in the worker."""
    def __getattr__(self, name):
        raise WorkerError("GUI access (Gui.{}) is not available to automatic modeling steps.".format(name))


def _content(obj, name):
    try:
        return bytes(obj.dumpPropertyContent(name))
    except Exception:
        return None


def capture_state(doc):
    state = {}
    for obj in doc.Objects:
        properties = {}
        for name in obj.PropertiesList:
            digest = property_digest(obj, name) if name != _EXPRESSIONS else None
            if digest is not None:
                properties[name] = digest
        state[obj.Name] = {"type": obj.TypeId, "properties": properties,
                           "dynamic": sorted(_dynamic_properties(obj)),
                           "expressions": [[p, e] for p, e in obj.ExpressionEngine]}
    return state


def _dynamic_properties(obj):
    try:
        return set(obj.getDynamicPropertyNames() if hasattr(obj, "getDynamicPropertyNames")
                   else ())
    except Exception:
        return set()


def compute_delta(doc, before):
    """Describe how the worker changed the document, in document order."""
    after_names = [obj.Name for obj in doc.Objects]
    removed = [name for name in before if name not in set(after_names)]
    added, dynamic, changed = [], [], []
    for obj in doc.Objects:
        previous = before.get(obj.Name)
        if previous is not None and previous["type"] != obj.TypeId:
            raise WorkerError("Object {} changed type; this step cannot be transferred.".format(obj.Name))
        if previous is None:
            added.append({"name": obj.Name, "type": obj.TypeId})
        known_dynamic = set(previous["dynamic"]) if previous else set()
        for name in sorted(_dynamic_properties(obj) - known_dynamic):
            dynamic.append({"object": obj.Name, "name": name,
                            "type": obj.getTypeIdOfProperty(name),
                            "group": obj.getGroupOfProperty(name) or "",
                            "doc": obj.getDocumentationOfProperty(name) or ""})
        properties = {}
        for name in obj.PropertiesList:
            if name == _EXPRESSIONS:
                continue
            digest = property_digest(obj, name)
            if digest is None:
                continue
            if previous is None or previous["properties"].get(name) != digest:
                content = _content(obj, name)
                properties[name] = base64.b64encode(content).decode("ascii")
        expressions = [[path, expression] for path, expression in obj.ExpressionEngine]
        previous_expressions = previous["expressions"] if previous else []
        if properties or expressions != previous_expressions:
            changed.append({"object": obj.Name, "properties": properties,
                            "expressions": expressions})
    touched = [obj.Name for obj in doc.Objects
               if "Touched" in obj.State or "Invalid" in obj.State]
    return {"removed": removed, "added": added, "dynamic": dynamic, "changed": changed,
            "touched": touched}


def _python_features(doc, names):
    return [name for name in names
            if doc.getObject(name) is not None and hasattr(doc.getObject(name), "Proxy")]


def run_request(request, app):
    """Execute one step in the snapshot named by request; return a result payload."""
    timings = {}
    started = time.monotonic()
    doc = app.openDocument(request["snapshot"], hidden=True) if _accepts_hidden(app) else \
        app.openDocument(request["snapshot"])
    timings["open"] = time.monotonic() - started
    before = capture_state(doc)
    baseline = capture_diagnostics(doc)
    inputs_before = input_digests(doc)
    output = _Output()
    namespace = {"App": app, "FreeCAD": app, "Gui": _NoGui(), "FreeCADGui": _NoGui(), "doc": doc}
    started = time.monotonic()
    try:
        compiled = compile(request["code"], "<FreeCAD AI assistant>", "exec")
        with redirect_stdout(output), redirect_stderr(output):
            exec(compiled, namespace, namespace)
    except WorkerError as error:
        return {"protocol": PROTOCOL, "ok": False, "kind": "unsupported",
                "error": str(error), "output": output.getvalue(), "timings": timings}
    except BaseException as error:
        return {"protocol": PROTOCOL, "ok": False, "kind": "code",
                "error": "{}: {}".format(type(error).__name__, error),
                "output": output.getvalue(), "timings": timings}
    timings["execute"] = time.monotonic() - started
    started = time.monotonic()
    recompute_error = None
    try:
        doc.recompute()
    except Exception as error:
        recompute_error = "{}: {}".format(type(error).__name__, error)
    timings["recompute"] = time.monotonic() - started
    valid, diagnostics = validate_step(doc, baseline, inputs_before, recompute_error)
    if not valid:
        return {"protocol": PROTOCOL, "ok": False, "kind": "validation",
                "error": summarize(diagnostics), "diagnostics": diagnostics,
                "output": output.getvalue(), "timings": timings}
    started = time.monotonic()
    delta = compute_delta(doc, before)
    timings["delta"] = time.monotonic() - started
    unsupported = _python_features(doc, [item["name"] for item in delta["added"]])
    if unsupported:
        return {"protocol": PROTOCOL, "ok": False, "kind": "unsupported",
                "error": "Custom Python features cannot be transferred: " + ", ".join(unsupported[:10]),
                "output": output.getvalue(), "timings": timings}
    return {"protocol": PROTOCOL, "ok": True, "delta": delta, "output": output.getvalue(),
            "diagnostics": diagnostics, "object_count": len(doc.Objects), "timings": timings}


def _accepts_hidden(app):
    try:
        import inspect
        return "hidden" in str(inspect.signature(app.openDocument))
    except (TypeError, ValueError):
        return False


def main():
    import FreeCAD as app
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise WorkerError("The modeling request exceeded the size limit.")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
            raise WorkerError("Incompatible modeling request.")
        result = run_request(request, app)
    except BaseException as error:
        result = {"protocol": PROTOCOL, "ok": False, "kind": "worker",
                  "error": "{}: {}".format(type(error).__name__, error),
                  "trace": traceback.format_exc()[-4000:]}
    data = encode_message(result)
    if len(data) > MAX_RESULT_BYTES:
        data = encode_message({"protocol": PROTOCOL, "ok": False, "kind": "worker",
                               "error": "The modeling result exceeded the transfer limit."})
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


# FreeCADCmd does not run scripts as __main__; the host sets this variable.
if __name__ == "__main__" or os.environ.get("FREECAD_AI_WORKER") == str(PROTOCOL):
    main()
