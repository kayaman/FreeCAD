# SPDX-License-Identifier: LGPL-2.1-or-later
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.session import (NOT_EXECUTED, TRANSITIONS, DocumentSession, Outcome,
                                SessionRegistry, StateError, TaskState as S, UndoRefused,
                                plan_undo)


class Doc:
    def __init__(self, name):
        self.Name = name


class TransitionTests(unittest.TestCase):
    def test_every_state_has_rules_and_terminal_states_are_final(self):
        self.assertEqual(set(TRANSITIONS), set(S))
        for state in (S.COMPLETED, S.STOPPED, S.FAILED):
            self.assertEqual(TRANSITIONS[state], set())

    def test_normal_autonomous_path(self):
        task = DocumentSession().start_task("Box")
        for state in (S.THINKING, S.EXECUTING, S.THINKING, S.EXECUTING, S.APPLYING,
                      S.THINKING, S.COMPLETED):
            task.transition(state)
        self.assertTrue(task.finished)

    def test_illegal_transitions_raise(self):
        task = DocumentSession().start_task("Box")
        with self.assertRaises(StateError):
            task.transition(S.APPLYING)
        task.transition(S.STOPPED)
        with self.assertRaises(StateError):
            task.transition(S.THINKING)

    def test_pause_and_resume(self):
        task = DocumentSession().start_task("Box")
        task.transition(S.THINKING)
        task.transition(S.PAUSED)
        task.transition(S.EXECUTING)
        task.transition(S.PAUSED)
        task.transition(S.AWAITING_REVIEW)
        self.assertEqual(task.state, S.AWAITING_REVIEW)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.session = DocumentSession(Doc("A"))
        self.task = self.session.start_task("Box")
        self.task.transition(S.THINKING)
        self.token = self.task.new_request()

    def test_current_token_is_accepted(self):
        self.assertTrue(self.session.accepts(self.token, S.THINKING))

    def test_wrong_state_is_rejected(self):
        self.assertFalse(self.session.accepts(self.token, S.EXECUTING))

    def test_stop_invalidates_before_cancellation(self):
        self.task.invalidate()
        self.assertFalse(self.session.accepts(self.token))
        self.task.transition(S.STOPPED)
        self.assertFalse(self.session.accepts(self.token, S.STOPPED))

    def test_newer_request_supersedes(self):
        newer = self.task.new_request()
        self.assertFalse(self.session.accepts(self.token))
        self.assertTrue(self.session.accepts(newer))

    def test_new_chat_rejects_old_task(self):
        self.session.reset()
        self.assertFalse(self.session.accepts(self.token))
        task = self.session.start_task("Other")
        task.transition(S.THINKING)
        self.assertFalse(self.session.accepts(self.token))

    def test_other_session_token_rejected(self):
        other = DocumentSession(Doc("B"))
        other_task = other.start_task("Box")
        other_task.transition(S.THINKING)
        token = other_task.new_request()
        self.assertFalse(self.session.accepts(token))

    def test_retired_session_rejects(self):
        registry = SessionRegistry()
        registry.sessions.append(self.session)
        registry.retire(self.session.document)
        self.assertFalse(self.session.accepts(self.token))

    def test_running_task_blocks_new_task(self):
        with self.assertRaises(StateError):
            self.session.start_task("Again")


class OutcomeTests(unittest.TestCase):
    def test_pending_proposal_outcomes(self):
        session = DocumentSession()
        task = session.start_task("Box")
        task.transition(S.THINKING)
        step = task.add_step(task.new_request().request, "Make box", "x=1")
        self.assertIs(task.pending_step, step)
        task.close_pending(Outcome.CANCELLED, "Stopped")
        self.assertIsNone(task.pending_step)
        self.assertIn(step.outcome, NOT_EXECUTED)
        self.assertEqual(task.executed_steps(), [])
        self.assertEqual(step.id, task.id + "/1")

    def test_superseded_review_is_cancelled_not_executed(self):
        session = DocumentSession()
        task = session.start_task("Box")
        task.transition(S.THINKING)
        task.add_step(1, "Make box", "x=1")
        task.transition(S.AWAITING_REVIEW)
        session.start_task("Something else")
        self.assertEqual(task.state, S.STOPPED)
        self.assertEqual(task.steps[0].outcome, Outcome.CANCELLED)

    def test_not_executed_note_reaches_provider_context(self):
        session = DocumentSession()
        session.conversation.begin("Box")
        session.conversation.record_not_executed("cancelled", "Stopped")
        note = json.loads(session.conversation.messages[-1]["content"])
        self.assertFalse(note["proposal_executed"])
        self.assertEqual(note["outcome"], "cancelled")


