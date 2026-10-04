# SPDX-License-Identifier: LGPL-2.1-or-later
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.core import Conversation, ProviderSettings
from freecad_ai.document import MAX_CONTEXT_OBJECTS, build_context, order_objects


class Unit:
    def __init__(self, kind):
        self.Type = kind

    def __str__(self):
        return self.Type


class Quantity:
    def __init__(self, value, kind):
        self.Value = value
        self.Unit = Unit(kind)

    def getValueAs(self, unit):
        return self.Value


class Obj:
    def __init__(self, name, type_id="Part::Box", parent=None, **properties):
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.parent = parent
        self.OutList = []
        self.values = properties
        self.PropertiesList = sorted(properties)

    def __getattr__(self, name):
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def getEditorMode(self, name):
        return []

    def getTypeIdOfProperty(self, name):
        return "App::PropertyEnumeration" if name == "Type" else "App::PropertyFloat"

    def getParentGeoFeatureGroup(self):
        return self.parent


class Doc:
    def __init__(self, objects):
        self.Name = "Doc"
        self.Label = "Doc"
        self.Objects = objects


def boxes(count):
    return [Obj("Box{:03d}".format(i), Length=Quantity(10.0 + i, "Length")) for i in range(count)]


def names(context):
    return [entry["name"] for entry in context["document"]["objects"]]


class OrderingTests(unittest.TestCase):
    def test_selection_beyond_old_80_object_slice_is_included(self):
        for position in (81, 500):
            objects = boxes(position + 20)
            target = objects[position]
            context = build_context(Doc(objects), [(target.Name, ["Face6"])], "1.1.4")
            entries = context["document"]["objects"]
            self.assertEqual(entries[0]["name"], target.Name)
            self.assertEqual(entries[0]["reason"], "selected")
            self.assertEqual(entries[0]["properties"]["Length"], "{:g} mm".format(10.0 + position))
            self.assertLessEqual(len(entries), MAX_CONTEXT_OBJECTS)
            self.assertTrue(context["document"]["truncated"])
            self.assertIn("other", context["omitted"]["objects"])

    def test_owners_then_dependencies_then_others(self):
        part = Obj("Part", "App::Part")
        body = Obj("Body", "PartDesign::Body", parent=part)
        sketch = Obj("Sketch", "Sketcher::SketchObject", parent=body)
        pad = Obj("Pad", "PartDesign::Pad", parent=body, Type="Length")
        pad.OutList = [sketch]
        unrelated = Obj("Other")
        ordered = order_objects([unrelated, part, body, sketch, pad], ["Pad"])
        self.assertEqual([(o.Name, r) for o, r in ordered], [
            ("Pad", "selected"), ("Body", "owner"), ("Part", "owner"),
            ("Sketch", "dependency"), ("Other", "other")])

    def test_dependency_cycles_terminate(self):
        a, b, c = Obj("A"), Obj("B"), Obj("C")
        a.OutList, b.OutList, c.OutList = [b], [c], [a]
        ordered = order_objects([a, b, c], ["A"])
        self.assertEqual([o.Name for o, _ in ordered], ["A", "B", "C"])

    def test_owner_cycle_terminates(self):
        a = Obj("A")
        b = Obj("B", parent=a)
        a.parent = b
        self.assertEqual([o.Name for o, _ in order_objects([a, b], ["A"])], ["A", "B"])

    def test_empty_selection_keeps_document_order(self):
        objects = boxes(5)
        context = build_context(Doc(objects), [], "1.1.4")
        self.assertEqual(names(context), [o.Name for o in objects])
        self.assertFalse(context["document"]["truncated"])

    def test_closed_document(self):
        context = build_context(None, [], "1.1.4")
        self.assertIsNone(context["document"])
        self.assertIn("document", context["omitted"])


class BudgetAndUnitTests(unittest.TestCase):
    def test_byte_budget_records_omissions(self):
        context = build_context(Doc(boxes(60)), [], "1.1.4", max_bytes=2000)
        self.assertLess(len(names(context)), 60)
        self.assertEqual(sum(context["omitted"]["objects"].values()) + len(names(context)), 60)

    def test_oversized_selection_is_reported_not_hidden(self):
        objects = boxes(100)
        selection = [(o.Name, []) for o in objects[:90]]
        context = build_context(Doc(objects), selection, "1.1.4")
        self.assertTrue(context["omitted"]["selected"])
        self.assertIn("select fewer", context["omitted"]["note"])

    def test_units_are_labelled_by_kind(self):
        obj = Obj("Feature", Length=Quantity(5.0, "Length"), Angle=Quantity(30.0, "Angle"),
                  Volume=Quantity(8.0, "Volume"), Count=3, Visible=True, Type="TwoLengths")
        properties = build_context(Doc([obj]), [("Feature", [])], "1.1.4")[
            "document"]["objects"][0]["properties"]
        self.assertEqual(properties["Length"], "5 mm")
        self.assertEqual(properties["Angle"], "30 deg")
        self.assertEqual(properties["Volume"], "8 mm^3")
        self.assertEqual(properties["Count"], 3)
        self.assertEqual(properties["Type"], "TwoLengths")

    def test_text_and_paths_are_excluded(self):
        obj = Obj("Feature", Label2="secret note", FileName="/home/user/file.FCStd")
        context = build_context(Doc([obj]), [], "1.1.4")
        text = json.dumps(context)
        self.assertNotIn("secret note", text)
        self.assertNotIn("/home/user", text)
        self.assertIn("file paths", context["omitted"]["content"])

    def test_context_disabled_sends_none(self):
        conversation = Conversation()
        conversation.begin("Help")
        self.assertNotIn(b"Current model context",
                         conversation.request_body(ProviderSettings(provider="codex")))


if __name__ == "__main__":
    unittest.main()
