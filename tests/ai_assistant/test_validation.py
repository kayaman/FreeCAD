# SPDX-License-Identifier: LGPL-2.1-or-later
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.core import Conversation, Proposal
from freecad_ai.validation import compare_diagnostics, summarize


def error(message="Linked object is missing", kind="Part::Cut"):
    return {"label": "Feature", "type": kind, "message": message}


class CompareTests(unittest.TestCase):
    def test_unrelated_change_beside_preexisting_error(self):
        report = compare_diagnostics({"Cut": error()}, {"Cut": error()}, changed=["Box"])
        self.assertTrue(report["ok"])
        self.assertEqual([e["name"] for e in report["preexisting"]], ["Cut"])
        self.assertIn("pre-existing", summarize(report))

    def test_newly_invalid_feature_fails(self):
        report = compare_diagnostics({}, {"Cut001": error()}, changed=["Cut001"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["new_invalid"][0]["name"], "Cut001")

    def test_repair_succeeds(self):
        report = compare_diagnostics({"Cut": error()}, {}, changed=["Cut"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["repaired"][0]["name"], "Cut")
        self.assertEqual(report["baseline"][0]["name"], "Cut")

    def test_repair_that_breaks_something_else_fails(self):
        report = compare_diagnostics({"Cut": error()}, {"Fillet": error("Failed")},
                                     changed=["Cut", "Fillet"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["repaired"][0]["name"], "Cut")
        self.assertEqual(report["new_invalid"][0]["name"], "Fillet")

    def test_changed_feature_still_invalid_is_rejected(self):
        report = compare_diagnostics({"Cut": error()}, {"Cut": error()}, changed=["Cut"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["changed_still_invalid"][0]["name"], "Cut")

    def test_unchanged_feature_with_different_error_is_worse(self):
        report = compare_diagnostics({"Cut": error("A")}, {"Cut": error("B")}, changed=["Box"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["worsened"][0]["message"], "B")

    def test_partial_repair_keeps_other_preexisting_error(self):
        report = compare_diagnostics({"Cut": error(), "Old": error()}, {"Old": error()},
                                     changed=["Cut"])
        self.assertTrue(report["ok"])
        self.assertEqual([e["name"] for e in report["preexisting"]], ["Old"])


class FeedbackTests(unittest.TestCase):
    def test_diagnostics_reach_the_provider_bounded(self):
        conversation = Conversation()
        conversation.begin("Fix it")
        conversation.accept(Proposal("Fix", "x=1"))
        before = {"F{}".format(i): error() for i in range(30)}
        report = compare_diagnostics(before, before, changed=[])
        conversation.record_execution(True, "done", report)
        feedback = json.loads(conversation.messages[-1]["content"])
        self.assertEqual(len(feedback["diagnostics"]["preexisting"]), 10)
        self.assertNotIn("baseline", feedback["diagnostics"])


if __name__ == "__main__":
    unittest.main()