class RegistryTests(unittest.TestCase):
    def test_identity_follows_document_object_not_name(self):
        registry = SessionRegistry()
        doc = Doc("Part")
        session = registry.for_document(doc)
        doc.Name = "Renamed"
        self.assertIs(registry.for_document(doc), session)

    def test_reopened_file_gets_new_identity(self):
        registry = SessionRegistry()
        first = Doc("Part")
        session = registry.for_document(first)
        registry.retire(first)
        reopened = Doc("Part")
        self.assertIsNot(registry.for_document(reopened), session)
        self.assertTrue(session.retired)

    def test_unbound_session_binds_to_created_document(self):
        registry = SessionRegistry()
        session = registry.for_document(None)
        doc = Doc("AIModel")
        registry.bind(session, doc)
        self.assertIs(registry.for_document(doc), session)
        self.assertIsNot(registry.for_document(None), session)

    def test_switching_documents_keeps_sessions_apart(self):
        registry = SessionRegistry()
        a, b = Doc("Same"), Doc("Same")
        session_a = registry.for_document(a)
        session_a.conversation.begin("A request")
        session_b = registry.for_document(b)
        self.assertIsNot(session_a, session_b)
        self.assertEqual(session_b.conversation.messages, [])
        self.assertIs(registry.for_document(a), session_a)


class UndoPlanTests(unittest.TestCase):
    def setUp(self):
        self.session = DocumentSession(Doc("A"))
        self.task = self.session.start_task("Three steps")
        for index, outcome in enumerate((Outcome.EXECUTED, Outcome.FAILED, Outcome.EXECUTED)):
            step = self.task.add_step(index, "step", "x=1", "rev{}".format(index))
            step.outcome = outcome
            if outcome is Outcome.EXECUTED:
                step.transaction = "AI assistant " + step.id
                step.revision_after = "after{}".format(index)
        self.names = ["AI assistant {}/3".format(self.task.id), "AI assistant {}/1".format(self.task.id),
                      "Earlier user edit"]

    def test_whole_task_undo_skips_rolled_back_steps(self):
        targets = plan_undo(self.task, "task", True, "after2", self.names)
        self.assertEqual([s.id.split("/")[1] for s in targets], ["3", "1"])

    def test_last_step_only(self):
        targets = plan_undo(self.task, "step", True, "after2", self.names)
        self.assertEqual([s.id.split("/")[1] for s in targets], ["3"])

    def test_intervening_edit_is_refused(self):
        with self.assertRaises(UndoRefused):
            plan_undo(self.task, "task", True, "user changed it", self.names)

    def test_interleaved_history_is_refused(self):
        names = [self.names[0], "User edit", self.names[1]]
        with self.assertRaises(UndoRefused):
            plan_undo(self.task, "task", True, "after2", names)

    def test_truncated_history_is_refused(self):
        with self.assertRaisesRegex(UndoRefused, "no longer holds"):
            plan_undo(self.task, "task", True, "after2", self.names[:1])

    def test_other_document_or_nothing_executed(self):
        with self.assertRaises(UndoRefused):
            plan_undo(self.task, "task", False, "after2", self.names)
        with self.assertRaises(UndoRefused):
            plan_undo(None, "task", True, "", [])

    def test_undo_candidate_is_latest_task_with_executed_steps(self):
        self.task.transition(S.THINKING)
        self.task.transition(S.COMPLETED)
        self.session.start_task("Chat only")
        self.assertIs(self.session.undo_candidate(), self.task)


if __name__ == "__main__":
    unittest.main()
