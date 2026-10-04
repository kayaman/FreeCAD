# SPDX-License-Identifier: LGPL-2.1-or-later
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.core import (MAX_AGENT_STEPS, MAX_BRIEF_CHARS, BriefOverflow, Conversation,
                             Proposal, ProviderSettings)
from freecad_ai.session import Outcome, SessionRegistry, TaskState as S

WALL = "Make an enclosure 80 x 60 x 40 mm with 2 mm walls"
SETTINGS = ProviderSettings(provider="codex")


def text(conversation, context=None):
    return conversation.request_body(SETTINGS, context).decode("utf-8")


def run_steps(conversation, count, invented=""):
    bodies = []
    for index in range(count):
        bodies.append(text(conversation))
        conversation.accept(Proposal("Step {} {}".format(index, invented), "x = {}".format(index)))
        conversation.record_execution(True, "ok")
    bodies.append(text(conversation))
    return bodies


class MemoryTests(unittest.TestCase):
    def test_wall_requirement_survives_long_task_and_follow_up(self):
        conversation = Conversation()
        conversation.begin(WALL)
        bodies = run_steps(conversation, MAX_AGENT_STEPS, invented="using 3 mm ribs")
        conversation.begin("Now add a snap-fit lid")
        conversation.grant_segment()
        bodies += run_steps(conversation, MAX_AGENT_STEPS)
        conversation.begin("Make the lid 1 mm thinner than the walls")
        bodies += run_steps(conversation, 3)
        self.assertEqual(len(conversation.messages), 16)  # The window is full...
        for body in bodies:  # ...but the requirement is in every single request.
            self.assertIn(WALL, body)
        self.assertNotIn(WALL, json.dumps(conversation.messages))

    def test_goal_is_kept_separately_from_the_window(self):
        conversation = Conversation()
        source = conversation.begin("Design a bracket")
        run_steps(conversation, MAX_AGENT_STEPS)
        memory = conversation.memory()
        self.assertEqual(memory["current_goal"], {"text": "Design a bracket", "source": source})

    def test_requirements_are_verbatim_with_provenance(self):
        conversation = Conversation()
        source = conversation.begin("  Use M3 screws, 6 mm deep  ")
        requirement = conversation.memory()["user_requirements"][0]
        self.assertEqual(requirement["text"], "Use M3 screws, 6 mm deep")
        self.assertEqual(requirement["source"], source)

    def test_assistant_dimensions_are_never_pinned(self):
        conversation = Conversation()
        conversation.begin(WALL)
        conversation.accept(Proposal("I chose 3 mm walls for strength", "x=1"))
        texts = [r["text"] for r in conversation.memory()["user_requirements"]]
        self.assertEqual(texts, [WALL])

    def test_explicit_replacement_keeps_provenance(self):
        conversation = Conversation()
        conversation.begin(WALL)
        conversation.begin("Change the walls to 3 mm")
        first, second = conversation.requirements
        conversation.supersede(first["id"], second["id"])
        requirements = conversation.memory()["user_requirements"]
        self.assertEqual(requirements[0]["status"], "superseded")
        self.assertEqual(requirements[0]["superseded_by"], second["id"])
        self.assertEqual(requirements[1]["status"], "active")
        self.assertIn("ask the user which applies", " ".join(text(conversation).lower().split()))

    def test_edit_and_remove(self):
        conversation = Conversation()
        conversation.begin(WALL)
        conversation.begin("Add vents")
        first, second = conversation.requirements
        conversation.edit_requirement(first["id"], "Enclosure 80 x 60 x 40 mm, 2.4 mm walls")
        conversation.remove_requirement(second["id"])
        requirements = conversation.memory()["user_requirements"]
        self.assertEqual(len(requirements), 1)
        self.assertTrue(requirements[0]["edited_by_user"])
        self.assertEqual(first["history"], [WALL])

    def test_overflow_pauses_for_a_summary_instead_of_dropping(self):
        conversation = Conversation()
        conversation.begin("a" * 20000)
        with self.assertRaises(BriefOverflow):
            conversation.begin("b" * 20000)
        self.assertEqual(len(conversation.requirements), 1)  # Nothing was dropped or added.
        conversation.summarize_requirements("All parts 2 mm walls, PETG")
        self.assertEqual([r["text"] for r in conversation.active_requirements],
                         ["All parts 2 mm walls, PETG"])
        conversation.begin("b" * 20000)
        self.assertLessEqual(conversation.brief_chars(), MAX_BRIEF_CHARS)

    def test_brief_is_sent(self):
        conversation = Conversation()
        conversation.set_brief("Outdoor sensor node; PETG; IP54")
        conversation.begin("Make the base")
        self.assertIn("Outdoor sensor node; PETG; IP54", text(conversation))


class SessionMemoryTests(unittest.TestCase):
    def test_same_object_names_in_two_documents_stay_separate(self):
        class Doc:
            Name = "Unnamed"
        registry = SessionRegistry()
        first, second = registry.for_document(Doc()), registry.for_document(Doc())
        first.conversation.begin("Box named Body with 2 mm walls")
        second.conversation.begin("Box named Body with 5 mm walls")
        self.assertNotIn("5 mm", text(first.conversation))
        self.assertNotIn("2 mm", text(second.conversation))

    def test_reopened_file_starts_without_old_memory(self):
        class Doc:
            Name = "Part"
        registry = SessionRegistry()
        old = Doc()
        registry.for_document(old).conversation.begin(WALL)
        registry.retire(old)
        self.assertEqual(registry.for_document(Doc()).conversation.requirements, [])

    def test_cancelled_proposals_are_marked_in_the_ledger(self):
        registry = SessionRegistry()
        session = registry.for_document(None)
        source = session.conversation.begin(WALL)
        task = session.start_task(WALL, source)
        task.transition(S.THINKING)
        step = task.add_step(task.new_request().request, "Create shell", "x=1")
        session.conversation.log_step(step.id, step.outcome.value, step.message)
        task.transition(S.AWAITING_REVIEW)
        session.start_task("Something else")
        ledger = session.conversation.memory()["execution_ledger"]
        self.assertEqual(ledger, [{"step": step.id, "outcome": Outcome.CANCELLED.value,
                                   "summary": "Create shell"}])

    def test_new_chat_clears_memory(self):
        registry = SessionRegistry()
        session = registry.for_document(None)
        session.conversation.begin(WALL)
        session.conversation.set_brief("Brief")
        session.reset()
        self.assertEqual(session.conversation.memory()["user_requirements"], [])
        self.assertEqual(session.conversation.brief, "")


if __name__ == "__main__":
    unittest.main()
